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

    def test_native_probe_resolves_directory_to_backing_device(self):
        import plistlib
        import subprocess
        from unittest.mock import patch
        from rescue.destination import VolumeProbe
        def run(argv,**kwargs):
            if argv[:2]==['/bin/df','-P']:
                return subprocess.CompletedProcess(argv,0,b'Filesystem 512-blocks Used Available Capacity Mounted on\n/dev/disk99s1 100 20 80 20% /Volumes/Test\n',b'')
            if argv==['/usr/sbin/diskutil','info','-plist','/dev/disk99s1']:
                return subprocess.CompletedProcess(argv,0,plistlib.dumps({'VolumeUUID':'native-fixture','MountPoint':'/Volumes/Test'}),b'')
            raise subprocess.CalledProcessError(1,argv)
        with patch('rescue.destination.subprocess.run',side_effect=run):
            self.assertEqual(VolumeProbe()(self.root)['uuid'],'native-fixture')

    def test_parent_directory_swapped_after_open_is_rejected(self):
        rel=self.dest.map_path(self.job('/folder/a'),self.store)
        attempt=self.dest.open_attempt(self.job('/folder/a'),rel)
        self.addCleanup(attempt.discard)
        outside=os.path.join(self.tmp.name,'moved')
        os.rename(os.path.join(self.root,'originals'),outside)
        os.symlink(outside,os.path.join(self.root,'originals'))
        with self.assertRaises(DestinationError):
            attempt.check()
        self.assertFalse(Path(outside,rel.split('/',1)[1]).exists())

    def test_exfat_unsupported_rename_uses_exclusive_copy(self):
        import errno
        from unittest.mock import patch
        rel=self.dest.map_path(self.job('/exfat'),self.store)
        attempt=self.dest.open_attempt(self.job('/exfat'),rel)
        self.addCleanup(attempt.discard)
        attempt.writer.write(b'abc')
        with patch('rescue.destination.publish_noreplace',side_effect=OSError(errno.ENOTSUP,'unsupported')):
            attempt.commit()
        self.assertEqual(Path(self.root,rel).read_bytes(),b'abc')
        self.assertEqual(list(Path(self.root).rglob('*.part')),[])

    def test_exfat_fallback_never_overwrites_existing_copy(self):
        import errno
        from unittest.mock import patch
        rel=self.dest.map_path(self.job('/exfat-conflict'),self.store)
        attempt=self.dest.open_attempt(self.job('/exfat-conflict'),rel)
        self.addCleanup(attempt.discard)
        attempt.writer.write(b'abc')
        target=Path(self.root,rel)
        target.write_bytes(b'keep')
        with patch('rescue.destination.publish_noreplace',side_effect=OSError(errno.ENOTSUP,'unsupported')):
            with self.assertRaises(FileExistsError):
                attempt.commit()
        self.assertEqual(target.read_bytes(),b'keep')

    def test_exfat_copy_stops_on_disk_change_and_removes_only_own_partial(self):
        import errno
        from unittest.mock import patch
        rel=self.dest.map_path(self.job('/exfat-interrupt'),self.store)
        attempt=self.dest.open_attempt(self.job('/exfat-interrupt'),rel)
        self.addCleanup(attempt.discard)
        attempt.writer.write(b'abc')
        original=os.write
        def write_and_disconnect(fd,data):
            n=original(fd,data)
            self.volume.uuid='disconnected'
            return n
        with patch('rescue.destination.publish_noreplace',side_effect=OSError(errno.ENOTSUP,'unsupported')),patch('rescue.destination.os.write',side_effect=write_and_disconnect):
            with self.assertRaises(DestinationError):
                attempt.commit()
        self.assertFalse(Path(self.root,rel).exists())
