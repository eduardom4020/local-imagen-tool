# comfyui-imagegen: local image generation for Claude Code

A Claude Code plugin that gives Claude its own image generator, running on your NVIDIA GPU:

- **comfyui MCP server**: ComfyUI in Docker with **FLUX.2 klein 4B** (Apache 2.0). It takes about 9 s per
  image on an RTX 4060 laptop GPU.
- **Detachable reference images and named styles**: plain text-to-image by default. Attach references to set an
  art style (or to edit their content), save them as a named style, and switch styles in one command.
- **Private best-of-N**: every request renders 3 candidates (configurable). The **image-picker** agent judges them
  out of sight, and you only see the best one. Approve it and it moves to your outputs folder. You can ask to see
  the candidates.
- **Setup skill + script**: builds ComfyUI, downloads the model and checks that everything works on a new machine.

## Install (any machine)

Requirements: an NVIDIA GPU (8 GB+), Docker with GPU access (Docker Desktop on Windows; Docker Engine + NVIDIA
Container Toolkit on Linux), [uv](https://docs.astral.sh/uv/), and about 30 GB of disk.

```
claude plugin marketplace add <this folder, or owner/repo on GitHub>
claude plugin install comfyui-imagegen@comfyui-imagegen-market
```

Then start Claude Code and say **"set up the comfyui image generator"**. The setup skill checks the prerequisites
and runs `plugins/comfyui-imagegen/scripts/setup.py`, which builds the image and downloads the model (15-30 min
the first time). Restart Claude Code when it finishes.

To use a ComfyUI you already run, use `setup.py --external-url http://host:8188` (no Docker needed).

## Use

Just ask Claude for images:
- "Draw a dwarven blacksmith, full body, watercolor concept art."
- "Attach these images as references and save them as style *anime-cel*." / "Use style anime-cel."
- "Show me the candidates." / "I like it, save it as blacksmith_v1." / "Clear the session."

Approved images go to `<home>/outputs/<date>/`, each with a JSON file holding the prompt, seed and references, so
any image can be reproduced.

## Configuration: `~/.comfyui-mcp/config.json`

| Key | Default | Meaning |
|---|---|---|
| `home` | `~/.comfyui-mcp` | data: `library/` (references), `styles.json`, `temp/` (candidates, picks), `outputs/` |
| `docker_dir` | `<home>/docker` | the docker-compose project (copied there by setup) |
| `comfyui_url` | `http://127.0.0.1:8188` | ComfyUI address |
| `manage_docker` | `true` | `false` = ComfyUI is run some other way (no start/stop) |
| `ollama_url` | `http://127.0.0.1:11434` | Ollama models are unloaded from the GPU while generating and reloaded on stop |
| `candidates` | `3` | candidates per request |
| `model` | FLUX.2 klein 4B files | override the `unet` / `text_encoder` / `vae` file names |

Environment variables override the file (`COMFYUI_MCP_HOME`, `COMFYUI_URL`, `COMFYUI_DOCKER_DIR`,
`COMFYUI_MANAGE_DOCKER`, `OLLAMA_URL`, `COMFYUI_CANDIDATES`).

## Update / remove
```
claude plugin marketplace update comfyui-imagegen-market && claude plugin update comfyui-imagegen@comfyui-imagegen-market
claude plugin uninstall comfyui-imagegen@comfyui-imagegen-market
```
Data in `~/.comfyui-mcp` (and the Docker image/volume `comfyui_comfyui-models`) is kept until you delete it.

## Layout
```
.claude-plugin/marketplace.json
plugins/comfyui-imagegen/
  .claude-plugin/plugin.json      manifest
  .mcp.json                       starts server/server.py with uv
  agents/image-picker.md          private best-of-N judge
  skills/comfyui-imagegen-setup/  install / verify / troubleshoot
  server/server.py, mcp_call.py   MCP server + shell bridge
  scripts/setup.py                machine setup (stdlib only)
  docker/                         Dockerfile, docker-compose.yml, download_models.py
```

## Licences
- Plugin code: yours to share.
- FLUX.2 klein 4B: Apache 2.0, so images can be used commercially.
- Check the rights on any reference images you attach.
