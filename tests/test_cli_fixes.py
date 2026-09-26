"""Authentication, regional estimates, and price-cache regressions; no API calls."""
import io
import json
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

import test_glab

glab = test_glab.glab


class OperationDeadline(unittest.TestCase):
    def test_pending_operation_cannot_poll_forever(self):
        api = mock.Mock()
        api.wait.return_value.status = glab.compute_v1.Operation.Status.RUNNING
        with mock.patch.object(glab, 'current_project', return_value='example-project'), \
                mock.patch.object(glab.compute_v1, 'ZoneOperationsClient', return_value=api), \
                mock.patch.object(glab.time, 'monotonic', side_effect=[0, 0, 601]):
            with self.assertRaisesRegex(SystemExit, 'example-operation did not finish'):
                glab.wait(SimpleNamespace(name='example-operation'), zone='example-zone')
        api.wait.assert_called_once_with(project='example-project', zone='example-zone',
                                         operation='example-operation', timeout=150, retry=None)

    def test_global_wait_uses_remaining_deadline(self):
        api = mock.Mock()
        api.wait.return_value.status = glab.compute_v1.Operation.Status.DONE
        api.wait.return_value.error.errors = []
        with mock.patch.object(glab, 'current_project', return_value='example-project'), \
                mock.patch.object(glab.compute_v1, 'GlobalOperationsClient', return_value=api), \
                mock.patch.object(glab.time, 'monotonic', side_effect=[0, 8]):
            glab.wait(SimpleNamespace(name='example-operation'), timeout=10)
        self.assertEqual(api.wait.call_args.kwargs['timeout'], 2)


class Login(unittest.TestCase):
    def test_missing_credentials_starts_both_login_commands(self):
        with mock.patch.object(glab.google.auth, 'default', side_effect=RuntimeError('missing')), \
                mock.patch.object(glab.subprocess, 'run', return_value=SimpleNamespace(returncode=0)) as run, \
                mock.patch.object(glab, 'cmd_whoami') as whoami, \
                mock.patch('sys.stdout', new_callable=io.StringIO):
            glab.cmd_login(SimpleNamespace())
        self.assertEqual([c.args[0] for c in run.call_args_list], [
            ['gcloud', 'auth', 'login'], ['gcloud', 'auth', 'application-default', 'login']])
        whoami.assert_called_once()

    def test_failed_adc_login_preserves_exit_status(self):
        with mock.patch.object(glab.google.auth, 'default', side_effect=RuntimeError('missing')), \
                mock.patch.object(glab.subprocess, 'run', side_effect=[
                    SimpleNamespace(returncode=0), SimpleNamespace(returncode=7)]), \
                mock.patch.object(glab, 'cmd_whoami') as whoami, \
                mock.patch('sys.stdout', new_callable=io.StringIO):
            with self.assertRaises(SystemExit) as caught:
                glab.cmd_login(SimpleNamespace())
        self.assertEqual(caught.exception.code, 7)
        whoami.assert_not_called()

    def test_existing_credentials_are_refreshed(self):
        creds = mock.Mock()
        with mock.patch.object(glab.google.auth, 'default', return_value=(creds, 'example-project')), \
                mock.patch.object(glab.subprocess, 'run') as run, \
                mock.patch.object(glab, 'cmd_whoami'):
            glab.cmd_login(SimpleNamespace())
        creds.refresh.assert_called_once()
        run.assert_not_called()


class Cache(unittest.TestCase):
    def test_expired_entry_is_fresh_after_one_fetch(self):
        self.check_refresh(fetched=1, refresh=False)

    def test_forced_refresh_replaces_fresh_entry(self):
        self.check_refresh(fetched=glab.time.time(), refresh=True)

    def check_refresh(self, fetched, refresh):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, 'prices.json')
            with open(path, 'w') as out:
                json.dump({'example-region:ondemand': {
                    'fetched': fetched, 'prices': {'e2-micro': 1}}}, out)
            with mock.patch.object(glab, 'PRICE_CACHE_PATH', path), \
                    mock.patch.object(glab, 'CONFIG_DIR', directory), \
                    mock.patch.object(glab, 'machine_price', return_value=2) as price:
                first, source = glab.region_prices('example-region', ['e2-micro'], refresh=refresh)
                second, next_source = glab.region_prices('example-region', ['e2-micro'])
            self.assertEqual((first, second), ({'e2-micro': 2}, {'e2-micro': 2}))
            self.assertEqual((source, next_source), ('live', 'cached'))
            price.assert_called_once()

    def test_types_passes_no_cache_to_pricing(self):
        with mock.patch.object(glab, 'current_zone', return_value='example-region-a'), \
                mock.patch.object(glab, 'zone_catalog', return_value={}), \
                mock.patch.object(glab, 'zone_accelerators', return_value={}), \
                mock.patch.object(glab, 'region_prices', return_value=({}, 'live')) as prices, \
                mock.patch('sys.stdout', new_callable=io.StringIO):
            glab.dispatch(['types', '--no-cache'])
        self.assertTrue(prices.call_args.kwargs['refresh'])


