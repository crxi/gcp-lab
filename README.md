# gcp-lab

A small Compute Engine control tool, and the experiments run with it. The
Google Cloud counterpart of the EC2 lab: same command surface, same
no-local-state design, same goal of multi-region LLM inference with KV-cache
telemetry.

```
./setup.sh
glab project my-project
glab zone asia-southeast1-b
glab types
glab init box --spot
glab run box 'uname -a'
glab destroy box
```

## Status

Run against a real project since 2026-09-25. `AGENTS.md` lists which commands
have been confirmed and what the first runs turned up. `COSTS.md` is sourced
from the Cloud Billing Catalog.

## Experiments

`experiments/README.md` covers both experiments so far, `ping-latency` and
`llm-chat`: how to run each, what a run costs, and the results of one run.

## Layout

| Path | What it is |
|---|---|
| `glab.py` | The whole tool. One module, not a package. |
| `setup.sh` | Installs `glab`, checks the rest. `--check` verifies without changing. |
| `COSTS.md` | What a small experiment costs, worked through. |
| `experiments/` | One directory per measurement, with its results. |
| `tests/` | `python -m unittest discover -s tests`. Mocks only. |

## Commands

```
glab login                 check or refresh credentials
glab whoami                project, identity, zone
glab project [ID]          show or set the project
glab zone [ZONE]           show or set the working zone
glab zones                 zones, and which GPUs each one sells
glab types                 machine types and prices in the zone
glab quota                 CPU and GPU limits, which gate launches
glab init NAME             create an instance
glab list                  list lab instances, and how long each has been up
glab start / stop NAME     stop billing for compute, keep the disk
glab shell NAME            interactive shell over IAP
glab run NAME CMD          one command
glab push / pull           copy files
glab cost                  rough cost of what is running
glab destroy NAME          delete it, and its boot disk
glab images                lab images and their monthly storage cost
glab image-create / image-delete   make an image from a stopped instance's disk, or delete one
```

## How it differs from the EC2 version

Most of the design carries over. Three things do not.

**There is no Systems Manager.** On EC2, `lab` reaches an instance through SSM:
no listener, no keys, no inbound rule at all. Compute Engine has no equivalent,
so `glab` uses Identity-Aware Proxy: one firewall rule allowing tcp/22 from
`35.235.240.0/20`, Google's IAP range, and only for instances tagged `lab`. A
connection from that range still has to pass IAM, so the port is not open to
the internet — but it is an ingress rule, which the EC2 side does not have.
`glab shell`, `run`, `push` and `pull` all shell out to `gcloud` for the tunnel
and the OS Login key exchange, because doing either here would mean handling
keys.

**Instances get no external IP by default.** That also means no route to the
internet, so no `apt`, no `pip`, no model download. `glab init --public-ip`
attaches one, billed at about $0.005/hour while attached. Cloud NAT is the
other way and is not wired up here.

**GPU quota is three numbers, not one.** A launch needs the project-wide
`GPUS_ALL_REGIONS`, the per-region count for the specific model, and, for
spot, a separate per-region preemptible count. All default to zero. `glab
quota` reads all three. A project on the free trial cannot request GPU quota
at all until it is upgraded to paid billing.

Carried over unchanged: no local state, everything labelled `lab=1`, the tool
only touches resources carrying that label, prices come from the provider's
catalog rather than a hardcoded table, and `destroy` takes the disk with it.
