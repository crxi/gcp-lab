# experiments

One directory per measurement. Each has a script that runs end to end, a
README explaining what it measures, and a `results/` directory of JSON, one
file per run, named `<UTC timestamp>-<zone>-<machine type>.json`.

| Directory | Measures |
|---|---|
| `ping-latency/` | ICMP round-trip time between two instances |

A script here creates instances, so it costs money. Each one deletes what it
created in a `finally`, including on Ctrl-C, and prints what it left behind if
a deletion fails. After a session, check with `glab list --all-zones`.
