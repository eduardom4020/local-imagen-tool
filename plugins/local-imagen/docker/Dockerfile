# ComfyUI on CUDA (NVIDIA GPU via Docker Desktop + WSL2 on Windows, or Docker + NVIDIA Container Toolkit on Linux)
FROM python:3.12-slim

ENV PIP_NO_CACHE_DIR=1 PYTHONUNBUFFERED=1 DEBIAN_FRONTEND=noninteractive

# git for ComfyUI; gcc because Triton (used by the int8/fp8/fp4 quantized models) compiles GPU kernels
RUN apt-get update \
 && apt-get install -y --no-install-recommends git libgl1 libglib2.0-0 ca-certificates gcc libc6-dev \
 && rm -rf /var/lib/apt/lists/*

# PyTorch with CUDA (the host NVIDIA driver provides the GPU)
ARG TORCH_INDEX=https://download.pytorch.org/whl/cu130
RUN pip install torch torchvision torchaudio --index-url ${TORCH_INDEX}

# ComfyUI + ComfyUI-Manager (pin COMFY_REF to a release tag for reproducible builds)
ARG COMFY_REF=master
RUN git clone https://github.com/Comfy-Org/ComfyUI.git /opt/ComfyUI \
 && cd /opt/ComfyUI && git checkout ${COMFY_REF} \
 && pip install -r requirements.txt \
 && git clone https://github.com/Comfy-Org/ComfyUI-Manager.git /opt/ComfyUI/custom_nodes/ComfyUI-Manager \
 && pip install -r /opt/ComfyUI/custom_nodes/ComfyUI-Manager/requirements.txt \
 && pip install huggingface_hub

WORKDIR /opt/ComfyUI
EXPOSE 8188
CMD ["python", "main.py", "--listen", "0.0.0.0", "--port", "8188"]
