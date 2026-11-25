---
name: comfyui-imagegen-setup
description: Install, set up, verify or repair the comfyui-imagegen plugin (local ComfyUI in Docker + FLUX.2 klein 4B + the comfyui MCP tools + the image-picker agent) on this machine. Use when the user says "set up / install the image generator", "set up comfyui", "make the image tools work on this PC", after installing the comfyui-imagegen plugin, or when comfyui tools fail because ComfyUI, Docker or the model is missing.
---

# Set up comfyui-imagegen on this machine

The plugin root is two folders above this skill's directory (`<root>/scripts/setup.py`, `<root>/docker/`).
Everything machine-specific lives in `~/.comfyui-mcp/` (config.json, data); the plugin itself holds no data.

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
3. Ask the user to restart Claude Code (or run `/reload-plugins`) so the comfyui tools load.
4. Verify: call `comfyui_status`, then delegate a small test image to the image-picker agent and show only its pick.

## Using it (for reference)
Every image request goes to the image-picker agent; show the user only the picked image with one or two short lines.
`approve_image` when they like it, `show_candidates` only when asked, `clear_session` when they're done. End image
replies with one short line saying the image session is still up and they can ask to clear it.

## Troubleshooting
| Symptom | Fix |
|---|---|
| "Failed to find C compiler" in ComfyUI | Rebuild the image: `docker compose build --no-cache` in the docker dir (the Dockerfile installs gcc). |
| Out of memory / very slow on 8 GB | Something else holds GPU memory (Ollama models are unloaded automatically; close games/3D apps). |
| `docker compose` not found from the MCP | Docker Desktop's CLI plugins missing from PATH; reinstall Docker Desktop or add its `resources\bin`. |
| WSL/Docker killed during model load (Windows) | Raise `memory=` in `%USERPROFILE%\.wslconfig` (16 GB+), then `wsl --shutdown`. Ask first. |
| Port 8188 busy | Another ComfyUI is running: use it with `--external-url`, or stop it. |
| Tools missing in a session | Restart Claude Code; meanwhile the agent can use the bridge in `~/.comfyui-mcp/bridge.json`. |
