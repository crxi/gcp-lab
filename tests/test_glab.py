"""Tests for glab. Mocks only -- nothing here contacts Google.

The google-cloud-compute package is not needed to run them: the google modules
are stubbed in sys.modules before glab is imported, so the tests check glab's
own logic rather than the client library's.

    python -m unittest discover -s tests
"""

import io
import json
import os
import sys
import types
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def stub_google():
    """Enough of the google namespace for `import glab` to succeed."""
    if "google.cloud.compute_v1" in sys.modules:
        return
    google = types.ModuleType("google")
    cloud = types.ModuleType("google.cloud")
    compute_v1 = mock.MagicMock(name="compute_v1")
    api_core = types.ModuleType("google.api_core")
    exceptions = types.ModuleType("google.api_core.exceptions")

    class GoogleAPICallError(Exception):
        pass

    class NotFound(GoogleAPICallError):
        pass

    exceptions.GoogleAPICallError = GoogleAPICallError
    exceptions.NotFound = NotFound
    auth = types.ModuleType("google.auth")
    auth.default = lambda: (mock.MagicMock(), "stub-project")
    transport = types.ModuleType("google.auth.transport")
    requests_mod = types.ModuleType("google.auth.transport.requests")
    requests_mod.AuthorizedSession = mock.MagicMock()

    google.cloud = cloud
    google.auth = auth
    cloud.compute_v1 = compute_v1
    api_core.exceptions = exceptions
    auth.transport = transport
    transport.requests = requests_mod
    sys.modules.update({
        "google": google, "google.cloud": cloud,
        "google.cloud.compute_v1": compute_v1,
        "google.api_core": api_core,
        "google.api_core.exceptions": exceptions,
        "google.auth": auth, "google.auth.transport": transport,
        "google.auth.transport.requests": requests_mod,
    })


stub_google()
import glab  # noqa: E402

sys.path.insert(0, os.path.join(ROOT, "experiments", "ping-latency"))
import run as ping  # noqa: E402


_silence = None


def setUpModule():
    """The commands under test print; the test output is easier to read
    without it. Failures still show, because they go to stderr."""
    global _silence
    _silence = mock.patch("builtins.print")
    _silence.start()


def tearDownModule():
    _silence.stop()


def machine(name, vcpu, ram_gb, gpu_count=0, gpu_type=""):
    m = mock.MagicMock()
    m.name = name
    m.guest_cpus = vcpu
    m.memory_mb = ram_gb * 1024
    if gpu_count:
        acc = mock.MagicMock()
        acc.guest_accelerator_count = gpu_count
        acc.guest_accelerator_type = gpu_type
        m.accelerators = [acc]
    else:
        m.accelerators = []
    return m


class ZoneCatalog(unittest.TestCase):
    """GPU machine types come from the zone, not from a table in the source.

    The EC2 version of this tool once intersected the region's offerings with a
    hardcoded list and so reported regions with GPUs as having none. The same
    mistake is available here, because SHORTLIST and PRICES both exist.
    """

    def setUp(self):
        glab.zone_catalog.cache_clear()

    def test_catalog_is_unfiltered_and_keeps_accelerator_counts(self):
        listed = [machine("e2-medium", 2, 4),
                  machine("g2-standard-4", 4, 16, 1, "nvidia-l4"),
                  machine("a2-highgpu-1g", 12, 85, 1, "nvidia-tesla-a100")]
        client = mock.MagicMock()
        client.list.return_value = listed
        with mock.patch.object(glab.compute_v1, "MachineTypesClient",
                               return_value=client), \
                mock.patch.object(glab, "current_project", return_value="p"):
            catalog = glab.zone_catalog("asia-southeast1-b")

        # No filter argument: the point is to find out what is there.
        self.assertNotIn("filter", client.list.call_args.kwargs)
        self.assertEqual(catalog["g2-standard-4"]["gpus"], 1)
        self.assertEqual(catalog["g2-standard-4"]["gpu"], "1 x nvidia-l4")
        self.assertEqual(catalog["e2-medium"]["gpus"], 0)
        self.assertEqual(catalog["a2-highgpu-1g"]["ram"], 85)

    def test_shortlist_carries_no_gpu_types(self):
        self.assertTrue(all(not t.startswith(("g2-", "a2-", "a3-"))
                            for t in glab.SHORTLIST), glab.SHORTLIST)


