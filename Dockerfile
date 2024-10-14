FROM nvidia/cuda:12.3.1-runtime-ubuntu22.04

ENV DEBIAN_FRONTEND=noninteractive \
    TZ=America/Los_Angeles

# Install dependencies
RUN apt-get update && apt-get install -y \
    git \
    make build-essential libssl-dev zlib1g-dev \
    libbz2-dev libreadline-dev libsqlite3-dev wget curl llvm \
    libncursesw5-dev xz-utils tk-dev libxml2-dev libxmlsec1-dev libffi-dev liblzma-dev git git-lfs  \
    ffmpeg libsm6 libxext6 cmake libgl1-mesa-glx zip unzip \
    && rm -rf /var/lib/apt/lists/* \
    && git lfs install

# Create and switch to a new user
RUN useradd -m -u 1000 user
USER user
ENV HOME=/home/user \
    PATH=/home/user/.local/bin:$PATH

# Pyenv and Python setup
RUN curl https://pyenv.run | bash
ENV PATH=$HOME/.pyenv/shims:$HOME/.pyenv/bin:$PATH
ARG PYTHON_VERSION=3.10.12
RUN pyenv install $PYTHON_VERSION && \
    pyenv global $PYTHON_VERSION && \
    pyenv rehash && \
    pip install --no-cache-dir --upgrade pip setuptools wheel 

# Set the working directory
WORKDIR /home/user/opt/ComfyUI

RUN mkdir -p /home/user/opt/ComfyUI && chown user:user /home/user/opt/ComfyUI && chmod 755 /home/user/opt/ComfyUI

# Clone ComfyUI directly into /home/user/opt/ComfyUI
RUN git clone --depth 1 https://github.com/comfyanonymous/ComfyUI .


# Create a Python virtual environment in a directory
ENV TEMP_VENV_PATH=/home/user/opt/ComfyUI/.venv
RUN python -m venv $TEMP_VENV_PATH

RUN . $TEMP_VENV_PATH/bin/activate && pip install xformers!=0.0.18 --no-cache-dir -r requirements.txt --extra-index-url https://download.pytorch.org/whl/cu121

# Clone ComfyUI-Manager and install its requirements
RUN mkdir -p custom_nodes/ComfyUI-Manager && \
    git clone --depth 1 https://github.com/ltdrdata/ComfyUI-Manager custom_nodes/ComfyUI-Manager && \
    . $TEMP_VENV_PATH/bin/activate && pip install --no-cache-dir --upgrade torch torchvision GitPython && \
    pip install -r custom_nodes/ComfyUI-Manager/requirements.txt

# Copy the configuration file
COPY comfyui_config/extra_model_paths.yaml ./extra_model_paths.yaml

# Krita ComfyUI setup https://github.com/Acly/krita-ai-diffusion/wiki/ComfyUI-Setup
# Clone custom nodes
RUN mkdir -p custom_nodes/comfyui_controlnet_aux && \
    git clone https://github.com/Fannovel16/comfyui_controlnet_aux custom_nodes/comfyui_controlnet_aux && \
    . $TEMP_VENV_PATH/bin/activate && pip install -r custom_nodes/comfyui_controlnet_aux/requirements.txt

RUN mkdir -p custom_nodes/ComfyUI_IPAdapter_plus && \
    git clone https://github.com/cubiq/ComfyUI_IPAdapter_plus custom_nodes/ComfyUI_IPAdapter_plus

RUN mkdir -p custom_nodes/comfyui-inpaint-nodes && \
    git clone https://github.com/Acly/comfyui-inpaint-nodes custom_nodes/comfyui-inpaint-nodes && \
    . $TEMP_VENV_PATH/bin/activate && pip install opencv-python

RUN mkdir -p custom_nodes/comfyui-tooling-nodes && \
    git clone https://github.com/Acly/comfyui-tooling-nodes custom_nodes/comfyui-tooling-nodes && \
    . $TEMP_VENV_PATH/bin/activate && pip install -r custom_nodes/comfyui-tooling-nodes/requirements.txt

# Download models
RUN wget https://github.com/Acly/krita-ai-diffusion/releases/download/v1.25.0/krita_ai_diffusion-1.25.0.zip
RUN unzip krita_ai_diffusion-1.25.0.zip
RUN . $TEMP_VENV_PATH/bin/activate && pip install --no-cache-dir --upgrade aiohttp tqdm && \
    python ai_diffusion/download_models.py /home/user/opt/ComfyUI --recommended


CMD ["bash", "-c", "source /home/user/opt/ComfyUI/.venv/bin/activate && exec python /home/user/opt/ComfyUI/main.py --listen 0.0.0.0 --port 8181 --output-directory /home/user/opt/ComfyUI/output/"]
