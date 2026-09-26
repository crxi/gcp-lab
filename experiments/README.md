# experiments

One directory per measurement. Each has a script that runs end to end, a
README with the details, and a `results/` directory of JSON, one file per
run. This page is the summary: how to run each, what it costs, and what one
run gave.

| Directory | Measures | One run | Cost per run |
|---|---|---|---|
| `ping-latency/` | ICMP round-trip time between two instances | ~3 min | < $0.01 |
| `llm-chat/` | a 50-turn chat with Qwen2.5-3B on an L4, timed on client and server | ~8 min | ~$0.07 spot, ~$0.12 on-demand; the first run also builds an image, ~$0.15 once |

## Before either

```
./setup.sh                    # installs glab, checks gcloud and credentials
glab project PROJECT_ID
glab zone asia-southeast1-a
glab quota                    # llm-chat needs one L4: GPUS_ALL_REGIONS, and the
                              # region's L4 count (spot runs: the preemptible one)
```

Each script creates what it needs, deletes it in a `finally` (Ctrl-C
included), and ends by printing `glab list --all-zones` so the last lines
show whether anything is left.

## ping-latency

```
cd experiments/ping-latency
./run.py                              # two spot e2-micro, same zone, 60 s of ping
./run.py --zone-b asia-southeast1-c   # across zones
```

Two spot `e2-micro` in `asia-southeast1-b`, 2026-09-25, 60 s at 0.2 s
intervals:

| Replies | min | p50 | p90 | p99 | max | mean (ms) |
|---|---|---|---|---|---|---|
| 295/295 | 0.192 | 0.262 | 0.319 | 0.397 | 1.360 | 0.272 |

The maximum was the first reply. A second run the same day averaged 0.225 ms.
Details: `ping-latency/README.md`.

## llm-chat

```
cd experiments/llm-chat
./run.py                      # spot; builds the image first if there is none
./run.py --on-demand
./run.py --no-prefix-cache    # the same with vLLM's prefix cache off
```

`llm-server` (g2-standard-4, one L4) serves Qwen2.5-3B-Instruct with vLLM
0.30.0 from a prebuilt image. `llm-client` (e2-small) in the same zone sends
50 scripted questions, each with the whole conversation so far, and times
the stream. vLLM's own metrics are read before and after every turn.

Spot, `asia-southeast1-a`, 2026-09-25, 50/50 turns, context 63 to 10,744
tokens:

| | p50 | p90 | min | max |
|---|---|---|---|---|
| time to first token, client (ms) | 65.4 | 84.1 | 40.9 | 90.4 |
| time to first token, server (ms) | 58.4 | 74.6 | 38.2 | 81.1 |
| network + HTTP, the difference (ms) | 6.7 | 9.3 | 2.3 | 10.3 |
| decode rate (tokens/s) | 37.6 | 38.6 | 36.5 | 39.2 |

Prefix-cache hit rate 99.3% of prompt tokens. vLLM ready 108 s after launch.
TCP connect client to server 0.45 ms median. GPU utilisation 99% mean at
72 W. Details and the per-turn table: `llm-chat/README.md`.

## Cost

Rates are list prices for `asia-southeast1`; `../COSTS.md` has the table
and the worked estimates for larger experiments.

**Per run.** Both experiments are billed by the minute while their instances
exist. llm-chat's two instances cost ~$0.54/h together on spot, ~$0.89/h
on-demand; a run is ~8 minutes. ping-latency's two e2-micro are under
$0.01/h.

**Recurring.** llm-chat keeps its image between runs: 11.6 GB stored,
$0.64/month, shown by `glab images`. `glab image-delete NAME` removes it;
the next `run.py` then builds a new one (~10 minutes of on-demand G2 with an
external IP, ~$0.15). Nothing else survives a run.

**Spot.** Both default to spot: 40% off on G2 and E2, 73% on N1. A spot
instance can be taken back at any time. On 2026-09-25 one image build and
one llm-chat session were preempted. llm-chat keeps the turns finished before
a preemption and marks the run incomplete. The image build runs on-demand,
because a preempted build loses its work.

**Run-time limit.** Spot does not bound the cost: a spot instance runs until
Google wants the capacity, possibly days. Every instance these scripts create
carries a `max_run_duration` (`glab init --max-run`), after which Compute
Engine deletes it and its disk, whether or not this machine is still on:

| | Limit | A normal run |
|---|---|---|
| ping-latency | 20 min + ping time | ~3 min |
| llm-chat session | 45 min | ~8 min |
| llm-chat image build | 60 min | ~10 min |

The worst case for an abandoned llm-chat session is therefore 45 minutes of
both instances, ~$0.40 spot. The firewall rule a script adds may be left
behind; it costs nothing and the next run reuses it.

**Capacity.** L4s in `asia-southeast1` ran out repeatedly on 2026-09-25, in
every zone at some point, spot and on-demand. llm-chat tries each zone in
`common.ZONES` before giving up; a refused insert costs nothing.

**Spent so far.** Estimated from the run logs, not the bill: about $1.20 up to
2026-09-26, of which ~$0.95 was getting the image build to work (one
preempted spot builder whose disk then sat for 10 hours, two failed builds
left up for inspection, one good build). The Cloud Billing console has the
actual figure.

## Adding an experiment

Settings that shape a result go in the script, next to the value, with the
reason, and the result file records them. The experiment's README points to
them without repeating the values, so it does not go stale when one changes.
Create instances with a `max_run`, delete them in a `finally`, and end with
`glab list --all-zones`.
