"""Helpers shared by image.py and run.py."""

import contextlib
import io
import os
import subprocess
import sys
import time
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))
import glab  # noqa: E402
from glab import compute_v1, NotFound  # noqa: E402,F401

HERE = os.path.dirname(os.path.abspath(__file__))
RESULTS = os.path.join(HERE, "results")

# ---------------------------------------------------------------- decisions
#
# Every choice that shapes a result is set here, with the reason. Change the
# value and its comment together. run.py copies these into each result file,
# so an old result still says what it ran with.

# Model. Qwen2.5-3B-Instruct: Apache 2.0 and not gated, so no Hugging Face
# token goes on an instance. BF16 weights are ~6 GB, leaving ~15 GB of the
# L4's 24 GB for KV cache. 3B chosen over 7B (2026-09-25, user) for speed:
# ~2.5x faster decode, a 50-turn session in minutes. 7B remains the more
# realistic size for the later KV-cache transfer work.
MODEL = "Qwen/Qwen2.5-3B-Instruct"

# Image family. run.py uses the newest image in it and builds one only when
# the family is empty. A different model needs a different family.
FAMILY = "glab-vllm-qwen2p5-3b"

# Server. One L4 (quota: 1 GPU across all regions, 1 L4 per region). G2 is
# the machine family the L4 comes in; -4 is the smallest. T4 has no BF16 and
# 16 GB, so it was not considered.
GPU_TYPE = "g2-standard-4"
GPU_DISK_GB = 40            # Ubuntu + driver ~6 GB, vLLM venv ~10 GB, model ~6 GB

# Base image for the build: Canonical's Ubuntu 24.04 with the NVIDIA 595
# driver. The driver's kernel module is signed, so glab's Secure Boot stays on.
BASE_IMAGE = ("projects/ubuntu-os-accelerator-images/global/images/family/"
              "ubuntu-accelerator-2404-amd64-with-nvidia-595")

# The builder needs PyPI and Hugging Face. It gets a temporary external IP,
# deleted with it (2026-09-25, user: cheaper than Cloud NAT by ~$0.40 a
# build). Servers made from the image have no external IP and no internet.
BUILD_PUBLIC_IP = True

# Zones to try for the GPU instance, in order after the one asked for. Every
# asia-southeast1 zone sells L4 (`glab zones`, 2026-09-25), and on that day
# each of them refused a g2-standard-4 at some point with a stockout, on-demand
# and spot, while another zone had one. The client goes wherever the server
# landed.
ZONES = ["asia-southeast1-a", "asia-southeast1-b", "asia-southeast1-c"]

# VM names. watch.py finds the VMs by these.
SERVER, CLIENT = "llm-server", "llm-client"

# Client. Only sends HTTP and times it, so the smallest type that is not
# shared-core-starved; e2-micro's 0.25 vCPU could add jitter to the timings.
CLIENT_TYPE = "e2-small"
CLIENT_DISK_GB = 10

# Spot for both, ~40% off on G2 (~$0.54/h against ~$0.89/h together). On
# 2026-09-25 a spot server was preempted after 7 of 50 turns; run.py keeps
# the finished turns and marks such a run incomplete, and a rerun costs
# ~$0.07. On-demand was the default briefly after that; spot again from
# 2026-09-26 (user). run.py --on-demand overrides.
SPOT = True

# Hard limits, minutes. Compute Engine deletes the instance, disk included,
# this long after it starts, so nothing bills on if this machine shuts down
# or loses its connection mid-run. Spot alone does not do that: a spot
# instance runs until Google wants the capacity back. A complete session run
# took 8 minutes (2026-09-25) and a build 10; the limits leave room for a
# slow start and for looking around with --keep.
MAX_RUN_MIN = 45
BUILD_MAX_RUN_MIN = 60

# The image build runs on-demand. The first build, on spot, was preempted
# 7 minutes in (asia-southeast1-b, 2026-09-25) and lost the install; a
# 25-minute build costs ~$0.15 more on-demand. image.py --spot overrides.
BUILD_SPOT = False

