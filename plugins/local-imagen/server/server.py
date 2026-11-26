# /// script
# requires-python = ">=3.10"
# dependencies = ["mcp>=2.3,<3", "httpx>=0.27", "pillow>=10.1", "filelock>=3.13", "psutil>=5.9", "send2trash>=1.8",
#                 "numpy>=1.26", "opencv-python-headless>=4.9"]
# ///
"""local-imagen MCP server: local image generation (ComfyUI + FLUX.2 klein 4B) organised in game projects that
carry a style guide (styles, characters, creatures, locations, props, poses, cameras...), plus detachable reference
images and a private candidate -> pick -> approve workflow. Part of the local-imagen Claude Code plugin.

Run by Claude Code over stdio:  uv run --script server.py   (uv installs the dependencies above)

Settings (environment variable > ~/.local-imagen/config.json key > default):
  LOCAL_IMAGEN_HOME       / home            machine data: sessions/, library/, outputs/ (one-off images),
                                            styles.json, projects.json; default ~/.local-imagen
  LOCAL_IMAGEN_PROJECTS   / projects_root   default folder for new projects;
                                            default <the OS's Documents folder>/local-imagen-projects
  COMFYUI_URL             / comfyui_url     default http://127.0.0.1:8188
  COMFYUI_DOCKER_DIR      / docker_dir      folder with docker-compose.yml, default <home>/docker
  COMFYUI_MANAGE_DOCKER   / manage_docker   false when ComfyUI is run some other way (no start/stop)
  OLLAMA_URL              / ollama_url      default http://127.0.0.1:11434 (models are unloaded to free the GPU)
  LOCAL_IMAGEN_CANDIDATES / candidates      candidates per request, default 3
  model                                     {"unet": ..., "text_encoder": ..., "vae": ...} file names override
  LOCAL_IMAGEN_SESSION                      session id; only the shell bridge sets it (normally 1 session = 1 process)

Isolation: each server process is one session (Claude Code starts one per conversation window). A session works on
one project at a time and keeps its working state (attached references, candidates, picks) in its own folder;
switching projects drops it. Shared files (guides, registry, backend usage) are written atomically under file
locks, so several sessions can work at the same time, even on the same project.
"""
from __future__ import annotations

import asyncio
import atexit
import contextlib
import datetime as dt
import functools
import hashlib
import html
import io
import json
import math
import logging
import os
import random
import re
import shutil
import subprocess
import sys
import threading
import time
import unicodedata
import uuid
from pathlib import Path

import httpx
import psutil
from filelock import FileLock
from PIL import Image as PILImage
from send2trash import send2trash
from mcp.server.mcpserver import Image, MCPServer
from mcp.server.mcpserver.exceptions import ToolError

logging.getLogger("httpx").setLevel(logging.WARNING)


def log(*a):
    print(*a, file=sys.stderr, flush=True)  # stdout is the MCP channel


# ---------------------------------------------------------------- OS folders (no hardcoded paths)
FOLDERID_DOCUMENTS = "FDD39AD0-238F-46AF-ADB4-6C85480369C7"
FOLDERID_PROGRAM_FILES = "905E63B6-C1BF-494E-B29C-65B732D3D21A"
FOLDERID_PROGRAM_DATA = "62AB5D82-FDC1-4DC3-A9DD-070D1D495D97"


def _win_known_folder(guid: str) -> Path | None:
    """Windows known folder (handles OneDrive/redirected Documents). None elsewhere or on failure."""
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        class GUID(ctypes.Structure):
            _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD), ("Data3", wintypes.WORD),
                        ("Data4", ctypes.c_ubyte * 8)]

        u = uuid.UUID(guid)
        g = GUID(u.time_low, u.time_mid, u.time_hi_version, (ctypes.c_ubyte * 8)(*u.bytes[8:]))
        out = ctypes.c_wchar_p()
        fn = ctypes.windll.shell32.SHGetKnownFolderPath
        fn.argtypes = [ctypes.POINTER(GUID), wintypes.DWORD, wintypes.HANDLE, ctypes.POINTER(ctypes.c_wchar_p)]
        if fn(ctypes.byref(g), 0, None, ctypes.byref(out)) != 0:
            return None
        try:
            return Path(out.value)
        finally:
            ctypes.windll.ole32.CoTaskMemFree(out)
    except Exception:  # noqa: BLE001
        return None


def documents_dir() -> Path | None:
    if sys.platform == "win32":
        d = _win_known_folder(FOLDERID_DOCUMENTS) or Path.home() / "Documents"
    elif sys.platform == "darwin":
        d = Path.home() / "Documents"
    else:  # Linux/BSD: XDG user dirs
        d = None
        cfg = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "user-dirs.dirs"
        try:
            for line in cfg.read_text(encoding="utf-8").splitlines():
                if line.startswith("XDG_DOCUMENTS_DIR="):
                    v = line.split("=", 1)[1].strip().strip('"').replace("$HOME", str(Path.home()))
                    if Path(v) != Path.home():
                        d = Path(v)
        except OSError:
            pass
        d = d or Path.home() / "Documents"
    return d if d.is_dir() else None


def _win_dir(env_name: str, guid: str) -> Path | None:
    v = os.environ.get(env_name)
    return Path(v) if v else _win_known_folder(guid)


# ---------------------------------------------------------------- config
CFG_DIR = Path.home() / ".local-imagen"
CFG_FILE = CFG_DIR / "config.json"


def _cfg() -> dict:
    try:
        return json.loads(CFG_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


CFG = _cfg()


def setting(key: str, env: str | None, default):
    v = os.environ.get(env) if env else None
    return v if v not in (None, "") else CFG.get(key, default)


HOME = Path(setting("home", "LOCAL_IMAGEN_HOME", CFG_DIR)).expanduser()
COMFY = str(setting("comfyui_url", "COMFYUI_URL", "http://127.0.0.1:8188")).rstrip("/")
OLLAMA = str(setting("ollama_url", "OLLAMA_URL", "http://127.0.0.1:11434")).rstrip("/")
DOCKER_DIR = Path(setting("docker_dir", "COMFYUI_DOCKER_DIR", HOME / "docker")).expanduser()
MANAGE_DOCKER = str(setting("manage_docker", "COMFYUI_MANAGE_DOCKER", True)).lower() not in ("false", "0", "no")
LIB = HOME / "library"          # ad-hoc reference images (content-addressed)
OUT = HOME / "outputs"          # approved one-off images (no project)
STYLES_FILE = HOME / "styles.json"  # global style presets for one-off images
REGISTRY = HOME / "projects.json"   # projects kept outside the default projects folder
BACKEND = HOME / "backend.json"     # which sessions use ComfyUI + Ollama models to restore
SESSIONS = HOME / "sessions"
LOCKS = HOME / "locks"
for d in (LIB, OUT, SESSIONS, LOCKS):
    d.mkdir(parents=True, exist_ok=True)
DEFAULT_CANDIDATES = int(setting("candidates", "LOCAL_IMAGEN_CANDIDATES", 3))
MAX_CANDIDATES = 8
MAX_REFS = 4
BRIDGE_GRACE = 15 * 60   # a bridge session (one process per call) counts as active this long after its last call
STALE_SESSION = 2 * 86400

MODEL = {  # FLUX.2 klein 4B (distilled, Apache 2.0)
    "unet": "flux-2-klein-4b.safetensors",
    "text_encoder": "qwen_3_4b_fp4_flux2.safetensors",
    "vae": "flux2-vae.safetensors",
    # RefControl depth LoRA (Apache 2.0, thedeoxen/refcontrol-FLUX.2-klein-4B-reference-depth-lora): used by
    # generate_image(pose_mode="depth"); downloaded by setup.py --models control
    "depth_lora": "flux2_klein_4b_refcontrol_depth.safetensors",
    **(CFG.get("model") or {}),
}
DEPTH_LORA_REPO = "thedeoxen/refcontrol-FLUX.2-klein-4B-reference-depth-lora"
# Session speed (set_speed): how character images are posed. Lower only when the user asks for faster images.
SPEEDS = {
    "full": "every character image is drawn from a Blender mannequin pose guide with the depth model",
    "fast": "mannequin pose guide sent as a plain reference (depth model off)",
    "fastest": "no mannequin pose guide (depth model off)",
}
DEFAULT_SPEED = "full"
IMG_EXT = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}
ASPECTS = {
    "portrait": (896, 1152), "square": (1024, 1024), "landscape": (1152, 896),
    "tall": (768, 1344), "wide": (1344, 768),
}
STYLE_PREFIX = (
    "Create a brand-new, original image drawn in exactly the same art style as the reference image(s): the "
    "same linework, coloring, shading, palette, texture and proportions. Do not copy the characters, objects "
    "or composition of the reference image(s); only their art style. Subject: "
)
KNOWN_KINDS = ["style", "character", "creature", "location", "prop", "pose", "camera"]
SECTION_ORDER = ["character", "creature", "location", "prop", "pose", "camera"]  # other kinds next, style last
SECTION_TITLES = {"style": "Art style", "character": "Characters", "creature": "Creatures",
                  "location": "Locations", "prop": "Props", "pose": "Pose", "camera": "Camera"}
GUIDE_TITLES = {"style": "Styles", "character": "Characters", "creature": "Creatures", "location": "Locations",
                "prop": "Props", "pose": "Poses", "camera": "Cameras"}
REF_PRIORITY = {"pose": -0.5, "character": 0, "creature": 0, "style": 1, "location": 2, "prop": 3}  # others: 4
ROLE_TEXT = {
    "style": "the art style to use: match its linework, colouring, shading, palette and texture, but do not copy "
             "its content",
    "character": "{name}: keep the same face, hair, body, outfit, colours and proportions",
    "creature": "{name}: keep the same anatomy, colours, markings and proportions",
    "location": "{name}: keep its architecture, layout, materials and mood",
    "prop": "{name}: keep its exact design and colours",
    "pose": "the pose to use: copy only the body pose, not the person, outfit or style",
    "camera": "the camera angle and framing to use: copy only the viewpoint, not the content",
}
NO_PROJECT = (
    "NO_PROJECT: no project is chosen in this session. Ask the user which project to work on (call list_projects "
    "and show them the names), or whether this is a one-off image without a project. Then call use_project(name), "
    "create_project(name) or use_project(scratch=True). Do not choose for them."
)


# ---------------------------------------------------------------- files, locks, names
def now_iso() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


def _load(p: Path, default):
    """Read a JSON state file; missing/corrupt -> default."""
    for i in range(10):
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return default
        except PermissionError:  # Windows: being replaced right now
            time.sleep(0.05 * (i + 1))
    return default


def _read_json(p: Path) -> dict:
    """Read a JSON data file (project/guide); errors propagate so corrupt data is never overwritten."""
    for i in range(10):
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except PermissionError:
            time.sleep(0.05 * (i + 1))
    return json.loads(p.read_text(encoding="utf-8"))


def _write_bytes(p: Path, data: bytes):
    """Atomic write: readers in other sessions never see a half-written file."""
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(f".{p.name}.{os.getpid()}.{uuid.uuid4().hex[:6]}.tmp")
    tmp.write_bytes(data)
    for i in range(20):
        try:
            os.replace(tmp, p)
            return
        except PermissionError:  # Windows: target briefly open by a reader
            time.sleep(0.05 * (i + 1))
    tmp.unlink(missing_ok=True)
    raise PermissionError(f"could not write {p} (file in use)")


def _save(p: Path, data):
    _write_bytes(p, json.dumps(data, indent=2, ensure_ascii=False).encode("utf-8"))


_LOCKS: dict[str, tuple[threading.RLock, FileLock]] = {}


@contextlib.contextmanager
def _lock(key: str, timeout: float = 60):
    """Cross-process (file) + cross-thread lock, re-entrant within one thread."""
    if key not in _LOCKS:
        name = hashlib.sha1(key.encode("utf-8")).hexdigest()[:16] + ".lock"
        _LOCKS[key] = (threading.RLock(), FileLock(str(LOCKS / name), thread_local=False))
    rl, fl = _LOCKS[key]
    if not rl.acquire(timeout=timeout):
        raise TimeoutError(f"timed out waiting for {key}")
    try:
        fl.acquire(timeout=timeout)
        try:
            yield
        finally:
            fl.release()
    finally:
        rl.release()


def _norm(p) -> str:
    return os.path.normcase(os.path.abspath(str(Path(p).expanduser())))


def _same_path(a, b) -> bool:
    return _norm(a) == _norm(b)


def _plock(path: Path, timeout: float = 60):
    return _lock("project:" + _norm(path), timeout)


RESERVED = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}


def slugify(text: str, limit: int = 60) -> str:
    s = unicodedata.normalize("NFKD", str(text)).encode("ascii", "ignore").decode()
    s = re.sub(r"[^A-Za-z0-9]+", "-", s).strip("-").lower()[:limit].strip("-")
    if not s:
        raise ValueError(f"'{text}' has no letters or digits to build a folder name from")
    return s + "-x" if s in RESERVED else s


# ---------------------------------------------------------------- process liveness + this session
def _proc_alive(pid, created) -> bool:
    try:
        return abs(psutil.Process(int(pid)).create_time() - float(created or 0)) < 2
    except psutil.AccessDenied:
        return True
    except (psutil.NoSuchProcess, TypeError, ValueError):
        return False


ME_PID = os.getpid()
ME_CREATED = psutil.Process(ME_PID).create_time()
_ENV_SID = os.environ.get("LOCAL_IMAGEN_SESSION", "").strip()
if _ENV_SID and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,80}", _ENV_SID):
    raise SystemExit("LOCAL_IMAGEN_SESSION may only contain letters, digits, '.', '_' and '-'")
SID = _ENV_SID or f"{dt.datetime.now():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:6]}"
SDIR = SESSIONS / SID
CAND = SDIR / "candidates"   # all candidates of open requests (hidden from the user unless asked)
SEL = SDIR / "selected"      # the picked candidate of each open request, waiting for the user's approval
POSES = SDIR / "poses"       # mannequin pose guides rendered by render_pose
PLUGIN_DIR = Path(__file__).resolve().parents[1]
BLENDER_DIR = Path(__file__).resolve().parent / "blender"   # pose_render.py (runs inside Blender) + poses.json
BLENDER_IMAGE = "local/imagen-blender:latest"
BLENDER_LOG = HOME / "logs" / "blender-setup.log"
BLENDER_STATE = HOME / "blender-setup.json"
BLENDER_BUILD: subprocess.Popen | None = None
_BG_TASKS: set = set()
S: dict = {"project": None, "scratch": False, "active": [], "candidates": None, "requests": {},
           "last_request": None, "started": now_iso()}
S.update(_load(SDIR / "session.json", {}))
S.update(id=SID, kind="bridge" if _ENV_SID else "process", pid=ME_PID, pid_created=ME_CREATED)
BUSY = 0  # generations running in this process


def _save_session():
    """Written lazily (only once the session does something), so idle Claude sessions leave no files."""
    S["touched"] = time.time()
    _save(SDIR / "session.json", S)


def _session_live(info: dict) -> bool:
    if info.get("id") == SID:
        return True
    if info.get("kind") == "bridge":
        return time.time() - float(info.get("touched", 0)) < BRIDGE_GRACE
    return _proc_alive(info.get("pid"), info.get("pid_created"))


def _other_live_sessions() -> list[dict]:
    out = []
    for f in SESSIONS.glob("*/session.json"):
        info = _load(f, None)
        if info and info.get("id") != SID and _session_live(info):
            out.append(info)
    return out


def _prune_sessions():
    now = time.time()
    for d in SESSIONS.iterdir():
        if not d.is_dir() or d.name == SID:
            continue
        info = _load(d / "session.json", None)
        touched = float(info.get("touched", 0)) if info else d.stat().st_mtime
        if (not info or not _session_live(info)) and now - touched > STALE_SESSION:
            shutil.rmtree(d, ignore_errors=True)


def _mode_text() -> str:
    if S.get("project"):
        return f"project '{S['project']['name']}'"
    if S.get("scratch"):
        return "one-off images (no project)"
    return "no project chosen yet"


# ---------------------------------------------------------------- shared backend usage (ComfyUI + Ollama)
def _backend_load() -> dict:
    b = _load(BACKEND, {})
    b.setdefault("leases", {})
    b.setdefault("unloaded_ollama", [])
    return b


def _lease_take():
    """Mark that this session uses ComfyUI, so other sessions don't stop it under our feet."""
    with _lock("backend", 600):
        b = _backend_load()
        b["leases"] = {k: v for k, v in b["leases"].items() if _session_live(v)}
        b["leases"][SID] = {"id": SID, "kind": S["kind"], "pid": ME_PID, "pid_created": ME_CREATED,
                            "touched": time.time(), "project": (S.get("project") or {}).get("name")}
        _save(BACKEND, b)


def _lease_release():
    with _lock("backend", 60):
        b = _backend_load()
        if b["leases"].pop(SID, None) is not None:
            _save(BACKEND, b)


def _ollama_unload_sync() -> list[str]:
    """Unload Ollama models from the GPU (8 GB cards can't hold both); remember them for restore."""
    names = []
    try:
        with httpx.Client(timeout=30) as c:
            models = c.get(f"{OLLAMA}/api/ps").json().get("models", [])
            if not models:
                return []
            with _lock("backend", 600):
                b = _backend_load()
                for m in models:
                    exp = m.get("expires_at", "")
                    keep = -1 if exp[:4].isdigit() and int(exp[:4]) >= 2100 else "5m"
                    c.post(f"{OLLAMA}/api/generate", json={"model": m["name"], "keep_alive": 0})
                    if m["name"] not in [u["name"] for u in b["unloaded_ollama"]]:
                        b["unloaded_ollama"].append({"name": m["name"], "keep_alive": keep})
                    names.append(m["name"])
                _save(BACKEND, b)
    except httpx.HTTPError:
        pass  # Ollama not running: nothing to unload
    return names


def _comfy_up_sync() -> bool:
    try:
        return httpx.get(f"{COMFY}/system_stats", timeout=3).status_code == 200
    except httpx.HTTPError:
        return False


def _stop_sync(restore_ollama: bool, force: bool) -> str:
    """Stop ComfyUI and restore Ollama, unless another live session still uses them (then only drop our lease).
    The backend lock is held throughout, so a session that starts generating meanwhile waits and restarts it."""
    with _lock("backend", 600):
        b = _backend_load()
        b["leases"].pop(SID, None)
        others = {k: v for k, v in b["leases"].items() if _session_live(v)}
        b["leases"] = others
        if others and not force:
            _save(BACKEND, b)
            names = ", ".join(sorted({v.get("project") or "no project" for v in others.values()}))
            return (f"ComfyUI kept running: {len(others)} other Claude session(s) still use it ({names}). "
                    "stop_backend(force=True) stops it anyway.")
        if _comfy_up_sync():
            try:
                httpx.post(f"{COMFY}/free", json={"unload_models": True, "free_memory": True}, timeout=30)
            except httpx.HTTPError:
                pass
        if MANAGE_DOCKER and (DOCKER_DIR / "docker-compose.yml").exists():
            r = _run(["docker", "compose", "stop"], str(DOCKER_DIR), 300)
            msg = "ComfyUI stopped." if r.returncode == 0 else "docker compose stop: " + (r.stderr or r.stdout)[-400:]
        else:
            msg = "ComfyUI models unloaded (ComfyUI is not managed by this server, so it keeps running)."
        if force:
            b["leases"] = {}
        if restore_ollama and b["unloaded_ollama"]:
            restored = []
            with httpx.Client(timeout=300) as c:
                for m in b["unloaded_ollama"]:
                    try:
                        c.post(f"{OLLAMA}/api/generate", json={"model": m["name"], "keep_alive": m["keep_alive"]})
                        restored.append(m["name"])
                    except httpx.HTTPError as e:
                        log("ollama restore failed", m, e)
            b["unloaded_ollama"] = []
            if restored:
                msg += " Reloaded Ollama: " + ", ".join(restored)
        _save(BACKEND, b)
        return msg


# ---------------------------------------------------------------- images
def _gather(src: str) -> list[tuple[str, bytes]]:
    """Raw images from: a file, a folder (all images inside), an http(s) URL, 'style:<preset>' (a saved one-off
    style) or the id of an open request of this session (its picked image)."""
    s = src.strip()
    if s.lower().startswith(("http://", "https://")):
        r = httpx.get(s, timeout=60, follow_redirects=True)
        r.raise_for_status()
        return [(Path(s.split("?")[0]).stem or "web-image", r.content)]
    if s.lower().startswith("style:"):
        name = s[6:].strip()
        st = _load(STYLES_FILE, {})
        if name not in st:
            raise ValueError(f"unknown style preset '{name}'. Presets: {', '.join(st) or 'none'}")
        return [(r["label"], (LIB / r["file"]).read_bytes()) for r in st[name]["refs"]]
    if s in S["requests"]:
        p = SEL / f"{s}.png"
        if not p.exists():
            raise ValueError(f"request {s} has no picked image yet")
        return [(f"pick-{s}", p.read_bytes())]
    p = Path(s).expanduser()
    if p.is_dir():
        files = sorted(f for f in p.iterdir() if f.suffix.lower() in IMG_EXT)
        if not files:
            raise ValueError(f"no images found in folder {p}")
        return [(f.stem, f.read_bytes()) for f in files]
    if p.is_file():
        return [(p.stem, p.read_bytes())]
    raise ValueError(f"not found: {src}")


def _normalize(data: bytes) -> tuple[bytes, str]:
    im = PILImage.open(io.BytesIO(data))
    im.load()
    im = im.convert("RGBA") if im.mode in ("RGBA", "LA", "P") else im.convert("RGB")
    if max(im.size) > 2048:
        im.thumbnail((2048, 2048), PILImage.LANCZOS)
    buf = io.BytesIO()
    im.save(buf, "PNG")
    png = buf.getvalue()
    return png, hashlib.sha1(png).hexdigest()[:12] + ".png"


def _import_to_library(src: str) -> list[dict]:
    out = []
    for label, raw in _gather(src):
        png, fid = _normalize(raw)
        if not (LIB / fid).exists():
            _write_bytes(LIB / fid, png)
        out.append({"file": fid, "label": label[:60]})
    return out


def _thumb(path: Path, size=256) -> Image:
    im = PILImage.open(path).convert("RGB")
    im.thumbnail((size, size), PILImage.LANCZOS)
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=80)
    return Image(data=buf.getvalue(), format="jpeg")


