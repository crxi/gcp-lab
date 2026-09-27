# Network and model experiments

Each experiment creates Google Cloud VMs, runs a measurement, saves a result
file on your computer, and removes the VMs. Complete the
[repository setup](../README.md#prepare-your-computer-and-google-cloud-project)
and first remote-connection check before running either one.

Run the commands on this page from the repository directory, with its Python
virtual environment active.

## Choose a test

| Test | Measurement | Start here |
|---|---|---|
| [ping-latency](ping-latency/README.md) | Round-trip time and packet loss between two small VMs | First test; no GPU quota needed |
| [llm-chat](llm-chat/README.md) | Response timing for a 50-turn conversation with a model on one L4 GPU | After ping works and GPU quota is available |

A VM, also called an instance, is a remote computer. A region is a geographic
area, such as Singapore (`asia-southeast1`); a zone is a deployment location
within it, such as `asia-southeast1-a`. Both tests use the working zone set by
`glab zone`, unless you pass a zone option.

## Run ping-latency

```bash
python3 experiments/ping-latency/run.py
```

Allow several minutes for VM creation, a 60-second measurement, and deletion.
The terminal prints progress and a final table of timings. The
[ping guide](ping-latency/README.md) explains that table and shows how to test
between different zones.

## Run llm-chat

First follow the [GPU quota checks](llm-chat/README.md#check-gpu-quota).
Then run:

```bash
python3 experiments/llm-chat/run.py
```

The first run also prepares a reusable VM image containing the model and
software. Allow roughly 20–30 minutes for a first run; startup and resource
availability vary. Later runs reuse the image.

The terminal prints each question's timing as it completes. To read the
answers and watch the GPU, run `python3 experiments/llm-chat/watch.py` in a
second terminal; the [llm-chat guide](llm-chat/README.md#watch-llm-chat-in-progress)
explains its output. This is an automated conversation, so you do not need
to type questions while it runs.

## Results from one run

These are single runs, kept as a reference for what a normal result looks
like. Your numbers will differ with zone, time of day, and hardware.

**ping-latency**: two Spot `e2-micro` in `asia-southeast1-a`, 2026-09-26,
60 s at 0.2 s intervals
(`ping-latency/results/20260926T024040Z-asia-southeast1-a-e2-micro.json`):

| Replies | min | p50 | p90 | p99 | max | mean (ms) |
|---|---|---|---|---|---|---|
| 295/295 | 0.166 | 0.244 | 0.284 | 0.666 | 1.200 | 0.252 |

Two earlier runs in `asia-southeast1-b` had p50 0.262 ms and 0.218 ms.

**llm-chat**: Spot `g2-standard-4` (one L4) serving Qwen2.5-3B-Instruct with
vLLM 0.30.0, and an `e2-small` client, in `asia-southeast1-a`, 2026-09-25.
50/50 turns; the conversation grew from 63 to 10,744 tokens
(`llm-chat/results/20260925T163229Z-asia-southeast1-a-qwen2.5-3b-instruct.json`):

| | p50 | p90 | min | max |
|---|---|---|---|---|
| time to first token, client (ms) | 65.4 | 84.1 | 40.9 | 90.4 |
| time to first token, server (ms) | 58.4 | 74.6 | 38.2 | 81.1 |
| client minus server TTFT (ms) | 6.7 | 9.3 | 2.3 | 10.3 |
| decode rate (tokens/s) | 37.6 | 38.6 | 36.5 | 39.2 |

Prefix-cache hit rate was 99.3% of prompt tokens. vLLM was ready 108 s after
launch. TCP connect from client to server took 0.45 ms (median). The GPU
averaged 99% utilisation at 72 W. The whole run took about 8 minutes.

## Cost

Rates are list prices for `asia-southeast1`. [COSTS.md](../COSTS.md) has the
price table and estimates for larger experiments.

| | Per run | Instances, per hour |
|---|---|---|
| ping-latency | under $0.01, about 3 minutes | two `e2-micro`, under $0.01/h |
| llm-chat | about $0.07 on Spot, $0.12 on-demand, about 8 minutes | server and client, $0.54/h Spot, $0.89/h on-demand |
| llm-chat image build (first run only) | about $0.15, about 10 minutes | one on-demand `g2-standard-4` with a public IP |

**What remains after a run.** Only the llm-chat image: 11.6 GB, about
$0.64/month, listed by `glab images`. `glab image-delete NAME` removes it;
the next llm-chat run then builds a new one.

**Spot.** Both tests use Spot by default: 40% off on G2 and E2. Google can
reclaim a Spot VM at any time; on 2026-09-25 one image build and one chat
session were reclaimed. llm-chat keeps the answers completed before that and
marks the result incomplete. The image build uses a regular VM, because a
reclaimed build loses its work.

**Deletion limits.** Spot does not limit cost by itself: a Spot VM can run
for days. Every VM these scripts create has a run-time limit
(`glab init --max-run`), after which Google deletes the VM and its disk even
if your computer is off:

| | Limit | A normal run |
|---|---|---|
| ping-latency | 20 min plus ping time | about 3 min |
| llm-chat chat VMs | 45 min | about 8 min |
| llm-chat image builder | 60 min | about 10 min |

An abandoned llm-chat run therefore costs at most 45 minutes of both VMs,
about $0.40 on Spot. A leftover temporary firewall rule costs nothing.

**GPU availability.** L4 GPUs in `asia-southeast1` ran out repeatedly on
2026-09-25, in every zone at some point, Spot and on-demand. llm-chat tries
each zone listed in `common.py` before giving up; a refused request costs
nothing.

## Find the results

Each script prints the path of its result file under its own `results/`
directory. These files are JSON: structured text you can open in an editor
or load in Python. Keep the filename printed by your run; the directory can
also contain results from other runs.

Ping results contain individual round-trip times and packet counts. Chat
results contain completed questions and answers, timing summaries, software
versions, and GPU measurements. A chat run with `complete: false` ended
early; check its `incomplete` field before comparing it with a full run.

The default VMs are Spot VMs, which Google can reclaim while a test is
running. `--on-demand` selects regular VMs. If a Spot run ends early, inspect
its result and rerun when ready.

## Finish or stop a run

Keep the launch terminal open while the test runs. Press Ctrl-C there once
to stop early, then allow cleanup to finish. Both scripts normally delete
their VMs and temporary firewall rule. They also set server-side deletion
limits: roughly 20 minutes plus measurement time for ping, 45 minutes for
chat VMs, and 60 minutes for the image builder. `--keep` leaves resources
available for inspection but does not remove these limits.

Check for remaining resources:

```bash
glab list --all-zones
glab images
```

A saved llm-chat image is expected and is reused next time. See the
[cleanup instructions](../README.md#inspect-and-remove-resources) for removing
it or a remaining VM. A lost local connection can leave a temporary firewall
rule behind; each test guide explains how to identify it.

## Ask an LLM to guide the run

Give an LLM the repository README and the guide for your chosen experiment.
Ask it to check prerequisites, explain the next command, help you interpret
progress, and verify cleanup. For example:

> Help me run llm-chat using the defaults. Check GPU quota first, show me how
> to watch answers in another terminal, and explain time to first token and
> prefix-cache hits in the saved result.