# Session. Temperature 0 and a fixed seed so repeated runs produce the same
# answers and the same token counts, which keeps timings comparable. 200
# tokens per answer keeps 50 turns under ~12k tokens of context.
TEMPERATURE = 0
SEED = 0
MAX_TOKENS = 200

# vLLM server. 16k context covers the session with room. Prefix caching is
# vLLM's default and the realistic setting for a chat; --no-prefix-cache on
# run.py turns it off to show what it saves.
MAX_MODEL_LEN = 16384
PORT = 8000

# vLLM release baked into the image. 0.30.0 was the newest on 2026-09-25 and
# is the one these settings were tested with; unpinned, a rebuild would take
# whatever is newest. image.py --vllm-version overrides.
VLLM_VERSION = "0.30.0"

# Environment for every `vllm serve`, in the image build's smoke test and in
# run.py. The server has no internet: no model lookup online, no usage
# statistics. VLLM_USE_FLASHINFER_SAMPLER=0 because FlashInfer's sampler
# compiles CUDA code on first use and the image has no nvcc (the first
# working build, 2026-09-25, failed with "Could not find nvcc"). At
# temperature 0 sampling is greedy either way; PyTorch's sampler is used.
VLLM_ENV = {"HF_HUB_OFFLINE": "1", "VLLM_NO_USAGE_STATS": "1",
            "DO_NOT_TRACK": "1", "VLLM_USE_FLASHINFER_SAMPLER": "0"}

# nvidia-smi sampling interval on the server, seconds.
GPU_SAMPLE_S = 1


def decisions():
    """The settings above, for the result file."""
    return {k: v for k, v in globals().items()
            if k.isupper() and k not in ("HERE", "RESULTS", "SSH_OPTS")}


def log(message):
    print(f"[{time.strftime('%H:%M:%S')}] {message}", flush=True)


def quietly(fn, *args):
    """Run a glab command without its hints to the interactive user."""
    with contextlib.redirect_stdout(io.StringIO()):
        return fn(*args)


def create(name, zone, type_, disk, spot, max_run, image=None, public_ip=False,
           run_id=None):
    glab.write_config(zone=zone)
    quietly(glab.cmd_init, SimpleNamespace(
        name=name, type=type_, disk=disk, disk_type="pd-balanced", gpu=None,
        image=image, spot=spot, public_ip=public_ip, max_run=max_run,
        run_id=run_id))


def create_gpu(name, zone, type_, disk, spot, max_run, image=None,
               public_ip=False, run_id=None, placed=None):
    """Create a GPU instance in `zone`, or in the next zone in ZONES when that
    one has no capacity. Returns the zone it was created in."""
    tried = []
    for z in [zone] + [z for z in ZONES if z != zone]:
        if placed is not None:
            placed.append((name, z))
        try:
            create(name, z, type_, disk, spot, max_run, image=image,
                   public_ip=public_ip, run_id=run_id)
            return z
        except SystemExit as e:
            if "does not have enough resources" not in str(e):
                raise
            tried.append(z)
            log(f"{z}: no {type_} capacity ({'spot' if spot else 'on-demand'})")
    glab.die(f"no {type_} capacity in {', '.join(tried)}; try again later")


def wait_running(name, timeout=300):
    deadline = time.time() + timeout
    while time.time() < deadline:
        i = glab.find(name)
        if i["state"] == "running":
            return i
        time.sleep(5)
    glab.die(f"{name} never reached running")


# Without keepalives an ssh session to an instance that was preempted hangs
# indefinitely; the first image build sat for 10 hours. With these it fails
# after about a minute of silence.
SSH_OPTS = ["--", "-q", "-o", "ServerAliveInterval=15",
            "-o", "ServerAliveCountMax=4"]


def ssh(name, zone, command, **kw):
    return subprocess.run(
        glab.gcloud_ssh(name, zone, ["--command", command] + SSH_OPTS),
        stdin=subprocess.DEVNULL, **kw)