class MachinePrice(unittest.TestCase):
    """A machine price is core-hours plus GB-hours, not a line item."""

    def sku(self, description, units, nanos=0, family="Compute"):
        return {"description": description,
                "category": {"resourceFamily": family},
                "pricingInfo": [{"pricingExpression": {"tieredRates": [
                    {"unitPrice": {"units": str(units), "nanos": nanos}}]}}]}

    def price(self, skus, spot=False, spec=None):
        with mock.patch.object(glab, "compute_skus", return_value=skus), \
                mock.patch.object(glab, "zone_names",
                                  return_value=["asia-southeast1-b"]), \
                mock.patch.object(glab, "zone_catalog",
                                  return_value={"n2-standard-4": spec or
                                                {"vcpu": 4, "ram": 16, "gpus": 0}}):
            return glab.machine_price("n2-standard-4", "asia-southeast1", spot)

    def test_sums_core_and_ram_rates(self):
        skus = [self.sku("N2 Instance Core running in Singapore", 0, 31_000_000),
                self.sku("N2 Instance Ram running in Singapore", 0, 4_000_000)]
        self.assertAlmostEqual(self.price(skus), 4 * 0.031 + 16 * 0.004)

    def test_spot_and_on_demand_do_not_cross(self):
        skus = [self.sku("N2 Instance Core running in Singapore", 0, 31_000_000),
                self.sku("N2 Instance Ram running in Singapore", 0, 4_000_000),
                self.sku("Spot Preemptible N2 Instance Core running in Singapore",
                         0, 8_000_000),
                self.sku("Spot Preemptible N2 Instance Ram running in Singapore",
                         0, 1_000_000)]
        self.assertAlmostEqual(self.price(skus, spot=True),
                               4 * 0.008 + 16 * 0.001)
        self.assertAlmostEqual(self.price(skus, spot=False),
                               4 * 0.031 + 16 * 0.004)

    def test_another_family_is_not_matched(self):
        skus = [self.sku("N2D AMD Instance Core running in Singapore", 0, 9),
                self.sku("N2D AMD Instance Ram running in Singapore", 0, 9)]
        self.assertIsNone(self.price(skus))

    def test_missing_half_the_pair_returns_none_not_half_a_price(self):
        skus = [self.sku("N2 Instance Core running in Singapore", 0, 31_000_000)]
        self.assertIsNone(self.price(skus))

    def test_custom_skus_are_not_matched(self):
        skus = [self.sku("N2 Instance Core running in Singapore", 0, 31_000_000),
                self.sku("N2 Instance Ram running in Singapore", 0, 4_000_000),
                self.sku("N2 Custom Instance Core running in Singapore", 0, 99_000_000),
                self.sku("N2 Custom Extended Instance Ram running in Singapore",
                         0, 99_000_000)]
        self.assertAlmostEqual(self.price(skus), 4 * 0.031 + 16 * 0.004)

    def test_gpu_is_added_to_cores_and_ram(self):
        skus = [self.sku("G2 Instance Core running in Singapore", 0, 30_000_000),
                self.sku("G2 Instance Ram running in Singapore", 0, 4_000_000),
                self.sku("Nvidia L4 GPU running in Singapore", 0, 690_000_000),
                self.sku("Nvidia L4 GPU attached to Spot Preemptible VMs "
                         "running in Singapore", 0, 410_000_000)]
        spec = {"vcpu": 4, "ram": 16, "gpus": 1, "accelerator": "nvidia-l4"}
        with mock.patch.object(glab, "compute_skus", return_value=skus), \
                mock.patch.object(glab, "zone_names",
                                  return_value=["asia-southeast1-b"]), \
                mock.patch.object(glab, "zone_catalog",
                                  return_value={"g2-standard-4": spec}):
            price = glab.machine_price("g2-standard-4", "asia-southeast1")
        self.assertAlmostEqual(price, 4 * 0.030 + 16 * 0.004 + 0.690)

    def test_unknown_gpu_returns_none_not_the_cpu_price(self):
        skus = [self.sku("A4 Instance Core running in Singapore", 0, 30_000_000),
                self.sku("A4 Instance Ram running in Singapore", 0, 4_000_000)]
        spec = {"vcpu": 224, "ram": 3968, "gpus": 8, "accelerator": "nvidia-b200"}
        with mock.patch.object(glab, "compute_skus", return_value=skus), \
                mock.patch.object(glab, "zone_names",
                                  return_value=["asia-southeast1-b"]), \
                mock.patch.object(glab, "zone_catalog",
                                  return_value={"a4-highgpu-8g": spec}):
            self.assertIsNone(glab.machine_price("a4-highgpu-8g", "asia-southeast1"))

    def test_shared_core_bills_its_fraction_not_its_vcpu_count(self):
        skus = [self.sku("E2 Instance Core running in Singapore", 0, 27_000_000),
                self.sku("E2 Instance Ram running in Singapore", 0, 3_600_000)]
        spec = {"vcpu": 2, "ram": 1, "gpus": 0}
        with mock.patch.object(glab, "compute_skus", return_value=skus), \
                mock.patch.object(glab, "zone_names",
                                  return_value=["asia-southeast1-b"]), \
                mock.patch.object(glab, "zone_catalog",
                                  return_value={"e2-micro": spec}):
            price = glab.machine_price("e2-micro", "asia-southeast1")
        self.assertAlmostEqual(price, 0.25 * 0.027 + 1 * 0.0036)