def _preview(png: bytes, size: int) -> Image:
    im = PILImage.open(io.BytesIO(png)).convert("RGB")
    im.thumbnail((size, size), PILImage.LANCZOS)
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=85)
    return Image(data=buf.getvalue(), format="jpeg")


def describe_refs(refs) -> str:
    if not refs:
        return "No ad-hoc reference images attached."
    return "Attached references:\n" + "\n".join(f"  {i}. {r['label']}  [{r['file']}]" for i, r in enumerate(refs, 1))


# ---------------------------------------------------------------- projects
def default_projects_root() -> Path:
    docs = documents_dir()
    return docs / "local-imagen-projects" if docs else HOME / "projects"


def projects_root() -> Path:
    v = os.environ.get("LOCAL_IMAGEN_PROJECTS") or _cfg().get("projects_root")
    return Path(v).expanduser() if v else default_projects_root()


def _registry() -> dict:
    return _load(REGISTRY, {}).get("projects", {})


def _register(slug: str, path: Path):
    with _lock("registry"):
        reg = _load(REGISTRY, {})
        reg.setdefault("projects", {})[slug] = {"path": str(Path(path).resolve())}
        _save(REGISTRY, reg)


def _unregister(slug: str):
    with _lock("registry"):
        reg = _load(REGISTRY, {})
        if reg.get("projects", {}).pop(slug, None) is not None:
            _save(REGISTRY, reg)


def all_projects() -> dict[str, Path]:
    """slug -> folder of every project on this computer (default folder + registered elsewhere)."""
    found: dict[str, Path] = {}
    cands = []
    root = projects_root()
    if root.is_dir():
        cands += [d for d in sorted(root.iterdir()) if (d / "project.json").is_file()]
    cands += [Path(v["path"]) for v in _registry().values()]
    for d in cands:
        if not (d / "project.json").is_file():
            continue
        try:
            slug = _read_json(d / "project.json").get("slug") or d.name
        except (OSError, json.JSONDecodeError):
            slug = d.name
        if slug in found and not _same_path(found[slug], d):
            log(f"duplicate project slug {slug}: {found[slug]} and {d}; keeping the first")
            continue
        found.setdefault(slug, d)
    return found


def _find_project(name: str) -> tuple[str, Path]:
    projects = all_projects()
    n = name.strip()
    if n in projects:
        return n, projects[n]
    try:
        s = slugify(n)
    except ValueError:
        s = ""
    if s in projects:
        return s, projects[s]
    for slug, p in projects.items():
        try:
            if str(_read_json(p / "project.json").get("name", "")).lower() == n.lower():
                return slug, p
        except (OSError, json.JSONDecodeError):
            pass
    raise ValueError(f"no project '{name}' on this computer. Projects: {', '.join(sorted(projects)) or 'none yet'} "
                     "(list_projects for details, create_project for a new one)")


def _sessions_on(slug: str, path) -> list[dict]:
    return [i for i in _other_live_sessions()
            if i.get("project") and i["project"].get("slug") == slug and _same_path(i["project"]["path"], path)]


def current() -> tuple[Path, dict]:
    pj = S.get("project")
    if not pj:
        if S.get("scratch"):
            raise ValueError("this session makes one-off images without a project; guide tools need a project "
                             "(use_project or create_project, after asking the user)")
        raise ValueError(NO_PROJECT)
    p = Path(pj["path"])
    if not (p / "project.json").is_file():  # moved or removed by another session?
        found = all_projects().get(pj["slug"])
        if not found:
            raise ValueError(f"project '{pj['name']}' is no longer at {p} and isn't registered anywhere else")
        S["project"]["path"] = str(found)
        _save_session()
        p = found
    return p, _read_json(p / "project.json")


def _entries(proj: Path) -> list[dict]:
    out = []
    g = proj / "guide"
    if g.is_dir():
        for f in sorted(g.glob("*/*/entry.json")):
            e = _read_json(f)
            e["_dir"] = f.parent
            out.append(e)
    return out


def _eid(e: dict) -> str:
    return f"{e['kind']}/{e['slug']}"


def _find_entry(proj: Path, ref: str, entries: list[dict] | None = None) -> dict:
    entries = _entries(proj) if entries is None else entries
    r, kind = ref.strip(), None
    if "/" in r:
        kind, r = r.split("/", 1)
        kind = slugify(kind)
    try:
        rs = slugify(r)
    except ValueError:
        rs = ""
    hits = [e for e in entries if (kind is None or e["kind"] == kind)
            and (e["slug"] == rs or str(e["name"]).lower() == r.lower())]
    if len(hits) == 1:
        return hits[0]
    if not hits:
        raise ValueError(f"no guide entry '{ref}' in this project. Entries: "
                         f"{', '.join(_eid(e) for e in entries) or 'none yet'}")
    raise ValueError(f"'{ref}' is ambiguous: {', '.join(_eid(e) for e in hits)}. Use kind/name.")


def _save_entry(e: dict):
    _save(e["_dir"] / "entry.json", {k: v for k, v in e.items() if k != "_dir"})


def _primary_ref(e: dict) -> dict | None:
    refs = e.get("refs") or []
    return next((r for r in refs if r.get("primary")), refs[0] if refs else None)


def _entry_text(e: dict, full: bool = True) -> str:
    lines = [f"{e['name']} ({_eid(e)})", f"  description: {e.get('description') or '(none yet)'}"]
    if e.get("concept"):
        lines.append("  concept (steers pose/expression/action; never sent to the model): " + e["concept"])
    if e.get("must_have"):
        lines.append("  must show: " + "; ".join(e["must_have"]))
    if e.get("never"):
        lines.append("  never: " + "; ".join(e["never"]))
    if full:
        if e.get("tags"):
            lines.append("  tags: " + ", ".join(e["tags"]))
        if e.get("notes"):
            lines.append("  notes: " + e["notes"])
        for i, r in enumerate(e.get("refs") or [], 1):
            extra = (" - " + r["role"]) if r.get("role") else ""
            star = " (primary)" if r is _primary_ref(e) else ""
            lines.append(f"  reference {i}{star}: {r['label']}{extra}  [{e['_dir'] / r['file']}]")
        if e.get("examples"):
            lines.append(f"  approved examples: {len(e['examples'])} (latest: {e['examples'][-1]})")
    return "\n".join(lines)


def _kind_order(kinds) -> list[str]:
    ks = list(dict.fromkeys(kinds))
    return [k for k in SECTION_ORDER if k in ks] + sorted(k for k in ks if k not in SECTION_ORDER and k != "style") \
        + (["style"] if "style" in ks else [])


def _write_guide_md(proj: Path, info: dict | None = None):
    """Human-readable overview of the guide (GUIDE.md in the project folder), regenerated on every change."""
    info = info or _read_json(proj / "project.json")
    entries = _entries(proj)
    lines = [f"# {info['name']}: style guide", "",
             "_Generated by the local-imagen plugin. Edit the guide by asking Claude; manual edits here are "
             "overwritten._", ""]
    if info.get("description"):
        lines += [info["description"], ""]
    if info.get("default_style"):
        lines += [f"Default style: **{info['default_style']}**", ""]
    if not entries:
        lines.append("_No entries yet._")
    groups: dict[str, list] = {}
    for e in entries:
        groups.setdefault(e["kind"], []).append(e)
    for k in ["style"] + [x for x in _kind_order(groups) if x != "style"]:
        if k not in groups:
            continue
        lines += [f"## {GUIDE_TITLES.get(k, k.replace('-', ' ').title())}", ""]
        for e in groups[k]:
            lines += [f"### {e['name']}", "", e.get("description") or "_(no description yet)_", ""]
            if e.get("concept"):
                lines += [f"**Concept:** {e['concept']}", ""]
            for label, key in (("Must show", "must_have"), ("Never", "never"), ("Tags", "tags")):
                if e.get(key):
                    lines.append(f"- **{label}:** " + ("; " if key != "tags" else ", ").join(e[key]))
            if e.get("notes"):
                lines.append(f"- **Notes:** {e['notes']}")
            if e.get("examples"):
                lines.append(f"- **Approved examples:** {len(e['examples'])}")
            if e.get("refs"):
                rel = e["_dir"].relative_to(proj).as_posix()
                lines += ["", " ".join(f'<img src="{html.escape(rel + "/" + r["file"])}" width="160" '
                                       f'alt="{html.escape(r["label"])}">' for r in e["refs"])]
            lines.append("")
    _write_bytes(proj / "GUIDE.md", "\n".join(lines).encode("utf-8"))


def _add_refs_locked(e: dict, imgs: list[tuple[str, bytes]], role: str, primary: bool) -> list[dict]:
    """Copy images into the entry's refs/ folder (caller holds the project lock). Existing ones get role/primary."""
    touched = []
    for label, raw in imgs:
        png, fid = _normalize(raw)
        rel = f"refs/{fid}"
        if not (e["_dir"] / rel).exists():
            _write_bytes(e["_dir"] / rel, png)
        hit = next((r for r in e["refs"] if r["file"] == rel), None)
        if hit is None:
            hit = {"file": rel, "label": label[:60], "role": role.strip(), "primary": False, "added": now_iso()}
            e["refs"].append(hit)
        elif role.strip():
            hit["role"] = role.strip()
        touched.append(hit)
    if primary and touched:
        for r in e["refs"]:
            r["primary"] = r["file"] == touched[0]["file"]
    e["updated"] = now_iso()
    _save_entry(e)
    return touched


def _guide_summary(proj: Path, info: dict) -> str:
    entries = _entries(proj)
    if not entries:
        return "Its style guide is empty."
    counts: dict[str, int] = {}
    for e in entries:
        counts[e["kind"]] = counts.get(e["kind"], 0) + 1
    parts = [f"{n} {k}" for k, n in counts.items()]
    ds = f" Default style: {info['default_style']}." if info.get("default_style") else ""
    return f"Guide: {', '.join(parts)}.{ds}"


def _switch_to(slug, path, info, discard_pending: bool, scratch: bool = False) -> str:
    cur = S.get("project") or {}
    if scratch and S.get("scratch"):
        return "Already making one-off images (no project)."
    if not scratch and cur.get("slug") == slug and _same_path(cur.get("path", ""), path):
        return f"Already on project '{info['name']}'. " + _guide_summary(Path(path), info)
    if BUSY:
        raise ValueError("an image is still being generated in this session; switch when it has finished")
    if S["requests"]:
        if not discard_pending:
            raise ValueError(
                f"{len(S['requests'])} image(s) of {_mode_text()} are waiting for approval ({', '.join(S['requests'])}). "
                "Ask the user to approve (approve_image) or discard them first, or call again with "
                "discard_pending=True if they agree to drop them.")
        for rid in list(S["requests"]):
            _close_request(rid, True)
    prev = _mode_text()
    S.update(project=None if scratch else {"slug": slug, "name": info["name"], "path": str(Path(path).resolve())},
             scratch=scratch, active=[], requests={}, last_request=None)
    _save_session()
    head = f"Switched from {prev} to {_mode_text()}."
    if prev != "no project chosen yet":
        head += " Nothing was carried over (no references, candidates or pending images)."
    return head if scratch else f"{head} Folder: {path}. " + _guide_summary(Path(path), info)


# ---------------------------------------------------------------- ComfyUI + Docker plumbing
async def comfy_up() -> bool:
    try:
        async with httpx.AsyncClient(timeout=3) as c:
            return (await c.get(f"{COMFY}/system_stats")).status_code == 200
    except httpx.HTTPError:
        return False


def _env():
    env = os.environ.copy()
    if sys.platform == "win32":
        # MCP clients may start servers with a minimal environment; the docker CLI needs these to find
        # its plugins (compose). Values come from the OS, not from hardcoded paths.
        for name, guid in (("ProgramFiles", FOLDERID_PROGRAM_FILES), ("ProgramW6432", FOLDERID_PROGRAM_FILES),
                           ("ProgramData", FOLDERID_PROGRAM_DATA), ("ALLUSERSPROFILE", FOLDERID_PROGRAM_DATA)):
            if not env.get(name):
                p = _win_known_folder(guid)
                if p:
                    env[name] = str(p)
        if env.get("ProgramFiles"):
            docker_bin = str(Path(env["ProgramFiles"]) / "Docker" / "Docker" / "resources" / "bin")
            if Path(docker_bin).is_dir() and docker_bin.lower() not in env.get("PATH", "").lower():
                env["PATH"] = docker_bin + os.pathsep + env.get("PATH", "")
    elif sys.platform == "darwin":
        env["PATH"] = env.get("PATH", "") + os.pathsep + "/usr/local/bin" + os.pathsep + "/opt/homebrew/bin"
    return env


def _run(cmd, cwd=None, timeout=300):
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout, env=_env(),
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def _docker_ready() -> bool:
    try:
        r = _run(["docker", "info", "--format", "{{.ServerVersion}}"], timeout=30)
        return r.returncode == 0 and r.stdout.strip()[:1].isdigit()
    except (OSError, subprocess.TimeoutExpired):
        return False


def _docker_desktop() -> Path | None:
    if sys.platform != "win32":
        return None
    pf = _win_dir("ProgramFiles", FOLDERID_PROGRAM_FILES)
    p = pf / "Docker" / "Docker" / "Docker Desktop.exe" if pf else None
    return p if p and p.exists() else None


async def _start_container(notes: list):
    if not MANAGE_DOCKER:
        raise RuntimeError(f"ComfyUI is not answering at {COMFY} and manage_docker is off: start ComfyUI yourself")
    if not (DOCKER_DIR / "docker-compose.yml").exists():
        raise RuntimeError(f"no docker-compose.yml in {DOCKER_DIR}: run the local-imagen-setup skill first")
    await _ensure_docker(notes)
    r = await asyncio.to_thread(_run, ["docker", "compose", "up", "-d"], str(DOCKER_DIR), 600)
    if r.returncode != 0:
        raise RuntimeError("docker compose up failed: " + (r.stderr or r.stdout)[-800:])
    for _ in range(90):
        if await comfy_up():
            notes.append("started ComfyUI")
            return
        await asyncio.sleep(2)
    raise RuntimeError("ComfyUI container started but the API did not answer within 3 minutes")


async def _backend_ready(free_gpu: bool = True) -> str:
    await asyncio.to_thread(_lease_take)
    notes = []
    if not await comfy_up():
        await _start_container(notes)
    if free_gpu:  # only after ComfyUI is confirmed up, so a failed start doesn't unload Ollama for nothing
        unloaded = await asyncio.to_thread(_ollama_unload_sync)
        if unloaded:
            notes.append("unloaded Ollama models from the GPU (stop_backend reloads them): " + ", ".join(unloaded))
    return "; ".join(notes)


async def upload_ref(c: httpx.AsyncClient, path: Path) -> str:
    data = path.read_bytes()
    name = hashlib.sha1(data).hexdigest()[:12] + ".png"  # content-addressed: identical across sessions
    r = await c.post(f"{COMFY}/upload/image", files={"image": (name, data, "image/png")},
                     data={"type": "input", "subfolder": "mcp_refs", "overwrite": "true"})
    r.raise_for_status()
    j = r.json()
    return f"{j['subfolder']}/{j['name']}" if j.get("subfolder") else j["name"]


def build_workflow(prompt: str, ref_names: list[str], w: int, h: int, seed: int, steps: int, ref_mp: float,
                   lora: str | None = None, lora_strength: float = 1.0):
    wf = {
        "1": {"class_type": "UNETLoader", "inputs": {"unet_name": MODEL["unet"], "weight_dtype": "default"}},
        "2": {"class_type": "CLIPLoader", "inputs": {"clip_name": MODEL["text_encoder"], "type": "flux2",
                                                      "device": "default"}},
        "3": {"class_type": "VAELoader", "inputs": {"vae_name": MODEL["vae"]}},
        "4": {"class_type": "CLIPTextEncode", "inputs": {"text": prompt, "clip": ["2", 0]}},
        "5": {"class_type": "ConditioningZeroOut", "inputs": {"conditioning": ["4", 0]}},
        "6": {"class_type": "EmptyFlux2LatentImage", "inputs": {"width": w, "height": h, "batch_size": 1}},
        "7": {"class_type": "Flux2Scheduler", "inputs": {"steps": steps, "width": w, "height": h}},
        "8": {"class_type": "RandomNoise", "inputs": {"noise_seed": seed}},
        "9": {"class_type": "KSamplerSelect", "inputs": {"sampler_name": "euler"}},
    }
    pos, neg = ["4", 0], ["5", 0]
    for i, name in enumerate(ref_names):  # chain one ReferenceLatent per reference image (official wiring)
        b = 100 + i * 10
        wf[str(b)] = {"class_type": "LoadImage", "inputs": {"image": name}}
        wf[str(b + 1)] = {"class_type": "ImageScaleToTotalPixels", "inputs": {
            "image": [str(b), 0], "upscale_method": "nearest-exact", "megapixels": ref_mp, "resolution_steps": 1}}
        wf[str(b + 2)] = {"class_type": "VAEEncode", "inputs": {"pixels": [str(b + 1), 0], "vae": ["3", 0]}}
        wf[str(b + 3)] = {"class_type": "ReferenceLatent", "inputs": {"conditioning": pos, "latent": [str(b + 2), 0]}}
        wf[str(b + 4)] = {"class_type": "ReferenceLatent", "inputs": {"conditioning": neg, "latent": [str(b + 2), 0]}}
        pos, neg = [str(b + 3), 0], [str(b + 4), 0]
    wf.update({
        "10": {"class_type": "CFGGuider", "inputs": {"model": ["20" if lora else "1", 0], "positive": pos,
                                                     "negative": neg, "cfg": 1.0}},
        "11": {"class_type": "SamplerCustomAdvanced", "inputs": {"noise": ["8", 0], "guider": ["10", 0],
                                                                  "sampler": ["9", 0], "sigmas": ["7", 0],
                                                                  "latent_image": ["6", 0]}},
        "12": {"class_type": "VAEDecode", "inputs": {"samples": ["11", 0], "vae": ["3", 0]}},
        "13": {"class_type": "PreviewImage", "inputs": {"images": ["12", 0]}},
    })
    if lora:
        wf["20"] = {"class_type": "LoraLoaderModelOnly", "inputs": {"model": ["1", 0], "lora_name": lora,
                                                                    "strength_model": lora_strength}}
    return wf


async def run_workflow(c: httpx.AsyncClient, wf: dict, timeout=900) -> bytes:
    r = await c.post(f"{COMFY}/prompt", json={"prompt": wf, "client_id": str(uuid.uuid4())})
    if r.status_code != 200:
        raise RuntimeError(f"ComfyUI rejected the workflow: {r.text[:1200]}")
    pid = r.json()["prompt_id"]
    t0 = time.monotonic()
    while True:
        h = (await c.get(f"{COMFY}/history/{pid}")).json()
        if pid in h:
            st = h[pid].get("status", {})
            errs = [m[1] for m in st.get("messages", []) if m[0] == "execution_error"]
            if errs:
                e = errs[0]
                raise RuntimeError(f"ComfyUI error in {e.get('node_type')}: {e.get('exception_message', '')[:600]}")
            if st.get("completed"):
                for out in h[pid]["outputs"].values():
                    for img in out.get("images", []):
                        v = await c.get(f"{COMFY}/view", params={k: img[k] for k in ("filename", "subfolder", "type")})
                        v.raise_for_status()
                        return v.content
                raise RuntimeError("workflow finished without an image")
        if time.monotonic() - t0 > timeout:
            raise TimeoutError("generation timed out")
        await asyncio.sleep(0.5)


# ---------------------------------------------------------------- prompt planning
def _pose_ref(pose: str) -> dict:
    """generate_image(pose=...): a render_pose image (path or name from this session), any pose image file, or a
    guide pose entry of the open project."""
    p = pose.strip().strip('"')
    cand = [Path(p).expanduser(), POSES / p, POSES / f"{p}.png"]
    hit = next((c for c in cand if c.is_file() and c.suffix.lower() in IMG_EXT), None)
    if hit:
        return {"path": hit, "legend": POSE_LEGEND, "source": f"pose guide {hit.stem}"}
    if S.get("project"):
        proj, _ = current()
        e = _find_entry(proj, p if "/" in p else f"pose/{p}")
        pr = _primary_ref(e)
        if not pr:
            raise ValueError(f"guide entry {_eid(e)} has no reference image to use as the pose")
        return {"path": e["_dir"] / pr["file"], "legend": POSE_LEGEND if e.get("pose_spec") else ROLE_TEXT["pose"],
                "source": _eid(e), "file": (e["_dir"] / pr["file"]).relative_to(proj).as_posix()}
    raise ValueError(f"pose '{pose}' is not an image file or a pose rendered in this session")


def _depth_of(pose_r: dict) -> dict:
    """The depth map rendered next to a render_pose mannequin (render_pose(..., depth=True))."""
    p = Path(pose_r["path"])
    d = p if p.stem.endswith("-depth") else p.with_name(f"{p.stem}-depth.png")
    if not d.is_file():
        raise ValueError(f"the depth model (pose_mode 'depth', the default at speed 'full') needs the pose's depth "
                         f"map ({d.name}): render the pose with render_pose (a guide pose entry: "
                         "render_pose(pose='<entry name>')); it writes the depth map at speed 'full' "
                         "(or pass depth=True), then pass that mannequin. For a pose image that isn't a mannequin, "
                         "pass pose_mode='guide'")
    return {"path": d, "source": f"depth map {d.stem}",
            "legend": "the depth map of the pose to use (near = bright): copy its pose, body proportions and "
                      "camera angle"}


