# Measure language-model response time

This test runs an automated conversation on two Google Cloud VMs:

| VM | Job |
|---|---|
| `llm-server` | Runs Qwen2.5-3B-Instruct using vLLM, model-serving software, on one NVIDIA L4 GPU |
| `llm-client` | Sends 50 prepared questions and measures the responses |

Each question includes the conversation so far, so the model processes a
growing conversation. The test saves the questions, answers, timings, and GPU
measurements on your computer. It is a scripted test; you do not type questions
into an interactive chat window.

Complete the [repository setup](../../README.md) and run
[ping-latency](../ping-latency/README.md) first. Commands below run on your
computer from the repository directory with `.venv` activated, unless a
step explicitly says to run them inside a VM.

## Check GPU quota

Quota is your project's permission to allocate a quantity of hardware. It is
separate from whether Google has that hardware available right now.

```bash
glab whoami
glab quota --all --region asia-southeast1
```

The default test needs one L4 GPU. Check that there is room for one more GPU
under the project-wide `GPUS_ALL_REGIONS` limit and the region's applicable
L4 limits. The first image build uses a regular VM, while chat VMs use Spot
by default, so check both regular L4 and preemptible/Spot L4 quota. A row
showing `used 0, limit 0` has no available quota.

Ask the project administrator to check or request the necessary limits in
[Google Cloud Quotas](https://console.cloud.google.com/iam-admin/quotas).
Google's [GPU quota documentation](https://docs.cloud.google.com/compute/docs/gpus/create-vm-with-gpus)
and [allocation quota reference](https://docs.cloud.google.com/compute/resource-usage)
explain the global, regional, and preemptible limits. A free-trial project
needs upgrading before GPU use.

## Start the conversation

```bash
python3 experiments/llm-chat/run.py
```

On the first run, the script prepares a reusable VM image: a saved boot disk
containing the model and its software. It creates `llm-build`, downloads and
installs the software, checks that the model responds, saves an image, and
removes the builder. The builder has a temporary public IP for downloads.
The later server and client use internal addresses.

The script then creates the server and client, starts vLLM, and sends the
50 questions. Allow roughly 20–30 minutes for a first run. Later runs reuse
the image and usually take several minutes. GPU availability and startup
time can extend the wait.

Keep the launch terminal open. During an image build it prints installation
steps. During a chat it prints readiness messages and one `TURN` line per
completed answer. `READY-WAIT` means the client is waiting for the model;
`READY` means it can begin sending questions. A `TURN 7/50` line means seven
answers have completed.

The server starts in your working zone. If L4 capacity is unavailable there,
the script tries the other Singapore zones listed in `common.py`, and puts
the client in the same zone as the server. Read the actual zone from the
progress messages or `glab list --all-zones`.

## Watch llm-chat in progress

The launch terminal shows one timing line per answer. To read the answers
and see what the server is doing, open a second terminal on your computer,
go to this repository, and run:

```bash
source .venv/bin/activate
export PATH="$HOME/.local/bin:$PATH"
python3 experiments/llm-chat/watch.py
```

`watch.py` can be started at any point after `run.py`. It waits for the
VMs to be created and to accept SSH, then shows two things together:

- each question and the model's answer, with its timing, from the client
- a status line every 5 seconds from the server: GPU use, memory, power
  and temperature, requests in progress, tokens generated per second, and
  the prefix-cache hit rate so far

Answers appear when each one is complete, not word by word. If you start
`watch.py` partway through, it first prints the answers so far.

To watch one side on its own, in a larger terminal each:

```bash
python3 experiments/llm-chat/watch.py chat     # questions and answers only
python3 experiments/llm-chat/watch.py server   # status every 2 s, plus vLLM's log
```

`server` mode shows vLLM loading the model, which takes about two minutes
before the first question, and its throughput lines during the conversation.

`watch.py` copies nothing to the VMs and does not affect the run, apart from
one GPU query and one metrics request on the server per status line. Press
Ctrl-C to stop watching; the experiment continues. `watch.py` ends by itself
after the last answer, or when `run.py` deletes the VMs.

To look around a VM yourself, `glab shell llm-client` or
`glab shell llm-server` opens a shell on it. The client's answers are in
`turns.jsonl` in the home directory; vLLM's log is `sudo journalctl -u vllm`
on the server.

## Read the results

At the end, the launch terminal prints a summary and a JSON file path under
`experiments/llm-chat/results/`. Open the printed file in an editor, or use:

```bash
python3 -m json.tool RESULT_FILE.json
```

Replace `RESULT_FILE.json` with the actual path. `turns` contains the questions,
answers, and individual measurements. `summary` contains statistics across
the conversation. `versions`, `decisions`, and `questions_sha256` identify
the software, settings, and question file used for that run.

| Measurement | Meaning |
|---|---|
| Client time to first token (`ttft_ms`) | Time from sending a request until the first nonempty text chunk arrives |
| Server time to first token | vLLM's own first-token timing, obtained from changes in its metrics |
| Whole-answer time (`total_ms`) | Time from sending the request until the response stream finishes |
| Decode rate (`decode_tok_s`) | Approximate tokens per second during text generation, using the server's token count |
| Chunk gaps (`gap_ms_*`) | Time between received text chunks; a chunk can contain more than one token |
| Prefix-cache hit rate | Fraction of prompt tokens reused from cached model work |
| GPU samples | GPU activity, memory use, power, and temperature during the conversation |

A token is a piece of text processed by the model; it is not necessarily a
whole word. Prefix caching reuses work for the beginning of a prompt when
that text has already been processed. Sending previous turns again creates
an opportunity for this reuse.

In the summary, p50 is the median and p90 covers 90% of the measured values.
The difference between client and server first-token timings includes
transport, buffering, and differences in measurement boundaries; it is not
a direct measurement of network latency. The separate TCP connection timings
measure connection setup.

Check `complete` before comparing runs. `complete: false` means the run ended
early; `incomplete` explains why when a result was saved. Completed turns are
streamed back to the launch process during the run, so they can be retained
if a Spot VM is reclaimed. Failures before server setup may produce only
terminal output, without a final result JSON.

The [experiments overview](../README.md#results-from-one-run) shows one
complete 50-turn run for comparison, and [what a run costs](../README.md#cost).

## Compare settings

Disable prefix caching to compare first-token times as the conversation grows:

```bash
python3 experiments/llm-chat/run.py --no-prefix-cache
```

Use regular VMs instead of Spot, which Google may reclaim during a run:

```bash
python3 experiments/llm-chat/run.py --on-demand
```

Choose the first zone to try:

```bash
python3 experiments/llm-chat/run.py --zone asia-southeast1-c
```

Change one setting at a time and keep both result files. The model, answer
length, and server settings are in `experiments/llm-chat/common.py`; the
questions are in `questions.json`. Changing the model requires preparing a
matching image. Use the default model for the first run.

## Stop and remove resources

Press Ctrl-C once in the launch terminal to stop the experiment, then wait
for cleanup. A normal run deletes the server, client, their boot disks, and
its temporary firewall rule. The saved model image remains for future runs.

`--keep` leaves the VMs and rule available for inspection. The automatic VM
limits still apply: 45 minutes for the chat VMs, 60 minutes for the builder.
A local interruption can leave a firewall rule behind.

Check remaining resources from your computer:

```bash
glab list --all-zones
glab images
```

For a remaining VM, select its zone from the listing and delete its name:

```bash
glab zone ZONE_FROM_LIST
glab destroy llm-server
glab destroy llm-client
```

If image creation times out, loses its connection, or is interrupted, the
script retains the stopped `llm-build` and its source disk. The image copy
may still be running. Check the named image with `glab images` and, if its
state is unclear, inspect the image operation in Compute Engine before
deleting the builder. Once the operation has finished, delete `llm-build`
the same way. Retained disk storage still bills.

To remove a leftover firewall rule, use the exact rule name printed by your run:

```bash
gcloud compute firewall-rules delete RULE_NAME_FROM_RUN --project YOUR_PROJECT_ID
```

To remove the saved image, choose its name from `glab images`:

```bash
glab image-delete IMAGE_NAME
```

The next run will build an image again. To rebuild deliberately while keeping
the existing image, use `python3 experiments/llm-chat/image.py --rebuild`.
Older images remain until you delete them.

## Resolve common failures

| Symptom | Next step |
|---|---|
| GPU quota exceeded | Check the exact quota metric in the error and the region in `glab quota --all` |
| No L4 capacity in any attempted zone | Wait and retry; quota approval does not reserve hardware |
| SSH or IAP fails | Repeat the repository README's remote-connection check |
| Waiting for vLLM for several minutes | Follow the server log using the commands above |
| A Spot VM disappears | Read the incomplete result and rerun; consider `--on-demand` |
| An instance name already exists | Inspect the previous run before removing or reusing its resources |

For guided help, give an LLM this README and the repository setup guide.
Ask it to check quota, explain progress, show the answers in another terminal,
and help interpret your result. Include the failing command and relevant
error text if needed; do not share credentials or private keys.
