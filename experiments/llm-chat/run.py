#!/usr/bin/env python3
"""A 50-turn scripted chat between two instances, timed on both.

    ./run.py                      # server from the image, client, session, teardown
    ./run.py --no-prefix-cache    # the same with vLLM's prefix cache off
    ./run.py --keep               # leave both running afterwards

`llm-server` is a g2-standard-4 (one L4) booted from the newest image in the
family, serving the model with vLLM. `llm-client` is an e2-small that asks the
questions in questions.json over the internal network. If the family has no
image, image.py builds one first (about 10 minutes, once).

Every setting and the reason for it is in common.py; the result file records
them. Output is one line per turn while the session runs, then a summary.
Everything created is deleted in a `finally`, including on Ctrl-C, except the
image, which is kept for the next run.
"""

import argparse
import hashlib
import json
import os
import shlex
import statistics
import subprocess
import time
import uuid
from datetime import datetime, timezone

import common
from client import percentile
from common import (CLIENT_DISK_GB, CLIENT_TYPE, FAMILY, GPU_DISK_GB,
                    GPU_SAMPLE_S, GPU_TYPE, HERE, MAX_MODEL_LEN, MAX_RUN_MIN,
                    MAX_TOKENS,
                    PORT, RESULTS, SEED, SPOT, TEMPERATURE, VLLM_ENV, compute_v1,
                    create, create_gpu,
                    delete_instances, glab, log, newest_image, scp_from,
                    scp_to, show_leftovers, ssh, state, stream, wait_running,
                    wait_ssh, NotFound)

SERVER, CLIENT = "llm-server", "llm-client"
PORT_RULE = "lab-llm-chat-port"
GPU_FIELDS = ["timestamp", "utilization.gpu", "utilization.memory",
              "memory.used", "power.draw", "temperature.gpu", "clocks.sm"]


def port_rule(name):
    """Allow the chat port from lab instances to lab instances. The source is
    the network tag, not an address range, so nothing outside the VPC is let
    in."""
    client = compute_v1.FirewallsClient()
    project = glab.current_project()
    try:
        client.get(project=project, firewall=name)
        return
    except NotFound:
        pass
    rule = compute_v1.Firewall(
        name=name,
        description="llm-chat: vLLM port between lab instances",
        network="global/networks/default", direction="INGRESS",
        source_tags=[glab.NETWORK_TAG], target_tags=[glab.NETWORK_TAG],
        allowed=[compute_v1.Allowed(I_p_protocol="tcp", ports=[str(PORT)])],
    )
    glab.wait(client.insert(project=project, firewall_resource=rule))
    log(f"created firewall rule {name} (tcp:{PORT}, lab -> lab)")


def drop_port_rule(name):
    try:
        glab.wait(compute_v1.FirewallsClient().delete(
            project=glab.current_project(), firewall=name))
        log(f"deleted firewall rule {name}")
    except NotFound:
        pass
    except Exception as e:
        log(f"could not delete {name}: {e}")


def pick(d, suffix):
    values = [v for k, v in (d or {}).items() if k.endswith(suffix)]
    return sum(values) if values else None


def parse_gpu_csv(text):
    """nvidia-smi --format=csv,noheader,nounits rows as dicts, with the
    timestamp as epoch seconds. The server's clock is UTC."""
    rows = []
    for line in text.splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) != len(GPU_FIELDS):
            continue
        try:
            t = datetime.strptime(parts[0], "%Y/%m/%d %H:%M:%S.%f") \
                .replace(tzinfo=timezone.utc).timestamp()
            values = [float(v) for v in parts[1:]]
        except ValueError:
            continue
        rows.append(dict(zip(GPU_FIELDS, [t] + values)))
    return rows


