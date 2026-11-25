# /// script
# requires-python = ">=3.10"
# dependencies = ["mcp>=2.3,<3", "httpx>=0.27", "pillow>=10"]
# ///
"""ComfyUI image-generation MCP server (FLUX.2 klein 4B) with detachable reference images, named styles and a
candidate -> pick -> approve workflow. Part of the local-imagen Claude Code plugin.

Run by Claude Code over stdio:  uv run --script server.py   (uv installs the dependencies above)

Settings (environment variable > ~/.local-imagen/config.json key > default):
  LOCAL_IMAGEN_HOME   / home            data folder (library/, outputs/, temp/, styles.json, state.json)
                                       default ~/.local-imagen
  COMFYUI_URL        / comfyui_url     default http://127.0.0.1:8188
  COMFYUI_DOCKER_DIR / docker_dir      folder with docker-compose.yml, default <home>/docker
  COMFYUI_MANAGE_DOCKER / manage_docker  false when ComfyUI is run some other way (no start/stop)
  OLLAMA_URL         / ollama_url      default http://127.0.0.1:11434 (models are unloaded to free the GPU)
  LOCAL_IMAGEN_CANDIDATES / candidates      candidates per request, default 3
  model                                {"unet": ..., "text_encoder": ..., "vae": ...} file names override
"""
from __future__ import annotations

import asyncio
import datetime as dt
import functools
import hashlib
import io
import json
import logging
import os
import random
import subprocess
import sys
import time
import uuid
from pathlib import Path

import httpx
from PIL import Image as PILImage
from mcp.server.mcpserver import Image, MCPServer
from mcp.server.mcpserver.exceptions import ToolError

logging.getLogger("httpx").setLevel(logging.WARNING)

# ---------------------------------------------------------------- config
CFG_DIR = Path.home() / ".local-imagen"
try:
    CFG = json.loads((CFG_DIR / "config.json").read_text(encoding="utf-8"))
except (FileNotFoundError, json.JSONDecodeError):
    CFG = {}


def setting(key: str, env: str | None, default):
    v = os.environ.get(env) if env else None
    return v if v not in (None, "") else CFG.get(key, default)


HOME = Path(setting("home", "LOCAL_IMAGEN_HOME", CFG_DIR)).expanduser()
COMFY = str(setting("comfyui_url", "COMFYUI_URL", "http://127.0.0.1:8188")).rstrip("/")
OLLAMA = str(setting("ollama_url", "OLLAMA_URL", "http://127.0.0.1:11434")).rstrip("/")
DOCKER_DIR = Path(setting("docker_dir", "COMFYUI_DOCKER_DIR", HOME / "docker")).expanduser()
MANAGE_DOCKER = str(setting("manage_docker", "COMFYUI_MANAGE_DOCKER", True)).lower() not in ("false", "0", "no")
DOCKER_DESKTOP = Path(r"C:\Program Files\Docker\Docker\Docker Desktop.exe")
LIB = HOME / "library"
OUT = HOME / "outputs"
STYLES_FILE = HOME / "styles.json"
STATE_FILE = HOME / "state.json"
TEMP = HOME / "temp"
CAND = TEMP / "candidates"   # all candidates of open requests (hidden from the user unless asked)
SEL = TEMP / "selected"      # the picked candidate of each open request, waiting for the user's approval
for d in (LIB, OUT, CAND, SEL):
    d.mkdir(parents=True, exist_ok=True)
DEFAULT_CANDIDATES = int(setting("candidates", "LOCAL_IMAGEN_CANDIDATES", 3))
MAX_CANDIDATES = 8

MODEL = {  # FLUX.2 klein 4B (distilled, Apache 2.0)
    "unet": "flux-2-klein-4b.safetensors",
    "text_encoder": "qwen_3_4b_fp4_flux2.safetensors",
    "vae": "flux2-vae.safetensors",
    **(CFG.get("model") or {}),
}

