#!/usr/bin/env python3
"""The chat client. Runs on the client instance, standard library only, so the
client needs no internet and no packages.

    python3 client.py --server 10.148.0.5 --questions questions.json --out turns.jsonl

Waits for vLLM's /health, measures TCP connect time to the server, then asks
the scripted questions in order, sending the whole conversation each time the
way a chat UI does. For every turn it records client-side timings from the
token stream and the change in the server's /metrics counters across the turn,
writes one JSON line, and prints one progress line.
"""

import argparse
import http.client
import json
import re
import socket
import statistics
import sys
import time

PORT = 8000  # run.py passes common.PORT with --port
METRIC = re.compile(r"^(vllm:[a-z0-9_:]+)(\{[^}]*\})?\s+([-+0-9.eE]+|NaN)$")


def say(line):
    print(line, flush=True)


def record(out, rec):
    """Write a record to the file and to stdout. run.py reads the stdout copy
    as it arrives, so turns finished before a preemption are not lost with
    this instance's disk."""
    line = json.dumps(rec)
    out.write(line + "\n")
    out.flush()
    say(f"RECORD {line}")


def get(host, path, timeout=10):
    conn = http.client.HTTPConnection(host, PORT, timeout=timeout)
    try:
        conn.request("GET", path)
        r = conn.getresponse()
        return r.status, r.read().decode()
    finally:
        conn.close()


def wait_ready(host, timeout):
    start = time.time()
    while time.time() - start < timeout:
        try:
            if get(host, "/health", timeout=5)[0] == 200:
                return time.time() - start
        except OSError:
            pass
        time.sleep(1)
    return None


def connect_times(host, n=20):
    """TCP handshake to the server port, in ms. The network part of any
    request, with no ICMP rule needed."""
    out = []
    for _ in range(n):
        t = time.perf_counter()
        socket.create_connection((host, PORT), timeout=5).close()
        out.append((time.perf_counter() - t) * 1000)
        time.sleep(0.05)
    return out


def scrape(host):
    """vLLM's Prometheus metrics, summed over label sets, without histogram
    buckets. Names differ between vLLM versions, so everything is kept and the
    summary looks names up by suffix."""
    status, text = get(host, "/metrics")
    out = {}
    for line in text.splitlines():
        m = METRIC.match(line)
        if not m or m.group(1).endswith("_bucket") or m.group(3) == "NaN":
            continue
        out[m.group(1)] = out.get(m.group(1), 0.0) + float(m.group(3))
    return out


def delta(before, after):
    return {k: round(after[k] - before.get(k, 0.0), 6) for k in after
            if after[k] != before.get(k, 0.0)}


def ask(host, model, messages, max_tokens, temperature, seed):
    """One streamed chat completion. Returns the answer text and timings."""
    body = json.dumps({
        "model": model, "messages": messages, "max_tokens": max_tokens,
        "temperature": temperature, "seed": seed, "stream": True,
        "stream_options": {"include_usage": True},
    })
    conn = http.client.HTTPConnection(host, PORT, timeout=600)
    t0 = time.perf_counter()
    conn.request("POST", "/v1/chat/completions", body,
                 {"Content-Type": "application/json"})
    r = conn.getresponse()
    if r.status != 200:
        raise RuntimeError(f"HTTP {r.status}: {r.read().decode()[:500]}")
    headers_at = time.perf_counter()
    arrivals, text, usage, finish = [], [], None, None
    for raw in r:
        line = raw.decode().strip()
        if not line.startswith("data: "):
            continue
        data = line[len("data: "):]
        if data == "[DONE]":
            break
        chunk = json.loads(data)
        usage = chunk.get("usage") or usage
        for choice in chunk.get("choices", []):
            piece = (choice.get("delta") or {}).get("content")
            if piece:
                arrivals.append(time.perf_counter())
                text.append(piece)
            finish = choice.get("finish_reason") or finish
    done = time.perf_counter()
    conn.close()
    gaps = [(b - a) * 1000 for a, b in zip(arrivals, arrivals[1:])]
    completion = (usage or {}).get("completion_tokens") or len(arrivals)
    decode_s = arrivals[-1] - arrivals[0] if len(arrivals) > 1 else None
    return "".join(text), {
        "ttft_ms": round((arrivals[0] - t0) * 1000, 2) if arrivals else None,
        "headers_ms": round((headers_at - t0) * 1000, 2),
        "total_ms": round((done - t0) * 1000, 2),
        "prompt_tokens": (usage or {}).get("prompt_tokens"),
        "completion_tokens": completion,
        "chunks": len(arrivals),
        "decode_tok_s": (round((completion - 1) / decode_s, 2)
                         if decode_s else None),
        "gap_ms_mean": round(statistics.mean(gaps), 3) if gaps else None,
        "gap_ms_p99": (round(sorted(gaps)[max(0, int(len(gaps) * 0.99) - 1)], 3)
                       if gaps else None),
        "gap_ms_max": round(max(gaps), 3) if gaps else None,
        "finish_reason": finish,
    }


