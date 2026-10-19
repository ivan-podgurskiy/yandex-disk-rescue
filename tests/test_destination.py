import os
import tempfile
import unittest
from pathlib import Path
from rescue.models import CloudFile, Job
from rescue.store import Store
from fakes import Volume

try:
    from rescue.destination import Destination, DestinationError
except ImportError:
    Destination = None


class DestinationTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(Destination, 'guarded destination must exist')
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = os.path.join(self.tmp.name, 'disk')
        os.mkdir(self.root)
        self.volume = Volume()
        self.store = Store(os.path.join(self.tmp.name, 'db.sqlite'))
        self.dest = Destination.bind(self.root, identify=self.volume.identify, capacity=self.volume.capacity)
        self.addCleanup(self.dest.close)

    def job(self, path, fid=1):
        return Job(fid,fid,CloudFile(path,3), 'original','',1)

    def test_missing_volume_never_creates_root(self):
        missing = os.path.join(self.tmp.name, 'absent')
        with self.assertRaises(DestinationError):
            Destination.bind(missing, identify=self.volume.identify)
        self.assertFalse(os.path.exists(missing))

    def test_replaced_mount_is_rejected(self):
        self.volume.uuid = 'replacement'
        with self.assertRaises(DestinationError) as caught:
            self.dest.check()
        self.assertEqual(caught.exception.code, 'wrong_volume')

    def test_unicode_case_and_reserved_collisions_stable_on_reopen(self):
        names = ['/a?b.jpg','/a:b.jpg','/A.JPG','/a.jpg','/é.jpg','/e\u0301.jpg']
        paths = [self.dest.map_path(self.job(p,i+1), self.store) for i,p in enumerate(names)]
        self.assertEqual(len({p.casefold() for p in paths}), len(names))
        self.assertEqual(self.dest.map_path(self.job(names[0]), Store(self.store.path)), paths[0])

    def test_symlink_swap_cannot_escape(self):
        rel = self.dest.map_path(self.job('/folder/a'), self.store)
        first = rel.split('/')[0]
        outside = os.path.join(self.tmp.name, 'outside')
        os.mkdir(outside)
        os.symlink(outside, os.path.join(self.root, first))
        with self.assertRaises(DestinationError):
            self.dest.open_attempt(self.job('/folder/a'), rel)
        self.assertEqual(os.listdir(outside), [])

    def test_reserved_capacity_keeps_one_gib(self):
        self.volume.free = 1073741824 + 3
        self.dest.reserve(1, 3)
        with self.assertRaises(DestinationError):
            self.dest.reserve(2, 1)
        self.dest.release(1)
        self.dest.reserve(2,3)

    def test_two_jobs_cannot_reserve_same_remaining_bytes(self):
        self.volume.free = 1073741824 + 5
        self.dest.reserve(1,3)
        with self.assertRaises(DestinationError):
            self.dest.reserve(2,3)

    def test_commit_does_not_replace_an_existing_file(self):
        rel = self.dest.map_path(self.job('/a'), self.store)
        attempt = self.dest.open_attempt(self.job('/a'), rel)
        self.addCleanup(attempt.discard)
        attempt.writer.write(b'new')
        target = Path(self.root, rel)
        target.write_bytes(b'old')
        with self.assertRaises(FileExistsError):
            attempt.commit()
        self.assertEqual(target.read_bytes(), b'old')

    def test_publish_works_without_hardlink_support(self):
        import errno
        from unittest.mock import patch
        rel=self.dest.map_path(self.job('/portable'),self.store)
        attempt=self.dest.open_attempt(self.job('/portable'),rel)
        self.addCleanup(attempt.discard)
        attempt.writer.write(b'abc')
        with patch('os.link',side_effect=OSError(errno.ENOTSUP,'hard links unavailable')):
            attempt.commit()
        self.assertEqual(Path(self.root,rel).read_bytes(),b'abc')
