"""experiments/llm-chat: the parts that do not touch an instance."""

import importlib.util
import io
import json
import os
import sys
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CHAT = os.path.join(ROOT, "experiments", "llm-chat")
sys.path.insert(0, ROOT)
sys.path.insert(0, CHAT)
import test_glab  # noqa: E402,F401  (installs the google client stubs)


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, os.path.join(CHAT, filename))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


chat = load("llm_chat_run", "run.py")
client = load("llm_chat_client", "client.py")
common = load("common", "common.py")


class Questions(unittest.TestCase):
    def test_fifty_questions_and_a_system_prompt(self):
        with open(os.path.join(CHAT, "questions.json")) as f:
            script = json.load(f)
        self.assertEqual(len(script["questions"]), 50)
        self.assertTrue(script["system"])


class Decisions(unittest.TestCase):
    def test_result_records_every_setting(self):
        d = common.decisions()
        for key in ("MODEL", "FAMILY", "GPU_TYPE", "BASE_IMAGE", "CLIENT_TYPE",
                    "SPOT", "TEMPERATURE", "SEED", "MAX_TOKENS", "MAX_MODEL_LEN"):
            self.assertIn(key, d)
        self.assertNotIn("HERE", d)


class Zones(unittest.TestCase):
    def test_create_gpu_moves_on_after_a_stockout(self):
        calls = []

        def create(name, zone, *a, **kw):
            calls.append(zone)
            if len(calls) == 1:
                raise SystemExit(f"error: The zone '{zone}' does not have "
                                 "enough resources available")
        with mock.patch.object(common, "create", create), \
                mock.patch("sys.stdout", new_callable=io.StringIO):
            zone = common.create_gpu("s", "asia-southeast1-c", "g2", 40, True, 45)
        self.assertEqual(calls, ["asia-southeast1-c", "asia-southeast1-a"])
        self.assertEqual(zone, "asia-southeast1-a")

    def test_create_gpu_passes_other_errors_through(self):
        def create(*a, **kw):
            raise SystemExit("error: quota exceeded")
        with mock.patch.object(common, "create", create):
            with self.assertRaisesRegex(SystemExit, "quota"):
                common.create_gpu("s", "asia-southeast1-c", "g2", 40, True, 45)


class Ownership(unittest.TestCase):
    def test_delete_requires_both_lab_and_current_run_labels(self):
        for labels in ({}, {"lab": "1"}, {"lab": "1", "lab-run": "old"},
                       {"lab-run": "current"},
                       {"lab": "1", "lab-run": "current"}):
            with self.subTest(labels=labels):
                api = mock.Mock()
                api.get.return_value.labels = labels
                with mock.patch.object(common.glab, "instances_client", return_value=api), \
                        mock.patch.object(common.glab, "current_project", return_value="p"), \
                        mock.patch.object(common.glab, "wait"), \
                        mock.patch.object(common, "log"):
                    common.delete_instances([("llm-build", "z")], "current")
                self.assertEqual(api.delete.call_count,
                                 int(labels == {"lab": "1", "lab-run": "current"}))

    def test_failed_fallback_attempts_remain_available_for_cleanup(self):
        placed = []
        with mock.patch.object(common, "create", side_effect=SystemExit(
                "does not have enough resources")), mock.patch.object(common, "log"):
            with self.assertRaises(SystemExit):
                common.create_gpu("s", common.ZONES[0], "g2", 40, True, 45,
                                  run_id="current", placed=placed)
        self.assertEqual(placed, [("s", z) for z in common.ZONES])

    def test_failed_identity_cleanup_prevents_image_creation(self):
        import tempfile
        builder = load("llm_chat_image", "image.py")
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.object(builder, "RESULTS", directory), \
                mock.patch.object(builder, "create_gpu", return_value="z"), \
                mock.patch.object(builder, "wait_running"), \
                mock.patch.object(builder, "wait_ssh"), \
                mock.patch.object(builder, "scp_to", return_value=mock.Mock(returncode=0)), \
                mock.patch.object(builder, "stream", return_value=(0, "")), \
                mock.patch.object(builder, "ssh", return_value=mock.Mock(
                    returncode=1, stderr="cleanup failed")), \
                mock.patch.object(builder, "delete_instances") as cleanup, \
                mock.patch.object(builder.glab, "cmd_image_create") as image_create, \
                mock.patch.object(builder.glab, "write_config"), \
                mock.patch.object(builder, "log"):
            with self.assertRaisesRegex(SystemExit, "clean builder identity"):
                builder.build("z")
        image_create.assert_not_called()
        cleanup.assert_called_once()


