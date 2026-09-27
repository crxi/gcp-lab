#!/usr/bin/env python3
"""Watch a running llm-chat session from a second terminal.

    python3 experiments/llm-chat/watch.py          # answers and server status
    python3 experiments/llm-chat/watch.py chat     # questions and answers
    python3 experiments/llm-chat/watch.py server   # GPU, vLLM counters, vLLM log

Start it any time after run.py; it waits for the VMs to exist and accept
ssh. It sends a small program over ssh (watch_client.py to llm-client,
watch_server.py to llm-server) and prints what that program prints. Nothing
is installed on the VMs. Ctrl-C stops watching, not the run. It ends when
the conversation finishes or run.py deletes the VMs.
"""

import argparse
import os
import shlex
import subprocess
import sys
import threading
import time

from common import (CLIENT, HERE, PORT, SERVER, SSH_OPTS, ZONES, NotFound,
                    glab, log, wait_ssh)

# How long to wait for run.py to create a VM. A first run builds the image
# before creating the chat VMs, which took 10 minutes (2026-09-25).
CREATE_WAIT_S = 40 * 60
# Server status every 2 s on its own, every 5 s when shown between answers.
SERVER_EVERY_S = 2
BOTH_SERVER_EVERY_S = 5

lock = threading.Lock()


def locate(name, wait_s=CREATE_WAIT_S, poll=10):
    """The zone `name` is running in, once it is. run.py falls back to
    another zone when one has no L4, so each zone is asked, starting with
    `glab zone`."""
    client = glab.instances_client()
    project = glab.current_project()
    first = glab.current_zone()
    zones = [first] + [z for z in ZONES if z != first]
    deadline = time.time() + wait_s
    said = None
    while True:
        status = "not created yet"
        for zone in zones:
            try:
                i = client.get(project=project, zone=zone, instance=name,
                               timeout=30)
            except NotFound:
                continue
            if i.status == "RUNNING":
                return zone
            status = f"{i.status.lower()} in {zone}"
            break
        if status != said:
            log(f"{name}: {status}; waiting")
            said = status
        if time.time() > deadline:
            glab.die(f"{name} was not running after {wait_s // 60} min. "
                     "Is run.py running? `glab list --all-zones` shows what exists.")
        time.sleep(poll)


def remote(name, zone, program, args):
    """Start `program` from this directory on `name` with python3, feeding
    its source over ssh's stdin, and return the process. gcloud's own
    messages go to stderr, kept apart so they can be dropped when the
    connection ended because the VM was deleted."""
    with open(os.path.join(HERE, program)) as f:
        source = f.read()
    command = " ".join(["python3", "-u", "-"] + [shlex.quote(a) for a in args])
    proc = subprocess.Popen(
        # --verbosity=error drops gcloud's tunnel warning about NumPy.
        glab.gcloud_ssh(name, zone, ["--command", command, "--verbosity=error"]
                        + SSH_OPTS),
        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True, bufsize=1)
    proc.stdin.write(source)
    proc.stdin.close()
    return proc


def relay(proc, prefix, blocks):
    """Print the program's output. With `blocks`, a blank line ends a block
    and the block is printed whole, so an answer is not split by status lines
    from the other VM. Without, each line gets this computer's time; the VMs
    keep UTC."""
    pending = []
    for line in proc.stdout:
        line = line.rstrip("\n")
        if blocks and line:
            pending.append(line)
            continue
        with lock:
            for held in pending + ([line] if line or not blocks else []):
                stamp = "" if blocks else time.strftime("%H:%M:%S  ")
                print(f"{prefix}{stamp}{held}", flush=True)
            if blocks:
                print(flush=True)
        pending = []
    proc.errors = proc.stderr.read() if proc.stderr else ""
    proc.wait()


def running(name, zone):
    """Whether `name` is still RUNNING; False once run.py is deleting it."""
    try:
        i = glab.instances_client().get(project=glab.current_project(),
                                        zone=zone, instance=name, timeout=30)
    except NotFound:
        return False
    return i.status == "RUNNING"


def watch(targets):
    """targets: [(vm name, program, program args, prefix, blocks)]."""
    zones = {}
    for name, *_ in targets:
        zones[name] = locate(name)
        log(f"{name} is running in {zones[name]}; waiting for ssh")
        wait_ssh(name, zones[name])
    procs = [(name, remote(name, zones[name], program, args), prefix, blocks)
             for name, program, args, prefix, blocks in targets]
    threads = [threading.Thread(target=relay, args=(p, prefix, blocks),
                                daemon=True)
               for _, p, prefix, blocks in procs]
    for t in threads:
        t.start()
    try:
        for t in threads:
            t.join()
    except KeyboardInterrupt:
        for _, p, _, _ in procs:
            p.terminate()
        print()
        log("stopped watching; the run carries on")
        return 130
    for name, p, _, _ in procs:
        if not p.returncode:
            continue
        if not running(name, zones[name]):
            log(f"{name} is being deleted or is gone; run.py has finished")
            continue
        print(p.errors.rstrip(), file=sys.stderr)
        log(f"{name}: the connection ended (exit {p.returncode}) while the "
            "VM is still running")
    return 0


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("what", nargs="?", choices=["both", "chat", "server"],
                   default="both")
    p.add_argument("--width", type=int, default=100,
                   help="wrap answers at this many characters")
    args = p.parse_args()

    chat = (CLIENT, "watch_client.py", ["--width", str(args.width)], "", True)
    if args.what == "chat":
        targets = [chat]
    elif args.what == "server":
        targets = [(SERVER, "watch_server.py",
                    ["--port", str(PORT), "--every", str(SERVER_EVERY_S),
                     "--log"], "", False)]
    else:
        targets = [(SERVER, "watch_server.py",
                    ["--port", str(PORT), "--every", str(BOTH_SERVER_EVERY_S)],
                    "server  ", False), chat]
    sys.exit(watch(targets))


if __name__ == "__main__":
    main()