# Tell the image-picker agent how to reach this server from a shell if the MCP tools aren't loaded in a session.
try:
    CFG_DIR.mkdir(parents=True, exist_ok=True)
    (CFG_DIR / "bridge.json").write_text(json.dumps({
        "python": sys.executable,
        "mcp_call": str(Path(__file__).resolve().parent / "mcp_call.py"),
        "usage": '"<python>" "<mcp_call>" <tool_name> \'<json arguments>\'',
    }, indent=2), encoding="utf-8")
except OSError:
    pass
MAX_REFS = 4
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


def log(*a):
    print(*a, file=sys.stderr, flush=True)  # stdout is the MCP channel


# ---------------------------------------------------------------- state
def _load(p: Path, default):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def _save(p: Path, data):
    p.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def state():
    s = _load(STATE_FILE, {})
    s.setdefault("active", [])          # [{"file": "<id>.png", "label": "..."}]
    s.setdefault("unloaded_ollama", [])  # models we unloaded to free the GPU, restored on stop
    s.setdefault("candidates", None)     # per-user override of DEFAULT_CANDIDATES
    s.setdefault("requests", {})         # open generation requests: rid -> metadata
    s.setdefault("last_request", None)
    return s


def styles():
    return _load(STYLES_FILE, {})


def describe_refs(refs):
    if not refs:
        return "No reference images attached (plain text-to-image)."
    return "Attached references:\n" + "\n".join(f"  {i}. {r['label']}  [{r['file']}]" for i, r in enumerate(refs, 1))


# ---------------------------------------------------------------- reference library
def _import_image(src: str) -> list[dict]:
    """Copy an image file / every image in a folder / an http(s) image into the library (normalized PNG)."""
    items = []
    if src.lower().startswith(("http://", "https://")):
        r = httpx.get(src, timeout=60, follow_redirects=True)
        r.raise_for_status()
        items.append((Path(src.split("?")[0]).name or "web-image", r.content))
    else:
        p = Path(src).expanduser()
        if p.is_dir():
            files = sorted(f for f in p.iterdir() if f.suffix.lower() in IMG_EXT)
            if not files:
                raise ValueError(f"no images found in folder {p}")
            items += [(f.name, f.read_bytes()) for f in files]
        elif p.is_file():
            items.append((p.name, p.read_bytes()))
        else:
            raise ValueError(f"not found: {src}")
    out = []
    for name, data in items:
        im = PILImage.open(io.BytesIO(data))
        im.load()
        im = im.convert("RGBA") if im.mode in ("RGBA", "LA", "P") else im.convert("RGB")
        if max(im.size) > 2048:
            im.thumbnail((2048, 2048), PILImage.LANCZOS)
        buf = io.BytesIO()
        im.save(buf, "PNG")
        png = buf.getvalue()
        fid = hashlib.sha1(png).hexdigest()[:12] + ".png"
        if not (LIB / fid).exists():
            (LIB / fid).write_bytes(png)
        out.append({"file": fid, "label": Path(name).stem[:60]})
    return out


def _thumb(path: Path, size=256) -> Image:
    im = PILImage.open(path).convert("RGB")
    im.thumbnail((size, size), PILImage.LANCZOS)
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=80)
    return Image(data=buf.getvalue(), format="jpeg")


# ---------------------------------------------------------------- ComfyUI + Docker + Ollama plumbing
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
        # its plugins (compose) under C:\Program Files\Docker\cli-plugins.
        env.setdefault("ProgramFiles", r"C:\Program Files")
        env.setdefault("ProgramW6432", r"C:\Program Files")
        env.setdefault("ProgramData", r"C:\ProgramData")
        env.setdefault("ALLUSERSPROFILE", r"C:\ProgramData")
        docker_bin = r"C:\Program Files\Docker\Docker\resources\bin"
        if docker_bin.lower() not in env.get("PATH", "").lower():
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


async def _ollama_unload() -> list[str]:
    """Unload Ollama models from the GPU (8 GB card can't hold both); remember them for restore."""
    names = []
    try:
        async with httpx.AsyncClient(timeout=30) as c:
            models = (await c.get(f"{OLLAMA}/api/ps")).json().get("models", [])
            st = state()
            for m in models:
                exp = m.get("expires_at", "")
                keep = -1 if exp[:4].isdigit() and int(exp[:4]) >= 2100 else "5m"
                await c.post(f"{OLLAMA}/api/generate", json={"model": m["name"], "keep_alive": 0})
                if m["name"] not in [u["name"] for u in st["unloaded_ollama"]]:
                    st["unloaded_ollama"].append({"name": m["name"], "keep_alive": keep})
                names.append(m["name"])
            _save(STATE_FILE, st)
    except httpx.HTTPError:
        pass  # Ollama not running: nothing to unload
    return names