class Metrics(unittest.TestCase):
    TEXT = (
        "# HELP vllm:prefix_cache_hits_total hits\n"
        'vllm:prefix_cache_hits_total{engine="0",model_name="m"} 120.0\n'
        'vllm:prefix_cache_queries_total{engine="0",model_name="m"} 200.0\n'
        'vllm:time_to_first_token_seconds_bucket{le="0.1"} 3.0\n'
        'vllm:time_to_first_token_seconds_sum{model_name="m"} 0.25\n'
        'vllm:kv_cache_usage_perc{model_name="m"} NaN\n'
        "process_cpu_seconds_total 5.0\n")

    def test_scrape_keeps_vllm_sums_and_drops_buckets_and_nan(self):
        with mock.patch.object(client, "get", return_value=(200, self.TEXT)):
            m = client.scrape("h")
        self.assertEqual(m["vllm:prefix_cache_hits_total"], 120.0)
        self.assertEqual(m["vllm:time_to_first_token_seconds_sum"], 0.25)
        self.assertNotIn("vllm:time_to_first_token_seconds_bucket", m)
        self.assertNotIn("vllm:kv_cache_usage_perc", m)
        self.assertNotIn("process_cpu_seconds_total", m)

    def test_delta_keeps_measured_zeros(self):
        self.assertEqual(client.delta({"a": 1.0, "b": 2.0}, {"a": 1.0, "b": 5.0}),
                         {"a": 0.0, "b": 3.0})

    def test_missing_baseline_is_not_treated_as_zero(self):
        self.assertEqual(client.delta({}, {"a": 5.0}), {})

    def test_cache_miss_is_zero_hits_not_missing_hits(self):
        metrics = client.delta({"prefix_cache_hits_total": 0,
                                "prefix_cache_queries_total": 0},
                               {"prefix_cache_hits_total": 0,
                                "prefix_cache_queries_total": 40})
        hits = client.pick(metrics, "prefix_cache_hits_total")
        queries = client.pick(metrics, "prefix_cache_queries_total")
        self.assertEqual(hits / queries, 0)

    def test_short_sample_p99_uses_ceiling_rank(self):
        self.assertEqual(client.percentile([1, 2, 100], 99), 100)


class Stream(unittest.TestCase):
    def test_ask_times_the_stream_and_reads_usage(self):
        chunks = [
            {"choices": [{"delta": {"role": "assistant"}}]},
            {"choices": [{"delta": {"content": "Hel"}}]},
            {"choices": [{"delta": {"content": "lo"}, "finish_reason": "stop"}]},
            {"choices": [], "usage": {"prompt_tokens": 30, "completion_tokens": 2}},
        ]
        body = [f"data: {json.dumps(c)}\n".encode() for c in chunks] + \
               [b"\n", b"data: [DONE]\n"]
        response = mock.MagicMock(status=200)
        response.__iter__.return_value = iter(body)
        conn = mock.MagicMock()
        conn.getresponse.return_value = response
        with mock.patch.object(client.http.client, "HTTPConnection",
                               return_value=conn):
            text, t = client.ask("h", "m", [], 10, 0, 0)
        self.assertEqual(text, "Hello")
        self.assertEqual((t["prompt_tokens"], t["completion_tokens"]), (30, 2))
        self.assertEqual(t["chunks"], 2)
        self.assertEqual(t["finish_reason"], "stop")
        self.assertIsNotNone(t["ttft_ms"])
        sent = json.loads(conn.request.call_args.args[2])
        self.assertEqual(sent["temperature"], 0)
        self.assertTrue(sent["stream_options"]["include_usage"])

    def test_missing_usage_does_not_count_chunks_as_tokens(self):
        response = mock.MagicMock(status=200)
        chunk = {"choices": [{"delta": {"content": "several tokens"},
                               "finish_reason": "stop"}]}
        response.__iter__.return_value = iter([
            f"data: {json.dumps(chunk)}\n".encode(), b"data: [DONE]\n"])
        conn = mock.MagicMock()
        conn.getresponse.return_value = response
        with mock.patch.object(client.http.client, "HTTPConnection", return_value=conn):
            _, timing = client.ask("h", "m", [], 10, 0, 0)
        self.assertEqual(timing["chunks"], 1)
        self.assertIsNone(timing["completion_tokens"])
        self.assertIsNone(timing["decode_tok_s"])
        conn.close.assert_called_once()

    def test_truncated_stream_is_not_a_finished_turn(self):
        response = mock.MagicMock(status=200)
        chunk = {"choices": [{"delta": {"content": "partial"}}]}
        response.__iter__.return_value = iter([f"data: {json.dumps(chunk)}\n".encode()])
        conn = mock.MagicMock()
        conn.getresponse.return_value = response
        with mock.patch.object(client.http.client, "HTTPConnection", return_value=conn):
            with self.assertRaisesRegex(RuntimeError, "finish marker"):
                client.ask("h", "m", [], 10, 0, 0)
        conn.close.assert_called_once()