def _plan(prompt: str, entry_names: list[str], use_refs: bool, mode: str, use_default_style: bool,
          pose: str = "", pose_crop: bool = True, pose_mode: str = "guide") -> dict:
    """Compose the full prompt and pick the references (max 4) for one request in this session's context."""
    notes: list[str] = []
    pose_r = _pose_ref(pose) if pose.strip() else None
    depth = pose_mode == "depth"
    if depth:
        if not pose_r:
            raise ValueError("pose_mode='depth' needs pose=<a render_pose mannequin rendered with depth=True>")
        pose_r = _depth_of(pose_r)
    if not S.get("project"):  # one-off images: the original behaviour
        if entry_names:
            raise ValueError("guide entries need a project; this session makes one-off images")
        refs = [{"path": LIB / r["file"], "source": f"attached '{r['label']}'"} for r in S["active"]] if use_refs else []
        if pose_r:  # say what each image is, so the pose guide isn't taken for a style reference
            refs = ([pose_r] + refs if depth else refs + [pose_r])[:MAX_REFS]
            other = ROLE_TEXT["style"] if mode == "style" else "what the prompt describes"
            legend = " ".join(f"Image {i} shows {r.get('legend') or other}." for i, r in enumerate(refs, 1))
            text = ("refcontrol\n\n" if depth else "") + f"{prompt.strip()}\n\nReference images: {legend}"
        else:
            text = (STYLE_PREFIX + prompt) if refs and mode == "style" else prompt
        return {"text": text, "refs": refs, "entries": [], "checklist": [], "notes": notes, "project": None}

    proj, info = current()
    all_e = _entries(proj)
    chosen: list[dict] = []
    for n in entry_names:
        e = _find_entry(proj, n, all_e)
        if _eid(e) not in [_eid(x) for x in chosen]:
            chosen.append(e)
    if use_default_style and info.get("default_style") and not any(e["kind"] == "style" for e in chosen):
        try:
            chosen.append(_find_entry(proj, "style/" + info["default_style"], all_e))
        except ValueError:
            notes.append(f"default style '{info['default_style']}' no longer exists in the guide")

    groups: dict[str, list] = {}
    for e in chosen:
        groups.setdefault(e["kind"], []).append(e)
    sections = []
    for k in _kind_order(groups):
        lines = []
        for e in groups[k]:
            desc = (e.get("description") or "").strip()
            must = "; ".join(e.get("must_have") or [])
            if not desc and not must:
                continue  # contributes only its reference image
            line = f"- {e['name']}: {desc}" if desc else f"- {e['name']}:"
            if must:
                line += ("" if not desc or desc.endswith((".", "!", "?")) else ".") + f" Must show: {must}."
            lines.append(line)
        if lines:
            sections.append(f"{SECTION_TITLES.get(k, k.replace('-', ' ').title())}:\n" + "\n".join(lines))

    ranked = []
    if use_refs:
        for r in S["active"]:
            legend = ROLE_TEXT["style"] if mode == "style" else f"'{r['label']}' (use it as the prompt describes)"
            ranked.append((-1, {"path": LIB / r["file"], "legend": legend, "source": f"attached '{r['label']}'"}))
        if pose_r:
            ranked.append((-2 if depth else -0.5, pose_r))  # the depth LoRA expects the depth map as image 1
        posed = bool(pose_r) or any(e["kind"] == "pose" and _primary_ref(e) for e in chosen)
        for e in chosen:
            pr = _primary_ref(e)
            if pr and depth and e["kind"] not in ("character", "creature"):
                # the LoRA was trained on (depth, subject reference) pairs: other images only confuse it; the
                # style still comes from the text and from the subject's own reference
                notes.append(f"{_eid(e)}'s reference left out in depth mode (its description is kept)")
                continue
            if pr:
                legend = ROLE_TEXT.get(e["kind"], "{name}").format(name=e["name"])
                if e["kind"] == "pose" and e.get("pose_spec"):
                    legend = POSE_LEGEND
                elif pr.get("role"):
                    legend += f" ({pr['role']})"
                ref = {"path": e["_dir"] / pr["file"], "legend": legend, "source": _eid(e),
                       "file": (e["_dir"] / pr["file"]).relative_to(proj).as_posix()}
                if posed and pose_crop and not depth and e["kind"] in ("character", "creature", "style"):
                    # a full-body figure in any reference overrides the pose guide: keep only its head and chest
                    crop = _head_chest_crop(ref["path"], f"{pr.get('label', '')} {pr.get('role', '')}")
                    if crop:
                        legend = (f"{e['name']}: keep the same face, hair, outfit and colours (this image shows only "
                                  "the head and chest; take the body pose from the pose guide)")
                        if e["kind"] == "style":
                            legend = ("the art style to use (a detail of the style reference): match its linework, "
                                      "colouring, shading, palette and texture, not its content or pose")
                        ref = {"path": crop, "source": f"{_eid(e)} (head and chest crop)", "legend": legend}
                        notes.append(f"{_eid(e)}'s reference was cropped to head and chest so the pose guide sets "
                                     "the pose (pose_crop=False keeps it whole)")
                ranked.append((REF_PRIORITY.get(e["kind"], 4), ref))
    refs = [r for _, r in sorted(ranked, key=lambda x: x[0])]
    if len(refs) > MAX_REFS:
        notes.append("left out (max 4 references per image): " + ", ".join(r["source"] for r in refs[MAX_REFS:]))
        refs = refs[:MAX_REFS]
    legend = ("Reference images: " + " ".join(f"Image {i} shows {r['legend']}." for i, r in enumerate(refs, 1))
              if refs else "")
    text = "\n\n".join(x for x in ["refcontrol" if depth else "", prompt.strip(), *sections, legend] if x)

    checklist = []
    for e in chosen:
        items = []
        if e.get("must_have"):
            items.append("Must show " + "; ".join(e["must_have"]))
        if e.get("never"):
            items.append("Must NOT show " + "; ".join(e["never"]))
        if items:
            checklist.append(f"{e['name']}: " + ". ".join(items))
    return {"text": text, "refs": refs, "notes": notes, "checklist": checklist,
            "entries": [{"kind": e["kind"], "slug": e["slug"], "name": e["name"]} for e in chosen],
            "project": {"slug": S["project"]["slug"], "name": info["name"], "path": str(proj)}}


# ---------------------------------------------------------------- requests (per session)
def _candidate_count() -> int:
    return S["candidates"] or DEFAULT_CANDIDATES


def _speed() -> str:
    s = S.get("speed")
    return s if s in SPEEDS else DEFAULT_SPEED


def _open_request(request_id: str) -> tuple[str, dict]:
    rid = request_id or S["last_request"]
    if not rid or rid not in S["requests"]:
        raise ValueError(f"no open request '{request_id or '(latest)'}' in this session. "
                         f"Open requests: {', '.join(S['requests']) or 'none'}")
    return rid, S["requests"][rid]


def _close_request(rid: str, delete_selected: bool):
    shutil.rmtree(CAND / rid, ignore_errors=True)
    if delete_selected:
        for f in (SEL / f"{rid}.png", SEL / f"{rid}.json"):
            f.unlink(missing_ok=True)
    S["requests"].pop(rid, None)
    if S["last_request"] == rid:
        S["last_request"] = next(reversed(list(S["requests"])), None) if S["requests"] else None


# ---------------------------------------------------------------- MCP server + tools
mcp = MCPServer(
    "local-imagen",
    instructions=(
        "Local image generation (ComfyUI + FLUX.2 klein 4B on the user's GPU, ~9 s/image, ~16 s with references).\n\n"
        "PROJECTS: ASK FIRST. Every image belongs to a game project (which carries a style guide) or to a one-off "
        "session without a project. At the first image or style-guide request of a conversation, unless the user "
        "named the project in THIS conversation, call list_projects and ask which project to work on: an existing "
        "one, a new one (create_project; they may choose its folder with parent_dir) or a one-off image without a "
        "project. Ask even if this session already has a project open (the server outlives /clear). Never choose "
        "for them. Then call use_project(name), create_project(name) or use_project(scratch=True).\n"
        "ISOLATION. use_project drops everything of the previous project from this session (references, "
        "candidates, pending images). After a switch, carry nothing from the previous project into prompts, guide "
        "edits or subagent requests: no names, descriptions, styles or wording. The open project's guide is the "
        "only source of truth; don't copy guide details into your own memory files. Every Claude session has its "
        "own image session, so parallel sessions never share working state.\n\n"
        "STYLE GUIDE: MANAGED BY CHAT. Entries have a kind (style, character, creature, location, prop, pose, "
        "camera, or another single word), a fixed description (the exact wording put into every prompt that uses "
        "the entry), an optional concept, must_have / never checklists, tags, notes and reference images. Read "
        "with guide_list / guide_show before editing; write only what the user said or approved (ask rather than "
        "invent). Keep descriptions short and visual. The concept is who the character, creature or place is: "
        "personality, how they move and fight, mood, what their symbols mean. It is never sent to the image model "
        "(a text-to-image model takes words literally, so story text turns into stray objects); the image-picker "
        "reads it to choose poses, expressions, actions and mood that fit. Put story and meaning in concept, "
        "looks in description, and design history in notes. "
        "set_project_info(default_style=...) applies a style entry to every image. "
        "Approved images are filed in the project's outputs; approve_image(reference_for=[...]) or "
        "guide_add_reference(<image path>) turns them into references.\n\n"
        "CANDIDATE WORKFLOW. Every generate_image call renders N candidates (default 3, set_candidate_count) and "
        "the best one is chosen with pick_candidate. Candidate images must never reach the user's chat: in the "
        "main conversation, do NOT call generate_image yourself. Delegate each request to the 'image-picker' "
        "subagent (local-imagen:image-picker when installed as a plugin) and give it: the project name (or "
        "'scratch'), what to draw, and the guide entry names involved. Nothing else from earlier projects. (If "
        "you ARE the image-picker, generate and pick yourself.) Show the user only the picked image (read the path "
        "the subagent returns), with at most one or two short lines. Use show_candidates only when the user "
        "explicitly asks. When the user likes the pick, call approve_image; discard_image drops it. End with one "
        "short line that the image session is still up and they can ask you to clear it (clear_session); no "
        "explanation about ComfyUI or Ollama.\n\n"
        "POSE GUIDES AND SPEED. render_pose draws a grey mannequin in a pose with Blender (presets + joint angles, "
        "any camera angle). By default (speed 'full') EVERY image with a character is drawn from a mannequin pose "
        "guide with the depth model: the image-picker renders the pose (render_pose writes the depth map by "
        "default) and generate_image(pose=...) uses the depth model automatically; a character request without a "
        "pose is refused. Lower the speed ONLY when the user asks for faster images, one step per request: "
        "set_speed('fast') turns the depth model off (the mannequin is sent as a plain pose reference); if they "
        "insist on even faster, set_speed('fastest') also skips the mannequin. set_speed('full') goes back; "
        "clear_session resets it. When the speed isn't 'full', say so in the image-picker request. If render_pose "
        "says Blender isn't set up, call setup_blender() (builds a small Docker image from the official Blender "
        "build) or, without Docker, setup_blender(path=<installed blender>); blender_status shows the state. When "
        "the user wants to keep a pose, render_pose(..., save_to='<name>') saves it as a guide pose entry.\n\n"
        "EDITS FROM RED MARKS. When the user attaches an image with red marks (drawn with the Claude app's pen) "
        "and asks for changes, edit only the marked areas, never draw a new image: call find_marks('latest') to "
        "see the numbered marks and the original each is on, then delegate to the image-picker with the project, "
        "the marked image's path, and one instruction per mark (it calls edit_image). A green line or loop next "
        "to a red mark means: put the red-marked thing there, pointing that way (the model removes it and redraws "
        "the connected region with it in place; name it and how it attaches in the instruction). Everything outside the marks stays as it was; approve_image files the edited "
        "version. PRECISION WORK WITH MANNEQUINS (e.g. a trustworthy grip on an existing image): place a Blender "
        "mannequin exactly (render_pose, or a custom Blender render), make its depth map lined up with the picture, "
        "and edit with edit_image(guide=<depth map>, strength=1.0): the model redraws the marked area from "
        "scratch following the guide. Never paste, trace or blend mannequin pixels into the picture, and never "
        "blur or smudge areas: without a guide, edit_image copies what is inside the mark very closely."
    ),
)


def tool(fn):
    """Register an MCP tool whose failures reach the client as a readable message (not a generic crash)."""
    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        try:
            return await fn(*args, **kwargs)
        except ToolError:
            raise
        except Exception as e:  # noqa: BLE001
            log(f"{fn.__name__} failed: {type(e).__name__}: {e}")
            raise ToolError(f"{type(e).__name__}: {e}") from e
    return mcp.tool()(wrapper)


# ---- projects
@tool
async def list_projects() -> str:
    """List every game project on this computer (the default projects folder plus projects kept elsewhere), with
    location, guide size, approved images and which one this session uses. Use it to ask the user which project to
    work on."""
    root = projects_root()
    found = all_projects()
    others: dict[str, int] = {}
    for i in _other_live_sessions():
        if i.get("project"):
            others[i["project"]["slug"]] = others.get(i["project"]["slug"], 0) + 1
    cur = (S.get("project") or {}).get("slug")
    lines = [f"Default projects folder: {root}"]
    if not found:
        lines.append("No projects yet.")
    for slug, path in sorted(found.items()):
        try:
            info = _read_json(path / "project.json")
        except (OSError, json.JSONDecodeError) as e:
            lines.append(f"- {slug}: unreadable project.json at {path} ({e})")
            continue
        n_e = len(list((path / "guide").glob("*/*/entry.json")))
        n_o = len(list((path / "outputs").glob("*/*.png")))
        tags = (["this session"] if slug == cur else []) + \
            ([f"open in {others[slug]} other session(s)"] if others.get(slug) else [])
        lines.append(f"- {info.get('name', slug)} ({slug}): {n_e} guide entries, {n_o} approved images | {path}"
                     + (f"  [{', '.join(tags)}]" if tags else ""))
        if info.get("description"):
            lines.append(f"    {info['description']}")
    for slug, v in _registry().items():
        if not (Path(v["path"]) / "project.json").is_file():
            lines.append(f"- {slug}: MISSING, registered at {v['path']} but the folder is gone (remove_project forgets it)")
    lines.append("This session: " + _mode_text())
    return "\n".join(lines)


@tool
async def create_project(name: str, description: str = "", parent_dir: str = "", switch: bool = True) -> str:
    """Create a game project with an empty style guide and (by default) switch this session to it.

    name: the game's working name. description: one line about the game (genre, mood).
    parent_dir: absolute folder to create the project in (the project becomes <parent_dir>/<name-as-folder>);
                empty = the default projects folder (see list_projects).
    """
    slug = slugify(name)
    projects = all_projects()
    if slug in projects:
        raise ValueError(f"a project '{slug}' already exists at {projects[slug]}; use_project('{slug}') opens it")
    base = Path(parent_dir.strip()).expanduser() if parent_dir.strip() else projects_root()
    if not base.is_absolute():
        raise ValueError("parent_dir must be an absolute folder path")
    path = base / slug
    if path.exists() and any(path.iterdir()):
        raise ValueError(f"{path} already exists and is not empty")
    info = {"schema": 1, "name": name.strip(), "slug": slug, "description": description.strip(),
            "default_style": None, "created": now_iso(), "updated": now_iso()}
    with _plock(path):
        (path / "guide").mkdir(parents=True, exist_ok=True)
        (path / "outputs").mkdir(exist_ok=True)
        _save(path / "project.json", info)
        _write_guide_md(path, info)
    if not _same_path(base, projects_root()):
        _register(slug, path)
    msg = f"Created project '{info['name']}' at {path}."
    if switch:
        try:
            msg += " " + _switch_to(slug, path, info, discard_pending=False)
        except ValueError as e:
            msg += f" Not switched: {e}"
    return msg


@tool
async def use_project(name: str = "", scratch: bool = False, discard_pending: bool = False) -> str:
    """Switch this session to a project (name or folder name from list_projects), or to one-off images without a
    project (scratch=True). Everything of the previous project is dropped from this session: attached
    references, candidates and pending images. If images are waiting for approval, this fails unless
    discard_pending=True (only after the user agrees). Other Claude sessions are not affected."""
    if scratch:
        return _switch_to(None, None, None, discard_pending, scratch=True)
    if not name.strip():
        raise ValueError("give a project name (list_projects), or scratch=True for one-off images")
    slug, path = _find_project(name)
    return _switch_to(slug, path, _read_json(path / "project.json"), discard_pending)


@tool
async def project_overview() -> str:
    """The open project: folder, description, default style, every guide entry (one line each), attached ad-hoc
    references and images waiting for approval."""
    proj, info = current()
    entries = _entries(proj)
    lines = [f"Project '{info['name']}' ({S['project']['slug']}) at {proj}"]
    if info.get("description"):
        lines.append(f"Description: {info['description']}")
    lines.append(f"Default style: {info.get('default_style') or 'none'}")
    groups: dict[str, list] = {}
    for e in entries:
        groups.setdefault(e["kind"], []).append(e)
    for k in _kind_order(groups):
        lines.append(f"{GUIDE_TITLES.get(k, k.title())}:")
        for e in groups[k]:
            d = (e.get("description") or "(no description)")[:110]
            lines.append(f"  - {e['name']} ({_eid(e)}): {d} [{len(e.get('refs') or [])} ref(s)]")
    if not entries:
        lines.append("The guide is empty.")
    lines.append(describe_refs(S["active"]))
    if S["requests"]:
        lines.append("Waiting for approval: " + ", ".join(S["requests"]))
    return "\n".join(lines)


@tool
async def set_project_info(description: str | None = None, default_style: str | None = None,
                           rename_to: str = "") -> str:
    """Change the open project's description, its default style (a style entry applied to every image unless the
    request names another style; "" clears it) or its display name (the folder name stays)."""
    proj, _ = current()
    with _plock(proj):
        info = _read_json(proj / "project.json")
        if description is not None:
            info["description"] = description.strip()
        if default_style is not None:
            if default_style.strip():
                e = _find_entry(proj, default_style)
                if e["kind"] != "style":
                    raise ValueError(f"{_eid(e)} is not a style entry")
                info["default_style"] = e["slug"]
            else:
                info["default_style"] = None
        if rename_to.strip():
            info["name"] = rename_to.strip()
            S["project"]["name"] = info["name"]
            _save_session()
        info["updated"] = now_iso()
        _save(proj / "project.json", info)
        _write_guide_md(proj, info)
    return (f"Project '{info['name']}': description '{info.get('description') or ''}', "
            f"default style {info.get('default_style') or 'none'}.")


@tool
async def move_project(parent_dir: str, name: str = "") -> str:
    """Move a project folder (default: the open one) into another folder: it becomes <parent_dir>/<folder name>.
    Refused while another Claude session has the project open."""
    if name.strip():
        slug, path = _find_project(name)
    else:
        path, _ = current()
        slug = S["project"]["slug"]
    base = Path(parent_dir.strip()).expanduser()
    if not base.is_absolute():
        raise ValueError("parent_dir must be an absolute folder path")
    target = base / path.name
    if _same_path(target, path):
        return f"Project '{slug}' is already at {path}."
    if target.exists():
        raise ValueError(f"{target} already exists")
    busy = _sessions_on(slug, path)
    if busy:
        raise ValueError(f"project '{slug}' is open in {len(busy)} other Claude session(s); close it there first")
    if BUSY and S.get("project", {}).get("slug") == slug:
        raise ValueError("an image of this project is being generated; move it when it has finished")
    with _plock(path), _plock(target):
        base.mkdir(parents=True, exist_ok=True)
        shutil.move(str(path), str(target))
    if _same_path(base, projects_root()):
        _unregister(slug)
    else:
        _register(slug, target)
    if S.get("project") and S["project"]["slug"] == slug:
        S["project"]["path"] = str(target.resolve())
        _save_session()
    return f"Moved project '{slug}' to {target}."


@tool
async def register_project(path: str) -> str:
    """Add an existing project folder (one with a project.json, e.g. copied from another computer or cloned from
    git) to this computer's project list. It stays where it is."""
    p = Path(path.strip()).expanduser()
    if not (p / "project.json").is_file():
        raise ValueError(f"{p} has no project.json, so it isn't a local-imagen project")
    info = _read_json(p / "project.json")
    slug = info.get("slug") or slugify(info.get("name") or p.name)
    existing = all_projects().get(slug)
    if existing and not _same_path(existing, p):
        raise ValueError(f"another project with the folder name '{slug}' is already listed at {existing}")
    _register(slug, p)
    return f"Registered project '{info.get('name', slug)}' at {p.resolve()}. use_project('{slug}') opens it."


@tool
async def remove_project(name: str, delete_files: bool = False) -> str:
    """Take a project off this computer's list. delete_files=False only forgets a project kept outside the default
    folder (its files stay). delete_files=True moves the whole project folder to the Recycle Bin / Trash. Only
    with the user's explicit OK. Refused while another Claude session has it open."""
    try:
        slug, path = _find_project(name)
    except ValueError:
        reg = _registry()
        missing = next((s for s in (name.strip(), slugify(name)) if s in reg), None)
        if missing and not (Path(reg[missing]["path"]) / "project.json").is_file():
            _unregister(missing)
            return f"Forgot project '{missing}' (its folder {reg[missing]['path']} was already gone)."
        raise
    busy = _sessions_on(slug, path)
    if busy:
        raise ValueError(f"project '{slug}' is open in {len(busy)} other Claude session(s); close it there first")
    mine = S.get("project") and S["project"]["slug"] == slug
    if mine and (S["requests"] or BUSY):
        raise ValueError("this session has images of the project pending or generating; approve/discard them first")
    in_root = _same_path(path.parent, projects_root())
    if delete_files:
        send2trash(str(path))
        _unregister(slug)
        msg = f"Moved project '{slug}' ({path}) to the Recycle Bin / Trash."
    elif in_root:
        raise ValueError(f"'{slug}' lives in the default projects folder, so it would still be listed. Use "
                         "delete_files=True (Recycle Bin / Trash) or move_project it elsewhere first")
    else:
        _unregister(slug)
        msg = f"Forgot project '{slug}'; its files stay at {path} (register_project brings it back)."
    if mine:
        S.update(project=None, scratch=False, active=[], requests={}, last_request=None)
        _save_session()
        msg += " This session has no project open now."
    return msg


@tool
async def set_default_projects_folder(path: str) -> str:
    """Change the folder where new projects are created by default (saved in ~/.local-imagen/config.json).
    Projects already in the old default folder stay where they are and remain listed."""
    new = Path(path.strip()).expanduser()
    if not new.is_absolute():
        raise ValueError("give an absolute folder path")
    if os.environ.get("LOCAL_IMAGEN_PROJECTS"):
        raise ValueError("the LOCAL_IMAGEN_PROJECTS environment variable sets the folder on this computer; "
                         "change it there")
    old = projects_root()
    new.mkdir(parents=True, exist_ok=True)
    kept = []
    if old.is_dir() and not _same_path(old, new):
        for d in old.iterdir():
            if (d / "project.json").is_file():
                slug = _read_json(d / "project.json").get("slug") or d.name
                _register(slug, d)
                kept.append(slug)
    with _lock("config"):
        cfg = _cfg()
        cfg["projects_root"] = str(new.resolve())
        CFG_DIR.mkdir(parents=True, exist_ok=True)
        _save(CFG_FILE, cfg)
    return (f"New projects now go to {new.resolve()}."
            + (f" Kept listing the projects in {old}: {', '.join(kept)}." if kept else ""))


