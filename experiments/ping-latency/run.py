#!/usr/bin/env python3
"""Ping RTT between two Compute Engine instances.

Launches two instances, has the first ping the second over the VPC's internal
network, records the round-trip times, and deletes both. Everything it creates
is labelled `lab=1` and removed in a `finally`, including on Ctrl-C.

    ./run.py                              # no arguments: two spot e2-medium
    ./run.py --type g2-standard-4         # an L4 pair, needs GPU quota
    ./run.py --zone-b asia-southeast1-c   # across zones instead of within one
    ./run.py --keep                       # leave them running

The default firewall rule only admits IAP on port 22, so ICMP between two
instances is dropped. This adds a second rule allowing ICMP between instances
carrying the `lab` tag, and deletes it afterwards. The rule's source is the
network tag, not an address range, so nothing outside the VPC is admitted.

Not yet run against a real project. See ../../README.md.
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))
import glab
from glab import compute_v1, NotFound

HERE = os.path.dirname(os.path.abspath(__file__))
RESULTS = os.path.join(HERE, "results")
ICMP_RULE = "lab-ping-test-icmp"
DEFAULT_TYPE = "e2-medium"


def log(message):
    print(f"[{time.strftime('%H:%M:%S')}] {message}", flush=True)


def icmp_rule():
    """Allow ICMP from anything tagged `lab` to anything tagged `lab`. Using
    the tag as the source rather than a CIDR keeps it inside the VPC."""
    client = compute_v1.FirewallsClient()
    project = glab.current_project()
    try:
        client.get(project=project, firewall=ICMP_RULE)
        return ICMP_RULE
    except NotFound:
        pass
    rule = compute_v1.Firewall(
        name=ICMP_RULE,
        description="ping-latency test: ICMP between lab instances",
        network="global/networks/default",
        direction="INGRESS",
        source_tags=[glab.NETWORK_TAG],
        target_tags=[glab.NETWORK_TAG],
        allowed=[compute_v1.Allowed(I_p_protocol="icmp")],
    )
    glab.wait(client.insert(project=project, firewall_resource=rule))
    log(f"created {ICMP_RULE} (icmp, lab -> lab)")
    return ICMP_RULE


def drop_icmp_rule():
    try:
        glab.wait(compute_v1.FirewallsClient().delete(
            project=glab.current_project(), firewall=ICMP_RULE))
        log(f"deleted {ICMP_RULE}")
    except NotFound:
        pass
    except Exception as e:
        log(f"could not delete {ICMP_RULE}: {e}")


def parse_ping(output):
    """ping's summary, as numbers: the packet counts and the rtt line, which
    iputils prints as min/avg/max/mdev."""
    out = {"raw": output}
    counts = re.search(r"(\d+) packets transmitted, (\d+) (?:packets )?received"
                       r".*?([\d.]+)% packet loss", output, re.S)
    if counts:
        out.update(sent=int(counts.group(1)), received=int(counts.group(2)),
                   loss_percent=float(counts.group(3)))
    rtt = re.search(r"=\s*([\d.]+)/([\d.]+)/([\d.]+)/([\d.]+)\s*ms", output)
    if rtt:
        out.update(min_ms=float(rtt.group(1)), avg_ms=float(rtt.group(2)),
                   max_ms=float(rtt.group(3)), mdev_ms=float(rtt.group(4)))
    return out


def ssh_output(name, zone, command):
    r = subprocess.run(
        glab.gcloud_ssh(name, zone, ["--command", command, "--", "-q"]),
        capture_output=True, text=True)
    return r.returncode, r.stdout + r.stderr


def teardown(placed, keep):
    if keep:
        log(f"--keep: {', '.join(n for n, _ in placed)} left running. "
            f"`glab destroy NAME` when done, and delete {ICMP_RULE}.")
        return
    for name, zone in placed:
        glab.write_config(zone=zone)
        live = {i["name"] for i in glab.describe(zone)}
        if name not in live:
            continue
        try:
            glab.cmd_destroy(SimpleNamespace(name=name, all=False, yes=True))
        except Exception as e:
            log(f"could not destroy {name}: {e}")
    drop_icmp_rule()


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--type", default=DEFAULT_TYPE,
                   help=f"machine type (default {DEFAULT_TYPE})")
    p.add_argument("--count", type=int, default=50, help="pings to send")
    p.add_argument("--interval", type=float, default=0.2,
                   help="seconds between pings")
    p.add_argument("--size", type=int, default=56, help="ICMP payload bytes")
    p.add_argument("--disk", type=int, default=20, help="boot disk in GB")
    p.add_argument("--gpu", metavar="MODEL[:N]", help="attach GPUs to both")
    p.add_argument("--on-demand", action="store_true",
                   help="on-demand instead of spot")
    p.add_argument("--zone", help="zone for the first instance")
    p.add_argument("--zone-b", help="zone for the second; default is the first")
    p.add_argument("--keep", action="store_true",
                   help="leave the instances running")
    args = p.parse_args()

    if args.zone:
        glab.write_config(zone=args.zone)
    zone_a = glab.current_zone()
    zone_b = args.zone_b or zone_a
    buy = "on-demand" if args.on_demand else "spot"
    started = time.time()
    placed = [("ping-a", zone_a), ("ping-b", zone_b)]
    result = {
        "started": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "project_zone_a": zone_a, "zone_b": zone_b,
        "machine_type": args.type, "purchase": buy, "gpu": args.gpu,
        "pings": args.count, "interval_s": args.interval,
        "payload_bytes": args.size, "same_zone": zone_a == zone_b,
    }

    try:
        icmp_rule()
        for name, zone in placed:
            glab.write_config(zone=zone)
            glab.cmd_init(SimpleNamespace(
                name=name, type=args.type, disk=args.disk,
                disk_type="pd-balanced", gpu=args.gpu, image=None,
                spot=not args.on_demand, public_ip=False))

        found = {}
        for name, zone in placed:
            glab.write_config(zone=zone)
            deadline = time.time() + 300
            while time.time() < deadline:
                i = glab.find(name)
                if i["state"] == "running":
                    break
                time.sleep(5)
            else:
                glab.die(f"{name} never reached running")
            found[name] = glab.find(name)
            log(f"{name}: {found[name]['private_ip']} {zone} {found[name]['buy']}")

        # An instance is RUNNING before sshd is. Retry rather than sleep a
        # fixed amount, which is either too short or wasted money.
        a, b = found["ping-a"], found["ping-b"]
        for attempt in range(20):
            code, _ = ssh_output(a["name"], zone_a, "true")
            if code == 0:
                break
            time.sleep(5)
        else:
            glab.die("ping-a never accepted an ssh connection over IAP")

        result["source"] = {k: a[k] for k in ("name", "zone", "private_ip", "buy")}
        result["target"] = {k: b[k] for k in ("name", "zone", "private_ip", "buy")}
        command = (f"ping -c {args.count} -i {args.interval} -s {args.size} "
                   f"-W 2 -q -- {b['private_ip']}")
        log(f"ping-a -> ping-b: {command}")
        code, out = ssh_output(a["name"], zone_a, command)
        result["exit_code"] = code
        result.update(parse_ping(out))
        if code:
            log(f"ping exited {code}:\n{out}")
    finally:
        teardown(placed, args.keep)
        glab.write_config(zone=zone_a)
        result["elapsed_s"] = round(time.time() - started, 1)

    os.makedirs(RESULTS, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = os.path.join(RESULTS, f"{stamp}-{zone_a}-{args.type}.json")
    with open(path, "w") as f:
        json.dump(result, f, indent=2)

    print()
    if "avg_ms" in result:
        print(f"{args.type}, {zone_a} -> {zone_b}: "
              f"{result['received']}/{result['sent']} replies, "
              f"{result['loss_percent']}% loss")
        print(f"rtt min/avg/max/mdev = {result['min_ms']}/{result['avg_ms']}/"
              f"{result['max_ms']}/{result['mdev_ms']} ms")
    else:
        print("no rtt measured; see the json for what came back")
    print(f"{result['elapsed_s']}s total -> {os.path.relpath(path, os.getcwd())}")


if __name__ == "__main__":
    main()
