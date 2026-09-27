"""llm-chat's watch.py and the two programs it runs on the VMs; no API calls."""
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from test_llm_chat import CHAT, load

watch = load("llm_chat_watch", "watch.py")
viewer = load("llm_chat_watch_client", "watch_client.py")
monitor = load("llm_chat_watch_server", "watch_server.py")

TURN = {"kind": "turn", "turn": 3, "question": "Which sensor?",
        "answer": "A BME280.\n- temperature\n- humidity", "ttft_ms": 61.2,
        "completion_tokens": 42, "total_ms": 1234.0, "prompt_tokens": 800,
        "prefix_hit_rate": 0.98}


class Viewer(unittest.TestCase):
    def test_a_turn_shows_timing_question_and_answer(self):
        text = viewer.show(TURN, 50, 80)
        self.assertIn("turn 3/50", text)
        self.assertIn("first token 61 ms", text)
        self.assertIn("cache hit 98%", text)
        self.assertIn("   Which sensor?", text)
        # The model's own line breaks survive the wrapping.
        self.assertIn("   - temperature\n   - humidity", text)

    def test_a_wrapped_list_item_hangs_under_its_text(self):
        text = viewer.wrap("   - Connect DATA to a GPIO pin on the board", 30)
        self.assertEqual(text, "      - Connect DATA to a GPIO\n"
                               "        pin on the board")

    def test_missing_measurements_show_as_dashes(self):
        rec = dict(TURN, ttft_ms=None, completion_tokens=None, prefix_hit_rate=None)
        text = viewer.show(rec, None, 80)
        self.assertIn("turn 3/?", text)
        self.assertIn("first token -", text)
        self.assertIn("cache hit -", text)

    def test_other_records(self):
        self.assertIn("median 0.450 ms", viewer.show(
            {"kind": "network", "tcp_connect_ms": [0.5, 0.45, 0.4]}, 50, 80))
        self.assertIn("ERROR in turn 7", viewer.show(
            {"kind": "error", "turn": 7, "error": "boom"}, 50, 80))
        self.assertIsNone(viewer.show({"kind": "other"}, 50, 80))

    def test_runs_standalone_and_stops_after_the_last_turn(self):
        """The VM gets the file's source on stdin, so it must need nothing
        but the standard library."""
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "questions.json"), "w") as f:
                json.dump({"questions": ["a", "b"]}, f)
            with open(os.path.join(d, "turns.jsonl"), "w") as f:
                for n in (1, 2):
                    f.write(json.dumps(dict(TURN, turn=n)) + "\n")
            with open(os.path.join(CHAT, "watch_client.py")) as source:
                r = subprocess.run([sys.executable, "-I", "-", "--width", "60"],
                                   stdin=source, cwd=d, capture_output=True,
                                   text=True, timeout=20)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("turn 2/2", r.stdout)
        self.assertIn("conversation is over", r.stdout)


class Monitor(unittest.TestCase):
    METRICS = {"vllm:num_requests_running": 1, "vllm:num_requests_waiting": 0,
               "vllm:kv_cache_usage_perc": 0.12,
               "vllm:generation_tokens_total": 1100,
               "vllm:prefix_cache_hits_total": 990,
               "vllm:prefix_cache_queries_total": 1000}

    def test_status_line(self):
        line = monitor.status((99, 20480, 23034, 72, 81), self.METRICS,
                              {"vllm:generation_tokens_total": 1000}, 2.0)
        self.assertIn("GPU  99%  20.0/22.5 GiB   72 W  81 C", line)
        self.assertIn("requests 1 running 0 waiting  KV cache 12% full", line)
        self.assertIn("50 tokens/s generated", line)
        self.assertIn("prefix cache hits 99.0% so far", line)

    def test_vllm_not_up_yet(self):
        line = monitor.status(None, None, None, 2.0)
        self.assertIn("nvidia-smi gave nothing", line)
        self.assertIn("vLLM not answering yet", line)

    def test_standard_library_only(self):
        with open(os.path.join(CHAT, "watch_server.py")) as source:
            r = subprocess.run([sys.executable, "-I", "-", "--help"], stdin=source,
                               capture_output=True, text=True, timeout=20)
        self.assertEqual(r.returncode, 0, r.stderr)


