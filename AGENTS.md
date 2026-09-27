# AGENTS.md

Notes for an AI agent working in this repo.

## Purpose

The Google Cloud counterpart of the EC2 lab. Same end goal: serving a model on
GPU instances in two regions, moving KV cache between them, and measuring
transfer time, time-to-first-token, cache hit behaviour and interconnect
throughput.

`glab.py` is the tool. It is small on purpose: a reader should see what each
API call does. Don't add abstraction that hides the calls.

## Layout

| Path | What it is |
|---|---|
| `glab.py` | The whole tool. One module, not a package. |
| `setup.sh` | Installs `glab`, checks the rest. `--check` verifies without changing. |
| `COSTS.md` | Worked cost estimates. Says where each number came from. |
| `experiments/` | One directory per measurement, script plus `results/`. |
| `tests/` | `python -m unittest discover -s tests`. Mocks only, no API contact. |

## Unverified

Confirmed against a real project on 2026-09-25: `whoami`, `project`, `zone`,
`zones`, `types` (on-demand and spot), `quota`, and a full e2-micro cycle of
`init`, `list`, `run`, `push`, `pull`, `stop`, `start`, `cost` and `destroy`.
`init --spot` ran via `experiments/ping-latency`. `experiments/llm-chat` ran
`init` of a G2 (L4 built in) with `--image` and `--public-ip`, `images` and
`image-create`, on 2026-09-25. Not yet run: `shell`, `image-delete`, and
`init --gpu` (an accelerator attached to an N1).
`experiments/llm-chat/watch.py` ran against a full llm-chat run on 2026-09-27
(both, chat and server modes); its quiet exit when run.py deletes the VMs is
tested with mocks only.

What the first runs showed, kept because each failed quietly or late:

- `InstancesClient.list()` takes `project` and `zone` as keywords and nothing
  else. A `filter` has to go in a `ListInstancesRequest`.
- `operations.wait` returns after about two minutes whether or not the
  operation is done, and a stop takes longer. `wait()` calls it again until the
  status is DONE.
- `start` returns when the instance is RUNNING; sshd answers about 30 seconds
  later, and an IAP connection before that fails with 4003.
- gcloud needs an SSH key at `~/.ssh/google_compute_engine` for OS Login, and
  on a machine with no terminal it cannot prompt for a passphrase. Create it
  once with `gcloud compute ssh NAME --tunnel-through-iap --quiet`.
- An ssh session to an instance that is preempted hangs with no error; an
  image build sat for 10 hours. `experiments/llm-chat/common.py` passes
  ServerAliveInterval so it fails within about a minute.
- L4 capacity in asia-southeast1 ran out in each zone at some point on
  2026-09-25, on-demand and spot. The insert fails with "does not have enough
  resources available" (STOCKOUT); another zone often had one.
- Spot was preempted twice on 2026-09-25: a G2 image builder 7 minutes in,
  and later a G2 server during a session, with its e2-small client minutes
  after it.
- A zone listing right after a preemption once hung for the client's default
  600 s read timeout. `InstancesClient.get` with `timeout=` bounds it.
- watch.py's viewer started before run.py had copied questions.json, so it
  read the question count lazily. gcloud prints a NumPy tunnel warning unless
  given `--verbosity=error`, and on a deleted VM a troubleshooting hint that
  names the project.
- vLLM 0.30.0 on the Ubuntu accelerator image needs `build-essential` and
  `python3-dev` (Triton compiles at start), and FlashInfer's sampler needs
  nvcc, which the image lacks; `VLLM_USE_FLASHINFER_SAMPLER=0` avoids it.
- `init` and `cost` now read regional catalog prices, including attached GPUs.
  Fallback tables are used only for machines without explicit accelerators in
  asia-southeast1. Unknown prices make the compute subtotal incomplete.
  Login refresh, forced price refresh, a cached read, and an N1-plus-T4
  catalog estimate were checked against the project on 2026-09-26.
- GPU maintenance settings are checked using real SDK message serialization
  with mocked API calls; an N1 GPU launch is still unverified.

Pricing, where each mistake gave a plausible wrong number rather than an
error:

- A GPU is its own SKU ("Nvidia L4 GPU running in Singapore"), including on
  G2/A2/A3 where the machine type includes it. Cores plus RAM alone priced
  g2-standard-4 at $0.18/h instead of $0.87.
