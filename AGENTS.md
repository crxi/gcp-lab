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

No call in `glab.py` has run against a real project. Before trusting any of it,
run the command and fix what breaks. The likely failure points, in order:

- `machine_price` matches Billing Catalog SKUs with a regex on the description
  string. Compute Engine sells core-hours and GB-hours per family, not machine
  types, so the price is `vcpu * core_rate + ram_gb * ram_rate`. A wrong family
  match gives a plausible wrong number instead of an error. Check one type
  against the console before believing the table.
- `compute_v1` field names with embedded capitals are mangled by the generated
  client (`network_i_p`, `nat_i_p`, `I_p_protocol`). If an attribute is
  missing, print the object.
- The operation-wait path assumes every mutating call returns an operation with
  a `.name`. Some return a different shape.

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
- `asia-southeast1` has T4 and L4 in all three zones, A100 40GB in a and c,
  A100 80GB in c only, H100 in b and c. All three zones are labelled Jurong
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