class Cost(unittest.TestCase):
    def test_attached_gpus_are_added(self):
        with mock.patch.object(glab, 'region_prices', return_value=({'n1-standard-4': 1}, 'live')), \
                mock.patch.object(glab, 'zone_catalog', return_value={'n1-standard-4': {'gpus': 0}}), \
                mock.patch.object(glab, 'gpu_price', return_value=2) as gpu:
            price, source = glab.instance_price('n1-standard-4', 'example-region-a', True,
                                                [('nvidia-tesla-t4', 2)])
        self.assertEqual((price, source), (5, 'live'))
        gpu.assert_called_once_with('nvidia-tesla-t4', 'example-region', True)

    def test_built_in_gpu_is_not_charged_twice(self):
        with mock.patch.object(glab, 'region_prices', return_value=({'g2-standard-4': 1}, 'cached')), \
                mock.patch.object(glab, 'zone_catalog', return_value={'g2-standard-4': {'gpus': 1}}), \
                mock.patch.object(glab, 'gpu_price') as gpu:
            price, _ = glab.instance_price('g2-standard-4', 'example-region-a', False,
                                           [('nvidia-l4', 1)])
        self.assertEqual(price, 1)
        gpu.assert_not_called()

    def test_unpriced_accelerator_makes_whole_estimate_unknown(self):
        with mock.patch.object(glab, 'region_prices', return_value=({'n1-standard-4': 1}, 'live')), \
                mock.patch.object(glab, 'zone_catalog', return_value={'n1-standard-4': {'gpus': 0}}), \
                mock.patch.object(glab, 'gpu_price', return_value=None):
            price, _ = glab.instance_price('n1-standard-4', 'example-region-a', False,
                                           [('unknown-gpu', 1)])
        self.assertIsNone(price)

    def test_fallback_does_not_apply_to_other_regions_or_attached_gpus(self):
        with mock.patch.object(glab, 'region_prices', side_effect=RuntimeError('offline')):
            self.assertIsNone(glab.instance_price('e2-micro', 'us-central1-a')[0])
            self.assertIsNone(glab.instance_price('n1-standard-4', 'asia-southeast1-a',
                                                 accelerators=[('nvidia-tesla-t4', 1)])[0])
            self.assertEqual(glab.instance_price('e2-micro', 'asia-southeast1-a')[0],
                             glab.PRICES['e2-micro'])

    def test_unknown_instance_makes_total_explicitly_incomplete(self):
        instances = [dict(name='known', type='e2-micro', zone='example-region-a',
                          state='running', buy='spot'),
                     dict(name='unknown', type='new-type', zone='example-region-a',
                          state='running', buy='spot')]
        with mock.patch.object(glab, 'describe', return_value=instances), \
                mock.patch.object(glab, 'instance_price', side_effect=[(1, 'live'), (None, 'live')]), \
                mock.patch('sys.stdout', new_callable=io.StringIO) as output:
            glab.cmd_cost(SimpleNamespace())
        self.assertIn('known compute subtotal: ~$1.0000/hour', output.getvalue())
        self.assertIn('incomplete: no price for unknown', output.getvalue())

    def test_only_running_instances_add_to_the_total(self):
        instances = [dict(name=n, type='e2-micro', zone='example-region-a',
                          state=state, buy='spot')
                     for n, state in [('up', 'running'), ('going', 'stopping'),
                                      ('down', 'terminated')]]
        with mock.patch.object(glab, 'describe', return_value=instances), \
                mock.patch.object(glab, 'instance_price', return_value=(1, 'live')) as price, \
                mock.patch('sys.stdout', new_callable=io.StringIO) as output:
            glab.cmd_cost(SimpleNamespace())
        price.assert_called_once()
        self.assertIn('compute estimate: ~$1.0000/hour', output.getvalue())


class ImageWait(unittest.TestCase):
    def test_image_create_waits_longer_than_the_default(self):
        source = dict(state='terminated', zone='example-region-a', disks=['example-disk'])
        with mock.patch.object(glab, 'find', return_value=source), \
                mock.patch.object(glab, 'current_project', return_value='example-project'), \
                mock.patch.object(glab, 'images_client'), \
                mock.patch.object(glab, 'wait') as wait, \
                mock.patch('sys.stdout', new_callable=io.StringIO):
            glab.cmd_image_create(SimpleNamespace(name='example-image', source='example-box',
                                                  family=None, description=''))
        self.assertEqual(wait.call_args.kwargs['timeout'], glab.IMAGE_WAIT_S)