class Labels(unittest.TestCase):
    def test_only_lab_labelled_resources_count(self):
        self.assertTrue(glab.has_lab_label(mock.Mock(labels={"lab": "1"})))
        self.assertFalse(glab.has_lab_label(mock.Mock(labels={"lab": "0"})))
        self.assertFalse(glab.has_lab_label(mock.Mock(labels={})))
        self.assertFalse(glab.has_lab_label(mock.Mock(labels=None)))


class Firewall(unittest.TestCase):
    """The one ingress rule allows IAP and nothing else."""

    def test_created_rule_admits_only_the_iap_range_and_the_lab_tag(self):
        client = mock.MagicMock()
        client.get.side_effect = glab.NotFound("nope")
        with mock.patch.object(glab.compute_v1, "FirewallsClient",
                               return_value=client), \
                mock.patch.object(glab.compute_v1, "Firewall", dict), \
                mock.patch.object(glab.compute_v1, "Allowed", dict), \
                mock.patch.object(glab, "current_project", return_value="p"), \
                mock.patch.object(glab, "wait"):
            glab.ensure_firewall()
        rule = client.insert.call_args.kwargs["firewall_resource"]
        self.assertEqual(rule["source_ranges"], ["35.235.240.0/20"])
        self.assertEqual(rule["target_tags"], ["lab"])
        self.assertEqual(rule["allowed"],
                         [{"I_p_protocol": "tcp", "ports": ["22"]}])

    def test_a_widened_rule_stops_rather_than_being_repaired(self):
        existing = mock.MagicMock()
        existing.source_ranges = ["35.235.240.0/20", "0.0.0.0/0"]
        client = mock.MagicMock()
        client.get.return_value = existing
        with mock.patch.object(glab.compute_v1, "FirewallsClient",
                               return_value=client), \
                mock.patch.object(glab, "current_project", return_value="p"):
            with self.assertRaises(SystemExit) as caught:
                glab.ensure_firewall()
        self.assertIn("0.0.0.0/0", str(caught.exception))
        client.insert.assert_not_called()
        client.patch.assert_not_called()


class Destroy(unittest.TestCase):
    def instance(self, name):
        return {"name": name, "type": "e2-medium", "zone": "asia-southeast1-b"}

    def test_yes_skips_the_prompt(self):
        args = glab.argparse.Namespace(name="box", all=False, yes=True)
        client = mock.MagicMock()
        with mock.patch.object(glab, "describe",
                               return_value=[self.instance("box")]), \
                mock.patch.object(glab, "instances_client", return_value=client), \
                mock.patch.object(glab, "current_project", return_value="p"), \
                mock.patch.object(glab, "wait"), \
                mock.patch("builtins.input",
                           side_effect=AssertionError("prompted with --yes")):
            glab.cmd_destroy(args)
        client.delete.assert_called_once()

    def test_a_no_at_the_prompt_deletes_nothing(self):
        args = glab.argparse.Namespace(name=None, all=True, yes=False)
        client = mock.MagicMock()
        with mock.patch.object(glab, "describe",
                               return_value=[self.instance("box")]), \
                mock.patch.object(glab, "instances_client", return_value=client), \
                mock.patch("builtins.input", return_value="n"):
            glab.cmd_destroy(args)
        client.delete.assert_not_called()