# ---- style guide
@tool
async def guide_list(kind: str = "") -> str:
    """List the open project's guide entries (optionally one kind: style, character, creature, location, prop,
    pose, camera, ...)."""
    proj, info = current()
    entries = [e for e in _entries(proj) if not kind or e["kind"] == slugify(kind)]
    if not entries:
        return f"No {kind + ' ' if kind else ''}entries in '{info['name']}' yet."
    lines = [f"Guide of '{info['name']}' (default style: {info.get('default_style') or 'none'}):"]
    for e in entries:
        lines.append(f"- {e['name']} ({_eid(e)}): {(e.get('description') or '(no description)')[:120]} "
                     f"[{len(e.get('refs') or [])} ref(s){', has concept' if e.get('concept') else ''}]")
    return "\n".join(lines)


@tool
async def guide_show(name: str, show_images: bool = True) -> list:
    """Show one guide entry in full (name, or kind/name if ambiguous), with thumbnails of its references."""
    proj, _ = current()
    e = _find_entry(proj, name)
    out: list = [_entry_text(e)]
    if show_images:
        out += [_thumb(e["_dir"] / r["file"], 192) for r in e.get("refs") or [] if (e["_dir"] / r["file"]).exists()]
    return out


@tool
async def guide_set(name: str, kind: str = "", description: str | None = None, must_have: list[str] | None = None,
                    never: list[str] | None = None, tags: list[str] | None = None, notes: str | None = None,
                    concept: str | None = None, rename_to: str = "") -> str:
    """Create or update a guide entry of the open project. Only the fields you pass change; lists you pass replace
    the old list (read the entry with guide_show first to keep existing items).

    kind: needed for a new entry: style, character, creature, location, prop, pose, camera, or another word.
    description: the fixed visual description used word for word in every prompt with this entry.
    concept: who the entry is: personality, how they move and fight, mood, meaning of their symbols, story role.
             Never sent to the image model; the image-picker reads it to choose fitting poses, expressions and
             actions. "" clears it.
    must_have: details every image must show (checked when picking). never: things that must not appear.
    tags / notes: for organising (e.g. design history); not sent to the model. rename_to: new display name
    (folder renamed too).
    """
    proj, info = current()
    created = False
    with _plock(proj):
        entries = _entries(proj)
        try:
            e = _find_entry(proj, f"{kind}/{name}" if kind else name, entries)
        except ValueError as err:
            if "ambiguous" in str(err):
                raise
            e = None
        if e is None:
            if not kind.strip():
                raise ValueError("this is a new entry: give its kind (" + ", ".join(KNOWN_KINDS) + ", or another word)")
            k, s = slugify(kind), slugify(name)
            e = {"name": name.strip(), "slug": s, "kind": k, "description": "", "concept": "", "must_have": [],
                 "never": [], "tags": [], "notes": "", "refs": [], "examples": [], "created": now_iso(),
                 "_dir": proj / "guide" / k / s}
            created = True
        if description is not None:
            e["description"] = description.strip()
        for key, val in (("must_have", must_have), ("never", never), ("tags", tags)):
            if val is not None:
                e[key] = [x.strip() for x in val if x and x.strip()]
        if notes is not None:
            e["notes"] = notes.strip()
        if concept is not None:
            e["concept"] = concept.strip()
        if rename_to.strip() and rename_to.strip() != e["name"]:
            ns = slugify(rename_to)
            nd = proj / "guide" / e["kind"] / ns
            if ns != e["slug"]:
                if nd.exists():
                    raise ValueError(f"{e['kind']}/{ns} already exists")
                if not created:
                    e["_dir"].rename(nd)
                if info.get("default_style") == e["slug"] and e["kind"] == "style":
                    info["default_style"] = ns
                    _save(proj / "project.json", info)
            e.update(name=rename_to.strip(), slug=ns, _dir=nd)
        e["updated"] = now_iso()
        _save_entry(e)
        _write_guide_md(proj, info)
    note = "" if e["kind"] in KNOWN_KINDS else f" (new kind '{e['kind']}')"
    return f"{'Created' if created else 'Updated'} {_eid(e)}{note}:\n" + _entry_text(e)


@tool
async def guide_delete(name: str) -> str:
    """Delete a guide entry of the open project (its folder, with its reference images, goes to the Recycle Bin /
    Trash). Approved images that used it are kept."""
    proj, _ = current()
    with _plock(proj):
        e = _find_entry(proj, name)
        send2trash(str(e["_dir"]))
        info = _read_json(proj / "project.json")
        if e["kind"] == "style" and info.get("default_style") == e["slug"]:
            info["default_style"] = None
            _save(proj / "project.json", info)
        _write_guide_md(proj, info)
    return f"Deleted {_eid(e)} (in the Recycle Bin / Trash if you need it back)."


@tool
async def guide_add_reference(name: str, sources: list[str], role: str = "", primary: bool = False) -> list:
    """Add reference images to a guide entry; they are copied into the project. sources: image files, folders,
    http(s) URLs, 'style:<preset>' (a saved one-off style) or a request id of this session (its picked image).
    role: what the image is for, e.g. 'turnaround sheet', 'face close-up', 'outfit only'. primary=True makes the
    (first) image the one used in generations (one reference per entry per image). Passing an image that is
    already a reference only updates its role/primary."""
    proj, _ = current()
    imgs = []
    for s in sources:
        imgs += _gather(s)
    with _plock(proj):
        e = _find_entry(proj, name)
        touched = _add_refs_locked(e, imgs, role, primary)
        _write_guide_md(proj)
    return [f"{_eid(e)} now has {len(e['refs'])} reference(s):\n" + _entry_text(e),
            *[_thumb(e["_dir"] / r["file"], 160) for r in touched]]


@tool
async def guide_remove_reference(name: str, reference: str) -> str:
    """Remove a reference image from a guide entry: its number from guide_show (1-based), label or file name.
    The file goes to the Recycle Bin / Trash."""
    proj, _ = current()
    with _plock(proj):
        e = _find_entry(proj, name)
        refs = e.get("refs") or []
        hit = None
        if reference.isdigit() and 1 <= int(reference) <= len(refs):
            hit = refs[int(reference) - 1]
        else:
            hit = next((r for r in refs if reference in (r["label"], r["file"], Path(r["file"]).name)), None)
        if not hit:
            raise ValueError(f"no reference '{reference}' in {_eid(e)}.\n" + _entry_text(e))
        refs.remove(hit)
        f = e["_dir"] / hit["file"]
        if f.exists():
            send2trash(str(f))
        e["updated"] = now_iso()
        _save_entry(e)
        _write_guide_md(proj)
    return f"Removed '{hit['label']}' from {_eid(e)}.\n" + _entry_text(e)


# ---- generation + review
def _open_for_request(project: str):
    """A subagent's request names its project: open it if this session has none, refuse a different one."""
    want = project.strip()
    if want:
        if want.lower() in ("scratch", "none", "one-off", "no project"):
            if S.get("project"):
                raise ValueError(f"PROJECT_MISMATCH: this session is working on {_mode_text()}, not one-off "
                                 "images. Only the main conversation switches (use_project), after the user asks.")
            if not S.get("scratch"):
                _switch_to(None, None, None, False, scratch=True)
        else:
            slug, path = _find_project(want)
            if (S.get("project") and S["project"]["slug"] != slug) or S.get("scratch"):
                raise ValueError(f"PROJECT_MISMATCH: this session is working on {_mode_text()}, not '{want}'. Only "
                                 "the main conversation switches projects (use_project), after the user asks.")
            if not S.get("project"):
                _switch_to(slug, path, _read_json(path / "project.json"), False)
    if not S.get("project") and not S.get("scratch"):
        raise ValueError(NO_PROJECT)


@tool
async def generate_image(prompt: str, entries: list[str] | None = None, project: str = "", aspect: str = "portrait",
                         width: int = 0, height: int = 0, seed: int = -1, candidates: int = 0,
                         use_references: bool = True, reference_mode: str = "style", use_default_style: bool = True,
                         pose: str = "", pose_crop: bool = True, pose_mode: str = "",
                         lora_strength: float = 1.0, steps: int = 4, preview_size: int = 512,
                         dry_run: bool = False) -> list:
    """Render N candidate images for one request with FLUX.2 klein 4B (local ComfyUI). You then MUST call
    pick_candidate with the best one; only the picked image is presented to the user.

    prompt: the scene: action, pose, composition, background. Don't restate guide entries; their fixed
            descriptions are added automatically. Entry concepts are NOT added: read them (guide_show) and turn
            them into visible pose, expression and action here, never into story words or symbols.
    entries: guide entry names (or kind/name) in the image, e.g. ["Aldric", "Frost Keep", "tactics camera"]. The
             project's default style is added unless use_default_style=False or a style entry is listed.
    project: the project the request is for (or 'scratch'). It must match this session's project; if the session
             has none yet it is opened. Mismatch = error (only the main conversation switches projects).
    aspect: portrait (896x1152), square (1024x1024), landscape (1152x896), tall (768x1344), wide (1344x768).
    width/height: explicit size (multiples of 16, overrides aspect).
    seed: -1 = random (candidates use seed, seed+1, ...). candidates: 0 = current default (3 unless changed).
    use_references: False sends no reference images this time (descriptions are still used).
    reference_mode: 'style' = ad-hoc attached references give only the art style; 'edit' = they are content to
                    edit/combine and the prompt may refer to 'image 1', 'image 2' (attached ones come first).
    pose: a pose guide that sets the body pose: a render_pose mannequin (its path or name), any pose image, or a
          guide pose entry. Full-length character, creature and style references are then cropped to head and
          chest (pose_crop=False keeps them whole), because a full-body figure makes the model copy its pose.
          REQUIRED for every request with a character entry unless the session speed is 'fastest': render the
          pose with render_pose first. pose='none' skips it only when one mannequin can't pose the image
          (several characters, a face close-up, Blender not set up); say why in your note.
    pose_mode: '' (default) follows the session speed: 'full' = 'depth', 'fast' / 'fastest' = 'guide'. Don't
          set it unless the user asked for a specific mode. 'guide' sends the grey mannequin as the pose
          reference. 'depth' uses the RefControl depth LoRA: image 1 = the pose's depth map (render_pose writes
          it by default at speed 'full'), then the full character/creature references (not cropped); other
          references are left out and the trigger word is added. lora_strength: 0.8-1.0 (lower keeps more of the
          reference's look).
    Max 4 reference images per image: attached ones first, then the pose guide, characters/creatures, style,
    locations, props.
    dry_run: only return the composed prompt, references and checklist (nothing is rendered).
    Starts Docker/ComfyUI automatically if needed (and frees the GPU from Ollama).
    """
    global BUSY
    _open_for_request(project)
    if width and height:
        w, h = width, height
    elif aspect in ASPECTS:
        w, h = ASPECTS[aspect]
    else:
        raise ValueError(f"unknown aspect '{aspect}'; use one of {list(ASPECTS)} or width+height")
    w, h = (max(256, min(2048, v)) // 16 * 16 for v in (w, h))
    if reference_mode not in ("style", "edit"):
        raise ValueError("reference_mode must be 'style' or 'edit'")
    speed = _speed()
    no_pose = pose.strip().lower() == "none"
    if no_pose:
        pose = ""
    if not pose_mode:
        pose_mode = "depth" if pose.strip() and speed == "full" else "guide"
    if pose_mode not in ("guide", "depth"):
        raise ValueError("pose_mode must be 'guide' or 'depth' (or '' to follow the session speed)")
    plan = _plan(prompt, entries or [], use_references, reference_mode, use_default_style, pose, pose_crop,
                 pose_mode)
    chars = [e["name"] for e in plan["entries"] if e["kind"] == "character"]
    if chars and not pose.strip() and not no_pose and speed != "fastest":
        raise ValueError(
            f"POSE_REQUIRED: at speed '{speed}' every character image is drawn from a mannequin pose guide "
            f"({', '.join(chars)} is in this request). Call render_pose first"
            f"{' (it writes the depth map)' if speed == 'full' else ''}, then pass "
            "generate_image(pose=<mannequin path>). pose='none' only when one mannequin can't pose the image "
            "(several characters, a face close-up, Blender not set up). Only the user lowers the speed (set_speed).")
    if no_pose and chars:
        plan["notes"].append("no pose guide (pose='none')")
    lora = MODEL.get("depth_lora") if pose_mode == "depth" else None
    checklist = ("\nChecklist (judge the candidates against it):\n" + "\n".join(f"- {c}" for c in plan["checklist"])
                 if plan["checklist"] else "")
    ref_lines = "\n".join(f"  image {i}: {r['source']}" for i, r in enumerate(plan["refs"], 1)) or "  none"
    notes = ("\nNotes: " + "; ".join(plan["notes"])) if plan["notes"] else ""
    posing = (f"speed {speed}, pose: " + ({"depth": "mannequin + depth model", "guide": "mannequin guide"}[pose_mode]
                                         if pose.strip() else "none"))
    if dry_run:
        return [f"DRY RUN for {_mode_text()} ({w}x{h}, {posing}).\nReferences:\n{ref_lines}{notes}{checklist}\n\n"
                f"Full prompt:\n{plan['text']}"]
    n = max(1, min(MAX_CANDIDATES, candidates or _candidate_count()))
    BUSY += 1
    try:
        startup = await _backend_ready()
        if lora:
            async with httpx.AsyncClient(timeout=30) as c:
                info = (await c.get(f"{COMFY}/object_info/LoraLoaderModelOnly")).json()
            have = info["LoraLoaderModelOnly"]["input"]["required"]["lora_name"][0]
            if lora not in have:
                raise RuntimeError(f"the depth LoRA {lora} isn't installed in ComfyUI's models/loras: run "
                                   "scripts/setup.py --skip-build --skip-blender --models control (or download it "
                                   f"from huggingface.co/{DEPTH_LORA_REPO})")
        ref_mp = 1.0 if len(plan["refs"]) <= 2 else 0.6
        seed = random.randrange(2**31) if seed < 0 else seed
        rid = f"{dt.datetime.now():%m%d-%H%M%S}"
        while rid in S["requests"] or (CAND / rid).exists():
            rid += "x"
        rdir = CAND / rid
        rdir.mkdir(parents=True)
        cands, out = [], []
        t_all = time.monotonic()
        async with httpx.AsyncClient(timeout=120) as c:
            ref_names = [await upload_ref(c, r["path"]) for r in plan["refs"]]
            for i in range(1, n + 1):
                s = seed + i - 1
                t0 = time.monotonic()
                png = await run_workflow(c, build_workflow(plan["text"], ref_names, w, h, s, steps, ref_mp,
                                                           lora, lora_strength))
                _write_bytes(rdir / f"cand_{i}.png", png)
                cands.append({"number": i, "file": f"cand_{i}.png", "seed": s,
                              "seconds": round(time.monotonic() - t0, 1)})
                out += [f"Candidate {i} (seed {s})", _preview(png, preview_size)]
    finally:
        BUSY -= 1
    meta = {"request_id": rid, "created": now_iso(), "project": plan["project"], "prompt": prompt,
            "full_prompt": plan["text"], "entries": plan["entries"], "checklist": plan["checklist"],
            "width": w, "height": h, "steps": steps, "model": MODEL, "speed": speed,
            "pose_mode": pose_mode if pose else None,
            "lora": {"name": lora, "strength": lora_strength} if lora else None,
            "reference_mode": reference_mode if plan["refs"] else None,
            "references": [{"source": r["source"], **({"file": r["file"]} if r.get("file") else {})}
                           for r in plan["refs"]],
            "candidates": cands, "picked": None}
    _save(rdir / "request.json", meta)
    S["requests"][rid] = meta
    S["last_request"] = rid
    _save_session()
    head = (f"{'[' + startup + '] ' if startup else ''}Request {rid} ({_mode_text()}): {n} candidates, {w}x{h}, "
            f"{posing}, {time.monotonic() - t_all:.0f} s total.\nReferences:\n{ref_lines}{notes}{checklist}\n"
            f"Now call pick_candidate(request_id='{rid}', number=<best>). Present only the picked image; "
            "do not show or mention the other candidates unless the user asks.")
    return [head, *out]


@tool
async def pick_candidate(request_id: str, number: int, reason: str = "") -> str:
    """Choose the best candidate of a request. It is copied to this session's selected/ folder and waits for the
    user's approval (approve_image). The other candidates stay hidden until the request is approved, discarded or
    the session is cleared, then they are deleted. Can be called again to switch the pick while it is open."""
    rid, req = _open_request(request_id)
    if not (CAND / rid / f"cand_{number}.png").exists():
        raise ValueError(f"request {rid} has candidates 1-{len(req['candidates'])}")
    dest = SEL / f"{rid}.png"
    _write_bytes(dest, (CAND / rid / f"cand_{number}.png").read_bytes())
    req["picked"] = number
    req["pick_reason"] = reason
    cand = next(c for c in req["candidates"] if c["number"] == number)
    _save(SEL / f"{rid}.json", {**req, "seed": cand["seed"]})
    _save_session()
    return (f"Picked candidate {number} of {rid} (seed {cand['seed']}). Waiting for approval at: {dest}\n"
            "Show the user only this image. approve_image when they like it, discard_image if not.")


@tool
async def show_candidates(request_id: str = "", preview_size: int = 512) -> list:
    """Show ALL candidates of a request (default: latest). Use ONLY when the user explicitly asks to see them."""
    rid, req = _open_request(request_id)
    out: list = [f"Request {rid}: {len(req['candidates'])} candidates; picked: {req.get('picked') or 'none yet'}"]
    for c in req["candidates"]:
        p = CAND / rid / c["file"]
        if p.exists():
            mark = " (picked)" if c["number"] == req.get("picked") else ""
            out += [f"Candidate {c['number']}{mark} - seed {c['seed']} - {p}", _preview(p.read_bytes(), preview_size)]
    return out


@tool
async def approve_image(request_id: str = "", name: str = "", reference_for: list[str] | None = None) -> str:
    """The user likes the picked image: save it (project: <project>/outputs/<date>/; one-off: the home outputs
    folder) with a JSON file (prompt, seed, entries, references), file it as an approved example of the guide
    entries it used, and close the request. reference_for: guide entry names that should also get this image as a
    reference (e.g. a character sheet the user approved). name: optional file name. Default request: the latest."""
    rid, req = _open_request(request_id)
    sel = SEL / f"{rid}.png"
    if not req.get("picked") or not sel.exists():
        raise ValueError(f"request {rid} has no picked candidate yet; call pick_candidate first")
    seed = next(c["seed"] for c in req["candidates"] if c["number"] == req["picked"])
    stem = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in name).strip("_") or f"{rid}_{seed}"
    pj = req.get("project")
    proj = Path(pj["path"]) if pj else None
    if proj and not (proj / "project.json").is_file():
        raise ValueError(f"the project folder {proj} is gone (moved or removed); register_project it again first")
    day = (proj / "outputs" if proj else OUT) / dt.date.today().isoformat()
    extra = ""
    with (_plock(proj) if proj else _lock("scratch-outputs")):
        day.mkdir(parents=True, exist_ok=True)
        k, final = 1, day / f"{stem}.png"
        while final.exists():
            k += 1
            final = day / f"{stem}_{k}.png"
        shutil.move(str(sel), str(final))
        side = {kk: v for kk, v in req.items() if kk not in ("candidates", "project")}
        side.update(seed=seed, approved=now_iso(), project=pj["name"] if pj else None)
        _save(final.with_suffix(".json"), side)
        if proj:
            rel = final.relative_to(proj).as_posix()
            all_e = _entries(proj)
            for ent in req.get("entries") or []:
                try:
                    e = _find_entry(proj, f"{ent['kind']}/{ent['slug']}", all_e)
                except ValueError:
                    continue  # deleted/renamed meanwhile
                e.setdefault("examples", []).append(rel)
                e["updated"] = now_iso()
                _save_entry(e)
            for nm in reference_for or []:
                e = _find_entry(proj, nm, _entries(proj))
                _add_refs_locked(e, [(final.stem, final.read_bytes())], "approved image", False)
                extra += f" Added as a reference of {_eid(e)}."
            _write_guide_md(proj)
        elif reference_for:
            extra = " (reference_for ignored: one-off images have no guide)"
    _close_request(rid, delete_selected=True)
    _save_session()
    return f"Saved: {final}.{extra}"


@tool
async def discard_image(request_id: str = "") -> str:
    """Drop a request: delete its picked image and all its candidates. Default request: the latest."""
    rid, _ = _open_request(request_id)
    _close_request(rid, delete_selected=True)
    _save_session()
    return f"Discarded request {rid}."


@tool
async def list_pending() -> str:
    """List this session's open requests: picked images waiting for approval (and requests not picked yet)."""
    if not S["requests"]:
        return f"No open requests ({_mode_text()})."
    lines = [f"Open requests ({_mode_text()}):"]
    for rid, r in S["requests"].items():
        status = f"picked #{r['picked']} -> {SEL / (rid + '.png')}" if r.get("picked") else "not picked yet"
        lines.append(f"- {rid}: {status} | {r['prompt'][:80]}")
    return "\n".join(lines)


@tool
async def set_candidate_count(count: int) -> str:
    """Set how many candidates each request renders in this session (1-8; default 3). Kept until clear_session."""
    S["candidates"] = max(1, min(MAX_CANDIDATES, count))
    _save_session()
    return f"Each request now renders {S['candidates']} candidates."


@tool
async def set_speed(level: str) -> str:
    """Make images faster ONLY when the user asks for it (default 'full'). Kept until clear_session.
    'full': every character image is drawn from a Blender mannequin pose guide with the depth model (best poses).
    'fast': the user asked for faster images: the depth model is off, the mannequin is sent as a plain pose
            reference (the style reference is used again).
    'fastest': the user insisted on even faster: no mannequin either (saves the Blender render, ~15 s per pose).
    Lower one step per request; go back with set_speed('full')."""
    lv = level.strip().lower()
    if lv not in SPEEDS:
        raise ValueError(f"level must be one of {', '.join(SPEEDS)}")
    S["speed"] = lv
    _save_session()
    return f"Speed '{lv}' for this session: {SPEEDS[lv]}. Tell the image-picker the speed in its requests."