def summarize(turns, gpu, start, end):
    """The numbers the report prints, from the per-turn records and the GPU
    samples taken between `start` and `end`."""
    def col(key):
        return [t.get(key) for t in turns]

    for t in turns:
        s = t.get("server") or {}
        for name, suffix in [("server_ttft_ms", "time_to_first_token_seconds_sum"),
                             ("server_prefill_ms", "request_prefill_time_seconds_sum"),
                             ("server_decode_ms", "request_decode_time_seconds_sum"),
                             ("server_queue_ms", "request_queue_time_seconds_sum"),
                             ("server_e2e_ms", "e2e_request_latency_seconds_sum")]:
            v = pick(s, suffix)
            t[name] = round(v * 1000, 2) if v is not None else None
        t["ttft_overhead_ms"] = (round(t["ttft_ms"] - t["server_ttft_ms"], 2)
                                 if t.get("ttft_ms") is not None
                                 and t.get("server_ttft_ms") is not None else None)

    out = {}
    for key in ("ttft_ms", "server_ttft_ms", "ttft_overhead_ms",
                "server_prefill_ms", "server_queue_ms", "decode_tok_s",
                "total_ms", "gap_ms_mean", "gap_ms_p99"):
        values = [v for v in col(key) if v is not None]
        if values:
            out[key] = {"p50": percentile(values, 50),
                        "p90": percentile(values, 90),
                        "min": min(values), "max": max(values),
                        "mean": round(statistics.mean(values), 3)}
    cache = [(pick(t.get("server"), "prefix_cache_hits_total"),
              pick(t.get("server"), "prefix_cache_queries_total")) for t in turns]
    if cache and all(h is not None and q is not None for h, q in cache):
        hits = sum(h for h, _ in cache)
        queries = sum(q for _, q in cache)
        out["prefix_hit_rate"] = round(hits / queries, 4) if queries else None
    else:
        out["prefix_hit_rate"] = None
    out["prompt_tokens_last"] = turns[-1].get("prompt_tokens") if turns else None
    counts = [t.get("completion_tokens") for t in turns]
    out["completion_tokens"] = (sum(counts) if all(n is not None for n in counts)
                                else None)
    out["session_s"] = round(end - start, 1) if turns else None

    during = [g for g in gpu if start <= g["timestamp"] <= end]
    if during:
        out["gpu"] = {
            "samples": len(during),
            "util_mean": round(statistics.mean(g["utilization.gpu"] for g in during), 1),
            "util_max": max(g["utilization.gpu"] for g in during),
            "power_w_mean": round(statistics.mean(g["power.draw"] for g in during), 1),
            "power_w_max": max(g["power.draw"] for g in during),
            "memory_mib_max": max(g["memory.used"] for g in during),
            "temp_c_max": max(g["temperature.gpu"] for g in during),
            "sm_mhz_mean": round(statistics.mean(g["clocks.sm"] for g in during)),
        }
    return out


def fmt(v, spec):
    return format(v, spec) if v is not None else "-"