def pick(d, suffix):
    """Sum of the metrics whose name ends with `suffix`."""
    values = [v for k, v in d.items() if k.endswith(suffix)]
    return sum(values) if values else None


def main():
    global PORT
    p = argparse.ArgumentParser()
    p.add_argument("--server", required=True)
    p.add_argument("--questions", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--model", required=True, help="the served model name")
    p.add_argument("--port", type=int, default=PORT)
    p.add_argument("--max-tokens", type=int, required=True)
    p.add_argument("--temperature", type=float, required=True)
    p.add_argument("--seed", type=int, required=True)
    p.add_argument("--ready-timeout", type=int, default=900)
    args = p.parse_args()

    PORT = args.port
    script = json.load(open(args.questions))
    host = args.server

    say(f"READY-WAIT waiting for vLLM on {host}:{PORT}")
    waited = wait_ready(host, args.ready_timeout)
    if waited is None:
        say(f"ERROR vLLM not ready after {args.ready_timeout}s")
        sys.exit(2)
    say(f"READY after {waited:.1f}s of waiting")

    tcp = connect_times(host)
    say(f"TCP connect to :{PORT} over {len(tcp)} tries: "
        f"median {statistics.median(tcp):.3f} ms, max {max(tcp):.3f} ms")

    messages = [{"role": "system", "content": script["system"]}]
    questions = script["questions"]
    with open(args.out, "w") as out:
        record(out, {"kind": "network", "tcp_connect_ms": tcp})
        for n, question in enumerate(questions, 1):
            messages.append({"role": "user", "content": question})
            before = scrape(host)
            wall = time.time()
            try:
                answer, t = ask(host, args.model, messages, args.max_tokens,
                                args.temperature, args.seed)
            except Exception as e:
                say(f"ERROR turn {n}: {e}")
                record(out, {"kind": "error", "turn": n, "error": str(e)})
                sys.exit(3)
            server = delta(before, scrape(host))
            hits = pick(server, "prefix_cache_hits_total")
            queries = pick(server, "prefix_cache_queries_total")
            t["prefix_hit_rate"] = (round(hits / queries, 4)
                                    if hits is not None and queries else None)
            messages.append({"role": "assistant", "content": answer})
            record(out, {"kind": "turn", "turn": n, "wall": wall,
                         "question": question, "answer": answer,
                         **t, "server": server})
            hit = (f"{t['prefix_hit_rate'] * 100:3.0f}%"
                   if t["prefix_hit_rate"] is not None else "  -")
            say(f"TURN {n:2d}/{len(questions)}  context {t['prompt_tokens']:5d} tok  "
                f"ttft {t['ttft_ms']:7.1f} ms  total {t['total_ms'] / 1000:5.1f} s  "
                f"{t['completion_tokens']:3d} tok at {t['decode_tok_s'] or 0:5.1f} tok/s  "
                f"cache hit {hit}")
    say("DONE")


if __name__ == "__main__":
    main()