@tool
async def clear_session(stop: bool = True) -> str:
    """Start fresh: discard this session's unapproved images, detach references, reset the candidate count and the
    speed, close the project (the next request asks again which project) and, if stop, stop ComfyUI and reload the
    Ollama models (only when no other Claude session is still using them). Projects, guides and approved images are
    kept."""
    if BUSY:
        raise ValueError("an image is still being generated in this session; clear when it has finished")
    n_open = len(S["requests"])
    for rid in list(S["requests"]):
        _close_request(rid, delete_selected=True)
    prev = _mode_text()
    S.update(active=[], candidates=None, last_request=None, project=None, scratch=False, poses=[], speed=None)
    shutil.rmtree(POSES, ignore_errors=True)
    _save_session()
    msg = (f"Session cleared ({prev} closed): {n_open} unapproved image(s) discarded, references detached, "
           f"{DEFAULT_CANDIDATES} candidates per request, speed '{DEFAULT_SPEED}'. The next request will ask which "
           "project to use.")
    if stop:
        msg += " " + await asyncio.to_thread(_stop_sync, True, False)
    else:
        await asyncio.to_thread(_lease_release)
    return msg


# ---- ad-hoc references (this session only)
@tool
async def attach_reference(paths: list[str], replace: bool = False) -> list:
    """Attach ad-hoc reference image(s) for this session's next generations (until detached or the project is
    switched). For lasting references, use guide_add_reference on a guide entry instead.

    paths: image files, folders (all images inside) or http(s) image URLs. replace=True clears current ones first.
    Max 4 attached at once (each one adds ~3-5 s per image).
    """
    if not S.get("project") and not S.get("scratch"):
        raise ValueError(NO_PROJECT)
    cur = [] if replace else list(S["active"])
    added = []
    for p in paths:
        added += _import_to_library(p)
    for ref in added:
        if ref["file"] not in [r["file"] for r in cur]:
            cur.append(ref)
    if len(cur) > MAX_REFS:
        raise ValueError(f"too many references ({len(cur)}); max is {MAX_REFS}. Nothing was changed.")
    S["active"] = cur
    _save_session()
    return [f"Added {len(added)} image(s).\n" + describe_refs(cur), *[_thumb(LIB / r["file"], 192) for r in added]]


@tool
async def detach_reference(reference: str) -> str:
    """Detach one ad-hoc reference: its number from list_references (1-based), its label, or its file id."""
    refs = S["active"]
    if reference.isdigit() and 1 <= int(reference) <= len(refs):
        hit = refs[int(reference) - 1]
    else:
        hit = next((r for r in refs if reference in (r["label"], r["file"], r["file"].removesuffix(".png"))), None)
    if not hit:
        raise ValueError(f"no attached reference matches '{reference}'.\n" + describe_refs(refs))
    refs.remove(hit)
    _save_session()
    return f"Detached '{hit['label']}'.\n" + describe_refs(refs)


@tool
async def clear_references() -> str:
    """Detach all ad-hoc reference images of this session (guide references are not affected)."""
    n = len(S["active"])
    S["active"] = []
    _save_session()
    return f"Detached {n} reference(s)."


@tool
async def list_references(show_thumbnails: bool = True) -> list:
    """List this session's attached ad-hoc reference images (and show small thumbnails)."""
    refs = S["active"]
    out: list = [describe_refs(refs)]
    if show_thumbnails:
        out += [_thumb(LIB / r["file"], 192) for r in refs]
    return out


# ---- style presets for one-off images (projects use style entries in their guide)
def _scratch_only(what: str):
    if S.get("project"):
        raise ValueError(f"{what} is for one-off images. In a project, styles are guide entries: "
                         "guide_set(kind='style', ...) + guide_add_reference(..., ['style:<preset>']) copies a preset "
                         "into the project")


@tool
async def save_style(name: str, description: str = "") -> str:
    """One-off images only: save the attached references as a named style preset. Overwrites a preset with the
    same name."""
    _scratch_only("save_style")
    refs = S["active"]
    if not refs:
        raise ValueError("no references attached; attach some first")
    with _lock("styles"):
        s = _load(STYLES_FILE, {})
        s[name] = {"refs": refs, "description": description, "saved": now_iso()}
        _save(STYLES_FILE, s)
    return f"Saved style preset '{name}' with {len(refs)} reference(s)."


@tool
async def use_style(name: str, mode: str = "replace") -> list:
    """One-off images only: attach a saved style preset's references. mode='replace' (default) swaps out the
    current references, 'add' appends them (max 4 total)."""
    _scratch_only("use_style")
    if not S.get("scratch"):
        raise ValueError(NO_PROJECT)
    s = _load(STYLES_FILE, {})
    if name not in s:
        raise ValueError(f"unknown style preset '{name}'. Presets: {', '.join(s) or 'none'}")
    cur = [] if mode == "replace" else list(S["active"])
    merged = cur + [r for r in s[name]["refs"] if r["file"] not in [c["file"] for c in cur]]
    if len(merged) > MAX_REFS:
        raise ValueError(f"would attach {len(merged)} references; max is {MAX_REFS}")
    S["active"] = merged
    _save_session()
    return [f"Using style preset '{name}'. " + describe_refs(merged),
            *[_thumb(LIB / r["file"], 160) for r in merged]]


@tool
async def list_styles() -> str:
    """List saved style presets (for one-off images; a project can copy one with guide_add_reference)."""
    s = _load(STYLES_FILE, {})
    if not s:
        return "No saved style presets."
    return "\n".join(f"- {k}: {len(v['refs'])} ref(s){' - ' + v['description'] if v.get('description') else ''}"
                     for k, v in s.items())


@tool
async def delete_style(name: str) -> str:
    """Delete a saved style preset (the images stay in the library)."""
    with _lock("styles"):
        s = _load(STYLES_FILE, {})
        if name not in s:
            raise ValueError(f"unknown style preset '{name}'")
        del s[name]
        _save(STYLES_FILE, s)
    return f"Deleted style preset '{name}'."


# ---- backend
@tool
async def backend_status() -> str:
    """Show whether ComfyUI is up, GPU memory, how many Claude sessions use it, Ollama models holding the GPU and
    this session's project."""
    lines = []
    if await comfy_up():
        async with httpx.AsyncClient(timeout=10) as c:
            ss = (await c.get(f"{COMFY}/system_stats")).json()
        dev = ss["devices"][0]
        lines.append(f"ComfyUI {ss['system'].get('comfyui_version')} up at {COMFY}; "
                     f"GPU free {dev['vram_free'] / 2**30:.1f} / {dev['vram_total'] / 2**30:.1f} GB")
    else:
        lines.append("ComfyUI is not running (generate_image or start_backend will start it).")
    b = _backend_load()
    live = [v for v in b["leases"].values() if _session_live(v)]
    lines.append(f"Claude sessions using it: {len(live)}" + (" (including this one)" if SID in b["leases"] else ""))
    try:
        async with httpx.AsyncClient(timeout=5) as c:
            models = (await c.get(f"{OLLAMA}/api/ps")).json().get("models", [])
        lines.append("Ollama models in memory: " + (", ".join(m["name"] for m in models) or "none"))
    except httpx.HTTPError:
        lines.append("Ollama not reachable.")
    lines.append(f"This session ({SID}): {_mode_text()}. " + describe_refs(S["active"]))
    return "\n".join(lines)


@tool
async def start_backend(free_gpu: bool = True) -> str:
    """Start Docker Desktop (if needed) and the ComfyUI container. free_gpu unloads Ollama models from the GPU
    (they are reloaded by stop_backend)."""
    return await _backend_ready(free_gpu) or "ComfyUI already running"


@tool
async def stop_backend(restore_ollama: bool = True, force: bool = False) -> str:
    """Stop the ComfyUI container (models stay installed) and reload the Ollama models it unloaded. If another
    Claude session is still using ComfyUI, it keeps running unless force=True (ask the user first)."""
    return await asyncio.to_thread(_stop_sync, restore_ollama, force)


# ---------------------------------------------------------------- pose guides (Blender)
# A posable mannequin rendered by Blender in background mode: in the local-imagen Blender Docker image by default
# (official portable Linux build, CPU only), or with a Blender installed on this computer (setting blender_path).
def _cfg_update(updates: dict):
    with _lock("config"):
        cfg = _cfg()
        for k, v in updates.items():
            if v is None:
                cfg.pop(k, None)
            else:
                cfg[k] = v
        CFG_DIR.mkdir(parents=True, exist_ok=True)
        _save(CFG_FILE, cfg)


def _blender_path() -> str:
    """Host Blender executable when one is configured (re-read every time, so setup_blender applies at once)."""
    return (os.environ.get("LOCAL_IMAGEN_BLENDER") or str(_cfg().get("blender_path") or "")).strip()


def _blender_image() -> str:
    return str(os.environ.get("LOCAL_IMAGEN_BLENDER_IMAGE") or _cfg().get("blender_image") or BLENDER_IMAGE)


def _host_blender_version(path: str) -> str:
    r = _run([path, "-b", "--factory-startup", "--version"], timeout=120)
    m = re.search(r"Blender (\d+\.\d+(?:\.\d+)?)", r.stdout or "")
    if r.returncode != 0 or not m:
        raise RuntimeError(f"'{path}' did not run as Blender: " + ((r.stderr or r.stdout) or "no output")[-400:])
    return m.group(1)


