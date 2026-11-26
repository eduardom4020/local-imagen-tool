---
name: local-imagen-setup
description: Install, set up, verify or repair the local-imagen plugin (local ComfyUI in Docker + FLUX.2 klein 4B + the imagen MCP tools + the image-picker agent + the Blender pose renderer) on this machine. Use when the user says "set up / install the image generator", "set up comfyui", "set up Blender for poses", "make the image tools work on this PC", after installing the local-imagen plugin, or when the image or pose tools fail because ComfyUI, Docker, Blender or the model is missing.
---

# Set up local-imagen on this machine

The plugin root is two folders above this skill's directory (`<root>/scripts/setup.py`, `<root>/docker/`).
Everything machine-specific lives in `~/.local-imagen/` (config.json, data); the plugin itself holds no data.

## What it needs
- An NVIDIA GPU with 8 GB+ and a current driver (`nvidia-smi` works).
- Docker with GPU access: Docker Desktop (WSL2 backend) on Windows; Docker Engine + NVIDIA Container Toolkit on
  Linux. macOS has no NVIDIA GPUs: use `--external-url` with a ComfyUI running elsewhere.
- `uv` (runs the MCP server and installs its Python packages).
- About 32 GB free disk (ComfyUI image ~13 GB, model ~12 GB, Blender image ~2 GB) and ~26 GB of downloads.

Installing Docker, drivers, uv or Blender changes the system: ask the user before doing it.

## Steps
1. Check: `uv run --script "<root>/scripts/setup.py" --check` (or `python "<root>/scripts/setup.py" --check`).
   Fix anything reported missing (with the user's OK).
2. Set up: run `python "<root>/scripts/setup.py"` in the background (15-30 min the first time; it prints `SETUP OK`
   at the end). Options:
   - `--home <folder>`: the data folder.
   - `--models core control extras`: which model sets to download (default `core control`; `control` is the
     92 MB depth LoRA for `pose_mode="depth"`, `extras` the comparison models).
   - `--skip-blender`: leave out the pose renderer.
   - `--external-url http://host:8188`: use an existing ComfyUI. Then put `flux-2-klein-4b.safetensors`,
     `qwen_3_4b_fp4_flux2.safetensors` and `flux2-vae.safetensors` from Comfy-Org/Flux-2-Klein-4B in its
     diffusion_models / text_encoders / vae folders.
3. Ask the user to restart Claude Code (or run `/reload-plugins`) so the image tools load.
4. Verify:
   - Call `backend_status` and `blender_status`.
   - Render a pose with `render_pose(pose="run")` and look at it.
   - Delegate a small test image to the image-picker agent and show only its pick.

## Pose renderer (Blender)
`render_pose` draws a grey mannequin in a pose with Blender in background mode. The image-picker uses it as a
pose guide with `generate_image(pose=...)`. Blender comes from one of two places.

- **Docker (default).** `docker/Dockerfile.blender` downloads the official portable Linux build from
  download.blender.org, checks its SHA-256 and runs it on the CPU. Set it up in any of these ways:
  - the full setup script (step 2);
  - `python "<root>/scripts/setup.py" --blender-only` for an existing install;
  - the MCP tool `setup_blender()`: about 1-2 min and ~400 MB of download, no restart needed.
    `setup_blender(version="x.y.z")` picks another release; its checksum is fetched from blender.org.
- **Blender on this computer.** Use this when Docker isn't available, for example with an external ComfyUI on
  macOS.
  1. Install Blender 4.2 or newer, with the user's OK:
     - the official download at https://www.blender.org/download/ (the Windows `.zip` and the Linux `.tar.xz`
       are portable: unpack anywhere);
     - or `winget install BlenderFoundation.Blender`, `brew install --cask blender`, or
       `snap install blender --classic`.
  2. Call `setup_blender(path=<the blender executable>)`. It checks the executable and saves it as
     `blender_path` in config.json. Typical executables:
     - Windows: `C:\Program Files\Blender Foundation\Blender <x.y>\blender.exe` or the unpacked folder;
     - macOS: `/Applications/Blender.app/Contents/MacOS/Blender`;
     - Linux: the `blender` binary.
  3. `setup_blender()` without a path switches back to the Docker image.

`blender_status` shows which renderer is used, its version and the last build's log.

## Using it (for reference)
- **Project first.** At the first image request of a conversation, unless the user named a project, call
  `list_projects` and ask which one to use: an existing project, a new one (`create_project`; they may choose its
  folder with `parent_dir`) or a one-off image (`use_project(scratch=True)`).
- **Style guide by chat.** Use `guide_set`, `guide_add_reference`, `guide_show`, `guide_list` and
  `set_project_info(default_style=...)`. Each project folder has a readable `GUIDE.md`.
- **Generating.** Every image request goes to the image-picker agent with the project name and the guide entry names.
  Show the user only the picked image, with one or two short lines.
  - `approve_image` when they like it (`reference_for` also makes it a reference).
  - `show_candidates` only when they ask.
  - `clear_session` when they're done.
  - End image replies with one short line saying the image session is still up and they can ask to clear it.
- **Poses.** For action or acrobatic shots, tell the picker to use a pose guide (it renders and checks one itself).
  When the user wants to keep a pose, call `render_pose(..., save_to="<name>")` in the main conversation: it
  becomes a guide pose entry, usable as `entries=["pose/<name>"]`.
- **Edits.** When the user attaches an image with red marks, edit it; don't redraw.
  1. Call `find_marks("latest")` to see the numbered marks and their originals.
  2. Delegate to the picker with the marked image's path and one instruction per mark. It calls `edit_image`.
  3. A green line or loop next to a red mark means "put it here, this way". The picker moves it automatically.
- **Switching projects** (`use_project`) drops the previous project's working state. Don't carry anything from it into
  new prompts.

## Troubleshooting
| Symptom | Fix |
|---|---|
| "Failed to find C compiler" in ComfyUI | Rebuild the image: `docker compose build --no-cache` in the docker dir (the Dockerfile installs gcc). |
| Out of memory / very slow on 8 GB | Something else holds GPU memory (Ollama models are unloaded automatically; close games/3D apps). |
| `docker compose` not found from the MCP | Docker Desktop's CLI plugins missing from PATH; reinstall Docker Desktop or add its `resources\bin`. |
| WSL/Docker killed during model load (Windows) | Raise `memory=` in the `.wslconfig` file in the Windows user folder (16 GB+), then `wsl --shutdown`. Ask first. |
| Port 8188 busy | Another ComfyUI is running: use it with `--external-url`, or stop it. |
| "ComfyUI kept running" on stop | Another Claude session still uses it. `stop_backend(force=True)` only after the user agrees. |
| Tools missing in a session | Restart Claude Code. Meanwhile the agent can use the bridge in `~/.local-imagen/bridge.json`; pass the same `--session <id>` to every call. |
| macOS | Docker can't reach Apple GPUs. Run ComfyUI natively (it uses Metal) and point the plugin at it with `--external-url`. For poses, install Blender and call `setup_blender(path=...)`. |
| render_pose: "isn't set up yet" | Call `setup_blender()` (Docker) or `setup_blender(path=...)` (installed Blender). |
| Blender image build fails at the checksum | The download was corrupted or the version/sha256 pair doesn't match: run `setup_blender()` again, or pass the version without a sha256 so it is fetched from blender.org. |
| render_pose "failed" with a POSE_FAILED line | A bad pose value (the message says which). Fix the pose and render again. |
