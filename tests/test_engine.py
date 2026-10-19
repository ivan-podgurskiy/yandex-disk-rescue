import os
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from fakes import Volume, FakeClient
from rescue.api import ApiError
from rescue.destination import Destination, DestinationError
from rescue.models import TransferResult
from rescue.store import Store
try:
    from rescue.engine import Engine
except ImportError:
    Engine = None


def wait_for(predicate, timeout=3):
    end = time.monotonic()+timeout
    while time.monotonic()<end:
        if predicate():
            return
        time.sleep(.01)
    raise AssertionError('condition not reached')


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(Engine, 'persistent engine must exist')
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = os.path.join(self.tmp.name,'disk')
        os.mkdir(root)
        self.volume = Volume()
        self.dest = Destination.bind(root,identify=self.volume.identify,capacity=self.volume.capacity)
        self.store = Store(os.path.join(self.tmp.name,'db.sqlite'))
        self.client = FakeClient()
        self.engine = Engine(self.store,self.client,self.dest,workers=1)
        self.addCleanup(self.engine.close)
        self.engine.scan_catalog()
        wait_for(lambda: not self.engine.status()['scanning'])

    def test_restart_requeues_only_unfinished_jobs(self):
        self.engine.start(False)
        wait_for(lambda: self.store.summary()['counts']['original'].get('verified_md5') == 2)
        self.engine.close()
        engine = Engine(self.store,self.client,self.dest,workers=1)
        self.addCleanup(engine.close)
        engine.start(False)
        time.sleep(.05)
        self.assertEqual(self.store.summary()['counts']['original']['verified_md5'],2)
        self.assertEqual(max(r['attempts'] for r in self.store.files()),1)

    def test_stuck_worker_never_gets_duplicate_writer(self):
        gate = threading.Event()
        self.addCleanup(gate.set)
        calls = []
        def blocked(job,*args,**kwargs):
            calls.append(job.id)
            gate.wait(3)
            return TransferResult('deferred',error_code='cancelled')
        self.engine.transfer_fn = blocked
        self.engine.start(False)
        wait_for(lambda: len(calls)==1)
        self.engine.pause()
        with self.assertRaises(ValueError):
            self.engine.resume()
        time.sleep(.1)
        self.assertEqual(len(calls),1)
        gate.set()

    def test_pause_ack_waits_for_writer_exit(self):
        self.test_stuck_worker_never_gets_duplicate_writer()
        wait_for(lambda: self.engine.status()['active_jobs']==0)
        self.assertTrue(self.engine.status()['safe_to_disconnect'])

    def test_429_delays_all_network_work(self):
        self.client.errors = {p:ApiError('throttled',30) for p in self.client.data}
        self.engine.start(False)
        wait_for(lambda: self.engine.status()['retry_in']>25)
        self.assertEqual(sum(r['attempts'] or 0 for r in self.store.files()),1)

    def test_auth_failure_pauses_network(self):
        self.client.errors = {p:ApiError('auth') for p in self.client.data}
        self.engine.start(False)
        wait_for(lambda: self.engine.status()['pause_reason']=='auth')
        self.assertFalse(self.engine.status()['connected'])

    def test_partial_catalog_can_be_queued(self):
        self.store.begin_snapshot()
        self.engine.start(False)
        wait_for(lambda: self.store.summary()['counts']['original'].get('verified_md5')==2)

    def test_failed_scan_retains_cursor(self):
        self.client.list_page = lambda *a: (_ for _ in ()).throw(ApiError('unavailable'))
        self.engine.scan_catalog()
        wait_for(lambda: not self.engine.status()['scanning'])
        self.assertFalse(self.store.snapshot()['complete'])
        self.assertEqual(self.engine.status()['scan_error'],'unavailable')

    def test_previews_off_keeps_originals(self):
        self.engine.start(False)
        wait_for(lambda: self.store.summary()['counts']['original'].get('verified_md5')==2)
        self.assertEqual(self.store.summary()['counts']['preview'],{})

    def test_volume_resume_requires_confirmation(self):
        self.volume.uuid = 'other'
        self.engine.start(False)
        wait_for(lambda: self.engine.status()['pause_reason']=='wrong_volume')
        with self.assertRaises((ValueError,DestinationError)):
            self.engine.resume()

    def test_another_account_cannot_use_existing_queue(self):
        self.engine.pause()
        other = FakeClient()
        other.check_access = lambda:'other-account'
        with self.assertRaises(ValueError):
            self.engine.connect(other)

    def test_confirmed_volume_resume_starts_previously_unstarted_workers(self):
        self.volume.uuid='other'
        self.engine.start(False)
        self.volume.uuid='test-volume'
        self.engine.resume(confirm_destination=True)
        wait_for(lambda: self.store.summary()['counts']['original'].get('verified_md5')==2,timeout=.5)
