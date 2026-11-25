"""Download model files into the ComfyUI models volume. Runs inside a container with /models mounted.

Usage: python download_models.py [core] [extras]      (default: core)
  core   = FLUX.2 klein 4B (used by the MCP server), about 12 GB, Apache 2.0
  extras = Z-Image-Turbo int8, Illustrious XL 2.0, Animagine XL 4.0 (comparison models), about 26 GB
"""
import os
import shutil
import sys
import time

from huggingface_hub import hf_hub_download

ROOT = "/models"
SETS = {
    "core": [
        ("Comfy-Org/Flux-2-Klein-4B", "split_files/diffusion_models/flux-2-klein-4b.safetensors", "diffusion_models"),
        ("Comfy-Org/Flux-2-Klein-4B", "split_files/text_encoders/qwen_3_4b_fp4_flux2.safetensors", "text_encoders"),
        ("Comfy-Org/Flux-2-Klein-4B", "split_files/vae/flux2-vae.safetensors", "vae"),
    ],
    "extras": [
        ("Comfy-Org/Z-Image-Turbo", "split_files/diffusion_models/z_image_turbo_int8_convrot.safetensors", "diffusion_models"),
        ("Comfy-Org/Z-Image-Turbo", "split_files/text_encoders/qwen_3_4b_fp8_mixed.safetensors", "text_encoders"),
        ("Comfy-Org/Z-Image-Turbo", "split_files/vae/ae.safetensors", "vae"),
        ("OnomaAIResearch/Illustrious-XL-v2.0", "Illustrious-XL-v2.0.safetensors", "checkpoints"),
        ("cagliostrolab/animagine-xl-4.0", "animagine-xl-4.0-opt.safetensors", "checkpoints"),
    ],
}

for d in ("checkpoints", "diffusion_models", "text_encoders", "vae", "loras", "controlnet", "upscale_models",
          "clip_vision", "embeddings", "unet", "clip", "style_models"):
    os.makedirs(f"{ROOT}/{d}", exist_ok=True)

wanted = sys.argv[1:] or ["core"]
for name in wanted:
    for repo, path, sub in SETS[name]:
        dest = f"{ROOT}/{sub}/{os.path.basename(path)}"
        if os.path.exists(dest):
            print(f"skip (exists) {dest}", flush=True)
            continue
        t0 = time.time()
        p = hf_hub_download(repo, path, cache_dir="/models/.hfcache")
        shutil.move(os.path.realpath(p), dest)
        size = os.path.getsize(dest) / 1e9
        print(f"done {dest}  {size:.2f} GB in {time.time() - t0:.0f}s", flush=True)

shutil.rmtree("/models/.hfcache", ignore_errors=True)
print("ALL DOWNLOADS DONE", flush=True)
