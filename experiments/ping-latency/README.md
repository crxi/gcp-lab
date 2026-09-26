# ping-latency

Round-trip time between two Compute Engine instances over the VPC's internal
network. The floor for anything that moves bytes between two machines, so it
is worth knowing before measuring a KV-cache transfer on top of it.

```
./run.py                              # two spot e2-micro, same zone, 60s
./run.py --seconds 300                # ping for five minutes
./run.py --zone-b asia-southeast1-c   # across zones in one region
./run.py --type g2-standard-4         # an L4 pair, needs GPU quota
./run.py --keep                       # leave them running
```

Defaults: `e2-micro`, spot, 10GB disk, ping every 0.2s for 60s (about 300
packets) with a 56-byte payload, both instances in glab's working zone. A bare
`./run.py` is a complete run of about five minutes. It needs `glab project`
and `glab zone` set, and nothing else.

## What you see

Timestamped lines for each phase, then a line every 5 seconds while ping runs:

```
[10:31:12]     5s     25 replies  last 5s: min 0.301  avg 0.412  max 0.702 ms  lost so far 0
```

At the end, the reply count and loss, min/p50/p90/p99/max/mean/stdev, a text
histogram of the RTTs, and the path of the result JSON, then
the output of `glab list --all-zones`. The JSON holds every
RTT (`rtts_ms`), the sequence numbers that got no reply (`no_reply_seq`), and
the same summary.

## What it does

1. Adds a firewall rule allowing ICMP from the `lab` tag to the `lab` tag.
2. Creates `ping-a` and `ping-b` with `glab init`, no external IP.
3. Waits for RUNNING, then retries `ssh true` until sshd answers. An instance
   is RUNNING before it accepts connections, and a fixed sleep is either too
   short or wasted money.
4. Runs `stdbuf -oL ping -i I -w SECONDS -s S -O` on `ping-a` against
   `ping-b`'s internal IP and reads the replies as they arrive. `stdbuf` is
   there because ping block-buffers into a pipe; `-O` reports a missing reply
   when it happens.
5. Writes every RTT and the summary to the result JSON.
6. Deletes both instances together, then the ICMP rule, in a `finally`. If
   that never runs, Compute Engine deletes the instances itself
   `MAX_RUN_MIN` (in `run.py`) plus the ping time after they start.
7. Prints `glab list --all-zones` as its last output, so the end of the run
   shows whether anything is left.

## The firewall rule

`glab`'s standing rule allows tcp/22 from the IAP range only, so ICMP between
two instances is dropped. Rather than widen that rule, the script adds a
second one, `lab-ping-test-icmp`, whose source is the `lab` network tag rather
than an address range: only instances already in the VPC and already tagged
can send. It is deleted with the instances.

## Cost

Two spot `e2-micro` for the ~5 minutes a run takes is under a tenth of a
cent, plus the disks for the same time. Two
`g2-standard-4` for 15 minutes is about $0.27. `../../COSTS.md` works both
through.

## Results

| Run | Pair | Replies | min | p50 | p90 | p99 | max | mean (ms) |
|---|---|---|---|---|---|---|---|---|
| 2026-09-25 | spot e2-micro, both in asia-southeast1-b | 295/295 | 0.192 | 0.262 | 0.319 | 0.397 | 1.360 | 0.272 |

The 1.36 ms maximum was the first reply; the other 294 were at or below
0.60 ms. The run took 165 s end to end, 50 s of it deleting the instances.
File: `results/20260925T023043Z-asia-southeast1-b-e2-micro.json`.

The AWS counterpart of this experiment measured 0.211 ms average between two
`t3.micro` in one availability zone of `ap-southeast-1`, 0% loss over 50
packets. The e2-micro pair above averaged 0.272 ms. e2-micro is a shared-core
type, so this is not a like-for-like comparison of the networks.
