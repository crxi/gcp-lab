#!/usr/bin/env python3
"""Ping RTT between two Compute Engine instances.

Launches two instances, has the first ping the second over the VPC's internal
network for a fixed time, records every round-trip time, and deletes both.
Everything it creates is labelled `lab=1` and removed in a `finally`, including
on Ctrl-C.

    ./run.py                              # two spot e2-micro, same zone, 60s
    ./run.py --seconds 300                # ping for five minutes
    ./run.py --zone-b asia-southeast1-c   # across zones instead of within one
    ./run.py --type g2-standard-4         # an L4 pair, needs GPU quota
    ./run.py --keep                       # leave them running

The IAP rule glab creates only admits tcp/22, so this adds a second rule
allowing ICMP between instances carrying the `lab` tag, and deletes it
afterwards. The rule's source is the network tag, not an address range, so
nothing outside the VPC is admitted.
"""

import argparse
import contextlib
import io
import json
import math
import os
import re
import statistics
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
DEFAULT_TYPE = "e2-micro"
REPORT_EVERY = 5  # seconds between progress lines while pinging
# Compute Engine deletes each instance this many minutes after it starts,
# plus the ping time, so nothing bills on if this machine goes away mid-run.
# A 60 s run took under 3 minutes end to end (2026-09-25).
MAX_RUN_MIN = 20

REPLY = re.compile(r"icmp_seq=(\d+) ttl=\d+ time=([\d.]+) ms")
NO_REPLY = re.compile(r"no answer yet for icmp_seq=(\d+)")


def log(message):
    print(f"[{time.strftime('%H:%M:%S')}] {message}", flush=True)


def quietly(fn, *args):
    """Run a glab command without its hints to the interactive user."""
    with contextlib.redirect_stdout(io.StringIO()):
        return fn(*args)


def icmp_rule():
    """Allow ICMP from anything tagged `lab` to anything tagged `lab`. Using
    the tag as the source rather than a CIDR keeps it inside the VPC."""
    client = compute_v1.FirewallsClient()
    project = glab.current_project()
    try:
        client.get(project=project, firewall=ICMP_RULE)
        return
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
    log(f"created firewall rule {ICMP_RULE} (icmp, lab -> lab)")


def drop_icmp_rule():
    try:
        glab.wait(compute_v1.FirewallsClient().delete(
            project=glab.current_project(), firewall=ICMP_RULE))
        log(f"deleted firewall rule {ICMP_RULE}")
    except NotFound:
        pass
    except Exception as e:
        log(f"could not delete {ICMP_RULE}: {e}")


def parse_summary(output):
    """ping's closing summary: the packet counts and the rtt line, which
    iputils prints as min/avg/max/mdev."""
    out = {}
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


def percentile(sorted_values, p):
    """Nearest-rank percentile."""
    if not sorted_values:
        return None
    k = math.ceil(p / 100 * len(sorted_values)) - 1
    return sorted_values[max(0, min(len(sorted_values) - 1, k))]


def histogram(values, bins=10, width=40):
    """Text histogram, one line per bin, as a list of strings."""
    if not values:
        return []
    lo, hi = min(values), max(values)
    step = (hi - lo) / bins or 1
    counts = [0] * bins
    for v in values:
        counts[min(bins - 1, int((v - lo) / step))] += 1
    top = max(counts)
    return [f"  {lo + i * step:7.3f} - {lo + (i + 1) * step:7.3f} ms "
            f"{'#' * max(1 if c else 0, round(c / top * width)):<{width}} {c}"
            for i, c in enumerate(counts)]


def ssh_ok(name, zone):
    r = subprocess.run(
        glab.gcloud_ssh(name, zone, ["--command", "true", "--", "-q"]),
        capture_output=True, text=True, stdin=subprocess.DEVNULL)
    return r.returncode == 0


