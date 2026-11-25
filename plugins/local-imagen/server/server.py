# /// script
# requires-python = ">=3.10"
# dependencies = ["mcp>=2.3,<3", "httpx>=0.27", "pillow>=10", "filelock>=3.13", "psutil>=5.9", "send2trash>=1.8"]
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
    **(CFG.get("model") or {}),
}
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
REF_PRIORITY = {"character": 0, "creature": 0, "style": 1, "location": 2, "prop": 3}  # other kinds: 4
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
    if not await asyncio.to_thread(_docker_ready):
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
                break
        else:
            raise RuntimeError("Docker Desktop did not become ready in 3 minutes")
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


def build_workflow(prompt: str, ref_names: list[str], w: int, h: int, seed: int, steps: int, ref_mp: float):
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
        "10": {"class_type": "CFGGuider", "inputs": {"model": ["1", 0], "positive": pos, "negative": neg, "cfg": 1.0}},
        "11": {"class_type": "SamplerCustomAdvanced", "inputs": {"noise": ["8", 0], "guider": ["10", 0],
                                                                  "sampler": ["9", 0], "sigmas": ["7", 0],
                                                                  "latent_image": ["6", 0]}},
        "12": {"class_type": "VAEDecode", "inputs": {"samples": ["11", 0], "vae": ["3", 0]}},
        "13": {"class_type": "PreviewImage", "inputs": {"images": ["12", 0]}},
    })
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
def _plan(prompt: str, entry_names: list[str], use_refs: bool, mode: str, use_default_style: bool) -> dict:
    """Compose the full prompt and pick the references (max 4) for one request in this session's context."""
    notes: list[str] = []
    if not S.get("project"):  # one-off images: the original behaviour
        if entry_names:
            raise ValueError("guide entries need a project; this session makes one-off images")
        refs = [{"path": LIB / r["file"], "source": f"attached '{r['label']}'"} for r in S["active"]] if use_refs else []
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
        for e in chosen:
            pr = _primary_ref(e)
            if pr:
                legend = ROLE_TEXT.get(e["kind"], "{name}").format(name=e["name"])
                if pr.get("role"):
                    legend += f" ({pr['role']})"
                ranked.append((REF_PRIORITY.get(e["kind"], 4),
                               {"path": e["_dir"] / pr["file"], "legend": legend, "source": _eid(e),
                                "file": (e["_dir"] / pr["file"]).relative_to(proj).as_posix()}))
    refs = [r for _, r in sorted(ranked, key=lambda x: x[0])]
    if len(refs) > MAX_REFS:
        notes.append("left out (max 4 references per image): " + ", ".join(r["source"] for r in refs[MAX_REFS:]))
        refs = refs[:MAX_REFS]
    legend = ("Reference images: " + " ".join(f"Image {i} shows {r['legend']}." for i, r in enumerate(refs, 1))
              if refs else "")
    text = "\n\n".join(x for x in [prompt.strip(), *sections, legend] if x)

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
        "the entry), must_have / never checklists, tags, notes and reference images. Read with guide_list / "
        "guide_show before editing; write only what the user said or approved (ask rather than invent). Keep "
        "descriptions short and visual. set_project_info(default_style=...) applies a style entry to every image. "
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
        "explanation about ComfyUI or Ollama."
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
                     f"[{len(e.get('refs') or [])} ref(s)]")
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
                    rename_to: str = "") -> str:
    """Create or update a guide entry of the open project. Only the fields you pass change; lists you pass replace
    the old list (read the entry with guide_show first to keep existing items).

    kind: needed for a new entry: style, character, creature, location, prop, pose, camera, or another word.
    description: the fixed visual description used word for word in every prompt with this entry.
    must_have: details every image must show (checked when picking). never: things that must not appear.
    tags / notes: for organising; not sent to the model. rename_to: new display name (folder renamed too).
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
            e = {"name": name.strip(), "slug": s, "kind": k, "description": "", "must_have": [], "never": [],
                 "tags": [], "notes": "", "refs": [], "examples": [], "created": now_iso(),
                 "_dir": proj / "guide" / k / s}
            created = True
        if description is not None:
            e["description"] = description.strip()
        for key, val in (("must_have", must_have), ("never", never), ("tags", tags)):
            if val is not None:
                e[key] = [x.strip() for x in val if x and x.strip()]
        if notes is not None:
            e["notes"] = notes.strip()
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
@tool
async def generate_image(prompt: str, entries: list[str] | None = None, project: str = "", aspect: str = "portrait",
                         width: int = 0, height: int = 0, seed: int = -1, candidates: int = 0,
                         use_references: bool = True, reference_mode: str = "style", use_default_style: bool = True,
                         steps: int = 4, preview_size: int = 512, dry_run: bool = False) -> list:
    """Render N candidate images for one request with FLUX.2 klein 4B (local ComfyUI). You then MUST call
    pick_candidate with the best one; only the picked image is presented to the user.

    prompt: the scene: action, pose, composition, background. Don't restate guide entries; their fixed
            descriptions are added automatically.
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
    Max 4 reference images per image: attached ones first, then characters/creatures, style, locations, props.
    dry_run: only return the composed prompt, references and checklist (nothing is rendered).
    Starts Docker/ComfyUI automatically if needed (and frees the GPU from Ollama).
    """
    global BUSY
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
    if width and height:
        w, h = width, height
    elif aspect in ASPECTS:
        w, h = ASPECTS[aspect]
    else:
        raise ValueError(f"unknown aspect '{aspect}'; use one of {list(ASPECTS)} or width+height")
    w, h = (max(256, min(2048, v)) // 16 * 16 for v in (w, h))
    if reference_mode not in ("style", "edit"):
        raise ValueError("reference_mode must be 'style' or 'edit'")
    plan = _plan(prompt, entries or [], use_references, reference_mode, use_default_style)
    checklist = ("\nChecklist (judge the candidates against it):\n" + "\n".join(f"- {c}" for c in plan["checklist"])
                 if plan["checklist"] else "")
    ref_lines = "\n".join(f"  image {i}: {r['source']}" for i, r in enumerate(plan["refs"], 1)) or "  none"
    notes = ("\nNotes: " + "; ".join(plan["notes"])) if plan["notes"] else ""
    if dry_run:
        return [f"DRY RUN for {_mode_text()} ({w}x{h}).\nReferences:\n{ref_lines}{notes}{checklist}\n\n"
                f"Full prompt:\n{plan['text']}"]
    n = max(1, min(MAX_CANDIDATES, candidates or _candidate_count()))
    BUSY += 1
    try:
        startup = await _backend_ready()
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
                png = await run_workflow(c, build_workflow(plan["text"], ref_names, w, h, s, steps, ref_mp))
                _write_bytes(rdir / f"cand_{i}.png", png)
                cands.append({"number": i, "file": f"cand_{i}.png", "seed": s,
                              "seconds": round(time.monotonic() - t0, 1)})
                out += [f"Candidate {i} (seed {s})", _preview(png, preview_size)]
    finally:
        BUSY -= 1
    meta = {"request_id": rid, "created": now_iso(), "project": plan["project"], "prompt": prompt,
            "full_prompt": plan["text"], "entries": plan["entries"], "checklist": plan["checklist"],
            "width": w, "height": h, "steps": steps, "model": MODEL,
            "reference_mode": reference_mode if plan["refs"] else None,
            "references": [{"source": r["source"], **({"file": r["file"]} if r.get("file") else {})}
                           for r in plan["refs"]],
            "candidates": cands, "picked": None}
    _save(rdir / "request.json", meta)
    S["requests"][rid] = meta
    S["last_request"] = rid
    _save_session()
    head = (f"{'[' + startup + '] ' if startup else ''}Request {rid} ({_mode_text()}): {n} candidates, {w}x{h}, "
            f"{time.monotonic() - t_all:.0f} s total.\nReferences:\n{ref_lines}{notes}{checklist}\n"
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
async def clear_session(stop: bool = True) -> str:
    """Start fresh: discard this session's unapproved images, detach references, reset the candidate count, close
    the project (the next request asks again which project) and, if stop, stop ComfyUI and reload the Ollama
    models (only when no other Claude session is still using them). Projects, guides and approved images are kept."""
    if BUSY:
        raise ValueError("an image is still being generated in this session; clear when it has finished")
    n_open = len(S["requests"])
    for rid in list(S["requests"]):
        _close_request(rid, delete_selected=True)
    prev = _mode_text()
    S.update(active=[], candidates=None, last_request=None, project=None, scratch=False)
    _save_session()
    msg = (f"Session cleared ({prev} closed): {n_open} unapproved image(s) discarded, references detached, "
           f"{DEFAULT_CANDIDATES} candidates per request. The next request will ask which project to use.")
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