def _image_blender_version() -> str | None:
    """Blender version inside the local-imagen Blender image, or None if the image isn't built."""
    try:
        r = _run(["docker", "image", "inspect", "--format", '{{index .Config.Labels "blender.version"}}',
                  _blender_image()], timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return (r.stdout.strip() or "unknown") if r.returncode == 0 else None


async def _ensure_docker(notes: list):
    if await asyncio.to_thread(_docker_ready):
        return
    desktop = _docker_desktop()
    if desktop:
        subprocess.Popen([str(desktop)], env=_env(), creationflags=getattr(subprocess, "DETACHED_PROCESS", 0))
    elif sys.platform == "darwin":
        subprocess.Popen(["open", "-a", "Docker"], env=_env())
    else:
        raise RuntimeError("Docker engine is not running (Linux: sudo systemctl start docker)")
    notes.append("started Docker Desktop")
    for _ in range(60):
        await asyncio.sleep(3)
        if await asyncio.to_thread(_docker_ready):
            return
    raise RuntimeError("Docker Desktop did not become ready in 3 minutes")


def _blender_sha256(version: str) -> str:
    """Official checksum of the Linux build, from download.blender.org."""
    mm = ".".join(version.split(".")[:2])
    url = f"https://download.blender.org/release/Blender{mm}/blender-{version}.sha256"
    r = httpx.get(url, timeout=60, follow_redirects=True)
    r.raise_for_status()
    for line in r.text.splitlines():
        if line.strip().endswith(f"blender-{version}-linux-x64.tar.xz"):
            return line.split()[0]
    raise ValueError(f"no Linux x64 build of Blender {version} listed in {url}")


def _build_state() -> dict:
    st = _load(BLENDER_STATE, {})
    if st.get("status") == "running" and not _proc_alive(st.get("pid"), st.get("pid_created")):
        st["status"] = "interrupted"  # the server that ran the build has gone
    return st


def _log_tail(n: int = 6) -> str:
    try:
        lines = [x for x in BLENDER_LOG.read_text(encoding="utf-8", errors="replace").splitlines() if x.strip()]
    except FileNotFoundError:
        return ""
    return "\n".join("    " + x[-160:] for x in lines[-n:])


def _blender_summary() -> str:
    hp = _blender_path()
    if hp:
        try:
            return f"host Blender {_host_blender_version(hp)} at {hp}"
        except (RuntimeError, OSError, subprocess.TimeoutExpired) as e:
            return f"host Blender at {hp} is not working ({e}); fix it or run setup_blender() for the Docker image"
    if not _docker_ready():
        return "unknown: Docker is not running (setup_blender and render_pose start it)"
    v = _image_blender_version()
    if v:
        return f"Docker image {_blender_image()} with Blender {v}"
    return ("not set up: call setup_blender() to build the Blender Docker image (about 1-2 min, ~400 MB "
            "download), or install Blender on this computer and call setup_blender(path=<blender executable>)")


def _pose_presets() -> dict:
    try:
        return {k: v for k, v in json.loads((BLENDER_DIR / "poses.json").read_text(encoding="utf-8")).items()
                if not k.startswith("_")}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


POSE_PARTS = {
    "root": {"turn", "pitch", "roll", "lift", "ground"}, "spine": {"bend", "side", "twist"},
    "head": {"nod", "tilt", "turn"},
    "left_arm": {"aim", "twist", "elbow", "wrist", "wrist_tilt", "hand"},
    "right_arm": {"aim", "twist", "elbow", "wrist", "wrist_tilt", "hand"},
    "left_leg": {"aim", "twist", "knee", "ankle"}, "right_leg": {"aim", "twist", "knee", "ankle"},
}
AIM_WORDS = {"forward", "front", "back", "backward", "up", "down", "out", "outward", "side", "in", "across", "inward"}
AIM_KEYS = {"forward", "back", "up", "down", "out", "in", "across"}
POSE_VIEWS = {"front": 0, "three-quarter": 35, "side": 90, "back-three-quarter": 145, "back": 180}
POSE_LEGEND = ("the pose to use, shown on a plain grey artist's mannequin: copy its body pose, limb angles, balance "
               "and camera angle; do not draw the mannequin, its grey colour or its plain background")


def _pose_entry_spec(name: str) -> tuple[dict, dict] | None:
    """(pose, camera) of a guide pose entry saved with render_pose(save_to=...)."""
    if not S.get("project"):
        return None
    proj, _ = current()
    try:
        e = _find_entry(proj, name if "/" in name else f"pose/{name}")
    except ValueError:
        return None
    if not e.get("pose_spec"):
        raise ValueError(f"guide entry {_eid(e)} has no saved mannequin pose (render_pose(..., save_to=...) saves "
                         "one)")
    return json.loads(json.dumps(e["pose_spec"])), dict(e.get("pose_camera") or {})


def _mirror_pose(spec: dict) -> dict:
    """Swap the figure's left and right (a left-handed version of the pose)."""
    out: dict = {}
    for k, v in spec.items():
        if k == "props":
            out[k] = [{**p, "hand": "left" if str(p.get("hand", "right")).lower() == "right" else "right"}
                      for p in v]
            continue
        nk = ("right_" + k[5:]) if k.startswith("left_") else ("left_" + k[6:]) if k.startswith("right_") else k
        v = dict(v)
        if k in ("root", "spine", "head"):
            for key in ("turn", "roll", "twist", "side", "tilt"):
                if key in v:
                    v[key] = -float(v[key])
        out[nk] = v  # limb values are side-relative already
    return out


def _resolve_pose(pose) -> tuple[dict, dict]:
    """Preset name / guide pose entry / dict (optionally {"preset": ...} plus overrides) -> (full pose dict,
    the preset's suggested camera)."""
    presets = _pose_presets()
    if isinstance(pose, str):
        pose = pose.strip()
        if pose.startswith("{"):
            pose = json.loads(pose)
        else:
            pose = {"preset": pose or "stand"}
    if not isinstance(pose, dict):
        raise ValueError("pose must be a preset name or an object")
    pose = json.loads(json.dumps(pose))
    base: dict = {}
    hint: dict = {}
    pre = pose.pop("preset", None)
    if pre:
        if pre in presets:
            base = {k: v for k, v in presets[pre].items() if not k.startswith("_")}
            hint = dict(presets[pre].get("_camera") or {})
        else:
            found = _pose_entry_spec(pre)
            if found is None:
                raise ValueError(f"unknown pose preset '{pre}'. Presets: {', '.join(presets)}"
                                 + ("" if S.get("project") else " (guide poses need a project)"))
            base, hint = found
    for k, v in pose.items():
        if k == "props":
            if not isinstance(v, list):
                raise ValueError("props must be a list, e.g. [{'type': 'sword', 'hand': 'right'}]")
            for p in v:
                if not isinstance(p, dict) or p.get("type") != "sword":
                    raise ValueError("the only prop so far is {'type': 'sword', 'hand': 'right'|'left', "
                                     "'grip': 'normal'|'reverse', 'length': <blade length in heads, default 3.2>, "
                                     "'two_handed': true|false}")
            base["props"] = v
        elif k in POSE_PARTS:
            if not isinstance(v, dict):
                raise ValueError(f"pose.{k} must be an object with keys {sorted(POSE_PARTS[k])}")
            bad = set(v) - POSE_PARTS[k]
            if bad:
                raise ValueError(f"unknown key(s) {sorted(bad)} in pose.{k}; valid: {sorted(POSE_PARTS[k])}")
            aim = v.get("aim")
            if isinstance(aim, str):
                words = [w for w in re.split(r"[-+ _]", aim.lower()) if w]
                if not words or set(words) - AIM_WORDS:
                    raise ValueError(f"pose.{k}.aim '{aim}': use words from {', '.join(sorted(AIM_WORDS))} joined "
                                     "by '-', or an object like {'forward': 1, 'up': 0.3, 'out': 0.2}")
            elif aim is not None and not (isinstance(aim, dict) and set(aim) <= AIM_KEYS):
                raise ValueError(f"pose.{k}.aim must be an object with keys from {sorted(AIM_KEYS)} (e.g. "
                                 "{'forward': 1, 'up': 0.3}) or a string like 'forward-up'")
            if "hand" in v and v["hand"] not in ("relaxed", "open", "fist"):
                raise ValueError(f"pose.{k}.hand must be relaxed, open or fist")
            base[k] = {**(base.get(k) or {}), **v}
        elif not k.startswith("_"):
            raise ValueError(f"unknown pose key '{k}'; valid: preset, props, {', '.join(POSE_PARTS)}")
    return base, hint


def _render_jobs(jobs: list[dict], out_dir: Path, timeout: int = 600) -> str:
    """Run pose_render.py once for several jobs; returns Blender's output. Docker image unless blender_path."""
    out_dir.mkdir(parents=True, exist_ok=True)
    hp = _blender_path()
    if hp:
        for j in jobs:
            j["out"] = str(out_dir)
        jf = out_dir / "_jobs.json"
        _save(jf, {"jobs": jobs})
        cmd = [hp, "-b", "--factory-startup", "--python", str(BLENDER_DIR / "pose_render.py"), "--", str(jf)]
    else:
        for j in jobs:
            j["out"] = "/out"
        _save(out_dir / "_jobs.json", {"jobs": jobs})
        cmd = ["docker", "run", "--rm", "--network", "none", "-e", "HOME=/tmp",
               "--mount", f"type=bind,src={BLENDER_DIR},dst=/work,readonly",
               "--mount", f"type=bind,src={out_dir},dst=/out"]
        if sys.platform.startswith("linux") and hasattr(os, "getuid"):
            cmd += ["--user", f"{os.getuid()}:{os.getgid()}"]  # files in the session folder stay the user's
        cmd += [_blender_image(), "--python", "/work/pose_render.py", "--", "/out/_jobs.json"]
    r = _run(cmd, timeout=timeout)
    out = (r.stdout or "") + (r.stderr or "")
    if "POSE_ALL_DONE" not in out:
        fails = [x for x in out.splitlines() if x.startswith("POSE_FAILED")]
        raise RuntimeError("Blender pose render failed: " + ("; ".join(fails) or out[-900:]))
    return out


def _flatten_white(path: Path):
    im = PILImage.open(path).convert("RGBA")
    bg = PILImage.new("RGBA", im.size, (255, 255, 255, 255))
    bg.alpha_composite(im)
    bg.convert("RGB").save(path, "PNG")


def _head_chest_crop(path: Path, about: str = "") -> Path | None:
    """Head-and-chest crop of a full-length figure reference (used with a pose guide: a full-body reference makes
    the model copy its pose). None when it isn't a full-length figure on a plain background, or its label/role
    says it is a face, portrait or close-up."""
    if re.search(r"face|portrait|close|head|bust|expression", about, re.I):
        return None
    from PIL import ImageChops
    im = PILImage.open(path).convert("RGB")
    w, h = im.size
    small = im.copy()
    small.thumbnail((320, 320))
    sw, sh = small.size
    px = small.load()
    corners = [px[0, 0], px[sw - 1, 0], px[0, sh - 1], px[sw - 1, sh - 1]]
    bg = tuple(sorted(c[i] for c in corners)[1] for i in range(3))
    mask = ImageChops.difference(small, PILImage.new("RGB", small.size, bg)).convert("L").point(
        lambda v: 255 if v > 28 else 0)
    box = mask.getbbox()
    if not box:
        return None
    x0, y0, x1, y1 = box
    if (x1 - x0) > 0.97 * sw and (y1 - y0) > 0.97 * sh:
        return None  # painted background: can't find the figure
    bh, bw = y1 - y0, x1 - x0
    if bh < 0.7 * sh or bh < 1.1 * bw:
        return None  # not a full-length figure filling the picture: use it as it is
    band = mask.crop((x0, y0, x1, y0 + max(2, int(bh * 0.12))))
    cols = [sum(1 for yy in range(band.height) if band.getpixel((xx, yy))) for xx in range(band.width)]
    cx = x0 + (sum(i * c for i, c in enumerate(cols)) / sum(cols) if sum(cols) else bw / 2)
    ch = bh * 0.36
    cw = min(bw, ch * 1.05)
    pad = bh * 0.04
    left = max(x0 - pad, min(cx - cw / 2, x1 - cw) - pad)
    k = w / sw
    crop = im.crop((int(max(0, left) * k), int(max(0, y0 - pad) * k), int(min(sw, left + cw + 2 * pad) * k),
                    int(min(sh, y0 + ch + pad) * k)))
    buf = io.BytesIO()
    crop.save(buf, "PNG")
    png, fid = _normalize(buf.getvalue())
    if not (LIB / fid).exists():
        _write_bytes(LIB / fid, png)
    return LIB / fid


@tool
async def blender_status() -> str:
    """Show whether the Blender pose renderer (render_pose) is ready: the Docker image or a host Blender, its
    version, and the progress or result of the last setup_blender build."""
    lines = ["Pose renderer: " + await asyncio.to_thread(_blender_summary)]
    st = _build_state()
    if st:
        lines.append(f"Last setup_blender build ({st.get('started')}): {st.get('status')}"
                     + (f", Blender {st['version']}" if st.get("version") else ""))
        if st.get("status") in ("running", "failed", "interrupted"):
            tail = _log_tail()
            if tail:
                lines.append("  log (" + str(BLENDER_LOG) + "):\n" + tail)
    lines.append("Presets: " + ", ".join(_pose_presets()))
    return "\n".join(lines)


@tool
async def setup_blender(path: str = "", version: str = "", sha256: str = "", wait_seconds: int = 540) -> str:
    """Set up the Blender pose renderer used by render_pose.

    Default (no path): builds the local-imagen Blender Docker image. It downloads the official portable Linux
    build from download.blender.org and checks its SHA-256, so nothing is installed on this computer. About
    1-2 min and ~400 MB of download; renders run on the CPU, so they never take GPU memory from FLUX.
      version: another Blender release, e.g. '5.3.0' (its checksum is fetched from download.blender.org unless
               sha256 is given). Default: the version pinned in docker/Dockerfile.blender.
      wait_seconds: how long to wait for the build; if it takes longer it keeps going: call blender_status.
    path: use a Blender installed on this computer instead (e.g. for an external ComfyUI without Docker):
          the blender executable. It is checked, then saved as blender_path in ~/.local-imagen/config.json.
          Run setup_blender() again later to go back to the Docker image.
    """
    if path.strip():
        p = str(Path(path.strip().strip('"')).expanduser())
        v = await asyncio.to_thread(_host_blender_version, p)
        await asyncio.to_thread(_cfg_update, {"blender_path": p})
        return f"Using host Blender {v} at {p} for render_pose (saved as blender_path)."
    if os.environ.get("LOCAL_IMAGEN_BLENDER"):
        raise ValueError("the LOCAL_IMAGEN_BLENDER environment variable points render_pose at a host Blender; "
                         "remove it to use the Docker image")
    global BLENDER_BUILD
    if BLENDER_BUILD and BLENDER_BUILD.poll() is None:
        raise ValueError("a Blender image build is already running in this session; call blender_status")
    st = _build_state()
    if st.get("status") == "running":
        raise ValueError(f"another Claude session is already building the Blender image (since {st['started']}); "
                         "call blender_status")
    if version and not re.fullmatch(r"\d+\.\d+\.\d+", version.strip()):
        raise ValueError("version must look like 5.2.5")
    notes: list = []
    await _ensure_docker(notes)
    args = ["docker", "build", "-t", _blender_image()]
    version = version.strip()
    if version:
        sha = sha256.strip() or await asyncio.to_thread(_blender_sha256, version)
        if not re.fullmatch(r"[0-9a-fA-F]{64}", sha):
            raise ValueError("sha256 must be 64 hex characters")
        args += ["--build-arg", f"BLENDER_VERSION={version}", "--build-arg", f"BLENDER_SHA256={sha}"]
    args.append("-")  # the Dockerfile comes from stdin: no build context is sent
    BLENDER_LOG.parent.mkdir(parents=True, exist_ok=True)
    log_fh = open(BLENDER_LOG, "w", encoding="utf-8")
    t0 = time.monotonic()
    BLENDER_BUILD = subprocess.Popen(args, stdin=open(PLUGIN_DIR / "docker" / "Dockerfile.blender", "rb"),
                                     stdout=log_fh, stderr=subprocess.STDOUT, env=_env(),
                                     creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    _save(BLENDER_STATE, {"status": "running", "started": now_iso(), "pid": ME_PID, "pid_created": ME_CREATED,
                          "version": version or None, "image": _blender_image()})
    while BLENDER_BUILD.poll() is None and time.monotonic() - t0 < max(5, wait_seconds):
        await asyncio.sleep(2)
    code = BLENDER_BUILD.poll()
    head = ("[" + "; ".join(notes) + "] ") if notes else ""
    if code is None:
        task = asyncio.get_running_loop().create_task(_finish_build(log_fh, version))
        _BG_TASKS.add(task)
        task.add_done_callback(_BG_TASKS.discard)
        return head + (f"Still building after {wait_seconds} s (big download?). It keeps going; call "
                       "blender_status to follow it.")
    return head + await _finish_build(log_fh, version, time.monotonic() - t0)


async def _finish_build(log_fh, version: str, seconds: float | None = None) -> str:
    while BLENDER_BUILD.poll() is None:
        await asyncio.sleep(3)
    log_fh.close()
    ok = BLENDER_BUILD.returncode == 0
    v = await asyncio.to_thread(_image_blender_version) if ok else None
    _save(BLENDER_STATE, {**_load(BLENDER_STATE, {}), "status": "done" if ok else "failed", "version": v or version,
                          "finished": now_iso()})
    if not ok:
        return f"Blender image build FAILED (exit {BLENDER_BUILD.returncode}):\n{_log_tail(12)}"
    if _cfg().get("blender_path"):
        await asyncio.to_thread(_cfg_update, {"blender_path": None})
    took = f" in {seconds:.0f} s" if seconds is not None else ""
    return (f"Built {_blender_image()} with Blender {v}{took}. render_pose is ready "
            f"(presets: {', '.join(_pose_presets())}).")


async def render_pose(pose: dict | str = "stand", view: str = "", facing: str = "",
                      azimuth: float | None = None, elevation: float | None = None, distance: float | None = None,
                      mirror: bool = False, ortho: bool = False, aspect: str = "portrait", width: int = 0,
                      height: int = 0,
                      heads: float = 7, build: float = 1.0, shoulders: float = 1.0, hips: float = 1.0,
                      depth: bool | None = None, color_sides: bool = False, name: str = "", save_to: str = "",
                      preview_size: int = 640) -> list:
    if depth is None:
        depth = _speed() == "full"
    proj_needed = bool(save_to.strip())
    if proj_needed and not S.get("project"):
        raise ValueError(NO_PROJECT)
    spec, hint = _resolve_pose(pose)
    if facing not in ("", "left", "right"):
        raise ValueError("facing must be 'left' or 'right' (the side of the picture the figure faces)")
    if view and view not in POSE_VIEWS:
        raise ValueError(f"unknown view '{view}'; use {', '.join(POSE_VIEWS)} or azimuth=<degrees>")
    if azimuth is None:
        if view or "azimuth" not in hint:  # a named view, or the default three-quarter
            azimuth = POSE_VIEWS[view or "three-quarter"] * (1 if facing == "left" else -1)
        else:  # the preset's own camera; facing only picks the side it is seen from
            azimuth = float(hint["azimuth"])
            if facing and 0 < abs(azimuth) < 180:
                azimuth = abs(azimuth) * (1 if facing == "left" else -1)
    if mirror:
        spec, azimuth = _mirror_pose(spec), -azimuth
    elevation = float(hint.get("elevation", 5)) if elevation is None else elevation
    distance = float(hint.get("distance", 4.0)) if distance is None else distance
    if width and height:
        w, h = width, height
    elif aspect in ASPECTS:
        w, h = ASPECTS[aspect]
    else:
        raise ValueError(f"unknown aspect '{aspect}'; use one of {list(ASPECTS)} or width+height")
    w, h = (max(256, min(2048, v)) // 16 * 16 for v in (w, h))
    stem = slugify(name) if name.strip() else f"pose-{dt.datetime.now():%H%M%S}"
    while (POSES / f"{stem}.png").exists():
        stem += "x"
    job = {"name": stem, "width": w, "height": h, "samples": 24, "color_sides": bool(color_sides),
           "outputs": ["mannequin", "depth"] if depth else ["mannequin"],
           "figure": {"heads": heads, "build": build, "shoulders": shoulders, "hips": hips}, "pose": spec,
           "camera": {"azimuth": azimuth, "elevation": elevation, "distance": distance, "ortho": bool(ortho),
                      "margin": 0.06}}
    notes: list = []
    if not _blender_path():
        await _ensure_docker(notes)
        if not await asyncio.to_thread(_image_blender_version):
            raise RuntimeError("the Blender pose renderer isn't set up yet: call setup_blender() (or "
                               "setup_blender(path=...) for a Blender installed on this computer)")
    t0 = time.monotonic()
    await asyncio.to_thread(_render_jobs, [job], POSES)
    png = POSES / f"{stem}.png"
    await asyncio.to_thread(_flatten_white, png)
    info = _load(POSES / f"{stem}.json", {})
    info.update(spec=spec, figure=job["figure"], camera_settings=job["camera"])
    _save(POSES / f"{stem}.json", info)
    if info.get("warnings"):
        notes.append("WARNING: " + "; ".join(info["warnings"]))
    used = (info.get("camera") or {}).get("elevation_used")
    if used is not None and abs(used - elevation) > 0.5:
        notes.append(f"elevation raised to {used:g} so the camera stays above the floor (for a stronger low "
                     "angle, lower distance)")
    S["poses"] = [*S.get("poses", [])[-50:], str(png)]
    _save_session()
    files = [f"mannequin: {png}"] + ([f"depth: {POSES / (stem + '-depth.png')}"] if depth else [])
    speed = _speed()
    nxt = {"full": "generate_image then uses the depth model (leave pose_mode unset).",
           "fast": "generate_image sends it as a plain pose reference (speed 'fast', the user asked for it).",
           "fastest": "speed is 'fastest' (the user asked to skip mannequins): use it only if the request asks."}
    msg = [f"{'[' + '; '.join(notes) + '] ' if notes else ''}Pose guide '{stem}' ({w}x{h}, azimuth "
           f"{azimuth:g}, elevation {elevation:g}, {time.monotonic() - t0:.0f} s).", *files,
           "Look at it: check limbs, sides (color_sides=True tints the left side blue, the right warm), "
           "the sword angle and balance; adjust and render again if needed. Then pass the mannequin path as "
           f"generate_image(pose=...); {nxt[speed]}"]
    if save_to.strip():
        proj, _ = current()
        with _plock(proj):
            try:
                e = _find_entry(proj, save_to if "/" in save_to else f"pose/{save_to}")
            except ValueError as err:
                if "ambiguous" in str(err):
                    raise
                e = None
            if e is None:
                s_ = slugify(save_to)
                e = {"name": save_to.strip(), "slug": s_, "kind": "pose", "description": "", "concept": "",
                     "must_have": [], "never": [], "tags": [], "notes": "", "refs": [], "examples": [],
                     "created": now_iso(), "_dir": proj / "guide" / "pose" / s_}
            e["pose_spec"] = spec
            e["pose_camera"] = {"azimuth": azimuth, "elevation": elevation, "distance": distance,
                                "ortho": bool(ortho), "heads": heads}
            _add_refs_locked(e, [(f"mannequin {stem}", png.read_bytes())], "mannequin pose guide", True)
            _write_guide_md(proj)
        msg.append(f"Saved as guide entry {_eid(e)} (its mannequin is the primary reference; "
                   f"render_pose(pose='{e['name']}') re-renders it from any angle).")
    return ["\n".join(msg), _preview(png.read_bytes(), preview_size)]


render_pose.__doc__ = f"""Render a pose guide: a plain grey artist's mannequin in the given pose, on white, with
    Blender. Use it for EVERY image with a character (required unless the session speed is 'fastest'), not only
    action poses: render the pose, look at it, fix it, then pass the mannequin path to generate_image(pose=...).
    Renders take ~5-15 s.

    pose: a preset name, or an object: {{"preset": "<name>", ...overrides}} or a full pose. Presets:
          {"; ".join(f"{k} ({v.get('_about', '')})" for k, v in _pose_presets().items())}.
          With a project open, a guide pose entry saved with save_to also works as a preset.
          Keys (angles in degrees; sides are the figure's own left/right; arm and leg directions are relative
          to the chest / hips):
            root: turn (+ toward the figure's left), pitch (+ lean/flip forward), roll (+ lean to its left),
                  lift (height above the ground, in heads; the lowest point is put on the floor otherwise)
            spine: bend (+ forward, - arch back), side (+ to its left), twist (+ chest turns to its left)
            head: nod (+ down), tilt (+ toward its left shoulder), turn (+ to its left)
            left_arm / right_arm: aim (upper arm direction: {{"forward": f, "up": u, "out": o}}, negatives =
                  back / down / across the body; or words like "forward-up", "down-out"), elbow (bend),
                  twist (+ turns the arm outward: changes which way the elbow bends), wrist (+ toward the palm),
                  wrist_tilt (+ toward the thumb; tilts a held sword), hand (relaxed / open / fist)
            left_leg / right_leg: aim (thigh direction, same form), knee (bend), ankle (+ toes down),
                  twist (+ knee and toes turn outward)
            props: [{{"type": "sword", "hand": "right", "grip": "normal" | "reverse", "length": 3.2,
                     "two_handed": false}}] (two_handed: the other arm is placed automatically so its fist
                     holds the grip too; the other arm's own settings are then ignored)
    Camera: by default a preset's own camera (the action presets come with a dramatic one), else three-quarter.
    view: front, three-quarter, side, back-three-quarter, back. facing: right / left, the side of the picture the
          figure faces (with a preset camera it only picks the side it is seen from). azimuth: exact camera
          angle instead (0 = front, + = from the figure's left). elevation: camera height angle (+ = from above,
          - = low angle looking up; the camera stays above the floor, so a stronger low angle needs a lower
          distance). distance: camera distance in figure heights (default 4; 2.5-3 = strong perspective).
    Tips: show the main limb (sword arm, kicking leg) broadside to the camera: a limb pointing at the camera is
          foreshortened and reads poorly. Low angles (elevation -10 to -20, distance 2.5-3) make action heroic.
    mirror: swap the figure's left and right (and the camera side): a left-handed version of the pose. Keep it
            off for a right-handed character; move the camera with facing instead.
    ortho: no perspective.
    aspect / width / height: use the same as the image you will generate.
    heads: figure height in heads (from the character's guide entry if it says; default 7). build, shoulders,
           hips: width multipliers (1 = average).
    depth: also write a depth map (near = white). Default: on at speed 'full' (generate_image then uses the depth
           model), off at 'fast' / 'fastest'. color_sides: tint the left side blue and the right side warm,
           only to check left/right yourself (don't pass that render to generate_image).
    save_to: main conversation only, when the user asks to keep a pose: saves it as a guide pose entry (mannequin
             as its primary reference plus the pose itself, re-renderable from any view).
    Works without a project. The files live in this session's folder until the session ends.
    """
render_pose = tool(render_pose)

# ---------------------------------------------------------------- edits from red marks
# The user marks what to change in red (e.g. with the pen of the Claude app's image editor) and attaches the
# image. Marks are found, matched to the original image they were drawn on (also inside a screenshot of a page,
# e.g. a guide PDF), and only the marked areas are repainted: each one in a crop around it at ~1 MP for detail,
# then pasted back with a soft edge, so everything outside the marks stays exactly as it was.
UPLOADS = Path.home() / ".claude" / "uploads"   # where Claude Code keeps images attached in chat
EDIT_LEGEND = ("the picture being edited (a crop around the marked area): keep everything that is not asked to "
               "change exactly as it is")


def _latest_upload() -> Path:
    files = [f for f in UPLOADS.glob("*/*") if f.suffix.lower() in IMG_EXT] if UPLOADS.is_dir() else []
    if not files:
        raise ValueError("no attached image found in ~/.claude/uploads; pass the marked image's file path")
    return max(files, key=lambda f: f.stat().st_mtime)


def _image_path(image: str) -> Path:
    s = image.strip().strip('"')
    if s.lower() in ("", "latest", "attached", "last"):
        return _latest_upload()
    if s in S["requests"] and (SEL / f"{s}.png").exists():
        return SEL / f"{s}.png"
    p = Path(s).expanduser()
    if not p.is_file():
        raise ValueError(f"image not found: {image}")
    return p


def _strokes(im, color: str):
    """Pen strokes of one colour: 'red' = what to fix, 'green' = where/which way it should go."""
    import numpy as np
    a = np.asarray(im.convert("RGB")).astype(np.int16)
    r, g, b = a[..., 0], a[..., 1], a[..., 2]
    if color == "red":
        m = (r > 140) & (r - np.maximum(g, b) > 70)
    else:
        m = (g > 110) & (g - np.maximum(r, b) > 50)
    return m.astype(np.uint8)


def _red_regions(im):
    """Numbered red marks (reading order) with a filled mask each: a loop marks what it encloses (its convex
    hull), a scribble marks what it covers. A loop broken into pieces (drawn past the picture's edge, or with a
    gap) counts as one mark."""
    import cv2
    import numpy as np
    red = _strokes(im, "red")
    H, W = red.shape
    k = max(3, int(round(max(W, H) * 0.006)))
    joined = cv2.dilate(red, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k)))
    n, lab, stats, _ = cv2.connectedComponentsWithStats(joined, connectivity=8)
    pieces = []
    for i in range(1, n):
        x, y, w, h, area = (int(v) for v in stats[i])
        if area < W * H * 0.0003 or max(w, h) < max(W, H) * 0.02:
            continue  # specks, JPEG noise
        ys, xs = np.nonzero(lab == i)
        pts = np.column_stack([xs, ys]).astype(np.int32)
        edges = {}  # where the stroke leaves the picture: side -> (min, max) along that side
        for side, sel, coord in (("left", xs <= 1, ys), ("right", xs >= W - 2, ys), ("top", ys <= 1, xs),
                                 ("bottom", ys >= H - 2, xs)):
            if sel.any():
                edges[side] = (int(coord[sel].min()), int(coord[sel].max()))
        pieces.append({"pts": pts, "edges": edges})

    def hull_mask(pts):
        m = np.zeros((H, W), np.uint8)
        cv2.fillConvexPoly(m, cv2.convexHull(pts), 255)
        return m

    def same_loop(p, q) -> bool:
        for side, (a0, a1) in p["edges"].items():
            if side in q["edges"]:
                b0, b1 = q["edges"][side]
                if max(a0, b0) - min(a1, b1) < 0.25 * (H if side in ("left", "right") else W):
                    return True  # both leave the same edge close together: the loop runs outside
        mp, mq = hull_mask(p["pts"]) > 0, hull_mask(q["pts"]) > 0
        return int((mp & mq).sum()) > 0.3 * min(int(mp.sum()), int(mq.sum()))

    merged = True
    while merged:
        merged = False
        for i in range(len(pieces)):
            for j in range(i + 1, len(pieces)):
                if same_loop(pieces[i], pieces[j]):
                    p, q = pieces[i], pieces[j]
                    edges = dict(p["edges"])
                    for side, (b0, b1) in q["edges"].items():
                        a0, a1 = edges.get(side, (b0, b1))
                        edges[side] = (min(a0, b0), max(a1, b1))
                    pieces[i] = {"pts": np.vstack([p["pts"], q["pts"]]), "edges": edges}
                    del pieces[j]
                    merged = True
                    break
            if merged:
                break
    regions = []
    for p in pieces:
        mask = cv2.dilate(hull_mask(p["pts"]), cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * k + 1, 2 * k + 1)))
        xs, ys = p["pts"][:, 0], p["pts"][:, 1]
        regions.append({"mask": mask, "bbox": [int(xs.min()), int(ys.min()), int(xs.max() - xs.min() + 1),
                                               int(ys.max() - ys.min() + 1)],
                        "center": (float(xs.mean()), float(ys.mean()))})
    band = H * 0.08
    regions.sort(key=lambda rg: (int(rg["center"][1] // band), rg["center"][0]))
    for i, rg in enumerate(regions, 1):
        rg["number"] = i
    return regions, red


def _green_marks(im) -> list[dict]:
    """Green marks: a line (or arrow) = the new position and direction for what its red mark holds; a loop = the
    new place only. Lettered A, B, ..."""
    import cv2
    import numpy as np
    gm = _strokes(im, "green")
    H, W = gm.shape
    k = max(3, int(round(max(W, H) * 0.006)))
    joined = cv2.dilate(gm, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k)))
    n, lab, stats, cents = cv2.connectedComponentsWithStats(joined, connectivity=8)
    greens = []
    for i in range(1, n):
        x, y, w, h, area = (int(v) for v in stats[i])
        if area < W * H * 0.0002 or max(w, h) < max(W, H) * 0.02:
            continue
        ys, xs = np.nonzero((lab == i) & (gm > 0))
        pts = np.column_stack([xs, ys]).astype(np.float64)
        mean = pts.mean(axis=0)
        evals, evecs = np.linalg.eigh(np.cov((pts - mean).T))
        axis = evecs[:, int(np.argmax(evals))]
        elong = math.sqrt(max(evals) / max(1e-6, min(evals)))
        proj = (pts - mean) @ axis
        g = {"bbox": [x, y, w, h], "center": (float(mean[0]), float(mean[1])),
             "shape": "line" if elong > 3.0 else "loop",
             "ends": [(mean + axis * proj.min()).tolist(), (mean + axis * proj.max()).tolist()],
             "pts": pts[:: max(1, len(pts) // 400)]}
        greens.append(g)
    greens.sort(key=lambda g: (g["center"][1], g["center"][0]))
    for i, g in enumerate(greens):
        g["letter"] = chr(ord("A") + i)
    return greens


def _edit_candidates() -> list[Path]:
    """Images a mark may have been drawn on: this session's picks, then the open project's outputs, documents
    (e.g. guide PDFs' images) and guide references (newest first), or the one-off outputs."""
    found = list(SEL.glob("*.png")) if SEL.is_dir() else []
    if S.get("project"):
        proj, _ = current()
        for pattern in ("outputs/*/*.png", "docs/**/*.png", "guide/*/*/refs/*.png"):
            found += sorted(proj.glob(pattern), key=lambda f: f.stat().st_mtime, reverse=True)[:300]
    elif OUT.is_dir():
        found += sorted(OUT.glob("*/*.png"), key=lambda f: f.stat().st_mtime, reverse=True)[:300]
    seen, out = set(), []
    for f in found:
        k = _norm(f)
        if k not in seen and "previews" not in f.parts and "build" not in f.parts:
            seen.add(k)
            out.append(f)
    return out


def _source_rank(p: Path) -> int:
    parts = [x.lower() for x in p.parts]
    return 0 if "selected" in parts else 1 if "outputs" in parts else 2 if "refs" in parts else 3


def _locate_sources(marked, red, cands: list[Path]) -> list[dict]:
    """Where each candidate original appears in the marked image (SIFT + RANSAC similarity transform), so a mark
    can be mapped back onto the original's own pixels, even from a scaled or cropped screenshot."""
    import cv2
    import numpy as np
    g2 = cv2.cvtColor(np.asarray(marked.convert("RGB")), cv2.COLOR_RGB2GRAY)
    pen = np.maximum(red, _strokes(marked, "green"))
    keep = 255 - cv2.dilate(pen * 255, np.ones((9, 9), np.uint8))   # no features on the pen strokes
    kp2, d2 = cv2.SIFT_create(nfeatures=8000).detectAndCompute(g2, keep)
    if d2 is None or len(kp2) < 20:
        return []
    sift = cv2.SIFT_create(nfeatures=1500)
    bf = cv2.BFMatcher(cv2.NORM_L2)
    thr = max(3.0, 0.006 * max(marked.size))
    found = []
    for p in cands:
        try:
            im = PILImage.open(p).convert("RGB")
        except OSError:
            continue
        sc = min(1.0, 720 / max(im.size))
        small = im.resize((max(1, int(im.width * sc)), max(1, int(im.height * sc))), PILImage.LANCZOS)
        kp1, d1 = sift.detectAndCompute(cv2.cvtColor(np.asarray(small), cv2.COLOR_RGB2GRAY), None)
        if d1 is None or len(kp1) < 12:
            continue
        good = [m for m, n2 in (x for x in bf.knnMatch(d1, d2, k=2) if len(x) == 2) if m.distance < 0.75 * n2.distance]
        if len(good) < 12:
            continue
        src = np.float32([kp1[m.queryIdx].pt for m in good]) / sc
        dst = np.float32([kp2[m.trainIdx].pt for m in good])
        A, inl = cv2.estimateAffinePartial2D(src, dst, method=cv2.RANSAC, ransacReprojThreshold=thr)
        if A is None or inl is None:
            continue
        n_in, scale = int(inl.sum()), float(np.hypot(A[0, 0], A[1, 0]))
        if n_in < 15 or not 0.03 < scale < 30:
            continue
        w, h = im.size
        corners = A @ np.array([[0, 0, 1], [w, 0, 1], [w, h, 1], [0, h, 1]], np.float64).T
        found.append({"path": p, "A": A, "inliers": n_in, "scale": scale, "size": (w, h),
                      "box": [float(corners[0].min()), float(corners[1].min()), float(corners[0].max()),
                              float(corners[1].max())]})
    return found


def _mark_diff(marked, pen, region_mask, src: dict) -> float:
    """How different a candidate original is from the marked picture in and around one mark (pen strokes left
    out): mean absolute grey difference after mapping the candidate onto the marked picture."""
    import cv2
    import numpy as np
    W, H = marked.size
    cand = np.asarray(PILImage.open(src["path"]).convert("L"))
    warped = cv2.warpAffine(cand, src["A"], (W, H), flags=cv2.INTER_AREA, borderValue=0)
    inside = cv2.warpAffine(np.full(cand.shape, 255, np.uint8), src["A"], (W, H), flags=cv2.INTER_NEAREST)
    area = cv2.dilate(region_mask, np.ones((25, 25), np.uint8)) > 0
    sel = area & (cv2.dilate(pen, np.ones((7, 7), np.uint8)) == 0) & (inside > 0)
    if not sel.any():
        return 0.0
    g = cv2.GaussianBlur(np.asarray(marked.convert("L")), (3, 3), 0).astype(np.float32)
    w = cv2.GaussianBlur(warped, (3, 3), 0).astype(np.float32)
    return float(np.abs(g[sel] - w[sel]).mean())


def _analyze_marks(image: str) -> dict:
    import numpy as np
    marked_p = _image_path(image)
    marked = PILImage.open(marked_p).convert("RGB")
    regions, red = _red_regions(marked)
    if not regions:
        raise ValueError(f"no red marks found on {marked_p.name}. Mark the areas to change in bright red "
                         "(a loop around each one, or a scribble over it)")
    sources = _locate_sources(marked, red, _edit_candidates())
    pen = np.maximum(red, _strokes(marked, "green"))
    for rg in regions:
        cx, cy = rg["center"]
        inside = [s for s in sources if s["box"][0] <= cx <= s["box"][2] and s["box"][1] <= cy <= s["box"][3]]
        if inside:
            best = max(s["inliers"] for s in inside)
            close = [s for s in inside if s["inliers"] >= 0.85 * best]
            if len(close) > 1:  # versions of one picture (an original and its edits, copies): the one whose
                for s in close:  # pixels in and around the mark match what the user drew on wins
                    s["_diff"] = _mark_diff(marked, pen, rg["mask"], s)
                lo = min(s["_diff"] for s in close)
                close = [s for s in close if s["_diff"] <= lo + 2.0]
            rg["source"] = min(close, key=lambda s: (_source_rank(s["path"]), -s["inliers"]))
        else:
            rg["source"] = None
        rg["green"] = None
    greens = _green_marks(marked)
    if greens:
        import cv2
        import numpy as np
        for g in greens:  # each green mark belongs to the nearest red mark
            best = None
            for rg in regions:
                dist = cv2.distanceTransform(255 - rg["mask"], cv2.DIST_L2, 3)
                pts = g["pts"].astype(int)
                dmin = float(dist[np.clip(pts[:, 1], 0, dist.shape[0] - 1), np.clip(pts[:, 0], 0, dist.shape[1] - 1)].min())
                if best is None or dmin < best[0]:
                    best = (dmin, rg)
            g["red"] = best[1]["number"]
            if best[1]["green"] is None or best[0] < best[1]["green"]["_d"]:
                g["_d"] = best[0]
                best[1]["green"] = g
    return {"marked_path": marked_p, "marked": marked, "regions": regions, "greens": greens}


def _mark_preview(an: dict, size: int):
    from PIL import ImageDraw
    im = an["marked"].copy()
    d = ImageDraw.Draw(im)
    fs = max(14, int(max(im.size) * 0.03))
    for rg in an["regions"]:
        x, y, w, h = rg["bbox"]
        d.rectangle([x, y, x + w, y + h], outline=(30, 90, 220), width=max(2, fs // 8))
        d.rectangle([x, y - fs - 4, x + fs * 1.4, y], fill=(30, 90, 220))
        d.text((x + 4, y - fs - 2), str(rg["number"]), fill=(255, 255, 255), font_size=fs)
    for g in an.get("greens") or []:
        (ax, ay), (bx, by) = g["ends"]
        if g["shape"] == "line":
            d.line([ax, ay, bx, by], fill=(250, 170, 0), width=max(2, fs // 6))
        x, y, w, h = g["bbox"]
        d.rectangle([x + w, y + h, x + w + fs * 1.4, y + h + fs + 4], fill=(20, 140, 60))
        d.text((x + w + 4, y + h + 2), g["letter"], fill=(255, 255, 255), font_size=fs)
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return _preview(buf.getvalue(), size)


def _source_mask(rg: dict, src: dict, w: int, h: int):
    """The mark's mask in the original image's own pixels."""
    import cv2
    if src is None:
        return rg["mask"]
    inv = cv2.invertAffineTransform(src["A"])
    m = cv2.warpAffine(rg["mask"], inv, (w, h), flags=cv2.INTER_NEAREST)
    return (m > 127).astype("uint8") * 255


def _clean_red(img):
    """Inpaint the pen strokes (red and green) out of an image that is itself the marked one (no original)."""
    import cv2
    import numpy as np
    a = np.asarray(img.convert("RGB"))
    pen = np.maximum(_strokes(img, "red"), _strokes(img, "green")) * 255
    pen = cv2.dilate(pen, np.ones((5, 5), np.uint8))
    return PILImage.fromarray(cv2.inpaint(a, pen, 5, cv2.INPAINT_TELEA))


def _green_to_source(g: dict, src: dict | None) -> dict:
    """A green mark's line ends / centre in the original image's pixels."""
    import cv2
    import numpy as np
    if src is None:
        return {**g}
    inv = cv2.invertAffineTransform(src["A"])

    def tf(p):
        v = inv @ np.array([p[0], p[1], 1.0])
        return [float(v[0]), float(v[1])]
    return {**g, "ends": [tf(g["ends"][0]), tf(g["ends"][1])], "center": tuple(tf(g["center"]))}


def _direction_words(v) -> str:
    """A direction on the picture in words (for the prompt; the image model also gets it drawn)."""
    dx, dy = float(v[0]), float(v[1])
    ang = math.degrees(math.atan2(dx, dy))   # 0 = straight down, + = toward the right of the picture
    side = "right" if dx > 0 else "left"
    a = abs(ang)
    if a <= 12:
        return "pointing straight down"
    if a <= 35:
        return f"pointing down, tilted slightly toward the {side} side of the picture"
    if a <= 60:
        return f"pointing diagonally down toward the {side} side of the picture"
    if a <= 120:
        return f"pointing sideways toward the {side} side of the picture"
    if a <= 150:
        return f"pointing diagonally up toward the {side} side of the picture"
    return "pointing up"


def _move_layers(img, red_mask, green: dict, reach: float) -> dict:
    """Plan a move as two model steps. The red-marked thing is cut out and turned/moved onto the green mark (a
    line: lie along it, the nearer ends kept together, e.g. a hilt at the belt; a loop: centre on it). Returns:
      remove  where step 1 repaints: the thing's old place and where it attached (the model removes it and
              draws what was behind it, e.g. the belt, sash and clothing)
      layer   the thing alone on white at its new position and angle (step 2's image 2), and moved/put, its
              pixels there
      region  where step 2 redraws: the old place, the new place and everything around where it attaches (e.g.
              the whole hip for a sword at the belt), so it is put back attached and the picture makes sense
      words   the new direction in words; info  what was done.
    The model copies reference pictures almost pixel for pixel, so both steps only ever show it clean, sharp
    pictures: never a hand-made cut-out with gaps or smudges."""
    import cv2
    import numpy as np
    a = np.asarray(img.convert("RGB"))
    H, W = red_mask.shape
    R = red_mask > 127
    ell = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (15, 15))
    ring = (cv2.dilate(red_mask, ell, iterations=2) > 127) & ~R
    bg = np.median(a[ring], axis=0) if ring.any() else np.array([255, 255, 255])
    diff = np.abs(a.astype(np.int16) - bg.astype(np.int16)).max(axis=2)
    # the marked thing: its solid body (clearly darker or more coloured than the background, thicker than an
    # outline), the largest such piece, plus its own edges; pale fabric or outlines next to it stay where they are
    core = ((diff > 60) & R).astype(np.uint8) * 255
    core = cv2.morphologyEx(core, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5)))
    n_c, lab_c, st_c, _ = cv2.connectedComponentsWithStats(core, connectivity=8)
    if n_c > 1:   # the largest piece, plus pieces of it split off (e.g. a scabbard's metal tip): big or close
        big = 1 + int(np.argmax(st_c[1:, cv2.CC_STAT_AREA]))
        span = max(int(st_c[big, cv2.CC_STAT_WIDTH]), int(st_c[big, cv2.CC_STAT_HEIGHT]))
        reach_big = cv2.dilate((lab_c == big).astype(np.uint8),
                               cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * max(5, span // 12) + 1,) * 2)) > 0
        keep = [i for i in range(1, n_c) if st_c[i, cv2.CC_STAT_AREA] >= 0.15 * st_c[big, cv2.CC_STAT_AREA]
                or (st_c[i, cv2.CC_STAT_AREA] >= 0.005 * st_c[big, cv2.CC_STAT_AREA] and (reach_big & (lab_c == i)).any())]
        core = np.isin(lab_c, keep).astype(np.uint8) * 255
    cand = (diff > 28) & R
    near_core = cv2.dilate(core, np.ones((9, 9), np.uint8)) > 0
    if core.any():  # plus whatever lies on its own axis, within its width, up to just past its ends (a pale tip)
        cy, cx = np.nonzero(core)
        cp = np.column_stack([cx, cy]).astype(np.float64)
        cm = cp.mean(axis=0)
        ev, evec = np.linalg.eigh(np.cov((cp - cm).T))
        ax, nrm = evec[:, int(np.argmax(ev))], evec[:, int(np.argmin(ev))]
        along, across = (cp - cm) @ ax, np.abs((cp - cm) @ nrm)
        lo_a, hi_a = along.min(), along.max()
        pad_a, half_w = 0.08 * (hi_a - lo_a), np.percentile(across, 90) * 1.4 + 3
        yy, xx = np.nonzero(cand)
        q = np.column_stack([xx, yy]).astype(np.float64) - cm
        on_axis = ((q @ ax >= lo_a - pad_a) & (q @ ax <= hi_a + pad_a) & (np.abs(q @ nrm) <= half_w))
        band = np.zeros_like(cand)
        band[yy[on_axis], xx[on_axis]] = True
        near_core |= band
    obj = (cand & near_core).astype(np.uint8) * 255
    obj = cv2.morphologyEx(obj, cv2.MORPH_CLOSE, np.ones((5, 5), np.uint8))
    if not obj.any():
        obj = ((diff > 28) & R).astype(np.uint8) * 255
    if not obj.any():
        obj = red_mask.copy()
    ys, xs = np.nonzero(obj)
    pts = np.column_stack([xs, ys]).astype(np.float64)
    mean = pts.mean(axis=0)
    evals, evecs = np.linalg.eigh(np.cov((pts - mean).T))
    axis = evecs[:, int(np.argmax(evals))]
    proj = (pts - mean) @ axis
    ea, eb = mean + axis * proj.min(), mean + axis * proj.max()
    if green["shape"] == "line":
        g1, g2 = np.array(green["ends"][0]), np.array(green["ends"][1])
        if np.linalg.norm(ea - g1) + np.linalg.norm(eb - g2) > np.linalg.norm(ea - g2) + np.linalg.norm(eb - g1):
            g1, g2 = g2, g1
        ang = math.atan2(*(g2 - g1)[::-1]) - math.atan2(*(eb - ea)[::-1])
        ang = (ang + math.pi) % (2 * math.pi) - math.pi
        pivot, dest = ea, g1
        words = _direction_words(g2 - g1)
    else:
        ang, pivot, dest = 0.0, mean, np.array(green["center"])
        words = "moved to its new place"
    c_, s_ = math.cos(ang), math.sin(ang)
    rot = np.array([[c_, -s_], [s_, c_]])
    t = dest - rot @ pivot
    M = np.array([[c_, -s_, t[0]], [s_, c_, t[1]]], np.float64)
    moved = cv2.warpAffine(a, M, (W, H), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)
    moved_obj = cv2.warpAffine(obj, M, (W, H), flags=cv2.INTER_NEAREST)
    put = moved_obj > 127
    length = float(np.linalg.norm(eb - ea)) or 50.0

    # step 1 (remove): where the old place is over plain background it is cleared here, exactly (the model
    # copies its reference, so a visible old thing there could come back); the model removes the rest
    hole = cv2.dilate(obj, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (13, 13))) > 127
    body = ((diff > 28) & ~(obj > 127)).astype(np.uint8)
    nb, blab, bstats, _ = cv2.connectedComponentsWithStats(body, connectivity=8)
    for i in range(1, nb):   # specks along the old edge are not clothing
        if bstats[i, cv2.CC_STAT_AREA] < 60:
            body[blab == i] = 0
    near = max(15, int(max(H, W) * 0.025))
    body_zone = cv2.dilate(body, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * near + 1,) * 2)) > 0
    start = a.copy()
    ring_flat = ring & (diff < 12)
    if bool(ring_flat.sum() > 0.4 * max(1, ring.sum())) and float(a[ring_flat].std(axis=0).max()) < 4:
        start[hole & ~body_zone] = bg.astype(np.uint8)
        remove = (hole & body_zone).astype(np.uint8) * 255
    else:
        remove = hole.astype(np.uint8) * 255
    cv2.circle(remove, (int(pivot[0]), int(pivot[1])), max(16, int(length * 0.15)), 255, -1)
    layer = np.full_like(a, 255)
    layer[put] = moved[put]

    # the region to redraw: the old place, the new place and everything near the attachments
    region = R.astype(np.uint8) * 255
    region = np.maximum(region, cv2.dilate(moved_obj, cv2.getStructuringElement(
        cv2.MORPH_ELLIPSE, (2 * max(5, int(length * 0.08)) + 1,) * 2)))
    r_reach = max(24, int(length * max(0.1, reach)))
    for pt in (dest, pivot):
        cv2.circle(region, (int(pt[0]), int(pt[1])), r_reach, 255, -1)
    k = max(9, int(length * 0.15)) | 1
    region = cv2.morphologyEx(region, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k)))
    info = f"turned {math.degrees(ang):+.0f} degrees, moved {np.linalg.norm(dest - pivot):.0f} px"
    return {"start": PILImage.fromarray(start), "remove": remove, "layer": PILImage.fromarray(layer),
            "moved": moved, "put": put, "region": region, "words": words, "info": info}


async def _redraw_once(c: httpx.AsyncClient, base, mv: dict, text: str, seed: int, steps: int, strength: float):
    """Step 2 of a move: base is the clean picture from step 1 (the thing removed). Redraw the region starting from
    base with the thing pasted at its new place; image 1 = base, image 2 = the thing alone in place. Pasted back
    over base with a soft edge."""
    import numpy as np
    start = np.asarray(base.convert("RGB")).copy()
    start[mv["put"]] = mv["moved"][mv["put"]]
    mv = {**mv, "collage": PILImage.fromarray(start), "plate": base}
    import cv2
    import numpy as np
    W, H = base.size
    region = mv["region"]
    x0, y0, x1, y1 = _edit_box(region, W, H)
    bw, bh = x1 - x0, y1 - y0
    sc = math.sqrt(1.05e6 / (bw * bh))
    cw, ch = (max(256, min(2048, int(round(v * sc / 16)) * 16)) for v in (bw, bh))
    rg = region[y0:y1, x0:x1]
    d = max(3, int(max(bw, bh) * 0.015))
    sample_m = (cv2.GaussianBlur(cv2.dilate(rg, np.ones((2 * d + 1,) * 2, np.uint8)), (0, 0), d)
                .astype(np.float32) * max(0.2, min(1.0, strength))).astype(np.uint8)
    comp_m = cv2.GaussianBlur(rg, (0, 0), d)

    def up(img) -> bytes:
        buf = io.BytesIO()
        img.crop((x0, y0, x1, y1)).resize((cw, ch), PILImage.LANCZOS).save(buf, "PNG")
        return buf.getvalue()
    mbuf = io.BytesIO()
    PILImage.fromarray(sample_m).resize((cw, ch), PILImage.BILINEAR).convert("RGB").save(mbuf, "PNG")
    names = []
    for data in (up(mv["collage"]), mbuf.getvalue(), up(mv["plate"]), up(mv["layer"])):
        name = hashlib.sha1(data).hexdigest()[:12] + ".png"
        r = await c.post(f"{COMFY}/upload/image", files={"image": (name, data, "image/png")},
                         data={"type": "input", "subfolder": "mcp_refs", "overwrite": "true"})
        r.raise_for_status()
        j = r.json()
        names.append(f"{j['subfolder']}/{j['name']}" if j.get("subfolder") else j["name"])
    out = await run_workflow(c, build_edit_workflow(text, names[0], names[1], [names[3]], cw, ch, seed, steps, 1.0,
                                                    context_name=names[2]))
    new = PILImage.open(io.BytesIO(out)).convert("RGB").resize((bw, bh), PILImage.LANCZOS)
    crop = base.crop((x0, y0, x1, y1))
    result = base.copy()
    result.paste(PILImage.composite(new, crop, PILImage.fromarray(comp_m)), (x0, y0))
    return result, (x0, y0, x1, y1)


def build_edit_workflow(prompt: str, img_name: str, mask_name: str, ref_names: list[str], w: int, h: int,
                        seed: int, steps: int, strength: float, context_name: str | None = None,
                        no_context: bool = False, lora: str | None = None, lora_strength: float = 1.0):
    total = steps if strength >= 0.999 else max(steps + 1, int(math.ceil(steps / max(0.05, strength))))
    wf = {
        "1": {"class_type": "UNETLoader", "inputs": {"unet_name": MODEL["unet"], "weight_dtype": "default"}},
        "2": {"class_type": "CLIPLoader", "inputs": {"clip_name": MODEL["text_encoder"], "type": "flux2",
                                                      "device": "default"}},
        "3": {"class_type": "VAELoader", "inputs": {"vae_name": MODEL["vae"]}},
        "4": {"class_type": "CLIPTextEncode", "inputs": {"text": prompt, "clip": ["2", 0]}},
        "5": {"class_type": "ConditioningZeroOut", "inputs": {"conditioning": ["4", 0]}},
        "7": {"class_type": "Flux2Scheduler", "inputs": {"steps": total, "width": w, "height": h}},
        "8": {"class_type": "RandomNoise", "inputs": {"noise_seed": seed}},
        "9": {"class_type": "KSamplerSelect", "inputs": {"sampler_name": "euler"}},
        "21": {"class_type": "DifferentialDiffusion", "inputs": {"model": ["20", 0] if lora else ["1", 0]}},
        "30": {"class_type": "LoadImage", "inputs": {"image": img_name}},
        "31": {"class_type": "VAEEncode", "inputs": {"pixels": ["30", 0], "vae": ["3", 0]}},
        "32": {"class_type": "LoadImageMask", "inputs": {"image": mask_name, "channel": "red"}},
        "33": {"class_type": "SetLatentNoiseMask", "inputs": {"samples": ["31", 0], "mask": ["32", 0]}},
    }
    if lora:
        wf["20"] = {"class_type": "LoraLoaderModelOnly", "inputs": {"model": ["1", 0], "lora_name": lora,
                                                                    "strength_model": lora_strength}}
    pos, neg = ["4", 0], ["5", 0]
    images = ref_names if no_context else [context_name or img_name, *ref_names]
    for i, name in enumerate(images):   # image 1 = the picture, as context (unless no_context: plain inpainting)
        b = 100 + i * 10
        wf[str(b)] = {"class_type": "LoadImage", "inputs": {"image": name}}
        wf[str(b + 1)] = {"class_type": "ImageScaleToTotalPixels", "inputs": {
            "image": [str(b), 0], "upscale_method": "nearest-exact", "megapixels": 1.0 if i == 0 else 0.6,
            "resolution_steps": 1}}
        wf[str(b + 2)] = {"class_type": "VAEEncode", "inputs": {"pixels": [str(b + 1), 0], "vae": ["3", 0]}}
        wf[str(b + 3)] = {"class_type": "ReferenceLatent", "inputs": {"conditioning": pos, "latent": [str(b + 2), 0]}}
        wf[str(b + 4)] = {"class_type": "ReferenceLatent", "inputs": {"conditioning": neg, "latent": [str(b + 2), 0]}}
        pos, neg = [str(b + 3), 0], [str(b + 4), 0]
    sig = ["7", 0]
    if total > steps:
        wf["40"] = {"class_type": "SplitSigmas", "inputs": {"sigmas": ["7", 0], "step": total - steps}}
        sig = ["40", 1]
    wf.update({
        "10": {"class_type": "CFGGuider", "inputs": {"model": ["21", 0], "positive": pos, "negative": neg, "cfg": 1.0}},
        "11": {"class_type": "SamplerCustomAdvanced", "inputs": {"noise": ["8", 0], "guider": ["10", 0],
                                                                  "sampler": ["9", 0], "sigmas": sig,
                                                                  "latent_image": ["33", 0]}},
        "12": {"class_type": "VAEDecode", "inputs": {"samples": ["11", 0], "vae": ["3", 0]}},
        "13": {"class_type": "PreviewImage", "inputs": {"images": ["12", 0]}},
    })
    return wf


def _edit_box(mask, W: int, H: int) -> tuple[int, int, int, int]:
    import numpy as np
    ys, xs = np.nonzero(mask > 0)
    x0, x1, y0, y1 = int(xs.min()), int(xs.max()) + 1, int(ys.min()), int(ys.max()) + 1
    pad = int(max(x1 - x0, y1 - y0) * 0.6) + 24
    x0, y0, x1, y1 = x0 - pad, y0 - pad, x1 + pad, y1 + pad
    for _ in range(2):  # at least ~384 px of context each way
        if x1 - x0 < 384:
            c = (x0 + x1) // 2
            x0, x1 = c - 192, c + 192
        if y1 - y0 < 384:
            c = (y0 + y1) // 2
            y0, y1 = c - 192, c + 192
    if x0 < 0:
        x1, x0 = x1 - x0, 0
    if y0 < 0:
        y1, y0 = y1 - y0, 0
    return max(0, x0), max(0, y0), min(W, x1), min(H, y1)


async def _edit_once(c: httpx.AsyncClient, base, mask, text: str, ref_names: list[str], seed: int, steps: int,
                     strength: float, before=None, redraw=None, no_context: bool = False, guide=None,
                     guide_strength: float = 1.0):
    """Repaint one marked area of base (PIL RGB); returns the new full image and the area's box. The mask's grey
    levels say how much each pixel may change (differential diffusion). redraw: areas (uint8 mask) that are
    blurred in the image-1 reference, because the model copies a sharp reference pixel for pixel (a rough
    collage's seams would come back); the picture being edited stays sharp. before: the picture before a move,
    sent as image 2 so the moved thing keeps its design."""
    import cv2
    import numpy as np
    W, H = base.size
    x0, y0, x1, y1 = _edit_box(mask, W, H)
    bw, bh = x1 - x0, y1 - y0
    s = math.sqrt(1.05e6 / (bw * bh))
    cw, ch = (max(256, min(2048, int(round(v * s / 16)) * 16)) for v in (bw, bh))
    crop = base.crop((x0, y0, x1, y1))
    m = mask[y0:y1, x0:x1]
    d = max(3, int(max(bw, bh) * 0.02))
    k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * d + 1, 2 * d + 1))
    if len(np.unique(m)) > 2:   # graded (a move): soften only, so low "keep it empty" zones stay low
        sample_m = cv2.GaussianBlur(m, (0, 0), max(1.0, d / 3))
    else:                       # a plain mark: let the model paint a little past it, for a clean edge
        sample_m = cv2.GaussianBlur(cv2.dilate(m, k, iterations=2), (0, 0), d / 2)
    comp_m = cv2.GaussianBlur(cv2.dilate((m > 0).astype(np.uint8) * 255, k), (0, 0), d / 2)  # pasted back

    def png(img) -> bytes:
        buf = io.BytesIO()
        img.save(buf, "PNG")
        return buf.getvalue()
    crop_big = crop.resize((cw, ch), PILImage.LANCZOS)
    mask_big = PILImage.fromarray(sample_m).resize((cw, ch), PILImage.BILINEAR).convert("RGB")
    names = []
    imgs = [png(crop_big), png(mask_big)]
    context = None
    if redraw is not None and redraw[y0:y1, x0:x1].any():
        rz = cv2.GaussianBlur(cv2.dilate(redraw[y0:y1, x0:x1], k), (0, 0), d / 2)
        rz = np.asarray(PILImage.fromarray(rz).resize((cw, ch), PILImage.BILINEAR), np.float32)[..., None] / 255
        sharp = np.asarray(crop_big, np.float32)
        soft = cv2.GaussianBlur(sharp, (0, 0), max(4.0, min(cw, ch) * 0.02))
        context = PILImage.fromarray((sharp * (1 - rz) + soft * rz).clip(0, 255).astype(np.uint8))
        imgs.append(png(context))
    if before is not None:
        imgs.append(png(before.crop((x0, y0, x1, y1)).resize((cw, ch), PILImage.LANCZOS)))
    if guide is not None:  # guided redraw: the marked area is drawn from scratch, following the depth map
        imgs.append(png(guide.crop((x0, y0, x1, y1)).resize((cw, ch), PILImage.LANCZOS)))
    for data in imgs:
        name = hashlib.sha1(data).hexdigest()[:12] + ".png"
        r = await c.post(f"{COMFY}/upload/image", files={"image": (name, data, "image/png")},
                         data={"type": "input", "subfolder": "mcp_refs", "overwrite": "true"})
        r.raise_for_status()
        j = r.json()
        names.append(f"{j['subfolder']}/{j['name']}" if j.get("subfolder") else j["name"])
    ctx_name = names[2] if context is not None else None
    extra = names[3:] if context is not None else names[2:]
    if guide is not None:
        wf = build_edit_workflow(text, names[0], names[1], [names[-1], *ref_names], cw, ch, seed, steps, strength,
                                 None, True, MODEL.get("depth_lora"), guide_strength)
    else:
        wf = build_edit_workflow(text, names[0], names[1], extra + ref_names, cw, ch, seed, steps, strength,
                                 ctx_name, no_context)
    out = await run_workflow(c, wf)
    new = PILImage.open(io.BytesIO(out)).convert("RGB").resize((bw, bh), PILImage.LANCZOS)
    pasted = PILImage.composite(new, crop, PILImage.fromarray(comp_m))
    result = base.copy()
    result.paste(pasted, (x0, y0))
    return result, (x0, y0, x1, y1)


