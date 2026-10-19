import hashlib
import io
import os
import tempfile
import threading
import unittest
from dataclasses import replace
from pathlib import Path
from fakes import Volume, FakeClient
from rescue.destination import Destination, DestinationError
from rescue.models import CloudFile, Job
from rescue.store import Store
try:
    from rescue.transfer import transfer
except ImportError:
    transfer = None


class TransferTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(transfer, 'verified transfer must exist')
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.volume = Volume()
        self.dest = Destination.bind(self.tmp.name, identify=self.volume.identify, capacity=self.volume.capacity)
        self.addCleanup(self.dest.close)
        self.store = Store(os.path.join(self.tmp.name,'db.sqlite'))
        self.client = FakeClient({'/a': b'abc'})
        self.cancel = threading.Event()
        self.job = self.make_job()

    def make_job(self, data=b'abc',kind='original'):
        job = Job(1,1,CloudFile('/a',len(data),hashlib.md5(data).hexdigest()),kind,'',1)
        return replace(job,local_rel=self.dest.map_path(job,self.store))

    def run_job(self, job=None):
        return transfer(job or self.job,self.client,self.dest,self.cancel,lambda n: None,store=self.store)

    def target(self, job=None):
        return Path(self.tmp.name, (job or self.job).local_rel)

    def test_same_size_bad_hash_is_not_success(self):
        self.client.data['/a'] = b'bad'
        self.assertEqual(self.run_job().error_code, 'checksum_mismatch')
        self.assertFalse(self.target().exists())

    def test_existing_conflict_is_preserved(self):
        self.target().parent.mkdir(parents=True)
        self.target().write_bytes(b'old')
        self.assertEqual(self.run_job().status,'verified_md5')
        self.assertEqual(self.target().read_bytes(),b'old')
        rel = self.store.mapping(self.dest.scope,1,'original')
        self.assertNotEqual(rel,self.job.local_rel)
        self.assertEqual(Path(self.tmp.name,rel).read_bytes(),b'abc')

    def test_zero_byte_original(self):
        self.client.data['/a'] = b''
        job = self.make_job(b'')
        self.assertEqual(self.run_job(job).status,'verified_md5')
        self.assertEqual(self.target(job).read_bytes(),b'')

    def test_interrupted_stream_not_published(self):
        class Broken(io.BytesIO):
            def read(self,n):
                raise OSError('private remote detail')
        self.client.open_stream = lambda *a,**k: Broken()
        result = self.run_job()
        self.assertEqual(result.error_code,'network_io')
        self.assertFalse(self.target().exists())
        self.assertEqual(list(Path(self.tmp.name).rglob('*.part')),[])

    def test_absent_preview_is_not_saved(self):
        result = self.run_job(self.make_job(kind='preview'))
        self.assertEqual(result.status,'preview_absent')
        self.assertEqual(result.written_bytes,0)

    def test_cancel_stops_before_commit(self):
        self.cancel.set()
        self.assertEqual(self.run_job().error_code,'cancelled')
        self.assertFalse(self.target().exists())

    def test_mount_swap_during_stream_pauses(self):
        volume = self.volume
        class Swap(io.BytesIO):
            def read(self,n):
                volume.uuid = 'replacement'
                return super().read(n)
        self.client.open_stream = lambda *a,**k: Swap(b'abc')
        self.assertEqual(self.run_job().error_code,'wrong_volume')
        self.assertFalse(self.target().exists())

    def test_no_space_during_chunk_pauses(self):
        volume = self.volume
        class Full(io.BytesIO):
            def read(self,n):
                volume.free = 100
                return super().read(n)
        self.client.open_stream = lambda *a,**k: Full(b'abc')
        self.assertEqual(self.run_job().error_code,'no_space')
        self.assertFalse(self.target().exists())