def wait_ssh(name, zone, attempts=36):
    """An instance is RUNNING before sshd answers. Retry rather than sleep."""
    for _ in range(attempts):
        if ssh(name, zone, "true", capture_output=True).returncode == 0:
            return
        time.sleep(5)
    glab.die(f"{name} never accepted an ssh connection over IAP")


def stream(name, zone, command, prefix="  ", on_line=None):
    """Run a command over ssh, print its output as it arrives, and return
    (exit code, output)."""
    proc = subprocess.Popen(
        glab.gcloud_ssh(name, zone, ["--command", command] + SSH_OPTS),
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        stdin=subprocess.DEVNULL, bufsize=1)
    lines = []
    for line in proc.stdout:
        lines.append(line)
        if on_line:
            on_line(line)
        else:
            print(prefix + line, end="", flush=True)
    proc.wait()
    return proc.returncode, "".join(lines)


def scp_from(name, zone, remote, local, timeout=180):
    """Copy a file back. A timeout rather than a hang if the instance goes
    away mid-copy; returns None then."""
    try:
        return subprocess.run(
            ["gcloud", "compute", "scp", "--tunnel-through-iap", "--zone", zone,
             "--project", glab.current_project(), f"{name}:{remote}", local],
            capture_output=True, text=True, stdin=subprocess.DEVNULL,
            timeout=timeout)
    except subprocess.TimeoutExpired:
        log(f"copying {remote} from {name} timed out after {timeout}s")
        return None


def state(name, zone, timeout=60):
    """One instance's state, or "unknown". glab.find lists the zone with the
    client's default 600 s read timeout, which a run hit once right after a
    preemption; this asks for the one instance with a shorter one."""
    try:
        i = glab.instances_client().get(project=glab.current_project(),
                                        zone=zone, instance=name,
                                        timeout=timeout)
        return i.status.lower()
    except NotFound:
        return "gone"
    except Exception as e:
        log(f"could not read {name}'s state: {e}")
        return "unknown"


def scp_to(name, zone, local, remote):
    return subprocess.run(
        ["gcloud", "compute", "scp", "--tunnel-through-iap", "--zone", zone,
         "--project", glab.current_project(), local, f"{name}:{remote}"],
        capture_output=True, text=True, stdin=subprocess.DEVNULL)


def delete_instances(placed, run_id):
    """Delete only instances labelled for this run, including failed launches."""
    client = glab.instances_client()
    project = glab.current_project()
    ops = []
    for name, zone in placed:
        try:
            instance = client.get(project=project, zone=zone, instance=name,
                                  timeout=60)
            if (not run_id or not glab.has_lab_label(instance)
                    or instance.labels.get("lab-run") != run_id):
                log(f"leaving {name} in {zone}: not owned by this run")
                continue
            ops.append((name, client.delete(project=project, zone=zone,
                                            instance=name), zone))
        except NotFound:
            pass
        except Exception as e:
            log(f"could not delete {name}: {e}")
    if ops:
        log(f"deleting {', '.join(n for n, _, _ in ops)} (about a minute)")
    for name, op, zone in ops:
        try:
            glab.wait(op, zone)
            log(f"deleted {name}")
        except BaseException as e:
            log(f"{name} may still exist: {e}. check `glab list --all-zones`.")


def newest_image(family=FAMILY):
    try:
        return glab.images_client().get_from_family(
            project=glab.current_project(), family=family)
    except NotFound:
        return None


def show_leftovers():
    """Print `glab list --all-zones` and `glab images`, so the end of every
    run shows what is still billing."""
    for title, fn, args in [
            ("glab list --all-zones", glab.cmd_list,
             SimpleNamespace(all_zones=True, region=None)),
            ("glab images", glab.cmd_images, SimpleNamespace())]:
        print(f"\n$ {title}", flush=True)
        try:
            fn(args)
        except BaseException as e:
            print(f"could not list: {e}. run `{title}`.")