async def _ollama_restore() -> list[str]:
    st = state()
    restored = []
    async with httpx.AsyncClient(timeout=300) as c:
        for m in st["unloaded_ollama"]:
            try:
                await c.post(f"{OLLAMA}/api/generate", json={"model": m["name"], "keep_alive": m["keep_alive"]})
                restored.append(m["name"])
            except httpx.HTTPError as e:
                log("ollama restore failed", m, e)
    st["unloaded_ollama"] = []
    _save(STATE_FILE, st)
    return restored


async def free_gpu_from_ollama(notes: list):
    unloaded = await _ollama_unload()
    if unloaded:
        notes.append("unloaded Ollama models from the GPU (stop_backend reloads them): " + ", ".join(unloaded))


async def ensure_comfyui(free_gpu=True) -> str:
    notes = []
    if not await comfy_up():
        await _start_container(notes)
    if free_gpu:  # only after ComfyUI is confirmed up, so a failed start doesn't unload Ollama for nothing
        await free_gpu_from_ollama(notes)
    return "; ".join(notes) or "ComfyUI already running"


async def _start_container(notes: list):
    if not MANAGE_DOCKER:
        raise RuntimeError(f"ComfyUI is not answering at {COMFY} and manage_docker is off: start ComfyUI yourself")
    if not (DOCKER_DIR / "docker-compose.yml").exists():
        raise RuntimeError(f"no docker-compose.yml in {DOCKER_DIR}: run the local-imagen-setup skill first")
    if not await asyncio.to_thread(_docker_ready):
        if sys.platform == "win32" and DOCKER_DESKTOP.exists():
            subprocess.Popen([str(DOCKER_DESKTOP)], env=_env(),
                             creationflags=getattr(subprocess, "DETACHED_PROCESS", 0))
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