def report(result):
    s, d = result.get("summary", {}), result["decisions"]
    v = result.get("versions", {})
    print()
    print(f"llm-chat: {d['MODEL']} on {d['GPU_TYPE']} "
          f"({'spot' if result['spot'] else 'on-demand'}), client {d['CLIENT_TYPE']}, "
          f"{result['zone']}")
    print(f"  vLLM {v.get('vllm', '?')}, torch {v.get('torch', '?')}, "
          f"driver {v.get('nvidia_driver', '?')}, prefix cache "
          f"{'off' if result['no_prefix_cache'] else 'on'}, image {result['image']}")
    c = result.get("cold_start_s", {})
    print(f"  cold start: running -> ssh {fmt(c.get('running_to_ssh'), '.0f')}s, "
          f"vllm launch -> ready {fmt(c.get('launch_to_ready'), '.0f')}s")
    tcp = result.get("tcp_connect_ms") or []
    if tcp:
        print(f"  network: TCP connect client -> server median "
              f"{statistics.median(tcp):.3f} ms over {len(tcp)}")
    turns = result.get("turns", [])
    print(f"  {len(turns)}/{result['questions']} turns, "
          f"{fmt(s.get('completion_tokens'), 'd')} tokens generated, context reached "
          f"{s.get('prompt_tokens_last')} tokens, session "
          f"{fmt(s.get('session_s'), '.0f')}s"
          + ("" if result["complete"] else f"  INCOMPLETE: {result.get('incomplete')}"))
    if not turns:
        return
    print()
    print(f"  {'':34}{'p50':>9}{'p90':>9}{'min':>9}{'max':>9}")
    for key, label in [("ttft_ms", "time to first token, client (ms)"),
                       ("server_ttft_ms", "time to first token, server (ms)"),
                       ("ttft_overhead_ms", "  client minus server TTFT (ms)"),
                       ("server_prefill_ms", "prefill, server (ms)"),
                       ("server_queue_ms", "queue, server (ms)"),
                       ("decode_tok_s", "decode rate (tokens/s)"),
                       ("gap_ms_p99", "p99 gap between chunks (ms)"),
                       ("total_ms", "whole answer (ms)")]:
        if key in s:
            x = s[key]
            print(f"  {label:34}" + "".join(f"{x[k]:9.1f}" for k in
                                             ("p50", "p90", "min", "max")))
    if s.get("prefix_hit_rate") is not None:
        print(f"  prefix cache hit rate over the session: "
              f"{s['prefix_hit_rate'] * 100:.1f}% of prompt tokens")
    g = s.get("gpu")
    if g:
        print(f"  GPU during the session: util mean {g['util_mean']}% "
              f"max {g['util_max']:.0f}%, power mean {g['power_w_mean']} W "
              f"max {g['power_w_max']:.0f} W, memory max {g['memory_mib_max']:.0f} MiB, "
              f"temp max {g['temp_c_max']:.0f} C, SM clock mean {g['sm_mhz_mean']} MHz")
    print()
    print("  turn  context  ttft ms  server ttft  prefill ms  tok/s  cache hit")
    for t in turns:
        if t["turn"] in (1, 2, 3, 5) or t["turn"] % 10 == 0 or t is turns[-1]:
            hit = t.get("prefix_hit_rate")
            print(f"  {t['turn']:4d}  {fmt(t.get('prompt_tokens'), '7d')}  "
                  f"{fmt(t.get('ttft_ms'), '7.1f')}  {fmt(t.get('server_ttft_ms'), '11.1f')}  "
                  f"{fmt(t.get('server_prefill_ms'), '10.1f')}  "
                  f"{fmt(t.get('decode_tok_s'), '5.1f')}  "
                  f"{fmt(hit * 100 if hit is not None else None, '8.0f')}%")