class Spot(unittest.TestCase):
    def test_spot_stops_rather_than_being_deleted(self):
        """STOP is what makes `glab start` able to bring one back."""
        args = glab.argparse.Namespace(
            name="box", type="e2-medium", disk=20, disk_type="pd-balanced",
            gpu=None, image=None, spot=True, public_ip=False)
        client = mock.MagicMock()
        scheduling = mock.MagicMock()
        with mock.patch.object(glab, "describe", return_value=[]), \
                mock.patch.object(glab, "current_zone",
                                  return_value="asia-southeast1-b"), \
                mock.patch.object(glab, "current_project", return_value="p"), \
                mock.patch.object(glab, "ensure_firewall"), \
                mock.patch.object(glab, "instances_client", return_value=client), \
                mock.patch.object(glab.compute_v1, "Scheduling",
                                  return_value=scheduling), \
                mock.patch.object(glab, "wait"):
            glab.cmd_init(args)
        self.assertEqual(scheduling.provisioning_model, "SPOT")
        self.assertEqual(scheduling.instance_termination_action, "STOP")

    def test_a_gpu_forces_terminate_on_host_maintenance(self):
        """A GPU cannot live-migrate; the insert is rejected without this."""
        args = glab.argparse.Namespace(
            name="box", type="n1-standard-4", disk=20, disk_type="pd-balanced",
            gpu="nvidia-tesla-t4:2", image=None, spot=False, public_ip=False)
        client = mock.MagicMock()
        scheduling = mock.MagicMock()
        with mock.patch.object(glab, "describe", return_value=[]), \
                mock.patch.object(glab, "current_zone",
                                  return_value="asia-southeast1-b"), \
                mock.patch.object(glab, "current_project", return_value="p"), \
                mock.patch.object(glab, "ensure_firewall"), \
                mock.patch.object(glab, "instances_client", return_value=client), \
                mock.patch.object(glab.compute_v1, "Scheduling",
                                  return_value=scheduling), \
                mock.patch.object(glab.compute_v1, "AcceleratorConfig", dict), \
                mock.patch.object(glab, "wait"):
            glab.cmd_init(args)
        self.assertEqual(scheduling.on_host_maintenance, "TERMINATE")
        accelerators = client.insert.call_args.kwargs[
            "instance_resource"].guest_accelerators
        self.assertEqual(accelerators[0]["accelerator_count"], 2)
        self.assertTrue(accelerators[0]["accelerator_type"].endswith(
            "zones/asia-southeast1-b/acceleratorTypes/nvidia-tesla-t4"))


class PriceCache(unittest.TestCase):
    def test_a_type_missing_from_the_cache_is_fetched_and_merged(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "prices.json")
            with open(path, "w") as f:
                json.dump({"asia-southeast1:ondemand": {
                    "fetched": glab.time.time(),
                    "prices": {"e2-medium": 0.0447}}}, f)
            with mock.patch.object(glab, "PRICE_CACHE_PATH", path), \
                    mock.patch.object(glab, "CONFIG_DIR", d), \
                    mock.patch.object(glab, "machine_price",
                                      return_value=0.8720) as priced:
                prices, source = glab.region_prices(
                    "asia-southeast1", ["e2-medium", "g2-standard-4"])
        self.assertEqual(source, "live")
        self.assertEqual(prices["e2-medium"], 0.0447)      # kept
        self.assertEqual(prices["g2-standard-4"], 0.8720)  # fetched
        # Only the missing one was priced.
        self.assertEqual([c.args[0] for c in priced.call_args_list],
                         ["g2-standard-4"])


