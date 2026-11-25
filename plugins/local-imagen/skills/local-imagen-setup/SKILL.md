---
name: local-imagen-setup
description: Install, set up, verify or repair the local-imagen plugin (local ComfyUI in Docker + FLUX.2 klein 4B + the imagen MCP tools + the image-picker agent) on this machine. Use when the user says "set up / install the image generator", "set up comfyui", "make the image tools work on this PC", after installing the local-imagen plugin, or when the image tools fail because ComfyUI, Docker or the model is missing.
---

# Set up local-imagen on this machine

The plugin root is two folders above this skill's directory (`<root>/scripts/setup.py`, `<root>/docker/`).
Everything machine-specific lives in `~/.local-imagen/` (config.json, data); the plugin itself holds no data.

## What it needs
- An NVIDIA GPU with 8 GB+ and a current driver (`nvidia-smi` works).
- Docker with GPU access: Docker Desktop (WSL2 backend) on Windows; Docker Engine + NVIDIA Container Toolkit on
  Linux. macOS has no NVIDIA GPUs: use `--external-url` with a ComfyUI running elsewhere.
- `uv` (runs the MCP server and installs its Python packages).
- About 30 GB free disk (image ~13 GB, model ~12 GB) and ~25 GB of downloads.

Installing Docker, drivers or uv changes the system: ask the user before doing it.

## Steps
1. Check: `uv run --script "<root>/scripts/setup.py" --check` (or `python "<root>/scripts/setup.py" --check`).
   Fix anything reported missing (with the user's OK).
2. Set up: run `python "<root>/scripts/setup.py"` in the background (15-30 min the first time; it prints `SETUP OK`
   at the end). Options: `--home <folder>` for the data folder, `--models core extras` for the comparison models,
   `--external-url http://host:8188` to use an existing ComfyUI (then put `flux-2-klein-4b.safetensors`,
   `qwen_3_4b_fp4_flux2.safetensors` and `flux2-vae.safetensors` from Comfy-Org/Flux-2-Klein-4B in its
   diffusion_models / text_encoders / vae folders).
3. Ask the user to restart Claude Code (or run `/reload-plugins`) so the image tools load.
4. Verify: call `backend_status`, then delegate a small test image to the image-picker agent and show only its pick.

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
| macOS | Docker can't reach Apple GPUs. Run ComfyUI natively (it uses Metal) and point the plugin at it with `--external-url`. |
