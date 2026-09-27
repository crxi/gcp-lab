"""Runs on llm-client; watch.py sends it there over ssh.

Prints each question and answer from turns.jsonl as client.py writes it,
starting from the first turn, so it can be started at any point in a run.
Standard library only: the client has no internet to install anything.
"""

import argparse
import json
import os
import re
import sys
import textwrap
import time


# A list item's marker and leading spaces: "- ", "  * ", "12. ".
ITEM = re.compile(r"\s*(?:[-*+]|\d+[.)])\s+|\s+")


def wrap(text, width, indent="   "):
    """Wrap each line of `text` on its own, keeping the model's line breaks
    (lists, code) as they are. A wrapped list item continues under its text,
    not under its marker."""
    out = []
    for line in text.splitlines() or [""]:
        lead = len(line) - len(line.lstrip())
        m = ITEM.match(line)
        hang = " " * (m.end() if m else 0)
        out.append(textwrap.fill(line[lead:], width,
                                 initial_indent=indent + line[:lead],
                                 subsequent_indent=indent + hang)
                   or indent.rstrip())
    return "\n".join(out)


def show(rec, total, width):
    """The text for one record from turns.jsonl."""
    kind = rec.get("kind")
    if kind == "network":
        tcp = sorted(rec.get("tcp_connect_ms") or [])
        if not tcp:
            return None
        return (f"TCP connect to the server: median {tcp[len(tcp) // 2]:.3f} ms "
                f"over {len(tcp)} tries\n")
    if kind == "error":
        return f"ERROR in turn {rec.get('turn')}: {rec.get('error')}\n"
    if kind != "turn":
        return None

    def num(key, spec, unit=""):
        v = rec.get(key)
        return "-" if v is None else f"{v:{spec}}{unit}"

    hit = rec.get("prefix_hit_rate")
    head = (f"--- turn {rec['turn']}/{total or '?'}   "
            f"first token {num('ttft_ms', '.0f', ' ms')}   "
            f"{num('completion_tokens', 'd')} tokens in "
            f"{(rec.get('total_ms') or 0) / 1000:.1f} s   "
            f"context {num('prompt_tokens', 'd')} tokens   "
            f"cache hit {'-' if hit is None else f'{hit * 100:.0f}%'}")
    return (f"{head}\nQ:\n{wrap(rec.get('question', ''), width)}\n"
            f"A:\n{wrap(rec.get('answer', ''), width)}\n")


def count(path):
    """How many questions `path` holds, or None if it is not there yet."""
    try:
        with open(path) as f:
            return len(json.load(f)["questions"])
    except (OSError, ValueError, KeyError):
        return None


def follow(path, poll=0.5):
    """Yield complete lines of `path` as they are written, waiting for the
    file to appear first."""
    while not os.path.exists(path):
        time.sleep(poll)
    with open(path) as f:
        partial = ""
        while True:
            chunk = f.readline()
            if not chunk:
                time.sleep(poll)
                continue
            partial += chunk
            if partial.endswith("\n"):
                yield partial
                partial = ""


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--file", default="turns.jsonl")
    p.add_argument("--questions", default="questions.json")
    p.add_argument("--width", type=int, default=100)
    args = p.parse_args()

    total = None
    if not os.path.exists(args.file):
        print(f"waiting for {args.file}: client.py writes it once vLLM "
              "is ready, about two minutes after the server starts\n",
              flush=True)
    for line in follow(args.file):
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        # run.py copies the questions at about the time this starts, so the
        # count is read once the turns begin, not before.
        total = total or count(args.questions)
        text = show(rec, total, args.width)
        if text:
            print(text, flush=True)
        if rec.get("kind") == "error" or (
                rec.get("kind") == "turn" and total and rec["turn"] >= total):
            print("the conversation is over; run.py now collects the results "
                  "and deletes the VMs", flush=True)
            return


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