class PingExperiment(unittest.TestCase):
    OUTPUT = ("PING 10.148.0.3 (10.148.0.3) 56(84) bytes of data.\n\n"
              "--- 10.148.0.3 ping statistics ---\n"
              "50 packets transmitted, 50 received, 0% packet loss, "
              "time 9812ms\n"
              "rtt min/avg/max/mdev = 0.146/0.211/0.780/0.114 ms\n")

    def test_parses_counts_and_rtt(self):
        parsed = ping.parse_summary(self.OUTPUT)
        self.assertEqual((parsed["sent"], parsed["received"]), (50, 50))
        self.assertEqual(parsed["loss_percent"], 0.0)
        self.assertEqual(parsed["avg_ms"], 0.211)
        self.assertEqual(parsed["mdev_ms"], 0.114)

    def test_total_loss_has_counts_but_no_rtt(self):
        output = ("--- 10.148.0.3 ping statistics ---\n"
                  "50 packets transmitted, 0 received, 100% packet loss, "
                  "time 10201ms\n")
        parsed = ping.parse_summary(output)
        self.assertEqual(parsed["loss_percent"], 100.0)
        self.assertNotIn("avg_ms", parsed)

    def test_the_icmp_rule_is_tag_to_tag_not_a_cidr(self):
        client = mock.MagicMock()
        client.get.side_effect = glab.NotFound("nope")
        with mock.patch.object(glab.compute_v1, "FirewallsClient",
                               return_value=client), \
                mock.patch.object(glab.compute_v1, "Firewall", dict), \
                mock.patch.object(glab.compute_v1, "Allowed", dict), \
                mock.patch.object(glab, "current_project", return_value="p"), \
                mock.patch.object(glab, "wait"):
            ping.icmp_rule("lab-ping-test-example")
        rule = client.insert.call_args.kwargs["firewall_resource"]
        self.assertNotIn("source_ranges", rule)
        self.assertEqual(rule["source_tags"], ["lab"])
        self.assertEqual(rule["target_tags"], ["lab"])
        self.assertEqual(rule["allowed"], [{"I_p_protocol": "icmp"}])

    def test_teardown_skips_instances_that_never_launched(self):
        """A failure before launch leaves nothing to delete."""
        client = mock.MagicMock()
        client.get.side_effect = glab.NotFound("nope")
        with mock.patch.object(glab, "instances_client", return_value=client), \
                mock.patch.object(glab, "current_project", return_value="p"), \
                mock.patch.object(glab, "wait") as wait, \
                mock.patch.object(ping, "drop_icmp_rule") as drop:
            ping.teardown([("ping-a", "asia-southeast1-b"),
                           ("ping-b", "asia-southeast1-b")], keep=False,
                          run_id="run-a", rule_name="rule-a")
        wait.assert_not_called()
        drop.assert_called_once()

    def test_teardown_issues_both_deletes_before_waiting(self):
        """Deleting takes about two minutes each; the two run together."""
        calls = []
        client = mock.MagicMock()
        client.get.return_value.labels = {"lab": "1", "lab-run": "run-a"}
        client.delete.side_effect = lambda **kw: calls.append(
            ("delete", kw["instance"])) or kw["instance"]
        with mock.patch.object(glab, "instances_client", return_value=client), \
                mock.patch.object(glab, "current_project", return_value="p"), \
                mock.patch.object(glab, "wait",
                                  side_effect=lambda op, z: calls.append(("wait", op))), \
                mock.patch.object(ping, "drop_icmp_rule"):
            ping.teardown([("ping-a", "z"), ("ping-b", "z")], keep=False,
                          run_id="run-a", rule_name="rule-a")
        self.assertEqual(calls, [("delete", "ping-a"), ("delete", "ping-b"),
                                 ("wait", "ping-a"), ("wait", "ping-b")])

    def test_teardown_preserves_instances_from_other_runs(self):
        client = mock.MagicMock()
        client.get.return_value.labels = {"lab": "1", "lab-run": "other-run"}
        with mock.patch.object(glab, "instances_client", return_value=client), \
                mock.patch.object(glab, "current_project", return_value="p"), \
                mock.patch.object(ping, "drop_icmp_rule") as drop:
            ping.teardown([("ping-a", "z")], False, "run-a", "rule-a")
        client.delete.assert_not_called()
        drop.assert_called_once_with("rule-a")

    def test_reply_and_missing_reply_lines(self):
        self.assertEqual(ping.REPLY.search(
            "64 bytes from 10.148.0.4: icmp_seq=12 ttl=64 time=0.262 ms").groups(),
            ("12", "0.262"))
        self.assertEqual(ping.NO_REPLY.search(
            "no answer yet for icmp_seq=7").group(1), "7")

    def test_percentile_is_nearest_rank(self):
        values = list(range(1, 101))
        self.assertEqual(ping.percentile(values, 50), 50)
        self.assertEqual(ping.percentile(values, 99), 99)
        self.assertEqual(ping.percentile([5.0], 90), 5.0)
        self.assertIsNone(ping.percentile([], 50))

    def test_leftovers_list_every_zone(self):
        with mock.patch.object(glab, "cmd_list") as listing, \
                mock.patch("sys.stdout"):
            ping.show_leftovers()
        self.assertTrue(listing.call_args.args[0].all_zones)

    def test_leftovers_failure_does_not_raise(self):
        with mock.patch.object(glab, "cmd_list", side_effect=SystemExit(1)), \
                mock.patch("sys.stdout"):
            ping.show_leftovers()

    def test_keep_destroys_nothing(self):
        with mock.patch.object(glab, "instances_client") as client, \
                mock.patch.object(ping, "drop_icmp_rule") as drop:
            ping.teardown([("ping-a", "asia-southeast1-b")], keep=True,
                          run_id="run-a", rule_name="rule-a")
        client.assert_not_called()
        drop.assert_not_called()