- "E2 Custom Instance Core" and "N2 Custom Extended Instance Ram" sit next to
  the standard SKUs, so the description match is exact.
- N1 SKUs say "N1 Predefined Instance Core"; every other family omits
  "Predefined".
- Shared-core types bill a total vCPU fraction: e2-micro is 0.25 vCPU, not 2.
- G4 below 48 vCPU is a fractional vGPU with its own SKU; B200 and TPUs are
  priced differently again. These return no price.

Update this section as things are confirmed rather than leaving it whole.

## Design

- `google-cloud-compute` directly, not the `gcloud` CLI. Four commands shell
  out, all for the same reason: `glab shell`, `run`, `push` and `pull` need the
  IAP tunnel and the OS Login key exchange, and reimplementing either here
  would mean this tool handling SSH keys. `glab login` shells out for the
  browser.
- No local state beyond `~/.glab/config.json` (project and zone) and a price
  cache. Everything `glab` creates carries the label `lab=1`, and it only
  touches resources carrying that label. Don't add a manifest.
- No SSH keys in this tool, and no port open to the internet. The one firewall
  rule allows tcp/22 from `35.235.240.0/20` only, which is IAP, and only for
  instances tagged `lab`. A connection from that range still passes IAM.
  `ensure_firewall` stops rather than repairs if someone widened the source
  ranges -- that is the one thing here that cannot be undone safely.
- Instances get Shielded VM (secure boot, vTPM, integrity monitoring), OS Login
  forced on via metadata, and no external IP unless `--public-ip`. No external
  IP also means no internet egress, so no `apt` and no model download.
- Spot uses `provisioning_model=SPOT` with `instance_termination_action=STOP`,
  so `glab start` brings one back. This is the analogue of the persistent EC2
  spot request, without the separate request object that outlives the instance.
- `init --max-run MINUTES` sets `max_run_duration` with termination action
  DELETE: Compute Engine deletes the instance and its boot disk at the limit,
  with nothing on this side running. Confirmed 2026-09-26 on an e2-micro:
  deletion began 2 minutes after start. DELETE also replaces STOP for a spot
  preemption. Experiments use it so a lost laptop cannot leave a GPU billing.
- A GPU cannot live-migrate, so `on_host_maintenance` must be `TERMINATE` or
  the insert is rejected.

## Query the project, don't hard-code figures

- Which GPUs a zone sells varies within a region, not just between regions.
  `glab zones` shows it. Machine types with a GPU built in (G2, A2, A3, A4) are
  in the machine-type catalog; T4 and P4 are accelerators you attach to an N1
  and are in a separate list. `glab types` reads both from the zone.
  `SHORTLIST` is the CPU types shown and `PRICES` is a fallback table, not a
  catalog -- keeping those separate is the whole point.
- GPU quota is three numbers and all default to zero: project-wide
  `GPUS_ALL_REGIONS`, the per-region count for the model, and a separate
  per-region count for the preemptible/spot version. A free-trial project
  cannot request any of them until billing is upgraded.
- `asia-southeast1` (as of 2026-09-25) has T4, L4 and A100 40GB in all three
  zones, A100 80GB in c only, H100 in b and c, B200 in b only. All three zones are labelled Jurong
  West; Google publishes no zone-level location and reassigns the letter-to-
  hardware mapping, so don't reason about buildings.

## Cost

- A stopped instance still pays for its disk, ~$0.12/GB-month for pd-balanced.
  `glab destroy` deletes the boot disk with the instance.
- An external IPv4 bills at ~$0.005/hour while attached, whether or not traffic
  flows over it.
- Egress between zones in a region is $0.01/GB each direction; between regions
  inside Asia it is ~$0.08/GiB. For an experiment whose point is moving bytes
  between regions, transfer costs more than the GPUs. `COSTS.md` works this
  through.
- Price spot at the spot rate. On G2 in Singapore that is 40% off, on N1 73%.

## Conventions

- Edits go on a `wip` branch, never `main`.
- Never write a credential, project number, or email address into a tracked
  file, a commit message, or command output. Placeholders stay obviously fake.
- Verify a change with `./setup.sh --check`, `python -m unittest discover -s
  tests`, and by running the affected command against the real project.
- Error output keeps the Google message. Don't replace it with a code -- a bare
  403 does not say which permission or which project.
- Destroy what you create. `glab list --all-zones` and `glab destroy --all` at
  the end of a session.
