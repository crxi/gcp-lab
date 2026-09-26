# llm-chat

A scripted 50-turn chat between two instances in one zone. `llm-server` serves
a small model with vLLM on one L4. `llm-client` asks the questions in
`questions.json` over the internal network, sending the whole conversation
each turn the way a chat UI does. Timings are taken on both sides.

```
./run.py                      # the whole thing: about 8 minutes, about $0.07
./run.py --no-prefix-cache    # the same with vLLM's prefix cache off
./run.py --on-demand          # on-demand instead of spot
./run.py --keep               # leave both instances running afterwards
./run.py --zone ZONE          # zone to try first
./image.py                    # build the image only (run.py does this if needed)
./image.py --rebuild          # build a new image even if one exists
./image.py --keep             # on a failed build, leave llm-build up to inspect
```

The GPU instance goes in the zone asked for (`--zone`, else `glab zone`); if
that zone has no L4 capacity, the other zones in `common.ZONES` are tried in
order, and the client follows the server.

To watch a run, `glab shell llm-server` from another terminal, then
`journalctl -u vllm -f` or `watch -n1 nvidia-smi`; `glab shell llm-client`,
then `tail -f turns.jsonl`. Both are deleted about a minute after the last
turn unless `--keep`.

Needs `glab project` and `glab zone` set, and GPU quota for one L4 in the
zone's region (`glab quota`).

## Settings

Every setting that shapes a result, and the reason for it, is in the
decisions block at the top of `common.py`: model, image family, machine
types, base image, zones, spot, run-time limits, vLLM version and environment, temperature,
seed, answer length, context length.
Change a value and its comment there, together. Each result file copies the
whole block under `decisions`, plus the vLLM, torch and driver versions read
from the image, the exact `vllm serve` command, and the sha256 of the
questions file, so an old result says what produced it.

## The image

The server needs vLLM and the model weights, and has no route to the
internet. They are baked into an image once:

1. `image.py` creates `llm-build` from the base image in `common.py`, with a
   temporary external IP.
2. `provision.sh` runs on it: installs the compiler and Python headers that
   vLLM's Triton kernels need, installs the pinned vLLM into `/opt/vllm`, downloads the
   model to `/opt/models/`, starts vLLM offline and asks one question, and
   writes `/opt/lab/versions.json`.
3. The builder is stopped, its disk becomes an image in the family named in
   `common.py`, and the builder is deleted along with its external IP.

`run.py` uses the newest image in the family and builds one only if the
family is empty. The image is kept between runs. `glab images` shows it and
its monthly storage cost; `glab image-delete NAME` removes it. The build log
is `results/build-<image>.log`.

## What a run does

1. Adds firewall rule `lab-llm-chat-port`: the vLLM port, from the `lab` tag
   to the `lab` tag only.
2. Creates `llm-server` from the image and `llm-client` from Debian 12. Neither
   has an external IP.
3. Starts `nvidia-smi` sampling and `vllm serve` on the server under
   `systemd-run`, offline.
4. Copies `client.py` and the questions to the client and runs it:
   - waits for vLLM's `/health`;
   - times 20 TCP connects to the server's port;
   - asks the 50 questions in order, one line of output per turn.
   Each per-turn record is printed as a `RECORD` line as well as written to
   a file, so run.py has every finished turn even if the client is lost.
5. Checks both instances are still running, then copies back the GPU
   samples and the vLLM log from the server.
6. Deletes both instances and the firewall rule, writes the result, prints
   the summary, then `glab list --all-zones` and `glab images`.

## What is measured

| Where | Per turn |
|---|---|
| Client | time to first token, time to response headers, whole-answer time, decode rate, gaps between streamed chunks (mean, p99, max), prompt and completion tokens |
| Server | the change in vLLM's `/metrics` across the turn: time to first token, prefill, decode and queue time, prefix-cache hits and queries |

Once per run: the server's cold start (RUNNING to ssh, `vllm serve` to
ready), TCP connect time between the two, and `nvidia-smi` every second
(GPU and memory utilisation, memory used, power, temperature, SM clock).

The client's time to first token minus the server's is the network and HTTP
part. The prefix-cache hit rate shows how much of each growing prompt vLLM
reused from the previous turn instead of recomputing.

A run that stops early (preemption, an error, Ctrl-C) keeps the turns that
finished, is marked `complete: false` with the reason, and the summary says
INCOMPLETE.

## Cost

Two spot instances, `g2-standard-4` and `e2-small`, at about $0.54/hour
together, for the ~8 minutes a run takes: about $0.07. On-demand is about
$0.89/hour, $0.12 a run.

Every instance is created with a run-time limit (`MAX_RUN_MIN`,
`BUILD_MAX_RUN_MIN` in `common.py`): Compute Engine deletes it, disk
included, that long after it starts. If this machine shuts down or loses its
connection mid-run, nothing is left billing past the limit. Spot on its own
does not do this; a spot instance runs until Google needs the capacity. The
firewall rule may be left behind in that case; it costs nothing, and the
next run reuses it. Building the image once took 10 minutes of
on-demand G2 plus the external IP (2026-09-25): about $0.15. Keeping the image costs $0.055/GiB-month of stored size; `glab images` prints the
figure.

## Results

`results/20260925T163229Z-asia-southeast1-a-qwen2.5-3b-instruct.json`,
2026-09-25, spot, asia-southeast1-a (c had no spot L4 and a, b and c no
on-demand L4 that hour). vLLM 0.30.0, torch 2.13.0, driver 595.91.07, prefix
cache on. 50/50 turns, 9,458 tokens generated, context 63 to 10,744 tokens,
session 255 s, whole run 8.2 minutes.

|  | p50 | p90 | min | max |
|---|---|---|---|---|
| time to first token, client (ms) | 65.4 | 84.1 | 40.9 | 90.4 |
| time to first token, server (ms) | 58.4 | 74.6 | 38.2 | 81.1 |
| difference: network + HTTP (ms) | 6.7 | 9.3 | 2.3 | 10.3 |
| prefill, server (ms) | 37.4 | 41.6 | 32.3 | 42.9 |
| decode rate (tokens/s) | 37.6 | 38.6 | 36.5 | 39.2 |
| p99 gap between tokens (ms) | 27.2 | 27.9 | 26.2 | 28.1 |

- Cold start: RUNNING to ssh 24 s; `vllm serve` to ready 108 s, most of it
  torch.compile.
- TCP connect client to server: 0.45 ms median over 20.
- Prefix cache: 99.3% of prompt tokens over the session were served from
  cache. Turn 2 was 83%, turns 40 onward 100%.
- Time to first token grows with the context, 41 ms at turn 2 (174 tokens)
  to 87 ms at turn 50 (10,744 tokens), even with every prompt token cached.
  The server's prefill time grows only from 32 to 43 ms over the same turns.
- Decode is 37 to 39 tokens/s throughout. A 3B model in BF16 reads ~6 GB of
  weights per token; at the L4's 300 GB/s that caps decode near 50
  tokens/s for one stream.
- GPU during the session: utilisation mean 99.4%, power mean 72 W (limit
  72 W), memory 20,930 MiB (vLLM reserves 92% of the 22 GiB it sees at start: 6.1 GiB
  weights, 13.6 GiB KV cache), temperature up
  to 81 C, SM clock mean 1,454 MHz.