class Wait(unittest.TestCase):
    """operations.wait can return before the operation is done."""

    def test_waits_again_until_done(self):
        running = mock.Mock(status=glab.compute_v1.Operation.Status.RUNNING)
        done = mock.Mock(status=glab.compute_v1.Operation.Status.DONE)
        done.error.errors = []
        client = mock.Mock()
        client.wait.side_effect = [running, running, done]
        with mock.patch.object(glab, "current_project", return_value="p"), \
                mock.patch.object(glab.compute_v1, "ZoneOperationsClient",
                                  return_value=client):
            result = glab.wait(mock.Mock(name="op"), zone="asia-southeast1-b")
        self.assertIs(result, done)
        self.assertEqual(client.wait.call_count, 3)


class MaxRun(unittest.TestCase):
    def insert(self, **extra):
        args = glab.argparse.Namespace(
            name="box", type="e2-micro", disk=10, disk_type="pd-balanced",
            gpu=None, image=None, spot=True, public_ip=False, **extra)
        client = mock.MagicMock()
        # compute_v1 is a stub here, so give Scheduling and Duration plain
        # objects to hold what cmd_init sets.
        scheduling = types.SimpleNamespace()
        with mock.patch.object(glab.compute_v1, "Scheduling",
                               return_value=scheduling), \
                mock.patch.object(glab.compute_v1, "Duration",
                                  side_effect=lambda seconds: types.SimpleNamespace(
                                      seconds=seconds)), \
                mock.patch.object(glab, "describe", return_value=[]), \
                mock.patch.object(glab, "current_zone", return_value="z"), \
                mock.patch.object(glab, "current_project", return_value="p"), \
                mock.patch.object(glab, "ensure_firewall"), \
                mock.patch.object(glab, "wait"), \
                mock.patch.object(glab, "instances_client", return_value=client), \
                mock.patch("sys.stdout", new_callable=io.StringIO):
            glab.cmd_init(args)
        return scheduling

    def test_limit_deletes_rather_than_stops(self):
        s = self.insert(max_run=45)
        self.assertEqual(s.max_run_duration.seconds, 45 * 60)
        self.assertEqual(s.instance_termination_action, "DELETE")

    def test_spot_without_a_limit_stops(self):
        self.assertEqual(self.insert().instance_termination_action, "STOP")


class Uptime(unittest.TestCase):
    # The API's timestamps carry the zone's offset, not UTC.
    now = glab.datetime(2026, 9, 26, 0, 40, tzinfo=glab.timezone.utc)

    def test_minutes_hours_days(self):
        self.assertEqual(glab.uptime("2026-09-25T17:33:05.123-07:00", "running",
                                     self.now), "6m")
        self.assertEqual(glab.uptime("2026-09-25T15:10:00.000-07:00", "running",
                                     self.now), "2h30m")
        self.assertEqual(glab.uptime("2026-09-23T00:00:00.000+00:00", "running",
                                     self.now), "3d0h")

    def test_only_a_running_instance_has_one(self):
        self.assertEqual(glab.uptime("2026-09-25T17:33:05.123-07:00",
                                     "terminated", self.now), "-")
        self.assertEqual(glab.uptime("", "running", self.now), "-")


if __name__ == "__main__":
    unittest.main()