def stream_ping(name, zone, target_ip, seconds, interval, size):
    """Run ping on `name`, print a progress line every REPORT_EVERY seconds,
    and return (rtts in ms, sequence numbers with no reply, full output)."""
    # stdbuf: over a pipe ping's stdout is block-buffered, so without it the
    # replies arrive all at once at the end. -O reports a missing reply as it
    # happens rather than only in the loss count.
    command = (f"stdbuf -oL ping -i {interval} -w {seconds} -s {size} -O "
               f"-- {target_ip}")
    log(f"on {name}: {command}")
    proc = subprocess.Popen(
        glab.gcloud_ssh(name, zone, ["--command", command, "--", "-q"]),
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        stdin=subprocess.DEVNULL, bufsize=1)
    rtts, lost, lines, window = [], [], [], []
    start = next_report = None
    for line in proc.stdout:
        lines.append(line)
        m = REPLY.search(line)
        if m:
            rtts.append(float(m.group(2)))
            window.append(float(m.group(2)))
        m = NO_REPLY.search(line)
        if m:
            lost.append(int(m.group(1)))
        if start is None and (rtts or lost):
            start = time.time()
            next_report = start + REPORT_EVERY
        if next_report and time.time() >= next_report:
            elapsed = time.time() - start
            if window:
                log(f"  {elapsed:4.0f}s  {len(rtts):5d} replies  "
                    f"last {REPORT_EVERY}s: min {min(window):.3f}  "
                    f"avg {statistics.mean(window):.3f}  "
                    f"max {max(window):.3f} ms  lost so far {len(lost)}")
            else:
                log(f"  {elapsed:4.0f}s  no replies in the last {REPORT_EVERY}s")
            window = []
            next_report += REPORT_EVERY
    proc.wait()
    return proc.returncode, rtts, lost, "".join(lines)


def teardown(placed, keep):
    if keep:
        log(f"--keep: {', '.join(n for n, _ in placed)} left running. "
            f"`glab destroy NAME` when done, and delete {ICMP_RULE}.")
        return
    client = glab.instances_client()
    project = glab.current_project()
    ops = []
    for name, zone in placed:
        try:
            ops.append((name, client.delete(project=project, zone=zone,
                                            instance=name), zone))
        except NotFound:
            pass
        except Exception as e:
            log(f"could not delete {name}: {e}")
    if ops:
        log(f"deleting {', '.join(n for n, _, _ in ops)} (about two minutes)")
    for name, op, zone in ops:
        try:
            glab.wait(op, zone)
            log(f"deleted {name}")
        except BaseException as e:
            log(f"{name} may still exist: {e}. check `glab list --all-zones`.")
    drop_icmp_rule()


def show_leftovers():
    """Print `glab list --all-zones`, so the end of every run shows whether
    anything is still billing."""
    print("\n$ glab list --all-zones", flush=True)
    try:
        glab.cmd_list(SimpleNamespace(all_zones=True, region=None))
    except BaseException as e:
        print(f"could not list instances: {e}. run `glab list --all-zones`.")


