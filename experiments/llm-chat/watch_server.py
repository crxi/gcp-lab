"""Runs on llm-server; watch.py sends it there over ssh.

Every few seconds prints one status line: the GPU from nvidia-smi, and
vLLM's own counters from its /metrics endpoint. With --log it also prints
vLLM's log as it is written. Standard library only.

It reads; it does not change the run. Each status line costs one nvidia-smi
query and one GET of /metrics on the server.
"""

import argparse
import re
import subprocess
import sys
import threading
import time
import urllib.request

METRIC = re.compile(r"^([a-zA-Z_:][a-zA-Z0-9_:]*)(\{.*\})?\s+(\S+)$")
GPU_QUERY = "utilization.gpu,memory.used,memory.total,power.draw,temperature.gpu"
lock = threading.Lock()


def say(text):
    with lock:
        print(text, flush=True)


def gpu():
    """(utilisation %, MiB used, MiB total, W, degrees C), or None."""
    try:
        r = subprocess.run(["nvidia-smi", f"--query-gpu={GPU_QUERY}",
                            "--format=csv,noheader,nounits"],
                           capture_output=True, text=True, timeout=10)
        return tuple(float(x) for x in r.stdout.splitlines()[0].split(","))
    except (OSError, IndexError, ValueError, subprocess.TimeoutExpired):
        return None


def metrics(port):
    """vLLM's counters summed over label sets, like client.py's scrape; None
    while vLLM is not answering."""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/metrics",
                                    timeout=3) as r:
            text = r.read().decode()
    except OSError:
        return None
    out = {}
    for line in text.splitlines():
        m = METRIC.match(line)
        if not m or m.group(1).endswith("_bucket") or m.group(3) == "NaN":
            continue
        out[m.group(1)] = out.get(m.group(1), 0.0) + float(m.group(3))
    return out


def pick(m, suffix):
    values = [v for k, v in m.items() if k.endswith(suffix)]
    return sum(values) if values else None


def status(g, m, before, elapsed):
    """One status line. `before` is the previous sample's metrics, for the
    token rate."""
    parts = []
    if g:
        util, used, total, watts, temp = g
        parts.append(f"GPU {util:3.0f}%  {used / 1024:4.1f}/{total / 1024:.1f} GiB  "
                     f"{watts:3.0f} W  {temp:.0f} C")
    else:
        parts.append("GPU: nvidia-smi gave nothing")
    if m is None:
        parts.append("vLLM not answering yet (loading takes about 100 s)")
        return "  |  ".join(parts)
    running = pick(m, "num_requests_running")
    waiting = pick(m, "num_requests_waiting")
    kv = pick(m, "kv_cache_usage_perc")
    parts.append(f"requests {running or 0:.0f} running {waiting or 0:.0f} waiting"
                 + (f"  KV cache {kv * 100:.0f}% full" if kv is not None else ""))
    made = pick(m, "generation_tokens_total")
    if before is not None and made is not None and elapsed > 0:
        parts.append(f"{(made - (pick(before, 'generation_tokens_total') or 0)) / elapsed:4.0f} "
                     "tokens/s generated")
    hits = pick(m, "prefix_cache_hits_total")
    queries = pick(m, "prefix_cache_queries_total")
    if hits is not None and queries:
        parts.append(f"prefix cache hits {hits / queries * 100:.1f}% so far")
    return "  |  ".join(parts)


def follow_log():
    """vLLM's journal as it is written, without the access-log line for each
    /metrics request (the client and this program both scrape it)."""
    proc = subprocess.Popen(
        ["sudo", "-n", "journalctl", "-u", "vllm", "-f", "-n", "20",
         "-o", "cat", "--no-pager"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    for line in proc.stdout:
        if "GET /metrics" not in line:
            say("vllm: " + line.rstrip())


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--every", type=float, default=2.0, help="seconds")
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--log", action="store_true", help="also print vLLM's log")
    args = p.parse_args()

    if args.log:
        threading.Thread(target=follow_log, daemon=True).start()
    before, then = None, time.time()
    while True:
        m = metrics(args.port)
        now = time.time()
        say(status(gpu(), m, before, now - then))
        before, then = m, now
        time.sleep(args.every)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
