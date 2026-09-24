# ping-latency

Round-trip time between two Compute Engine instances over the VPC's internal
network. The floor for anything that moves bytes between two machines, so it
is worth knowing before measuring a KV-cache transfer on top of it.

```
./run.py                              # two spot e2-medium, same zone
./run.py --zone-b asia-southeast1-c   # across zones in one region
./run.py --type g2-standard-4         # an L4 pair, needs GPU quota
./run.py --keep                       # leave them running
```

Defaults: `e2-medium`, spot, 20GB disk, 50 pings at 0.2s with a 56-byte
payload, both instances in the working zone. A bare `./run.py` is a complete
run; nothing has to be typed for the common case.

## What it does

1. Adds a firewall rule allowing ICMP from the `lab` tag to the `lab` tag.
2. Creates `ping-a` and `ping-b` with `glab init`, no external IP.
3. Waits for RUNNING, then retries `ssh true` until sshd answers. An instance
   is RUNNING before it accepts connections, and a fixed sleep is either too
   short or wasted money.
4. Runs `ping -c N -i I -s S -q` on `ping-a` against `ping-b`'s internal IP.
5. Parses the counts and the `min/avg/max/mdev` line into the result JSON.
6. Deletes both instances and the ICMP rule in a `finally`.

## The firewall rule

`glab`'s standing rule allows tcp/22 from the IAP range only, so ICMP between
two instances is dropped. Rather than widen that rule, the script adds a
second one, `lab-ping-test-icmp`, whose source is the `lab` network tag rather
than an address range: only instances already in the VPC and already tagged
can send. It is deleted with the instances.

## Cost

Two spot `e2-medium` for the ~10 minutes a run takes is about a cent. Two
`g2-standard-4` for 15 minutes is about $0.27. `../../COSTS.md` works both
through.

## Results

None yet. The tool has not been pointed at a project.

The AWS counterpart of this experiment measured 0.211 ms average between two
`t3.micro` in one availability zone of `ap-southeast-1`, 0% loss over 50
packets. That is the number to compare a same-zone GCP run against.