def report(result):
    rtts = sorted(result.get("rtts_ms", []))
    print()
    print(f"ping {result['source']['name']} -> {result['target']['name']}, "
          f"{result['machine_type']} {result['purchase']}, "
          f"{result['zone_a']} -> {result['zone_b']}")
    if not rtts:
        print("no replies. the raw output is in the json.")
        return
    print(f"  {result.get('received', len(rtts))}/{result.get('sent', '?')} "
          f"replies, {result.get('loss_percent', '?')}% loss, "
          f"{result['seconds']}s at {result['interval_s']}s intervals, "
          f"{result['payload_bytes']}-byte payload")
    print()
    print("  min     p50     p90     p99     max     mean    stdev   (ms)")
    s = result["stats_ms"]
    print("  " + "  ".join(f"{s[k]:6.3f}" for k in
                           ("min", "p50", "p90", "p99", "max", "mean", "stdev")))
    print()
    for line in histogram(rtts):
        print(line)


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--type", default=DEFAULT_TYPE,
                   help=f"machine type (default {DEFAULT_TYPE})")
    p.add_argument("--seconds", type=int, default=60,
                   help="how long to ping (default 60)")
    p.add_argument("--interval", type=float, default=0.2,
                   help="seconds between pings (default 0.2; below 0.2 needs root)")
    p.add_argument("--size", type=int, default=56,
                   help="ICMP payload bytes (default 56)")
    p.add_argument("--disk", type=int, default=10, help="boot disk in GB")
    p.add_argument("--gpu", metavar="MODEL[:N]", help="attach GPUs to both")
    p.add_argument("--on-demand", action="store_true",
                   help="on-demand instead of spot")
    p.add_argument("--zone", help="zone for the first instance "
                   "(default: glab's working zone)")
    p.add_argument("--zone-b", help="zone for the second; default is the first")
    p.add_argument("--keep", action="store_true",
                   help="leave the instances running")
    args = p.parse_args()

    if args.zone:
        glab.write_config(zone=args.zone)
    zone_a = glab.current_zone()
    zone_b = args.zone_b or zone_a
    buy = "on-demand" if args.on_demand else "spot"
    placed = [("ping-a", zone_a), ("ping-b", zone_b)]
    prices = glab.PRICES if args.on_demand else glab.SPOT_PRICES
    price = prices.get(args.type)
    cost = (f", about ${2 * price * 6 / 60 + 2 * price * args.seconds / 3600:.3f}"
            if price else "")
    log(f"project {glab.current_project()}: two {buy} {args.type} in "
        f"{zone_a}{'' if zone_a == zone_b else ' and ' + zone_b}, "
        f"ping for {args.seconds}s{cost}")
    log("takes about 5 minutes; Ctrl-C at any point still deletes everything")

    started = time.time()
    result = {
        "started": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "zone_a": zone_a, "zone_b": zone_b, "same_zone": zone_a == zone_b,
        "machine_type": args.type, "purchase": buy, "gpu": args.gpu,
        "seconds": args.seconds, "interval_s": args.interval,
        "payload_bytes": args.size,
    }

    try:
        icmp_rule()
        for name, zone in placed:
            glab.write_config(zone=zone)
            log(f"creating {name} in {zone}")
            quietly(glab.cmd_init, SimpleNamespace(
                name=name, type=args.type, disk=args.disk,
                disk_type="pd-balanced", gpu=args.gpu, image=None,
                spot=not args.on_demand, public_ip=False,
                max_run=MAX_RUN_MIN + args.seconds // 60 + 1))

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
            found[name] = i
            log(f"{name} running, internal ip {i['private_ip']}")
        glab.write_config(zone=zone_a)

        # An instance is RUNNING before sshd is. Retry rather than sleep a
        # fixed amount, which is either too short or wasted money.
        a, b = found["ping-a"], found["ping-b"]
        log("waiting for ssh on ping-a over IAP")
        for attempt in range(24):
            if ssh_ok(a["name"], zone_a):
                break
            time.sleep(5)
        else:
            glab.die("ping-a never accepted an ssh connection over IAP")

        result["source"] = {k: a[k] for k in ("name", "zone", "private_ip", "buy")}
        result["target"] = {k: b[k] for k in ("name", "zone", "private_ip", "buy")}
        code, rtts, lost, out = stream_ping(a["name"], zone_a, b["private_ip"],
                                            args.seconds, args.interval, args.size)
        result["exit_code"] = code
        result.update(parse_summary(out))
        result["rtts_ms"] = rtts
        result["no_reply_seq"] = lost
        if rtts:
            ordered = sorted(rtts)
            result["stats_ms"] = {
                "min": ordered[0], "p50": percentile(ordered, 50),
                "p90": percentile(ordered, 90), "p99": percentile(ordered, 99),
                "max": ordered[-1], "mean": round(statistics.mean(rtts), 4),
                "stdev": round(statistics.pstdev(rtts), 4),
            }
        if code or not rtts:
            result["raw"] = out
            log(f"ping exited {code}:\n{out[-2000:]}")
    finally:
        teardown(placed, args.keep)
        glab.write_config(zone=zone_a)
        result["elapsed_s"] = round(time.time() - started, 1)

        if "source" in result:
            os.makedirs(RESULTS, exist_ok=True)
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            path = os.path.join(RESULTS, f"{stamp}-{zone_a}-{args.type}.json")
            with open(path, "w") as f:
                json.dump(result, f, indent=2)
            report(result)
            print(f"\n{result['elapsed_s']:.0f}s total. every rtt is in "
                  f"{os.path.relpath(path, os.getcwd())}")
        show_leftovers()


if __name__ == "__main__":
    main()