class Summary(unittest.TestCase):
    def test_gpu_csv(self):
        rows = chat.parse_gpu_csv(
            "2026/09/25 04:30:01.123, 87, 40, 7000, 55.5, 61, 2040\n"
            "garbage line\n")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["utilization.gpu"], 87.0)
        self.assertAlmostEqual(rows[0]["timestamp"] % 60, 1.123, places=3)

    def test_percentile_is_nearest_rank(self):
        self.assertEqual(chat.percentile(list(range(1, 101)), 99), 99)
        self.assertEqual(chat.percentile([3, None, 1, 2], 50), 2)
        self.assertIsNone(chat.percentile([], 50))

    def test_summary_splits_client_and_server_ttft(self):
        turns = [
            {"turn": 1, "wall": 100.0, "ttft_ms": 60.0, "total_ms": 1000.0,
             "prompt_tokens": 40, "completion_tokens": 10, "decode_tok_s": 50.0,
             "server": {"vllm:time_to_first_token_seconds_sum": 0.05,
                        "vllm:prefix_cache_hits_total": 0.0,
                        "vllm:prefix_cache_queries_total": 40.0}},
            {"turn": 2, "wall": 102.0, "ttft_ms": 30.0, "total_ms": 1000.0,
             "prompt_tokens": 90, "completion_tokens": 10, "decode_tok_s": 52.0,
             "server": {"vllm:time_to_first_token_seconds_sum": 0.02,
                        "vllm:prefix_cache_hits_total": 48.0,
                        "vllm:prefix_cache_queries_total": 90.0}},
        ]
        gpu = [{"timestamp": 101.0, "utilization.gpu": 90.0, "power.draw": 60.0,
                "memory.used": 20000.0, "temperature.gpu": 60.0,
                "clocks.sm": 2000.0},
               {"timestamp": 500.0, "utilization.gpu": 0.0, "power.draw": 20.0,
                "memory.used": 20000.0, "temperature.gpu": 40.0,
                "clocks.sm": 200.0}]
        s = chat.summarize(turns, gpu, 100.0, 103.0)
        self.assertEqual(turns[0]["server_ttft_ms"], 50.0)
        self.assertEqual(turns[0]["ttft_overhead_ms"], 10.0)
        self.assertEqual(s["prefix_hit_rate"], round(48 / 130, 4))
        self.assertEqual(s["completion_tokens"], 20)
        self.assertEqual(s["gpu"]["samples"], 1)   # the idle sample is outside
        self.assertEqual(s["gpu"]["util_mean"], 90.0)

    def test_report_prints_without_turns(self):
        result = {"decisions": common.decisions(), "spot": True, "zone": "z",
                  "no_prefix_cache": False, "image": "i", "questions": 50,
                  "complete": False, "incomplete": "x", "turns": []}
        with mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            chat.report(result)
        self.assertIn("INCOMPLETE", out.getvalue())


