"""Check real SDK serialization in a fresh interpreter, with all API calls mocked."""
import os
import subprocess
import sys
import textwrap
import unittest


class SDKRequests(unittest.TestCase):
    def test_gpu_scheduling_is_in_serialized_insert_request(self):
        script = textwrap.dedent('''
            import contextlib, io
            from types import SimpleNamespace
            from unittest.mock import Mock, patch
            try:
                from google.cloud import compute_v1
            except ImportError:
                raise SystemExit(77)
            import glab
            for machine, gpu, built_in in [('n1-standard-4', 'nvidia-tesla-t4:2', 0),
                                           ('g2-standard-4', None, 1),
                                           ('e2-micro', None, 0)]:
                client = Mock()
                args = SimpleNamespace(name='example-box', type=machine, disk=20,
                    disk_type='pd-balanced', gpu=gpu, image=None, spot=False,
                    public_ip=False, max_run=None)
                with patch.object(glab, 'describe', return_value=[]), \\
                        patch.object(glab, 'current_zone', return_value='asia-southeast1-b'), \\
                        patch.object(glab, 'current_project', return_value='example-project'), \\
                        patch.object(glab, 'ensure_firewall'), \\
                        patch.object(glab, 'zone_catalog', return_value={machine: {'gpus': built_in}}), \\
                        patch.object(glab, 'instance_price', return_value=(None, 'unavailable')), \\
                        patch.object(glab, 'instances_client', return_value=client), \\
                        patch.object(glab, 'wait'), contextlib.redirect_stdout(io.StringIO()):
                    glab.cmd_init(args)
                instance = client.insert.call_args.kwargs['instance_resource']
                encoded = compute_v1.Instance.serialize(instance)
                decoded = compute_v1.Instance.deserialize(encoded)
                assert decoded.scheduling.on_host_maintenance == (
                    'TERMINATE' if gpu or built_in else ''), machine
                if gpu:
                    assert decoded.guest_accelerators[0].accelerator_count == 2
        ''')
        result = subprocess.run([sys.executable, '-c', script], capture_output=True,
                                text=True, cwd=os.path.dirname(os.path.dirname(__file__)))
        if result.returncode == 77:
            self.skipTest('google-cloud-compute is not installed')
        self.assertEqual(result.returncode, 0, result.stderr)
