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

The terminal prints each question's timing as it completes. You can also
[watch the answers, server logs, and GPU](llm-chat/README.md#watch-llm-chat-in-progress)
from another terminal. This is an automated conversation, so you do not need
to type questions while it runs.

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