class Preemption(unittest.TestCase):
    """Run 2 on 2026-09-25 lost seven finished turns: they were only on the
    client's disk when both spot instances were preempted."""

    def turn(self, n):
        return {"kind": "turn", "turn": n, "wall": 100.0 + n, "ttft_ms": 40.0,
                "total_ms": 500.0, "prompt_tokens": 100 * n,
                "completion_tokens": 20, "decode_tok_s": 39.0, "server": {}}

    def result(self):
        return {"spot": True, "complete": False, "turns": []}

    def test_client_prints_each_record(self):
        out = io.StringIO()
        with mock.patch("sys.stdout", new_callable=io.StringIO) as stdout:
            client.record(out, {"kind": "turn", "turn": 1})
        line = stdout.getvalue().strip()
        self.assertTrue(line.startswith("RECORD "))
        self.assertEqual(json.loads(line[7:]), json.loads(out.getvalue()))

    def test_streamed_turns_survive_a_preempted_server(self):
        import tempfile
        records = [{"kind": "network", "tcp_connect_ms": [0.7]}] + \
            [self.turn(n) for n in range(1, 8)]
        marks = {"client_exit": 255,
                 "states": {"llm-server": "terminated", "llm-client": "running"}}
        result = self.result()
        with tempfile.TemporaryDirectory() as work:
            chat.collect(result, records, marks, work, 50, 10.0, 40.0, 50.0)
            self.assertTrue(os.path.exists(os.path.join(work, "turns.jsonl")))
        self.assertEqual(len(result["turns"]), 7)
        self.assertFalse(result["complete"])
        self.assertIn("llm-server is terminated after 7 turns", result["incomplete"])
        self.assertEqual(result["summary"]["completion_tokens"], 140)

    def test_an_exception_is_the_reason(self):
        import tempfile
        result = self.result()
        with tempfile.TemporaryDirectory() as work:
            chat.collect(result, [self.turn(1)], {"error": "ReadTimeout: x"},
                         work, 50, None, None, None)
        self.assertEqual(result["incomplete"], "ReadTimeout: x after 1 turns")
        self.assertIsNone(result["cold_start_s"]["running_to_ssh"])

    def test_all_turns_and_both_running_is_complete(self):
        import tempfile
        result = self.result()
        marks = {"client_exit": 0,
                 "states": {"llm-server": "running", "llm-client": "running"}}
        with tempfile.TemporaryDirectory() as work:
            chat.collect(result, [self.turn(n) for n in (1, 2)], marks, work, 2,
                         10.0, 40.0, 50.0)
        self.assertTrue(result["complete"])


if __name__ == "__main__":
    unittest.main()


class MetricsFailure(unittest.TestCase):
    def test_metrics_http_error_is_not_an_empty_success(self):
        with mock.patch.object(client, "get", return_value=(503, "unavailable")):
            with self.assertRaisesRegex(RuntimeError, "503"):
                client.scrape("host")

    def test_failed_scrapes_preserve_answers_and_continue(self):
        import tempfile
        for samples in ([OSError("before failed"), {}, {}, {}],
                        [{}, OSError("after failed"), {}, {}]):
            with self.subTest(samples=samples), tempfile.TemporaryDirectory() as d:
                questions = os.path.join(d, "questions.json")
                output = os.path.join(d, "turns.jsonl")
                with open(questions, "w") as f:
                    json.dump({"system": "test", "questions": ["one", "two"]}, f)
                argv = ["client.py", "--server", "host", "--questions", questions,
                        "--out", output, "--model", "model", "--max-tokens", "2",
                        "--temperature", "0", "--seed", "0"]
                timing = dict(prompt_tokens=1, completion_tokens=2, ttft_ms=1,
                              total_ms=2, decode_tok_s=100)
                with mock.patch.object(sys, "argv", argv), \
                        mock.patch.object(client, "wait_ready", return_value=0), \
                        mock.patch.object(client, "connect_times", return_value=[1]), \
                        mock.patch.object(client, "scrape", side_effect=samples), \
                        mock.patch.object(client, "ask", side_effect=lambda *a: ("answer", dict(timing))), \
                        mock.patch("sys.stdout", new_callable=io.StringIO):
                    client.main()
                with open(output) as f:
                    turns = [r for r in map(json.loads, f) if r["kind"] == "turn"]
                self.assertEqual(len(turns), 2)
                self.assertEqual(turns[0]["answer"], "answer")
                self.assertEqual(turns[0]["server"], {})
                self.assertIsNone(turns[0]["prefix_hit_rate"])
                self.assertEqual(len(turns[0]["metrics_errors"]), 1)
                self.assertEqual(turns[1]["metrics_errors"], [])
