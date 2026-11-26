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
- **Pose guides**: Blender draws a grey mannequin in the exact pose and camera angle. The picker uses it for
  action and acrobatic shots, so limbs stay in the right place. The pose comes from presets or joint angles, and
  you can save poses to the guide.
- **Edits from red marks**: mark what's wrong in red on an image (even on a screenshot of a page) and only
  that part is redrawn. The rest stays exactly as it was.
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
and runs `plugins/local-imagen/scripts/setup.py`. It builds the image, downloads the model and builds the
Blender pose-renderer image (15-30 min the first time). Restart Claude Code when it finishes.

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

## Editing marked images

To fix part of an image, mark it in red (for example with the pen in the Claude app's image editor), attach
it, and say what to change. Claude edits only the marked areas and keeps the rest exactly as it was. It does
not draw a new image.

- **Marks:** `find_marks` finds the red marks. A loop marks what it encloses, and a scribble marks what it
  covers.
- **Finding the original:** it matches each mark to the original image it was drawn on, at full resolution.
  That works even when you marked a screenshot of a page, such as a character-guide PDF with several images.
  Originals are searched in the session's picks and the project's outputs, docs and guide references.
- **The edit:** `edit_image` repaints each marked area in a crop around it at about 1 MP, so small things like
  hands get detail. It pastes the result back with a soft edge.
  - It makes candidates as usual, and the picker judges them on before/after crops.
  - `approve_image` files the edited version next to the original.
  - Use `strength` 1.0 to redraw anatomy or objects, or 0.4-0.7 for colour or detail changes.
- **Attachment location:** Claude Code keeps chat attachments in `~/.claude/uploads`. `image="latest"` uses the
  newest one.
- **Green = where it should go.** Draw a green line next to a red mark to show the position and direction the
  red-marked thing should have, for example where a sword should hang. A green loop moves it there without
  turning it.
  - It's a real redraw in two model steps, not a cut-and-paste:
    1. **Remove:** the model takes the thing out and draws what was behind it, such as the belt, sash or
       clothing.
    2. **Put back:** the thing goes back on the green mark. The end nearer to the line keeps its place, such
       as a hilt at the belt or a hand. The model redraws the whole connected region (for a sword at the
       belt, the hips and belt) so it is attached and the picture makes sense.
  - The model only ever sees clean pictures, because it copies its references almost pixel for pixel. Where
    the old place was over plain background, it is restored to the exact background beforehand.
  - Outside those areas nothing changes.
  - Name the thing in the instruction and say how it attaches, for example "his sheathed sword, hanging from
    his belt on a buckled strap".
  - Two settings: `reach` (default 0.3) sets how much of the body around the attachment is redrawn, and
    `redraw_strength` (default 0.9) sets how freely it is redrawn.

## Pose guides (Blender)

Image models easily mix up limbs in action poses: crossed legs, doubled hands, odd arm angles. A pose guide
fixes most of that. `render_pose` uses Blender to draw a plain grey artist's mannequin in the pose you want, at
the camera angle you want. `generate_image(pose=...)` then sends it as the pose reference. Every full-length
character, creature or style reference is cut down to head and chest at the same time, because FLUX.2 klein
copies the pose of any full-body figure it is shown.

- **Presets:** stand, idle, walk, run, jump, leap, crouch, kneel, sit, sit_ground, lunge, guard, slash, overhead,
  flip, slide, land, wave, lie.
- **Action presets with their own dramatic camera:** aerial_slash, dash_strike, leap_strike (two-handed),
  rising_slash, hero_landing (sword planted), backflip, iai_draw, flying_kick, dodge_back, reverse_guard.
- **Adjusting:** any joint can be changed with readable angles: spine bend/twist, head nod/turn, arm and leg
  `aim` (forward / up / out), elbow and knee bend, wrist.
- **Sword:** held normally or in a reverse grip, one-handed or two-handed. For two hands, the second arm is
  placed on the hilt automatically.
- **Left-handed versions:** `mirror` flips any pose.
- **Figure:** proportions are set in heads (`heads=7`), plus build, shoulders and hips.
- **Camera:** views are front, three-quarter, side and back. The figure faces left or right, and camera height
  and perspective can be changed.
- **Outputs:** a depth map on request, and the 2D joint positions in a JSON file.
- **Keeping poses:** "Save this pose as *the draw*" stores it in the project guide as a pose entry. It can be
  re-rendered from any angle and used as `entries=["pose/the draw"]`.
- **Depth mode:** `generate_image(pose=..., pose_mode="depth")` uses the
  [RefControl depth LoRA](https://huggingface.co/thedeoxen/refcontrol-FLUX.2-klein-4B-reference-depth-lora)
  (Apache 2.0, 92 MB; `setup.py` downloads it).
  - Image 1 is the pose's depth map (`render_pose(..., depth=True)`), and image 2 is the character's full
    reference.
  - It follows the silhouette even more tightly than the mannequin guide, and identity holds.
  - In our tests, props such as a sheathed sword come out weaker. Use the default `guide` mode when props
    matter.

**Where Blender comes from:** a small Docker image, `docker/Dockerfile.blender`. It downloads the **official
portable Linux build** of Blender (5.2 LTS) from download.blender.org and checks its SHA-256, so nothing is
installed on your computer.
- Renders run on the CPU, so they never take GPU memory from FLUX. A render takes about 5-15 s.
- `setup.py` builds the image (`--skip-blender` leaves it out, `--blender-only` adds it to an existing install).
  You can also just ask Claude to "set up Blender for poses", which calls the `setup_blender` tool.
- For another Blender release, use `setup_blender(version="5.3.0")`. The checksum is fetched from
  download.blender.org.

**Without Docker** (for example with an external ComfyUI), install Blender yourself and point the plugin at it:
1. Get Blender 4.2 or newer from https://www.blender.org/download/. There are three ways:
   - The Windows `.zip` or the Linux `.tar.xz` are portable: unpack them anywhere.
   - On macOS, drag Blender into Applications.
   - Or use a package manager: `winget install BlenderFoundation.Blender`, `brew install --cask blender`,
     or `snap install blender --classic`.
2. Ask Claude to run `setup_blender(path=...)` with the Blender executable. That is
   `...\blender.exe` on Windows, `/Applications/Blender.app/Contents/MacOS/Blender` on macOS, or
   `.../blender` on Linux. The plugin checks the executable and saves it as `blender_path`.
3. `blender_status` shows which Blender is in use. Running `setup_blender()` again goes back to the Docker image.

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
| `blender_path` | unset | a Blender on this computer for `render_pose` (set by `setup_blender(path=...)`); unset = Docker image |
| `blender_image` | `local/imagen-blender:latest` | the Blender Docker image |

Environment variables override the file (`LOCAL_IMAGEN_HOME`, `LOCAL_IMAGEN_PROJECTS`, `COMFYUI_URL`,
`COMFYUI_DOCKER_DIR`, `COMFYUI_MANAGE_DOCKER`, `OLLAMA_URL`, `LOCAL_IMAGEN_CANDIDATES`, `LOCAL_IMAGEN_BLENDER`,
`LOCAL_IMAGEN_BLENDER_IMAGE`).

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
  server/blender/                 pose_render.py (runs inside Blender: mannequin, posing, render) + poses.json presets
  scripts/setup.py                machine setup (stdlib only)
  docker/                         Dockerfile, docker-compose.yml, download_models.py, Dockerfile.blender
```

## Licences
- Plugin code: yours to share.
- FLUX.2 klein 4B: Apache 2.0, so images can be used commercially.
- Blender: GPL, downloaded from blender.org when the image is built (not bundled here). Its renders are yours.
- Check the rights on any reference images you add.