def collect(result, records, marks, work, n_questions, running_at, ssh_at,
            launched_at):
    """Fill in the result from whatever the run got to: the records streamed
    from the client, the GPU samples if they were fetched, and why the run
    stopped if it did not finish."""
    turns = [r for r in records if r.get("kind") == "turn"]
    net = [r for r in records if r.get("kind") == "network"]
    if records:
        os.makedirs(work, exist_ok=True)
        with open(os.path.join(work, "turns.jsonl"), "w") as f:
            f.writelines(json.dumps(r) + "\n" for r in records)
    result["turns"] = turns
    result["tcp_connect_ms"] = net[0]["tcp_connect_ms"] if net else []
    result["cold_start_s"] = {
        "running_to_ssh": (round(ssh_at - running_at, 1)
                           if ssh_at and running_at else None),
        "launch_to_ready": (round(marks["ready"] - launched_at, 1)
                            if "ready" in marks and launched_at else None),
    }
    gpu_path = os.path.join(work, "gpu.csv")
    gpu = parse_gpu_csv(open(gpu_path).read()) if os.path.exists(gpu_path) else []
    result["gpu_samples"] = gpu
    if turns:
        start = turns[0]["wall"]
        end = turns[-1]["wall"] + turns[-1]["total_ms"] / 1000
        result["summary"] = summarize(turns, gpu, start, end)
    states = marks.get("states", {})
    stopped = {n: s for n, s in states.items() if s != "running"}
    code = marks.get("client_exit")
    if "error" in marks:
        result["incomplete"] = f"{marks['error']} after {len(turns)} turns"
    elif stopped:
        result["incomplete"] = (", ".join(f"{n} is {s}" for n, s in stopped.items())
                                + f" after {len(turns)} turns"
                                + ("; spot preemption likely" if result["spot"] else ""))
    elif code or len(turns) < n_questions:
        result["incomplete"] = f"client exited {code} after {len(turns)} turns"
    else:
        result["complete"] = True


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--no-prefix-cache", action="store_true",
                   help="start vLLM with prefix caching off")
    p.add_argument("--on-demand", action="store_true",
                   help="on-demand instead of spot for both instances")
    p.add_argument("--questions", default=os.path.join(HERE, "questions.json"))
    p.add_argument("--keep", action="store_true",
                   help="leave both instances running")
    p.add_argument("--zone", help="zone to try first instead of `glab zone`; "
                   "the others in common.ZONES are tried after it")
    args = p.parse_args()

    zone = args.zone or glab.current_zone()
    spot = SPOT and not args.on_demand
    prices = glab.SPOT_PRICES if spot else glab.PRICES
    per_hour = prices.get(GPU_TYPE, 0) + prices.get(CLIENT_TYPE, 0)
    questions_raw = open(args.questions, "rb").read()
    n_questions = len(json.loads(questions_raw)["questions"])

    image = newest_image()
    if image is None:
        log(f"no image in family {FAMILY}; building one first (about 10 min, "
            "about $0.15, once)")
        import image as image_builder
        image_builder.build(zone)
        image = newest_image()
    log(f"project {glab.current_project()}, zone {zone}: {GPU_TYPE} server and "
        f"{CLIENT_TYPE} client, {'spot' if spot else 'on-demand'}, "
        f"~${per_hour:.2f}/h together, about ${per_hour * 0.2:.2f} for a run")
    log(f"image {image.name}; Ctrl-C at any point still deletes the instances, "
        f"and Compute Engine deletes them {MAX_RUN_MIN} min after they start "
        f"if nothing else does")

    started = time.time()
    run_id = uuid.uuid4().hex
    rule_name = f"{PORT_RULE}-{run_id[:12]}"
    placed = []
    result = {
        "started": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "zone": zone, "image": image.name, "spot": spot,
        "no_prefix_cache": args.no_prefix_cache,
        "decisions": common.decisions(),
        "questions_file": os.path.relpath(args.questions, HERE),
        "questions_sha256": hashlib.sha256(questions_raw).hexdigest(),
        "questions": n_questions, "complete": False, "turns": [],
    }
    try:
        result["image_description"] = json.loads(image.description)
    except (TypeError, ValueError):
        pass
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    work = os.path.join(RESULTS, f"{stamp}-work")
    records, marks = [], {}
    running_at = ssh_at = launched_at = None

    try:
        port_rule(rule_name)
        log(f"creating {SERVER} ({GPU_TYPE}) from {image.name}")
        zone = create_gpu(SERVER, zone, GPU_TYPE, GPU_DISK_GB, spot,
                          MAX_RUN_MIN, image=image.name,
                          run_id=run_id, placed=placed)
        result["zone"] = zone
        log(f"creating {CLIENT} ({CLIENT_TYPE})")
        placed.append((CLIENT, zone))
        create(CLIENT, zone, CLIENT_TYPE, CLIENT_DISK_GB, spot, MAX_RUN_MIN,
               run_id=run_id)

        server = wait_running(SERVER)
        running_at = time.time()
        client = wait_running(CLIENT)
        log(f"{SERVER} running at {server['private_ip']}, {CLIENT} at "
            f"{client['private_ip']}")

        log(f"waiting for ssh on {SERVER}")
        wait_ssh(SERVER, zone)
        ssh_at = time.time()
        r = ssh(SERVER, zone, "cat /opt/lab/versions.json",
                capture_output=True, text=True)
        versions = json.loads(r.stdout.strip().splitlines()[-1])
        result["versions"] = versions

        vllm = (f"/opt/vllm/bin/vllm serve {versions['model_dir']} "
                f"--served-model-name {versions['served_name']} "
                f"--host 0.0.0.0 --port {PORT} --max-model-len {MAX_MODEL_LEN} "
                f"--seed {SEED}"
                + (" --no-enable-prefix-caching" if args.no_prefix_cache else ""))
        gpulog = (f"stdbuf -oL nvidia-smi --query-gpu={','.join(GPU_FIELDS)} "
                  f"--format=csv,noheader,nounits -l {GPU_SAMPLE_S} > /tmp/gpu.csv")
        # systemd-run detaches both from the ssh session and keeps their
        # output in the journal. VLLM_ENV is explained in common.py.
        setenv = " ".join(f"--setenv={k}={v}" for k, v in VLLM_ENV.items())
        # Write line-buffered stdout: nvidia-smi -f buffers its file and can
        # leave the newest samples out of the copy collected after a short run.
        launch = (f"sudo systemd-run --quiet --unit gpulog /bin/sh -c "
                  f"{shlex.quote(gpulog)} && "
                  f"sudo systemd-run --quiet --unit vllm {setenv} {vllm}")
        log(f"on {SERVER}: {vllm}")
        r = ssh(SERVER, zone, launch, capture_output=True, text=True)
        launched_at = time.time()
        if r.returncode:
            glab.die(f"could not start vLLM: {r.stdout}{r.stderr}")
        result["vllm_command"] = vllm

        log(f"waiting for ssh on {CLIENT}, then copying the client")
        wait_ssh(CLIENT, zone)
        for f in ("client.py", os.path.basename(args.questions)):
            src = os.path.join(HERE, f) if f == "client.py" else args.questions
            r = scp_to(CLIENT, zone, src, f)
            if r.returncode:
                glab.die(f"could not copy {f}: {r.stderr}")

        def on_line(line):
            line = line.rstrip("\n")
            if line.startswith("RECORD "):
                try:
                    records.append(json.loads(line[len("RECORD "):]))
                except ValueError:
                    log(f"  unreadable record: {line[:80]}")
                return
            if line.startswith("READY "):
                marks["ready"] = time.time()
            if line.startswith(("TURN", "READY", "TCP", "ERROR", "DONE")):
                log(line)
            elif line.strip():
                log(f"  {line}")

        log(f"on {CLIENT}: waiting for vLLM, then {n_questions} questions")
        code, _ = stream(CLIENT, zone,
                         f"python3 client.py --server {server['private_ip']} "
                         f"--port {PORT} --model {versions['served_name']} "
                         f"--questions {os.path.basename(args.questions)} "
                         f"--out turns.jsonl --max-tokens {MAX_TOKENS} "
                         f"--temperature {TEMPERATURE} --seed {SEED}",
                         on_line=on_line)
        marks["client_exit"] = code

        # The turns came back over the ssh stream. Only the server's GPU
        # samples and log are still on an instance, and only worth fetching
        # if it is still up.
        states = {n: state(n, zone) for n in (SERVER, CLIENT)}
        marks["states"] = states
        os.makedirs(work, exist_ok=True)
        if states[SERVER] == "running":
            try:
                ssh(SERVER, zone, "sudo journalctl -u vllm --no-pager > /tmp/vllm.log",
                    capture_output=True, timeout=120)
            except subprocess.TimeoutExpired:
                log(f"writing the vLLM log on {SERVER} timed out")
            scp_from(SERVER, zone, "/tmp/gpu.csv", os.path.join(work, "gpu.csv"))
            scp_from(SERVER, zone, "/tmp/vllm.log", os.path.join(work, "vllm.log"))
    except BaseException as e:
        marks["error"] = f"{type(e).__name__}: {e}".strip()
        raise
    finally:
        try:
            collect(result, records, marks, work, n_questions,
                    running_at, ssh_at, launched_at)
        except Exception as e:  # never skip the deletes below
            log(f"could not assemble the result: {e}")
        if args.keep:
            log(f"--keep: {SERVER} and {CLIENT} left running until Compute "
                f"Engine deletes them, {MAX_RUN_MIN} min after they started. "
                f"`glab destroy --all` sooner, and delete firewall rule {rule_name}.")
        else:
            delete_instances(placed, run_id)
            drop_port_rule(rule_name)
        glab.write_config(zone=zone)
        result["elapsed_s"] = round(time.time() - started, 1)
        if result.get("versions"):
            os.makedirs(RESULTS, exist_ok=True)
            name = (f"{stamp}-{zone}-{result['versions']['served_name']}"
                    f"{'-nocache' if args.no_prefix_cache else ''}.json")
            out = os.path.join(RESULTS, name)
            with open(out, "w") as f:
                json.dump(result, f, indent=1)
            report(result)
            print(f"\n{result['elapsed_s'] / 60:.1f} min total. every turn, "
                  f"answer and GPU sample is in {os.path.relpath(out, os.getcwd())}")
            print(f"the vLLM log and raw files are in "
                  f"{os.path.relpath(work, os.getcwd())}")
        show_leftovers()


if __name__ == "__main__":
    main()
