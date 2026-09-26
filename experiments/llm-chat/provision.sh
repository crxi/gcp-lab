#!/usr/bin/env bash
# Runs as root on the image builder, which has an external IP for the
# duration. Installs vLLM into a venv, downloads the model, and checks that
# the model answers one request. The instance is then stopped and its disk
# becomes the image, so the server never needs the internet.
#
#   sudo MODEL=Qwen/Qwen2.5-3B-Instruct bash provision.sh
#
# image.py runs it with the values from common.py.
#
# VLLM_VERSION pins vLLM; unset installs the newest and records which.
# VLLM_ENV is the environment for `vllm serve`, as KEY=VALUE pairs.
set -euo pipefail

MODEL=${MODEL:?set MODEL to a Hugging Face repo id}
VLLM_VERSION=${VLLM_VERSION:-}
VLLM_ENV=${VLLM_ENV:-HF_HUB_OFFLINE=1}
MAX_MODEL_LEN=${MAX_MODEL_LEN:-16384}
MODEL_DIR=/opt/models/$(basename "$MODEL" | tr '[:upper:]' '[:lower:]')
SERVED_NAME=$(basename "$MODEL_DIR")
export DEBIAN_FRONTEND=noninteractive

step() { echo "=== $(date +%H:%M:%S) $*"; }

step "driver, from the image"
nvidia-smi --query-gpu=name,driver_version,memory.total --format=csv

# Triton, which vLLM uses to compile kernels at startup, builds a small C
# module and needs gcc and Python.h. The base image has neither; without them
# vLLM fails with "Failed to find C compiler".
step "apt: python3-venv, build-essential, python3-dev"
apt-get update -q
apt-get install -y -q python3-venv build-essential python3-dev > /dev/null

step "vllm ${VLLM_VERSION:-newest} into /opt/vllm"
python3 -m venv /opt/vllm
/opt/vllm/bin/pip install -q --upgrade pip
/opt/vllm/bin/pip install -q "vllm${VLLM_VERSION:+==$VLLM_VERSION}"

step "download $MODEL to $MODEL_DIR"
/opt/vllm/bin/python - "$MODEL" "$MODEL_DIR" <<'EOF'
import sys
from huggingface_hub import snapshot_download
snapshot_download(repo_id=sys.argv[1], local_dir=sys.argv[2],
                  allow_patterns=["*.json", "*.safetensors", "*.txt",
                                  "*.model", "*.tiktoken", "merges.txt"])
EOF
du -sh "$MODEL_DIR"

step "smoke test: start vllm offline and ask one question"
env $VLLM_ENV \
    /opt/vllm/bin/vllm serve "$MODEL_DIR" --served-model-name "$SERVED_NAME" \
    --host 127.0.0.1 --port 8000 --max-model-len "$MAX_MODEL_LEN" \
    > /tmp/vllm-smoke.log 2>&1 &
pid=$!
for _ in $(seq 1 120); do
    curl -sf http://127.0.0.1:8000/health > /dev/null && break
    kill -0 $pid 2> /dev/null || { cat /tmp/vllm-smoke.log; exit 1; }
    sleep 5
done
curl -sf http://127.0.0.1:8000/v1/chat/completions \
    -H 'Content-Type: application/json' \
    -d "{\"model\": \"$SERVED_NAME\", \"max_tokens\": 20, \"temperature\": 0,
         \"messages\": [{\"role\": \"user\", \"content\": \"Say hello in five words.\"}]}"
echo
kill $pid
wait $pid 2> /dev/null || true

step "record versions"
mkdir -p /opt/lab
/opt/vllm/bin/python - "$MODEL" "$MODEL_DIR" "$SERVED_NAME" <<'EOF' | tee /opt/lab/versions.json
import json, subprocess, sys, importlib.metadata as md
driver = subprocess.run(["nvidia-smi", "--query-gpu=driver_version",
                         "--format=csv,noheader"], capture_output=True,
                        text=True).stdout.strip()
print(json.dumps({
    "model": sys.argv[1], "model_dir": sys.argv[2], "served_name": sys.argv[3],
    "vllm": md.version("vllm"), "torch": md.version("torch"),
    "nvidia_driver": driver,
}))
EOF

step "clean caches so the image is smaller"
rm -rf /root/.cache/pip /root/.cache/huggingface
apt-get clean
echo "VERSIONS $(cat /opt/lab/versions.json)"
step done
