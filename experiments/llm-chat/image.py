#!/usr/bin/env python3
"""Build the vLLM + model image, once.

    ./image.py              # build if family glab-vllm-qwen2p5-3b has no image
    ./image.py --rebuild    # build a new one anyway; the family points at it

Creates `llm-build` (g2-standard-4, on-demand, with a temporary external IP so it
can reach PyPI and Hugging Face), runs provision.sh on it, stops it, makes an
image of its disk, and deletes it. The builder is deleted in a `finally`, and
with it the external IP. run.py calls this when no image exists.

Settings and the reasons for them are in common.py. The build that made the
current image took 10 minutes, about $0.15. The image then costs about
$0.055/GiB-month to keep; `glab images` shows the figure.
"""

import argparse
import json
import os
import time
import uuid
from datetime import datetime, timezone

from common import (BASE_IMAGE, BUILD_MAX_RUN_MIN, BUILD_PUBLIC_IP,
                    BUILD_SPOT, FAMILY,
                    GPU_DISK_GB, GPU_TYPE, HERE, MAX_MODEL_LEN, MODEL, RESULTS,
                    VLLM_ENV, VLLM_VERSION, create_gpu, delete_instances, glab,
                    log, newest_image, scp_to, show_leftovers, ssh, stream,
                    wait_running, wait_ssh)

# The settings this uses are in common.py, with the reasons for each.
BUILDER = "llm-build"


def build(zone, spot=BUILD_SPOT, disk=GPU_DISK_GB, vllm_version=VLLM_VERSION,
          keep=False):
    """Build an image into FAMILY and return its name."""
    name = f"{FAMILY}-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M')}"
    started = time.time()
    os.makedirs(RESULTS, exist_ok=True)
    log_path = os.path.join(RESULTS, f"build-{name}.log")
    versions = None
    keep_builder = created = False
    run_id = uuid.uuid4().hex
    placed = []
    try:
        log(f"creating {BUILDER}: {GPU_TYPE} {'spot' if spot else 'on-demand'}, "
            f"{disk}GB, external ip for the build only, deleted by Compute "
            f"Engine after {BUILD_MAX_RUN_MIN} min at most")
        zone = create_gpu(BUILDER, zone, GPU_TYPE, disk, spot, BUILD_MAX_RUN_MIN,
                          image=BASE_IMAGE, public_ip=BUILD_PUBLIC_IP,
                          run_id=run_id, placed=placed)
        created = True
        wait_running(BUILDER)
        log("waiting for ssh")
        wait_ssh(BUILDER, zone)
        r = scp_to(BUILDER, zone, os.path.join(HERE, "provision.sh"),
                   "provision.sh")
        if r.returncode:
            glab.die(f"could not copy provision.sh: {r.stderr}")
        vllm_env = " ".join(f"{k}={v}" for k, v in VLLM_ENV.items())
        env = (f"MODEL={MODEL} MAX_MODEL_LEN={MAX_MODEL_LEN} "
               f"VLLM_VERSION={vllm_version} VLLM_ENV='{vllm_env}'")
        log(f"running provision.sh on {BUILDER} (the long part)")
        code, out = stream(BUILDER, zone, f"sudo {env} bash provision.sh")
        with open(log_path, "w") as f:
            f.write(out)
        if code:
            state = glab.find(BUILDER)["state"]
            glab.die(f"provision.sh exited {code} with {BUILDER} {state}"
                     + ("; spot preemption likely" if state != "running" else "")
                     + f". the log is {log_path}")
        for line in out.splitlines():
            if line.startswith("VERSIONS "):
                versions = json.loads(line[len("VERSIONS "):])
        # Remove the builder's ssh host keys and cloud-init state, so each
        # instance made from the image generates its own.
        cleaned = ssh(BUILDER, zone, "sudo cloud-init clean --logs --machine-id "
                      "&& sudo rm -f /etc/ssh/ssh_host_*",
                      capture_output=True, text=True)
        if cleaned.returncode:
            glab.die(f"could not clean builder identity: {cleaned.stderr}")
        log(f"stopping {BUILDER}")
        glab.cmd_stop(argparse.Namespace(name=BUILDER))
        description = json.dumps(versions or {"model": MODEL})[:2048]
        glab.cmd_image_create(argparse.Namespace(
            name=name, source=BUILDER, family=FAMILY,
            description=description))
    except BaseException:
        if keep and created:
            log(f"left {BUILDER} running for inspection: glab shell {BUILDER}; "
                f"glab destroy {BUILDER} when done")
            keep_builder = True
        raise
    finally:
        if not keep_builder:
            delete_instances(placed, run_id)
        glab.write_config(zone=zone)
    log(f"image {name} built in {(time.time() - started) / 60:.0f} min, "
        f"versions {versions}")
    return name


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--rebuild", action="store_true",
                   help="build even if the family already has an image")
    p.add_argument("--spot", action="store_true",
                   help="build on spot instead of on-demand (cheaper, can be "
                   "preempted mid-build)")
    p.add_argument("--vllm-version", default=VLLM_VERSION,
                   help=f"vLLM release (default {VLLM_VERSION}, from common.py)")
    p.add_argument("--keep", action="store_true",
                   help="leave the builder running if the build fails")
    p.add_argument("--zone", help="zone to try first instead of `glab zone`; "
                   "the others in common.ZONES are tried after it")
    args = p.parse_args()

    zone = args.zone or glab.current_zone()
    existing = newest_image()
    if existing and not args.rebuild:
        log(f"{existing.name} already in family {FAMILY}; nothing to do. "
            "--rebuild makes a new one.")
    else:
        build(zone, spot=BUILD_SPOT or args.spot,
              vllm_version=args.vllm_version, keep=args.keep)
    show_leftovers()


if __name__ == "__main__":
    main()
