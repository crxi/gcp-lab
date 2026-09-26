# Measure network round-trip time

This test creates two small Google Cloud VMs. One sends a short packet to the
other and measures how long the reply takes. That duration is the round-trip
time (RTT), measured in milliseconds (ms). One millisecond is one thousandth
of a second.

You do not need a GPU for this test. Complete the
[repository setup and remote-connection check](../../README.md) first.
Run the commands below on your computer, from the repository directory,
with `.venv` activated.

## Run the first test

Check the project and zone, then start the default test:

```bash
glab whoami
python3 experiments/ping-latency/run.py
```

The script creates `ping-a` and `ping-b` in the working zone. Both are small
`e2-micro` Spot VMs. Spot means Google can reclaim them during a run; use
`--on-demand` if you want regular VMs instead.

Allow several minutes. The script creates the VMs, waits for remote access,
pings for 60 seconds, and deletes the VMs. Keep the terminal open until the
final resource listing appears. It sends a 56-byte payload about every
0.2 seconds, so a full run collects roughly 300 replies.

## Read the progress and summary

The terminal prints setup messages, then progress about every five seconds.
For example, a line might look like this:

```text
5s  25 replies  last 5s: min 0.301  avg 0.412  max 0.702 ms  lost so far 0
```

The three times describe replies in that reporting interval. The running
`lost so far` count records packets still awaiting a reply when ping checks;
a delayed reply can arrive later. Use the final packet-loss percentage for
the completed measurement.

The final table includes:

| Field | Meaning |
|---|---|
| Replies and loss | How many sent packets received a reply, and the percentage that did not |
| min / max | Fastest and slowest reply |
| p50 | Median: at least half the replies were this fast or faster |
| p90 / p99 | Times covering at least 90% / 99% of replies |
| mean | Average reply time |
| stdev | How much reply times varied around their average |

Lower RTT means a shorter round trip. A high p99 relative to p50 means some
replies took much longer than typical ones. RTT alone does not measure bulk
transfer speed or model response time.

## Open the saved result

The script prints a JSON filename under `experiments/ping-latency/results/`.
Open that exact file in a text editor. To format it in the terminal, replace
`RESULT_FILE.json` with the printed path:

```bash
python3 -m json.tool RESULT_FILE.json
```

`rtts_ms` contains each measured RTT. `sent`, `received`, and `loss_percent`
come from ping's final summary. `stats_ms` contains the timing statistics.
`no_reply_seq` records packets whose replies had not arrived at a check;
it is not necessarily a list of permanently lost packets.

## Change one setting at a time

Run a longer measurement:

```bash
python3 experiments/ping-latency/run.py --seconds 300
```

Compare two zones within Singapore, keeping the other settings unchanged:

```bash
python3 experiments/ping-latency/run.py --zone asia-southeast1-a --zone-b asia-southeast1-c
```

Use regular VMs instead of Spot:

```bash
python3 experiments/ping-latency/run.py --on-demand
```

Run `python3 experiments/ping-latency/run.py --help` for all options.
Keep the default CPU type for your first comparisons; changing machine type
also changes the conditions of the measurement.

## Stop and clean up

Press Ctrl-C once in the launch terminal to stop early, then wait for cleanup.
A normal run deletes both VMs, their boot disks, and its temporary ICMP
firewall rule. ICMP is the network protocol used by ping. The rule allows
ping traffic between VMs tagged `lab` within the project's network.

The VMs also have automatic deletion limits of about 20 minutes plus the
requested ping duration. The `--keep` option leaves them available for
inspection until that limit; it also leaves the temporary firewall rule.

Check what remains:

```bash
glab list --all-zones
```

If a VM remains, set its zone from the listing and delete it:

```bash
glab zone ZONE_FROM_LIST
glab destroy ping-a
```

Repeat for `ping-b` in its listed zone. For a leftover firewall rule, use the
exact name printed by your run. Replace both placeholders:

```bash
gcloud compute firewall-rules delete RULE_NAME_FROM_RUN --project YOUR_PROJECT_ID
```

## Resolve common failures

| Symptom | Next step |
|---|---|
| `glab` is not found | Activate `.venv`, add `~/.local/bin` to PATH, and rerun the setup instructions |
| An instance name already exists | Run `glab list --all-zones`; finish or clean up the earlier test before rerunning |
| SSH or IAP access fails | Repeat the remote-connection check in the repository README |
| Quota exceeded | Read the metric named in the error and ask the project administrator to check its quota |
| No replies | Read the saved error output, check both VM states, and check network/firewall permissions |

For guided help, give an LLM this page and the repository README. Ask it to
explain your command and error, then help you inspect the result and confirm
that the test resources have been removed.
