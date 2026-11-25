# local-imagen-tool: local image generation for Claude Code

A Claude Code plugin that gives Claude its own image generator, running on your NVIDIA GPU:

- **imagen MCP server**: ComfyUI in Docker with **FLUX.2 klein 4B** (Apache 2.0). It takes about 9 s per
  image on an RTX 4060 laptop GPU.
- **Game projects with a style guide**: each project holds a guide of named entries: styles, characters,
  creatures, locations, props, poses, cameras, or any kind you need. Each entry has a fixed description,
  must-show / never checklists and reference images. You manage it by chatting with Claude. Images are generated
  from entry names, so the same wording and references are used every time.
- **Private best-of-N**: every request renders 3 candidates (configurable). The **image-picker** agent judges them
  out of sight against the guide's checklist, and you only see the best one. Approve it and it is filed in the
  project. You can ask to see the candidates.
- **Isolated sessions**: every Claude session has its own image session. Switching projects carries nothing over,
  and two sessions can work at the same time, even on the same project.
- **Setup skill + script**: builds ComfyUI, downloads the model and checks that everything works on a new machine.

## Install (any machine)

Requirements: an NVIDIA GPU (8 GB+), Docker with GPU access (Docker Desktop on Windows; Docker Engine + NVIDIA
Container Toolkit on Linux), [uv](https://docs.astral.sh/uv/), and about 30 GB of disk. On macOS, Docker can't
reach the GPU: run ComfyUI natively (it uses Metal) and set it up with `setup.py --external-url`.

```
claude plugin marketplace add eduardom4020/local-imagen-tool
claude plugin install local-imagen@local-imagen-tool
```

Then start Claude Code and say **"set up the local image generator"**. The setup skill checks the prerequisites
and runs `plugins/local-imagen/scripts/setup.py`, which builds the image and downloads the model (15-30 min
the first time). Restart Claude Code when it finishes.

To use a ComfyUI you already run, use `setup.py --external-url http://host:8188` (no Docker needed).

## Use

Just talk to Claude. At the first image request of a conversation, Claude asks which project you're working on
unless you've already named it. The choices are an existing project, a new one, or a one-off image without a
project.

- "List my projects." / "New project *Ember Tactics*, put it in my game repo's `art` folder."
- "Add a character *Aldric*: young knight, short silver hair, blue tabard; he always has a scar on his left cheek
  and never wears a helmet." / "Use this image as Aldric's reference."
- "Aldric's concept: proud and stubborn, fights defensively behind his shield, the scar is from his first
  duel." Concepts are never sent to the image model: the picker uses them to choose poses and expressions
  that fit (a text-to-image model would draw story words literally).
- "Add a style *watercolor* with these images and make it the default." / "Add a camera *tactics*: isometric 3/4
  view from above."
- "Draw Aldric guarding the gate of Frost Keep, tactics camera."
- "I like it, save it as aldric_gate and use it as a reference for Aldric." / "Show me the candidates."
- "Switch to project *Space Game*." / "Clear the session."

## Projects

New projects go to `<your Documents folder>/local-imagen-projects/` by default. On Windows that's the real
Documents folder, even if OneDrive moved it; on macOS `~/Documents`; on Linux the XDG Documents folder. If there's
no Documents folder, they go to `<home>/projects`. You can:
- create a project in any folder you like;
- move a project later;
- change the default folder;
- register a project folder copied from another computer or cloned from git.

```
<project>/
  project.json                 name, description, default style
  GUIDE.md                     readable overview of the guide (regenerated on every change)
  guide/<kind>/<entry>/        entry.json (description, concept, must_have, never, tags, notes, refs, examples) + refs/*.png
  outputs/<date>/              approved images, each with a JSON file (prompt, seed, entries, references)
```

All paths inside a project are relative, so a project folder works on any OS and can be versioned with git.

### Sessions and isolation
- Claude Code starts one MCP server process per session, and each process is its own image session. A session's
  working state (open project, attached references, candidates, picks waiting for approval) lives in
  `<home>/sessions/<id>/`.
- `use_project` drops everything of the previous project. If images are still waiting for approval, it asks you to
  approve or discard them first.
- Sessions can edit the same guide at the same time: writes are atomic and file-locked.
- Moving or removing a project is refused while another session has it open.
- ComfyUI is stopped only when no other session is still using it. The Ollama models it unloaded are reloaded
  at that point.

## Configuration: `~/.local-imagen/config.json`

| Key | Default | Meaning |
|---|---|---|
| `home` | `~/.local-imagen` | machine data: `sessions/`, `library/`, `outputs/` (one-off images), `styles.json`, `projects.json` |
| `projects_root` | `<Documents>/local-imagen-projects` | default folder for new projects (also settable by chat) |
| `docker_dir` | `<home>/docker` | the docker-compose project (copied there by setup) |
| `comfyui_url` | `http://127.0.0.1:8188` | ComfyUI address |
| `manage_docker` | `true` | `false` = ComfyUI is run some other way (no start/stop) |
| `ollama_url` | `http://127.0.0.1:11434` | Ollama models are unloaded from the GPU while generating and reloaded on stop |
| `candidates` | `3` | candidates per request |
| `model` | FLUX.2 klein 4B files | override the `unet` / `text_encoder` / `vae` file names |

Environment variables override the file (`LOCAL_IMAGEN_HOME`, `LOCAL_IMAGEN_PROJECTS`, `COMFYUI_URL`,
`COMFYUI_DOCKER_DIR`, `COMFYUI_MANAGE_DOCKER`, `OLLAMA_URL`, `LOCAL_IMAGEN_CANDIDATES`).

## Update / remove
```
claude plugin marketplace update local-imagen-tool && claude plugin update local-imagen@local-imagen-tool
claude plugin uninstall local-imagen@local-imagen-tool
```
Your projects, the data in `~/.local-imagen`, and the Docker image/volume `comfyui_comfyui-models` are kept until
you delete them.

## Layout
```
.claude-plugin/marketplace.json
plugins/local-imagen/
  .claude-plugin/plugin.json      manifest
  .mcp.json                       starts server/server.py with uv
  agents/image-picker.md          private best-of-N judge
  skills/local-imagen-setup/      install / verify / troubleshoot
  server/server.py, mcp_call.py   MCP server + shell bridge (pass --session <id> to keep a session across calls)
  scripts/setup.py                machine setup (stdlib only)
  docker/                         Dockerfile, docker-compose.yml, download_models.py
```

## Licences
- Plugin code: yours to share.
- FLUX.2 klein 4B: Apache 2.0, so images can be used commercially.
- Check the rights on any reference images you add.