@tool
async def find_marks(image: str = "latest", preview_size: int = 900) -> list:
    """Find the red marks on an image the user marked up (e.g. with the pen of the Claude app's image editor) and
    the original image each mark was drawn on. Green marks next to a red mark show where and which way the red-
    marked thing should go (a line = position and direction, a loop = position). image: a file path, or 'latest' = the newest image attached in a
    Claude chat on this computer (~/.claude/uploads). Originals are searched in this session's picks and the open
    project's outputs, docs (e.g. a guide PDF's images) and guide references, also inside a screenshot of a page.
    Returns the numbered marks and a preview with the numbers, to match the user's notes to marks before
    edit_image."""
    an = await asyncio.to_thread(_analyze_marks, image)
    lines = [f"Marked image: {an['marked_path']} ({an['marked'].width}x{an['marked'].height}), "
             f"{len(an['regions'])} mark(s):"]
    for rg in an["regions"]:
        src = rg["source"]
        x, y, w, h = rg["bbox"]
        where = (f"on {src['path']} ({src['inliers']} matching points; the attached picture shows it at "
                 f"{src['scale'] * 100:.0f}% size)") if src else "no original found: the attached image itself is edited"
        lines.append(f"  mark {rg['number']}: at x {x}-{x + w}, y {y}-{y + h} of the attached image, {where}")
        if rg.get("green"):
            g = rg["green"]
            what = ("a line: the marked thing will be turned and moved to lie along it" if g["shape"] == "line"
                    else "a loop: the marked thing will be moved there")
            lines.append(f"    + green mark {g['letter']} ({what})")
    unused = [g["letter"] for g in an.get("greens") or [] if all(rg.get("green") is not g for rg in an["regions"])]
    if unused:
        lines.append(f"  green mark(s) {', '.join(unused)} not used (another green mark is closer to the same red mark)")
    lines.append("Next: edit_image(instruction=..., image=<same>, marks=[numbers on one original]) per original; "
                 "write one instruction per mark when the user's notes differ. A red mark with a green partner is "
                 "moved/turned onto the green mark first (the instruction may then be empty).")
    return ["\n".join(lines), _mark_preview(an, preview_size)]