class Watch(unittest.TestCase):
    def test_locate_finds_the_vm_in_a_fallback_zone(self):
        api = mock.Mock()
        api.get.side_effect = lambda **kw: (
            SimpleNamespace(status="RUNNING") if kw["zone"] == "asia-southeast1-c"
            else (_ for _ in ()).throw(watch.NotFound("no")))
        with mock.patch.object(watch.glab, "instances_client", return_value=api), \
                mock.patch.object(watch.glab, "current_project", return_value="p"), \
                mock.patch.object(watch.glab, "current_zone",
                                  return_value="asia-southeast1-a"):
            self.assertEqual(watch.locate("llm-client"), "asia-southeast1-c")
        self.assertEqual(api.get.call_args_list[0].kwargs["zone"], "asia-southeast1-a")

    def test_locate_waits_for_a_vm_that_is_starting(self):
        states = iter(["STAGING", "RUNNING"])
        api = mock.Mock()
        api.get.side_effect = lambda **kw: SimpleNamespace(status=next(states))
        with mock.patch.object(watch.glab, "instances_client", return_value=api), \
                mock.patch.object(watch.glab, "current_project", return_value="p"), \
                mock.patch.object(watch.glab, "current_zone", return_value="z"), \
                mock.patch.object(watch.time, "sleep"), \
                mock.patch.object(watch, "log") as log:
            self.assertEqual(watch.locate("llm-server"), "z")
        self.assertIn("staging in z", log.call_args.args[0])

    def test_remote_sends_the_program_on_stdin(self):
        proc = mock.Mock()
        with mock.patch.object(watch.glab, "gcloud_ssh",
                               side_effect=lambda n, z, extra: ["ssh"] + extra), \
                mock.patch.object(watch.subprocess, "Popen", return_value=proc) as popen:
            watch.remote("llm-client", "z", "watch_client.py", ["--width", "80"])
        command = popen.call_args.args[0]
        self.assertEqual(command[2], "python3 -u - --width 80")
        self.assertIn("--verbosity=error", command)
        sent = proc.stdin.write.call_args.args[0]
        self.assertIn("def follow(", sent)
        proc.stdin.close.assert_called_once()

    def test_relay_keeps_an_answer_together(self):
        proc = SimpleNamespace(stdout=iter(["Q: a\n", "A: b\n", "\n"]),
                               stderr=None, wait=lambda: 0)
        with mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            watch.relay(proc, "", blocks=True)
        self.assertEqual(out.getvalue(), "Q: a\nA: b\n\n")

    def test_relay_prefixes_status_lines(self):
        proc = SimpleNamespace(stdout=iter(["GPU 99%\n"]), stderr=None,
                               wait=lambda: 0)
        with mock.patch("sys.stdout", new_callable=io.StringIO) as out, \
                mock.patch.object(watch.time, "strftime", return_value="11:40:44  "):
            watch.relay(proc, "server  ", blocks=False)
        self.assertEqual(out.getvalue(), "server  11:40:44  GPU 99%\n")

    def run_one(self, vm_status):
        """watch() over one VM whose connection ended with ssh's 255."""
        proc = SimpleNamespace(stdout=iter([]), stderr=io.StringIO("gcloud: 255"),
                               wait=lambda: 255, returncode=255)
        api = mock.Mock()
        if vm_status is None:
            api.get.side_effect = watch.NotFound("gone")
        else:
            api.get.return_value = SimpleNamespace(status=vm_status)
        with mock.patch.object(watch, "locate", return_value="z"), \
                mock.patch.object(watch, "wait_ssh"), \
                mock.patch.object(watch, "remote", return_value=proc), \
                mock.patch.object(watch.glab, "instances_client", return_value=api), \
                mock.patch.object(watch.glab, "current_project", return_value="p"), \
                mock.patch.object(watch, "log") as log, \
                mock.patch("sys.stderr", new_callable=io.StringIO) as err:
            watch.watch([("llm-server", "watch_server.py", [], "", False)])
        return log.call_args.args[0], err.getvalue()

    def test_a_deleted_vm_ends_quietly(self):
        for status in (None, "STOPPING"):
            said, err = self.run_one(status)
            self.assertIn("run.py has finished", said)
            self.assertEqual(err, "")

    def test_a_lost_connection_to_a_running_vm_shows_gcloud(self):
        said, err = self.run_one("RUNNING")
        self.assertIn("still running", said)
        self.assertIn("gcloud: 255", err)
