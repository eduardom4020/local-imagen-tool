#!/usr/bin/env python3
"""Set up the local-imagen plugin on this machine (standard library only; Windows, Linux, macOS).

  python setup.py --check                  check prerequisites only (GPU, Docker + GPU access, uv)
  python setup.py                          full setup: config, Docker image, FLUX.2 klein model, start + verify
  python setup.py --models core extras     also download the comparison models (~26 GB more)
                                           (default: core + control, the 92 MB depth LoRA for pose_mode='depth')
  python setup.py --external-url URL       use an existing ComfyUI instead of Docker (no build/start)
  python setup.py --home PATH              data folder (default ~/.local-imagen)
  python setup.py --blender-only           only build the Blender pose-renderer image (existing installs)
  python setup.py --skip-blender           leave out the Blender pose renderer

Writes ~/.local-imagen/config.json (read by the MCP server). Safe to re-run: finished steps are skipped.
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[1]
CFG_DIR = Path.home() / ".local-imagen"
CFG_FILE = CFG_DIR / "config.json"
COMPOSE_PROJECT = "comfyui"
MODELS_VOLUME = f"{COMPOSE_PROJECT}_comfyui-models"
CORE_FILES = ["flux-2-klein-4b.safetensors"]
BLENDER_IMAGE = "local/imagen-blender:latest"


def step(msg):
    print(f"\n== {msg}", flush=True)


def run(cmd, cwd=None, check=True, timeout=None, quiet=False):
    """Run a command, streaming its output unless quiet. Returns (code, output)."""
    p = subprocess.Popen(cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                         encoding="utf-8", errors="replace")
    out = []
    for line in p.stdout:
        out.append(line)
        if not quiet:
            print("   " + line.rstrip(), flush=True)
    code = p.wait(timeout=timeout)
    if check and code != 0:
        sys.exit(f"FAILED ({code}): {' '.join(map(str, cmd))}\n" + "".join(out[-20:]))
    return code, "".join(out)


def http_json(url, timeout=5):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read().decode())


def docker_ready():
    try:
        code, out = run(["docker", "info", "--format", "{{.ServerVersion}}"], check=False, quiet=True, timeout=60)
        return code == 0 and out.strip()[:1].isdigit()
    except (OSError, subprocess.TimeoutExpired):
        return False


def ensure_docker():
    if docker_ready():
        return True
    pf = os.environ.get("ProgramFiles") if sys.platform == "win32" else None  # from the OS, never hardcoded
    desktop = Path(pf) / "Docker" / "Docker" / "Docker Desktop.exe" if pf else None
    if desktop and desktop.exists():
        subprocess.Popen([str(desktop)])
    elif sys.platform == "darwin":
        subprocess.Popen(["open", "-a", "Docker"])
    else:
        return False
    print("   starting Docker Desktop...", flush=True)
    for _ in range(60):
        time.sleep(3)
        if docker_ready():
            return True
    return False


def check_prereqs():
    step("Checking prerequisites")
    ok = True
    gpu = shutil.which("nvidia-smi")
    if gpu:
        _, out = run(["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader"], check=False, quiet=True)
        print(f"   GPU: {out.strip()}")
        try:
            mib = int(out.strip().split(",")[1].split()[0])
            if mib < 7500:
                print("   WARNING: less than 8 GB of GPU memory; FLUX.2 klein 4B may be very slow or fail.")
        except (IndexError, ValueError):
            pass
    else:
        print("   MISSING: nvidia-smi (an NVIDIA GPU + driver is required for the Docker setup)")
        ok = False
    if shutil.which("uv"):
        _, out = run(["uv", "--version"], check=False, quiet=True)
        print(f"   uv: {out.strip()}")
    else:
        print("   MISSING: uv (runs the MCP server). Install: https://docs.astral.sh/uv/getting-started/installation/")
        ok = False
    return ok


def check_docker_gpu():
    if not shutil.which("docker"):
        print("   MISSING: docker CLI (install Docker Desktop, or Docker Engine + NVIDIA Container Toolkit on Linux)")
        return False
    if not ensure_docker():
        print("   Docker engine is not running and could not be started.")
        return False
    code, out = run(["docker", "run", "--rm", "--gpus", "all", "python:3.12-slim", "nvidia-smi", "-L"],
                    check=False, quiet=True, timeout=900)
    if code == 0 and "GPU" in out:
        print(f"   Docker GPU access: OK ({out.strip().splitlines()[-1]})")
        return True
    print("   Docker cannot use the GPU:\n" + out[-600:])
    return False


def write_config(updates):
    CFG_DIR.mkdir(parents=True, exist_ok=True)
    cfg = {}
    if CFG_FILE.exists():
        cfg = json.loads(CFG_FILE.read_text(encoding="utf-8"))
    cfg.update({k: v for k, v in updates.items() if v is not None})
    CFG_FILE.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    print(f"   {CFG_FILE}: {json.dumps(cfg)}")
    return cfg


def wait_for_comfyui(url, seconds=240):
    for _ in range(seconds // 3):
        try:
            return http_json(f"{url}/system_stats")
        except OSError:
            time.sleep(3)
    sys.exit(f"ComfyUI did not answer at {url} within {seconds}s")


def verify_models(url):
    unets = http_json(f"{url}/object_info/UNETLoader", timeout=30)["UNETLoader"]["input"]["required"]["unet_name"][0]
    missing = [f for f in CORE_FILES if f not in unets]
    if missing:
        sys.exit(f"ComfyUI is up but model files are missing: {missing}")
    print(f"   models visible to ComfyUI: {', '.join(unets)}")


def build_blender():
    """Headless Blender for render_pose: official portable Linux build, checksum-verified, CPU only. The Dockerfile
    is piped in, so no build context is sent."""
    step("Building the Blender pose-renderer image (~1-2 min, ~400 MB download)")
    with open(PLUGIN / "docker" / "Dockerfile.blender", "rb") as fh:
        p = subprocess.Popen(["docker", "build", "-t", BLENDER_IMAGE, "-"], stdin=fh, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace")
        out = []
        for line in p.stdout:
            out.append(line)
            print("   " + line.rstrip(), flush=True)
        if p.wait() != 0:
            sys.exit("FAILED: Blender image build\n" + "".join(out[-20:]))
    code, out = run(["docker", "run", "--rm", BLENDER_IMAGE, "--version"], check=False, quiet=True, timeout=300)
    line = next((x for x in out.splitlines() if x.startswith("Blender ")), "")
    if code != 0 or not line:
        sys.exit("Blender image built but Blender did not start:\n" + out[-800:])
    print(f"   {line.strip()}: render_pose is ready")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--check", action="store_true", help="only check prerequisites")
    ap.add_argument("--home", help="data folder (default: keep current, else ~/.local-imagen)")
    ap.add_argument("--docker-dir", help="where to put docker-compose.yml (default <home>/docker)")
    ap.add_argument("--external-url", help="use an existing ComfyUI at this URL instead of Docker")
    ap.add_argument("--models", nargs="+", default=["core", "control"], choices=["core", "control", "extras"])
    ap.add_argument("--force-copy", action="store_true", help="overwrite the Docker files in docker-dir")
    ap.add_argument("--skip-build", action="store_true")
    ap.add_argument("--skip-models", action="store_true")
    ap.add_argument("--skip-blender", action="store_true", help="leave out the Blender pose renderer")
    ap.add_argument("--blender-only", action="store_true", help="only build the Blender pose-renderer image")
    args = ap.parse_args()

    if args.blender_only:
        step("Checking Docker")
        if not shutil.which("docker") or not ensure_docker():
            sys.exit("Docker is not available. Without Docker, install Blender and ask Claude to run "
                     "setup_blender(path=<blender executable>).")
        build_blender()
        print("\nBLENDER SETUP OK. render_pose works now (no restart needed).")
        return

    prereqs_ok = check_prereqs()
    if args.external_url:
        url = args.external_url.rstrip("/")
        step(f"Using existing ComfyUI at {url}")
        write_config({"home": args.home, "comfyui_url": url, "manage_docker": False})
        wait_for_comfyui(url, 30)
        verify_models(url)
        print("\nSETUP OK (external ComfyUI). Restart Claude Code so the image tools load.")
        return
    step("Checking Docker GPU access")
    docker_ok = check_docker_gpu()
    if args.check:
        print("\nCHECK " + ("OK" if prereqs_ok and docker_ok else "FAILED"))
        sys.exit(0 if prereqs_ok and docker_ok else 1)
    if not (prereqs_ok and docker_ok):
        sys.exit("Prerequisites missing (see above). Fix them, then re-run.")

    step("Writing config")
    old = json.loads(CFG_FILE.read_text(encoding="utf-8")) if CFG_FILE.exists() else {}
    home = Path(args.home or old.get("home") or CFG_DIR).expanduser()
    docker_dir = Path(args.docker_dir or old.get("docker_dir") or home / "docker").expanduser()
    cfg = write_config({"home": str(home), "docker_dir": str(docker_dir)})
    url = cfg.get("comfyui_url", "http://127.0.0.1:8188").rstrip("/")

    step(f"Docker files in {docker_dir}")
    docker_dir.mkdir(parents=True, exist_ok=True)
    for f in ("Dockerfile", "docker-compose.yml", "download_models.py"):
        if args.force_copy or not (docker_dir / f).exists():
            shutil.copy2(PLUGIN / "docker" / f, docker_dir / f)
            print(f"   copied {f}")
        else:
            print(f"   kept existing {f}")
    for d in ("output", "input", "user"):
        (docker_dir / d).mkdir(exist_ok=True)

    if not args.skip_build:
        step("Building the ComfyUI image (first time: ~10-15 min, ~13 GB)")
        run(["docker", "compose", "build"], cwd=docker_dir)

    if not args.skip_models:
        step(f"Downloading models: {', '.join(args.models)} (core ~12 GB)")
        run(["docker", "volume", "create", MODELS_VOLUME, "--label", f"com.docker.compose.project={COMPOSE_PROJECT}",
             "--label", "com.docker.compose.volume=comfyui-models"], quiet=True)
        run(["docker", "run", "--rm", "-e", "HF_HUB_DISABLE_PROGRESS_BARS=1", "-v", f"{MODELS_VOLUME}:/models",
             "-v", f"{docker_dir}:/work:ro", "python:3.12-slim", "sh", "-c",
             "pip install -q --root-user-action=ignore huggingface_hub && python /work/download_models.py "
             + " ".join(args.models)], cwd=docker_dir)

    if not args.skip_blender:
        build_blender()

    step("Starting ComfyUI and verifying")
    run(["docker", "compose", "up", "-d"], cwd=docker_dir)
    stats = wait_for_comfyui(url)
    dev = stats["devices"][0]
    print(f"   ComfyUI {stats['system'].get('comfyui_version')} on {dev['name']}")
    verify_models(url)
    run(["docker", "compose", "stop"], cwd=docker_dir, quiet=True)
    print("\nSETUP OK. ComfyUI is installed and stopped; the MCP server starts it on demand.\n"
          "Restart Claude Code (or run /reload-plugins) so the image tools and the image-picker agent load.")


if __name__ == "__main__":
    main()
