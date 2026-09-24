# What a small experiment costs

Prices are `asia-southeast1` (Singapore), USD, checked 2026-09-24 against
public price pages. Nothing here was read from a billing account, so treat
them as list prices and confirm with `glab types` once the tool is pointed at
a project. Spot prices move; Google republishes them at most monthly, but the
rate you get is the rate at launch.

## The rates that matter

| Resource | On-demand /hr | Spot /hr |
|---|---|---|
| `e2-medium` (2 vCPU, 4GB) | $0.0447 | $0.0134 |
| `n1-standard-4` (4 vCPU, 15GB) | $0.2344 | $0.0623 |
| `g2-standard-4` (4 vCPU, 16GB, 1x L4 24GB) | $0.8720 | $0.5233 |
| `n1-standard-4` + 1x T4 | see below | see below |

The T4 is not a machine type. On Compute Engine it is an accelerator you bolt
onto an N1, and it is billed as a separate line, so the cost is the N1 rate
plus the T4 rate. I could not confirm the Singapore T4 accelerator rate from a
source I trust, so it is left blank rather than guessed. `glab types` reads it
from the Billing Catalog. If you want a number today, the L4 row above is a
complete, verified price for a comparable single-GPU machine, and the L4 is
the faster card.

Other lines that show up on the bill:

| Resource | Price |
|---|---|
| `pd-balanced` disk | ~$0.12/GB-month |
| `pd-standard` disk | ~$0.048/GB-month |
| External IPv4, while attached | ~$0.005/hr (~$3.65/month) |
| Egress between zones in one region | $0.01/GB, each direction |
| Egress between regions inside Asia | ~$0.08/GiB |

## Four experiments, costed

**A ping test between two CPU VMs, 10 minutes.** Two `e2-medium` spot, 20GB
disk each.

```
compute   2 x $0.0134 x 0.17h  = $0.005
disk      40GB x $0.12/730h x 0.17h = $0.001
                                 -------
                                 ~$0.01
```

Under a cent. This is the same measurement the AWS side already ran for about
the same price, and it does not need a GPU: the RTT between two VMs does not
depend on what card is in them.

**A ping test between two L4 VMs, 15 minutes** (allowing for boot and driver
install). Two `g2-standard-4` spot, 50GB disk each.

```
compute   2 x $0.5233 x 0.25h  = $0.262
disk      100GB x $0.12/730h x 0.25h = $0.004
                                 -------
                                 ~$0.27
```

On-demand instead of spot: ~$0.44. Either way it is a coffee, not a decision.

**A day of single-GPU inference work, 8 hours.** One `g2-standard-4` spot,
100GB disk for the model weights.

```
compute   $0.5233 x 8h         = $4.19
disk      100GB x $0.12/730h x 8h = $0.13
                                 -------
                                 ~$4.32
```

On-demand: ~$7.11. The trap is the disk: if you keep that 100GB around between
sessions instead of deleting it, it bills $12/month whether or not anything is
running. `glab destroy` deletes the boot disk with the instance, which is what
you want unless you have baked an image.

**The cross-region KV-cache experiment, 4 hours.** One `g2-standard-4` spot in
`asia-southeast1`, one in `asia-southeast2` (Jakarta), moving cache between
them.

```
compute   2 x $0.5233 x 4h     = $4.19
disk      200GB x $0.12/730h x 4h = $0.13
transfer  100GB inter-region x $0.08 = $8.00
                                 -------
                                 ~$12.3
```

Read that last line twice. At four hours of compute, the data transfer costs
more than the GPUs. Cross-region egress is the dominant term in any experiment
whose point is moving bytes between regions, and it scales with what you move,
not with how long you run. Two zones inside one region is $0.01/GB instead of
$0.08/GiB, eight times cheaper, at the cost of measuring a sub-millisecond hop
instead of a ~30ms one.

## Rules of thumb

- Spot is 40% off on G2 and 73% off on N1. It is the default worth using; both
  Spot and the old Preemptible are the same product now, and Spot has no
  24-hour cap.
- Sustained use discounts apply automatically to on-demand N1 and N2 if an
  instance runs most of a month. They do not apply to spot, and you will not
  hit them doing short experiments.
- Budget on the order of $5-15 per full day of GPU experimentation, and
  under a dollar for anything that is only measuring the network.
- The recurring costs that survive a `destroy` are disks, images and static
  IPs. Check for orphans; an unattached 100GB disk is $12/month of nothing.

## Against the AWS side

Same work, `ap-southeast-1`, verified against the account:

| | AWS | GCP |
|---|---|---|
| Cheapest single-GPU, on-demand | `g4dn.xlarge` T4 16GB, $0.736/hr | `g2-standard-4` L4 24GB, $0.872/hr |
| Same, spot | ~$0.313/hr | $0.523/hr |
| Block storage | $0.08/GB-month | ~$0.12/GB-month |
| Cross-AZ / cross-zone egress | $0.01/GB each way | $0.01/GB each way |

AWS is cheaper per GPU-hour in Singapore. GCP gives you a newer card with more
VRAM for the extra, and has L4, A100 and H100 in Singapore where AWS has only
T4 and one very large A100 machine. For experiments in the dollars, the
availability difference matters more than the price difference.