@tool
async def edit_image(instruction: str = "", image: str = "latest", marks: list[int] | None = None,
                     source: str = "", entries: list[str] | None = None, project: str = "", strength: float = 1.0,
                     redraw_strength: float = 0.9, reach: float = 0.3, candidates: int = 0, seed: int = -1,
                     steps: int = 4, guide: str = "", guide_strength: float = 1.0, fresh: bool = False,
                     preview_size: int = 512) -> list:
    """Edit ONLY the areas the user marked in red, instead of drawing a new image: everything outside the marks
    stays exactly as it was. Renders N candidates like generate_image: then pick_candidate and approve_image.

    instruction: what to change inside the marks, e.g. "fix the hand: five fingers wrapped around the sword grip".
    image: the marked image (path, or 'latest' = the newest image attached in a Claude chat). The marks are
           mapped onto the original image they were drawn on (found like find_marks: this session's picks, the
           project's outputs, docs and guide references, also inside a page screenshot) and the edit is made
           on that original at full resolution. If no original is found, the attached image itself is edited.
    marks: which mark numbers to edit (from find_marks). Default: all; they must then be on one original.
    source: force the original to edit (a path or request id), e.g. when find_marks matched the wrong one.
    entries: guide entries in the marked area (e.g. ["Aldric"]): their descriptions and references keep details
             right. project: as in generate_image.
    strength: 1.0 = repaint the marked areas fully (fix wrong anatomy, remove or replace things); 0.4-0.7 =
              keep their shapes and change details or colour.
    GREEN MARKS: a green line next to a red mark = "put the red-marked thing here, pointing this way" (a green
    loop = move it there without turning). This is a real redraw in two model steps, not a patch: 1) the model
    removes the thing and draws what was behind it (belt, sash, clothing...); 2) the thing, turned and moved
    onto the green mark (its end nearer to one end of the line stays at that end, e.g. a hilt at the belt), is
    added back and the whole connected region (the new place and everything around where it attaches, e.g.
    the hips and belt for a sword) is redrawn so it is attached and the picture makes sense. Outside those
    areas nothing changes. Name the thing in the instruction ("his sword, hanging from the belt") and add
    how it should attach. redraw_strength: how freely the region is redrawn (0.9; lower keeps the rough
    placement more literally). reach: how far around the attachment the redraw extends, as a share of the
    thing's length (0.3; larger redraws more of the body around it).
    Each mark is repainted in a crop around it at ~1 MP (so small things like hands get detail) and pasted
    back with a soft edge. ~10-15 s per mark per candidate.
    GUIDED REDRAW (precision work with a mannequin): guide = a depth map (near = bright) lined up with the
    original (same size, or resized to it), e.g. a Blender mannequin of the hands placed where they must be.
    The marked areas are then drawn FROM SCRATCH by the model with the depth LoRA, following the guide's shapes
    and layout: the original inside the marks is not shown to the model (so it can't copy what was there) and
    the mannequin is never pasted into the picture; outside the marks nothing changes. Use strength 1.0 and mark
    the whole area to redraw (the old part and where the new one goes). guide_strength: the LoRA strength (1.0;
    0.8 follows the guide more loosely). No green marks with a guide.
    fresh: redraw the marked areas from scratch, without showing the model what is inside them (it otherwise
    copies the original very closely, even at strength 1.0). Use it with strength 1.0 to REMOVE something (say
    what should be there instead: "the cape and the sky behind, no arm") or to really change it (bare skin that
    should be a fabric sleeve, missing armour details). The picture around the marks keeps the style.
    """
    global BUSY
    _open_for_request(project)
    if not 0.05 <= strength <= 1.0:
        raise ValueError("strength must be between 0.05 and 1.0")
    an = await asyncio.to_thread(_analyze_marks, image)
    regs = an["regions"]
    if marks:
        bad = [m for m in marks if m not in [rg["number"] for rg in regs]]
        if bad:
            raise ValueError(f"no mark(s) {bad}; this image has marks 1-{len(regs)} (see find_marks)")
        regs = [rg for rg in regs if rg["number"] in marks]
    if source.strip():
        sp = _image_path(source)
        src_img = PILImage.open(sp).convert("RGB")
        # map the marks with this original's own match, if it appears in the marked image
        loc = await asyncio.to_thread(_locate_sources, an["marked"], _red_regions(an["marked"])[1], [sp])
        for rg in regs:
            rg["source"] = loc[0] if loc else None
        if not loc and an["marked"].size != src_img.size:
            raise ValueError("the forced source doesn't appear in the marked image and has a different size, so "
                             "the marks can't be placed on it")
    else:
        srcs = {_norm(rg["source"]["path"]) if rg["source"] else "attached" for rg in regs}
        if len(srcs) > 1:
            listing = "; ".join(f"mark {rg['number']} on {rg['source']['path'].name if rg['source'] else 'the attached image'}"
                                for rg in regs)
            raise ValueError(f"the marks are on different originals ({listing}). Edit one original per call with "
                             "marks=[...]")
        sp = regs[0]["source"]["path"] if regs[0]["source"] else an["marked_path"]
        src_img = PILImage.open(sp).convert("RGB")
    W, H = src_img.size
    masks = [await asyncio.to_thread(_source_mask, rg, rg["source"], W, H) for rg in regs]
    greens = [_green_to_source(rg["green"], rg["source"]) if rg.get("green") else None for rg in regs]
    if not instruction.strip() and not all(greens):
        raise ValueError("say what to change in the red marks (instruction), or add a green mark showing where "
                         "and which way the marked thing should go")
    if _same_path(sp, an["marked_path"]):  # editing the attached picture itself: paint the pen strokes out first
        src_img = await asyncio.to_thread(_clean_red, src_img)
    guide_img = None
    if guide.strip():
        if any(greens):
            raise ValueError("a guide can't be combined with green marks: edit the moved thing first, then guide")
        guide_img = PILImage.open(_image_path(guide)).convert("RGB")
        if guide_img.size != (W, H):
            guide_img = guide_img.resize((W, H), PILImage.LANCZOS)
    plan = _plan("", entries or [], False, "style", True)   # only the entries' descriptions + checklist
    char_refs = []
    if S.get("project") and entries:
        proj, _ = current()
        for nm in entries:
            e = _find_entry(proj, nm)
            pr = _primary_ref(e)
            if pr and e["kind"] in ("character", "creature", "prop") and len(char_refs) < 2:
                char_refs.append({"path": e["_dir"] / pr["file"], "source": _eid(e),
                                  "legend": ROLE_TEXT.get(e["kind"], "{name}").format(name=e["name"])})
    def mark_text() -> str:
        if guide_img is not None:
            legend = " ".join(["Image 1 shows the depth map of what to draw in the redrawn area (near = bright): "
                               "follow its shapes, pose and layout exactly (finger positions, grip, angles), but "
                               "draw it as part of the picture, not as a grey shape."] +
                              [f"Image {i} shows {r['legend']}." for i, r in enumerate(char_refs, 2)])
            return "\n\n".join(x for x in [
                "refcontrol", f"Redraw the masked area of the picture: {instruction.strip().rstrip('.')}. Match the "
                "art style, linework, colours and lighting of the rest of the picture exactly, and connect cleanly "
                "to what is around the area.", plan["text"], f"Reference images: {legend}"] if x)
        if fresh:
            legend = " ".join(f"Image {i} shows {r['legend']}." for i, r in enumerate(char_refs, 1))
            return "\n\n".join(x for x in [
                f"Redraw the masked area of the picture: {instruction.strip().rstrip('.')}. Match the art style, "
                "linework, colours and lighting of the rest of the picture exactly, and connect cleanly to what is "
                "around the area.", plan["text"], f"Reference images: {legend}" if legend else ""] if x)
        legend = " ".join([f"Image 1 shows {EDIT_LEGEND}."] +
                          [f"Image {i} shows {r['legend']}." for i, r in enumerate(char_refs, 2)])
        return "\n\n".join(x for x in [
            f"Edit image 1: {instruction.strip().rstrip('.')}. Change only that; keep the rest of image 1 exactly "
            "as it is, in the same art style, linework and colours.", plan["text"], f"Reference images: {legend}"]
            if x)

    thing = instruction.strip().rstrip(".") or "the object in the edited area"

    def remove_text() -> str:
        return "\n\n".join(x for x in [
            "Edit image 1: remove the object in the edited area completely, together with any strap or holder that "
            f"only holds it (the object: {thing}). Where it was, draw what is behind it so the picture is complete "
            "and clean: continue the clothing, belt, sash, body, outlines and background naturally. Keep everything "
            "else exactly as it is, in the same art style, linework and colours.", plan["text"],
            "Reference images: Image 1 shows the picture (a crop around the edited area)."] if x)

    def move_text(words: str) -> str:
        return "\n\n".join(x for x in [
            f"Edit image 1: add this object to it: {thing}. Image 2 shows the object alone at the exact position, "
            f"angle and size it must have in image 1: put it exactly there, {words}, with the same design. Redraw "
            "what it connects to (for example the belt, straps, hands or clothing around it) so it is held or "
            "attached naturally and the whole picture makes sense. Keep everything else exactly as in image 1: "
            "the same character, face, outfit details, colours, art style, linework and shading. The object "
            "appears only once.",
            plan["text"], "Reference images: Image 1 shows the picture (a crop). Image 2 shows the object alone, "
            "in place."] if x)
    text = move_text("in its new place") if any(greens) else mark_text()
    n = max(1, min(MAX_CANDIDATES, candidates or _candidate_count()))
    BUSY += 1
    try:
        startup = await _backend_ready()
        seed = random.randrange(2**31) if seed < 0 else seed
        rid = f"{dt.datetime.now():%m%d-%H%M%S}"
        while rid in S["requests"] or (CAND / rid).exists():
            rid += "x"
        rdir = CAND / rid
        rdir.mkdir(parents=True)
        cands, out, moves = [], [], []
        t_all = time.monotonic()
        async with httpx.AsyncClient(timeout=120) as c:
            ref_names = [await upload_ref(c, r["path"]) for r in char_refs]
            for i in range(1, n + 1):
                s = seed + i - 1
                t0 = time.monotonic()
                img, boxes = src_img, []
                for m, g in zip(masks, greens):
                    if g:  # 1) the model removes it (drawing what was behind), 2) adds it back in its new place
                        mv = await asyncio.to_thread(_move_layers, img, m, g, reach)
                        moves.append(mv["info"])
                        clean, box1 = await _edit_once(c, mv["start"], mv["remove"], remove_text(), [], s, steps,
                                                       1.0)
                        img, box = await _redraw_once(c, clean, mv, move_text(mv["words"]), s + 7919, steps,
                                                      redraw_strength)
                        box = (min(box[0], box1[0]), min(box[1], box1[1]), max(box[2], box1[2]), max(box[3], box1[3]))
                    else:
                        img, box = await _edit_once(c, img, m, mark_text(), ref_names, s, steps, strength,
                                                    no_context=fresh, guide=guide_img,
                                                    guide_strength=guide_strength)
                    boxes.append(box)
                buf = io.BytesIO()
                img.save(buf, "PNG")
                png = buf.getvalue()
                _write_bytes(rdir / f"cand_{i}.png", png)
                cands.append({"number": i, "file": f"cand_{i}.png", "seed": s,
                              "seconds": round(time.monotonic() - t0, 1)})
                out += [f"Candidate {i} (seed {s})", _preview(png, preview_size)]
                for bx in boxes:  # before | after of each edited area, to judge small fixes
                    pair = PILImage.new("RGB", ((bx[2] - bx[0]) * 2 + 8, bx[3] - bx[1]), "white")
                    pair.paste(src_img.crop(bx), (0, 0))
                    pair.paste(img.crop(bx), (bx[2] - bx[0] + 8, 0))
                    zb = io.BytesIO()
                    pair.save(zb, "PNG")
                    out += [f"Candidate {i}: marked area before | after", _preview(zb.getvalue(), preview_size)]
    finally:
        BUSY -= 1
    meta = {"request_id": rid, "created": now_iso(), "project": plan["project"], "prompt": instruction,
            "full_prompt": text, "entries": plan["entries"], "checklist": plan["checklist"],
            "width": W, "height": H, "steps": steps, "model": MODEL,
            "edit": {"source": str(sp), "marked": str(an["marked_path"]), "marks": [rg["number"] for rg in regs],
                     "strength": strength, "moves": sorted(set(moves)) or None,
                     "guide": str(_image_path(guide)) if guide.strip() else None, "fresh": fresh or None,
                     "redraw_strength": redraw_strength if any(greens) else None,
                     "reach": reach if any(greens) else None},
            "references": [{"source": "edited image"}, *({"source": r["source"]} for r in char_refs)],
            "candidates": cands, "picked": None}
    _save(rdir / "request.json", meta)
    S["requests"][rid] = meta
    S["last_request"] = rid
    _save_session()
    checklist = ("\nChecklist (judge the candidates against it):\n" + "\n".join(f"- {x}" for x in plan["checklist"])
                 if plan["checklist"] else "")
    head = (f"{'[' + startup + '] ' if startup else ''}Edit request {rid} ({_mode_text()}): {n} candidates of "
            f"{Path(sp).name} ({W}x{H}), marks {[rg['number'] for rg in regs]}"
            f"{' (moved onto green marks: ' + '; '.join(sorted(set(moves))) + ')' if moves else ''}, "
            f"{time.monotonic() - t_all:.0f} s."
            f"{checklist}\nOnly the marked areas changed; judge them (the before | after crops) for the "
            f"instruction, anatomy and seams. Then pick_candidate(request_id='{rid}', number=<best>).")
    return [head, *out]


# ---------------------------------------------------------------- startup
def _startup():
    # Tell the image-picker agent how to reach this server from a shell if the MCP tools aren't loaded.
    try:
        _save(CFG_DIR / "bridge.json", {
            "python": sys.executable,
            "mcp_call": str(Path(__file__).resolve().parent / "mcp_call.py"),
            "usage": '"<python>" "<mcp_call>" --session <id> <tool_name> \'<json arguments>\'',
        })
    except OSError:
        pass
    try:  # Ollama models unloaded by the single-session version (<= 1.0) are restored by the shared registry now
        legacy = HOME / "state.json"
        old = _load(legacy, {})
        if old.get("unloaded_ollama"):
            with _lock("backend"):
                b = _backend_load()
                names = [u["name"] for u in b["unloaded_ollama"]]
                b["unloaded_ollama"] += [u for u in old["unloaded_ollama"] if u["name"] not in names]
                _save(BACKEND, b)
            old["unloaded_ollama"] = []
            _save(legacy, old)
        _prune_sessions()
    except Exception as e:  # noqa: BLE001
        log("startup housekeeping failed:", e)


def _on_exit():
    if S["kind"] != "process":
        return  # bridge sessions span several processes
    try:
        _lease_release()
    except Exception:  # noqa: BLE001
        pass
    if not S["requests"]:
        shutil.rmtree(SDIR, ignore_errors=True)


if __name__ == "__main__":
    _startup()
    atexit.register(_on_exit)
    mcp.run("stdio")