async def upload_ref(c: httpx.AsyncClient, ref: dict) -> str:
    data = (LIB / ref["file"]).read_bytes()
    r = await c.post(f"{COMFY}/upload/image", files={"image": (ref["file"], data, "image/png")},
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


# ---------------------------------------------------------------- MCP server + tools
mcp = MCPServer(
    "local-imagen",
    instructions=(
        "Local image generation (ComfyUI, FLUX.2 klein 4B, ~9 s/image, ~16 s with references) on the user's "
        "RTX 4060. By default generate_image is plain text-to-image with NO references. Reference images are "
        "detachable: attach_reference / detach_reference / clear_references, or save the current set as a named "
        "style (save_style) and switch with use_style. reference_mode='style' copies only the art style; "
        "'edit' uses the references as content to edit or combine.\n"
        "CANDIDATE WORKFLOW: every generate_image call renders N candidates (default 3, set_candidate_count) "
        "and the best one is chosen with pick_candidate. Candidate images must never reach the user's chat: in "
        "the main conversation, do NOT call generate_image yourself - delegate each request to the "
        "'image-picker' subagent (listed as local-imagen:image-picker when installed as a plugin), which "
        "generates, judges privately and returns only the picked file. (If you "
        "ARE the image-picker, generate and pick yourself.) Then show the user only the picked image by reading "
        "temp/selected/<request>.png, with at most one or two short lines. Use show_candidates only when the "
        "user explicitly asks. When the user likes the pick, call approve_image (moves it to outputs/); "
        "discard_image drops it. End with one short line that the image session is still up and they can ask "
        "you to clear it (clear_session) - no explanation about ComfyUI or Ollama."
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


def _preview(png: bytes, size: int) -> Image:
    im = PILImage.open(io.BytesIO(png)).convert("RGB")
    im.thumbnail((size, size), PILImage.LANCZOS)
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=85)
    return Image(data=buf.getvalue(), format="jpeg")


def _candidate_count(st) -> int:
    return st["candidates"] or DEFAULT_CANDIDATES


def _open_request(st, request_id: str) -> tuple[str, dict]:
    rid = request_id or st["last_request"]
    if not rid or rid not in st["requests"]:
        open_ids = ", ".join(st["requests"]) or "none"
        raise ValueError(f"no open request '{request_id or '(latest)'}'. Open requests: {open_ids}")
    return rid, st["requests"][rid]


def _drop_candidates(rid: str):
    d = CAND / rid
    if d.exists():
        for f in d.iterdir():
            f.unlink()
        d.rmdir()


def _close_request(st, rid: str, delete_selected: bool):
    _drop_candidates(rid)
    if delete_selected:
        for f in (SEL / f"{rid}.png", SEL / f"{rid}.json"):
            f.unlink(missing_ok=True)
    st["requests"].pop(rid, None)
    if st["last_request"] == rid:
        st["last_request"] = next(reversed(st["requests"]), None) if st["requests"] else None


@tool
async def generate_image(prompt: str, aspect: str = "portrait", width: int = 0, height: int = 0, seed: int = -1,
                         candidates: int = 0, use_references: bool = True, reference_mode: str = "style",
                         steps: int = 4, preview_size: int = 512) -> list:
    """Render N candidate images for one request with FLUX.2 klein 4B (local ComfyUI). You then MUST call
    pick_candidate with the best one; only the picked image is presented to the user.

    prompt: what to draw (describe subject, pose, outfit, background, and style if no references are attached).
    aspect: portrait (896x1152), square (1024x1024), landscape (1152x896), tall (768x1344), wide (1344x768).
    width/height: explicit size (multiples of 16, overrides aspect).
    seed: -1 = random (candidates use seed, seed+1, ...). candidates: 0 = current default (3 unless changed).
    use_references: False ignores attached references for this call only (they stay attached).
    reference_mode: 'style' = copy only the art style of the references (new subject from the prompt);
                    'edit' = prompt is passed as-is and may refer to 'image 1', 'image 2' (edit/combine content).
    steps: 4 is the distilled default; preview_size: max side of the candidate previews returned to you.
    Starts Docker/ComfyUI automatically if needed (and frees the GPU from Ollama).
    """
    st = state()
    n = max(1, min(MAX_CANDIDATES, candidates or _candidate_count(st)))
    if width and height:
        w, h = width, height
    elif aspect in ASPECTS:
        w, h = ASPECTS[aspect]
    else:
        raise ValueError(f"unknown aspect '{aspect}'; use one of {list(ASPECTS)} or width+height")
    w, h = (max(256, min(2048, v)) // 16 * 16 for v in (w, h))
    if reference_mode not in ("style", "edit"):
        raise ValueError("reference_mode must be 'style' or 'edit'")
    notes = []
    if not await comfy_up():
        await _start_container(notes)
    await free_gpu_from_ollama(notes)  # an Ollama model may have been reloaded since ComfyUI started
    startup = "; ".join(notes)
    refs = st["active"] if use_references else []
    text = (STYLE_PREFIX + prompt) if refs and reference_mode == "style" else prompt
    ref_mp = 1.0 if len(refs) <= 2 else 0.6
    seed = random.randrange(2**31) if seed < 0 else seed
    rid = f"{dt.datetime.now():%m%d-%H%M%S}"
    while rid in st["requests"] or (CAND / rid).exists():
        rid += "x"
    rdir = CAND / rid
    rdir.mkdir(parents=True)
    cands, out = [], []
    t_all = time.monotonic()
    async with httpx.AsyncClient(timeout=120) as c:
        ref_names = [await upload_ref(c, r) for r in refs]
        for i in range(1, n + 1):
            s = seed + i - 1
            t0 = time.monotonic()
            png = await run_workflow(c, build_workflow(text, ref_names, w, h, s, steps, ref_mp))
            (rdir / f"cand_{i}.png").write_bytes(png)
            cands.append({"number": i, "file": f"cand_{i}.png", "seed": s, "seconds": round(time.monotonic() - t0, 1)})
            out += [f"Candidate {i} (seed {s})", _preview(png, preview_size)]
    meta = {"request_id": rid, "created": dt.datetime.now().isoformat(timespec="seconds"), "prompt": prompt,
            "full_prompt": text, "width": w, "height": h, "steps": steps, "model": MODEL,
            "reference_mode": reference_mode if refs else None, "references": refs, "candidates": cands,
            "picked": None}
    _save(rdir / "request.json", meta)
    st = state()  # re-read: other calls may have changed it while rendering
    st["requests"][rid] = meta
    st["last_request"] = rid
    _save(STATE_FILE, st)
    head = (f"{'[' + startup + '] ' if startup else ''}Request {rid}: {n} candidates, {w}x{h}, "
            f"{len(refs)} reference(s){' (' + reference_mode + ' mode)' if refs else ''}, "
            f"{time.monotonic() - t_all:.0f} s total.\n"
            f"Now call pick_candidate(request_id='{rid}', number=<best>). Present only the picked image; "
            "do not show or mention the other candidates unless the user asks.")
    return [head, *out]


@tool
async def pick_candidate(request_id: str, number: int, reason: str = "") -> str:
    """Choose the best candidate of a request. It is copied to temp/selected/ and waits for the user's approval
    (approve_image). The other candidates stay hidden until the request is approved, discarded or the session is
    cleared, then they are deleted. Can be called again to switch the pick while the request is open."""
    st = state()
    rid, req = _open_request(st, request_id)
    if not (CAND / rid / f"cand_{number}.png").exists():
        raise ValueError(f"request {rid} has candidates 1-{len(req['candidates'])}")
    dest = SEL / f"{rid}.png"
    dest.write_bytes((CAND / rid / f"cand_{number}.png").read_bytes())
    req["picked"] = number
    req["pick_reason"] = reason
    cand = next(c for c in req["candidates"] if c["number"] == number)
    _save(SEL / f"{rid}.json", {**req, "seed": cand["seed"]})
    _save(STATE_FILE, st)
    return (f"Picked candidate {number} of {rid} (seed {cand['seed']}). Waiting for approval at: {dest}\n"
            "Show the user only this image. approve_image when they like it, discard_image if not.")


@tool
async def show_candidates(request_id: str = "", preview_size: int = 512) -> list:
    """Show ALL candidates of a request (default: latest). Use ONLY when the user explicitly asks to see them."""
    st = state()
    rid, req = _open_request(st, request_id)
    out = [f"Request {rid}: {len(req['candidates'])} candidates; picked: {req.get('picked') or 'none yet'}"]
    for c in req["candidates"]:
        p = CAND / rid / c["file"]
        if p.exists():
            mark = " (picked)" if c["number"] == req.get("picked") else ""
            out += [f"Candidate {c['number']}{mark} - seed {c['seed']} - {p}", _preview(p.read_bytes(), preview_size)]
    return out


@tool
async def approve_image(request_id: str = "", name: str = "") -> str:
    """The user likes the picked image: move it from temp to outputs/<date>/ (optional file name), delete the
    other candidates and close the request. Default request: the latest."""
    st = state()
    rid, req = _open_request(st, request_id)
    sel = SEL / f"{rid}.png"
    if not req.get("picked") or not sel.exists():
        raise ValueError(f"request {rid} has no picked candidate yet; call pick_candidate first")
    day = OUT / dt.date.today().isoformat()
    day.mkdir(exist_ok=True)
    seed = next(c["seed"] for c in req["candidates"] if c["number"] == req["picked"])
    stem = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in name).strip("_") or f"{rid}_{seed}"
    k, final = 1, day / f"{stem}.png"
    while final.exists():
        k += 1
        final = day / f"{stem}_{k}.png"
    sel.replace(final)
    _save(final.with_suffix(".json"), {**{k2: v for k2, v in req.items() if k2 != "candidates"}, "seed": seed})
    _close_request(st, rid, delete_selected=True)
    _save(STATE_FILE, st)
    return f"Saved: {final}"


@tool
async def discard_image(request_id: str = "") -> str:
    """Drop a request: delete its picked image and all its candidates. Default request: the latest."""
    st = state()
    rid, _ = _open_request(st, request_id)
    _close_request(st, rid, delete_selected=True)
    _save(STATE_FILE, st)
    return f"Discarded request {rid}."


@tool
async def list_pending() -> str:
    """List open requests: picked images waiting for approval (and requests not picked yet)."""
    st = state()
    if not st["requests"]:
        return "No open requests."
    lines = []
    for rid, r in st["requests"].items():
        status = f"picked #{r['picked']} -> {SEL / (rid + '.png')}" if r.get("picked") else "not picked yet"
        lines.append(f"- {rid}: {status} | {r['prompt'][:80]}")
    return "\n".join(lines)


@tool
async def set_candidate_count(count: int) -> str:
    """Set how many candidates each request renders (1-8; default 3). Persists until clear_session."""
    st = state()
    st["candidates"] = max(1, min(MAX_CANDIDATES, count))
    _save(STATE_FILE, st)
    return f"Each request now renders {st['candidates']} candidates."


@tool
async def clear_session(stop: bool = True) -> str:
    """Start fresh: discard unapproved requests (temp), detach references, reset the candidate count to the
    default and, if stop, stop ComfyUI and reload the Ollama models it unloaded. Saved styles and approved
    outputs are kept."""
    st = state()
    n_open = len(st["requests"])
    for rid in list(st["requests"]):
        _close_request(st, rid, delete_selected=True)
    st["active"], st["candidates"], st["last_request"] = [], None, None
    _save(STATE_FILE, st)
    msg = f"Session cleared: {n_open} unapproved request(s) discarded, references detached, {DEFAULT_CANDIDATES} candidates per request."
    if stop:
        msg += " " + await stop_backend(restore_ollama=True)
    return msg


@tool
async def attach_reference(paths: list[str], replace: bool = False) -> list:
    """Attach reference image(s) that steer the next generations (until detached).

    paths: image files, folders (all images inside) or http(s) image URLs. Images are copied into the
    reference library, so the originals can be moved or deleted. replace=True clears current references first.
    Max 4 attached at once (each one adds ~3-5 s per image).
    """
    st = state()
    if replace:
        st["active"] = []
    added = []
    for p in paths:
        added += _import_image(p)
    for ref in added:
        if ref["file"] not in [r["file"] for r in st["active"]]:
            st["active"].append(ref)
    if len(st["active"]) > MAX_REFS:
        raise ValueError(f"too many references ({len(st['active'])}); max is {MAX_REFS}. Nothing was changed.")
    _save(STATE_FILE, st)
    return [f"Added {len(added)} image(s).\n" + describe_refs(st["active"]),
            *[_thumb(LIB / r["file"], 192) for r in added]]


@tool
async def detach_reference(reference: str) -> str:
    """Detach one reference: its number from list_references (1-based), its label, or its file id."""
    st = state()
    refs = st["active"]
    hit = None
    if reference.isdigit() and 1 <= int(reference) <= len(refs):
        hit = refs[int(reference) - 1]
    else:
        hit = next((r for r in refs if reference in (r["label"], r["file"], r["file"].removesuffix(".png"))), None)
    if not hit:
        raise ValueError(f"no attached reference matches '{reference}'.\n" + describe_refs(refs))
    refs.remove(hit)
    _save(STATE_FILE, st)
    return f"Detached '{hit['label']}'.\n" + describe_refs(refs)


@tool
async def clear_references() -> str:
    """Detach all reference images (back to plain text-to-image). The library and saved styles are kept."""
    st = state()
    n = len(st["active"])
    st["active"] = []
    _save(STATE_FILE, st)
    return f"Detached {n} reference(s). Generations are plain text-to-image now."


@tool
async def list_references(show_thumbnails: bool = True) -> list:
    """List the attached reference images (and show small thumbnails)."""
    refs = state()["active"]
    out = [describe_refs(refs)]
    if show_thumbnails:
        out += [_thumb(LIB / r["file"], 192) for r in refs]
    return out


@tool
async def save_style(name: str, description: str = "") -> str:
    """Save the currently attached references as a named style (e.g. 'tactics-watercolor', 'realistic').
    Overwrites a style with the same name."""
    refs = state()["active"]
    if not refs:
        raise ValueError("no references attached; attach some first")
    s = styles()
    s[name] = {"refs": refs, "description": description, "saved": dt.datetime.now().isoformat(timespec="seconds")}
    _save(STYLES_FILE, s)
    return f"Saved style '{name}' with {len(refs)} reference(s)."


@tool
async def use_style(name: str, mode: str = "replace") -> list:
    """Attach a saved style's references. mode='replace' (default) swaps out the current references,
    'add' appends them (max 4 total)."""
    s = styles()
    if name not in s:
        raise ValueError(f"unknown style '{name}'. Saved styles: {', '.join(s) or 'none'}")
    st = state()
    cur = [] if mode == "replace" else st["active"]
    merged = cur + [r for r in s[name]["refs"] if r["file"] not in [c["file"] for c in cur]]
    if len(merged) > MAX_REFS:
        raise ValueError(f"would attach {len(merged)} references; max is {MAX_REFS}")
    st["active"] = merged
    _save(STATE_FILE, st)
    return [f"Using style '{name}'. " + describe_refs(merged), *[_thumb(LIB / r["file"], 160) for r in merged]]


@tool
async def list_styles() -> str:
    """List saved styles (name, number of references, description)."""
    s = styles()
    if not s:
        return "No saved styles yet."
    return "\n".join(f"- {k}: {len(v['refs'])} ref(s){' - ' + v['description'] if v.get('description') else ''}"
                     for k, v in s.items())


@tool
async def delete_style(name: str) -> str:
    """Delete a saved style (the images stay in the library)."""
    s = styles()
    if name not in s:
        raise ValueError(f"unknown style '{name}'")
    del s[name]
    _save(STYLES_FILE, s)
    return f"Deleted style '{name}'."


@tool
async def backend_status() -> str:
    """Show whether ComfyUI is up, GPU memory, attached references and Ollama models holding the GPU."""
    lines = []
    if await comfy_up():
        async with httpx.AsyncClient(timeout=10) as c:
            ss = (await c.get(f"{COMFY}/system_stats")).json()
        dev = ss["devices"][0]
        lines.append(f"ComfyUI {ss['system'].get('comfyui_version')} up at {COMFY}; "
                     f"GPU free {dev['vram_free'] / 2**30:.1f} / {dev['vram_total'] / 2**30:.1f} GB")
    else:
        lines.append("ComfyUI is not running (generate_image or start_backend will start it).")
    try:
        async with httpx.AsyncClient(timeout=5) as c:
            models = (await c.get(f"{OLLAMA}/api/ps")).json().get("models", [])
        lines.append("Ollama models in memory: " + (", ".join(m["name"] for m in models) or "none"))
    except httpx.HTTPError:
        lines.append("Ollama not reachable.")
    lines.append(describe_refs(state()["active"]))
    return "\n".join(lines)


@tool
async def start_backend(free_gpu: bool = True) -> str:
    """Start Docker Desktop (if needed) and the ComfyUI container. free_gpu unloads Ollama models from the GPU
    (they are reloaded by stop_backend)."""
    return await ensure_comfyui(free_gpu)


@tool
async def stop_backend(restore_ollama: bool = True) -> str:
    """Stop the ComfyUI container (models stay installed) and reload any Ollama models start_backend unloaded."""
    if await comfy_up():
        try:
            async with httpx.AsyncClient(timeout=30) as c:
                await c.post(f"{COMFY}/free", json={"unload_models": True, "free_memory": True})
        except httpx.HTTPError:
            pass
    if MANAGE_DOCKER and (DOCKER_DIR / "docker-compose.yml").exists():
        r = await asyncio.to_thread(_run, ["docker", "compose", "stop"], str(DOCKER_DIR), 300)
        msg = "ComfyUI stopped." if r.returncode == 0 else "docker compose stop: " + (r.stderr or r.stdout)[-400:]
    else:
        msg = "ComfyUI models unloaded (ComfyUI is not managed by this server, so it keeps running)."
    if restore_ollama:
        restored = await _ollama_restore()
        if restored:
            msg += " Reloaded Ollama: " + ", ".join(restored)
    return msg


if __name__ == "__main__":
    mcp.run("stdio")
