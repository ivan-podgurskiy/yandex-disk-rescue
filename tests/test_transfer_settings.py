import os
import tempfile
import threading
import time
import unittest
from rescue.engine import Engine
from rescue.store import Store
from rescue.api import ApiError
from fakes import FakeClient


class TransferSettingsTests(unittest.TestCase):
    def test_catalog_continues_after_transient_failure(self):
        with tempfile.TemporaryDirectory() as root:
            ahead=[0]
            client=FakeClient()
            real_page=client.list_page
            attempts=[]
            def page(offset):
                attempts.append(offset)
                if len(attempts)==1:raise ApiError('timeout')
                return real_page(offset)
            client.list_page=page
            engine=Engine(Store(os.path.join(root,'state.sqlite')),client,clock=lambda:time.monotonic()+ahead[0])
            try:
                engine.configure_transfer({'workers':1,'timeout':12,'request_interval':0,'catalog_auto_retry':True})
                engine.scan_catalog()
                end=time.monotonic()+2
                while engine.status()['catalog_retry_in']==0 and time.monotonic()<end:time.sleep(.01)
                self.assertGreater(engine.status()['catalog_retry_in'],0)
                ahead[0]=6
                end=time.monotonic()+3
                while engine.status()['scanning'] and time.monotonic()<end:time.sleep(.01)
                self.assertTrue(engine.status()['snapshot']['complete'])
                self.assertEqual(attempts[:2],[0,0])
                self.assertEqual(engine.status()['scan_error'],'')
            finally:engine.close()

    def test_lower_parallelism_does_not_cancel_existing_transfers(self):
        from rescue.destination import Destination
        from rescue.models import CloudFile, TransferResult
        from fakes import Volume
        with tempfile.TemporaryDirectory() as root:
            store=Store(os.path.join(root,'state.sqlite'))
            volume=Volume()
            dest=Destination.bind(root,identify=volume.identify,capacity=volume.capacity)
            release=threading.Event()
            active=threading.Event()
            seen=[]
            def transfer(job,client,destination,cancel,*args,**kwargs):
                seen.append(job.id)
                if len(seen)==2:active.set()
                release.wait(2)
                self.assertFalse(cancel.is_set())
                return TransferResult('verified_size',verification='size')
            engine=Engine(store,FakeClient(),dest,workers=2,transfer_fn=transfer)
            try:
                sid=store.begin_snapshot()
                store.record_page(sid,[CloudFile('/%s'%i,1) for i in range(3)],3)
                engine.start(False)
                self.assertTrue(active.wait(2))
                engine.configure_transfer({'workers':1,'timeout':12,'request_interval':0,'catalog_auto_retry':False})
                self.assertTrue(engine.status()['running'])
                self.assertEqual(engine.status()['active_jobs'],2)
                time.sleep(.05)
                self.assertEqual(len(seen),2)
                release.set()
                end=time.monotonic()+2
                while len(seen)<3 and time.monotonic()<end:time.sleep(.01)
                self.assertEqual(len(seen),3)
            finally:
                release.set()
                engine.close()
                dest.close()

    def test_settings_validate_persist_and_apply_to_client(self):
        with tempfile.TemporaryDirectory() as root:
            store=Store(os.path.join(root,'state.sqlite'))
            engine=Engine(store,FakeClient())
            try:
                chosen={'workers':2,'timeout':30,'request_interval':0.5,'catalog_auto_retry':True}
                engine.configure_transfer(chosen)
                self.assertEqual(engine.status()['transfer_settings'],chosen)
                self.assertEqual(engine.client.timeout,30)
                for bad in ({**chosen,'workers':0},{**chosen,'timeout':float('nan')},{**chosen,'catalog_auto_retry':1},{**chosen,'request_interval':-1}):
                    with self.assertRaises(ValueError):engine.configure_transfer(bad)
                self.assertEqual(store.get_setting('transfer_settings'),chosen)
            finally:engine.close()
            reopened=Engine(store)
            try:self.assertEqual(reopened.worker_count,2)
            finally:reopened.close()

    def test_catalog_retries_transient_failure_and_pause_cancels_wait(self):
        with tempfile.TemporaryDirectory() as root:
            engine=Engine(Store(os.path.join(root,'state.sqlite')),FakeClient())
            try:
                engine.configure_transfer({'workers':1,'timeout':12,'request_interval':0,'catalog_auto_retry':True})
                called=threading.Event()
                def fail(offset):called.set();raise ApiError('timeout')
                engine.client.list_page=fail
                engine.scan_catalog()
                self.assertTrue(called.wait(2))
                self.assertTrue(engine.status()['scanning'])
                engine.pause()
                engine._catalog_thread.join(2)
                self.assertFalse(engine.status()['scanning'])
            finally:engine.close()
