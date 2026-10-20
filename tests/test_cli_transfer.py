import hashlib
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from fakes import FakeClient, Volume
from rescue.destination import Destination
import yd_rescue as yd


class CliTransferTests(unittest.TestCase):
    def test_known_md5_rejects_same_size_wrong_content(self):
        # New shared adapter must preserve a pre-existing conflicting copy.
        try:
            from rescue.cli import run_transfer_command
        except ImportError:
            self.fail('CLI must use shared verified transfer')
        self.run_fixture({'/a':b'abc'}, preexisting=True)

    def test_sanitized_name_collisions_preserve_both_files(self):
        try:
            from rescue.cli import run_transfer_command
        except ImportError:
            self.fail('CLI must use collision-safe transfer')
        self.run_fixture({'/a?b':b'one','/a:b':b'two'})

    def run_fixture(self,data,preexisting=False):
        import argparse
        import csv
        from rescue.cli import run_transfer_command
        from rescue.models import Job
        from rescue.store import Store
        with tempfile.TemporaryDirectory() as root:
            client = FakeClient(data)
            volume = Volume()
            bind = Destination.bind
            def fixture_bind(path):
                return bind(path,identify=volume.identify,capacity=volume.capacity)
            listing = os.path.join(root,'list.csv')
            with open(listing,'w',newline='') as f:
                writer = csv.writer(f)
                writer.writerow(['path','size','md5','media_type'])
                for p,b in data.items():
                    writer.writerow([p,len(b),hashlib.md5(b).hexdigest(),''])
            out = os.path.join(root,'out')
            os.mkdir(out)
            if preexisting:
                dest = fixture_bind(out)
                store = Store(os.path.join(out,'.yd-rescue-state.sqlite'))
                sid=store.begin_snapshot()
                files=client.list_page(0)
                store.record_page(sid,files,len(files))
                rel=dest.map_path(Job(1,1,files[0],'original','',1),store)
                path=Path(out,rel)
                path.parent.mkdir(parents=True,exist_ok=True)
                path.write_bytes(b'old')
                dest.close()
            args=argparse.Namespace(list=listing,dest=out,workers=2,pause=0,stop_after=1,timeout=1,only_ext=None,size='XXXL')
            with patch('rescue.cli.YandexClient',return_value=client),patch('rescue.cli.Destination.bind',side_effect=fixture_bind),patch('rescue.cli.cli_state_path',return_value=os.path.join(root,'internal.sqlite')):
                run_transfer_command(args,'fixture',False)
            saved=[p.read_bytes() for p in Path(out).rglob('*') if p.is_file()]
            for b in data.values():
                self.assertIn(b,saved)
            if preexisting:
                self.assertIn(b'old',saved)

    def test_disk_loss_does_not_write_report_into_replacement(self):
        import argparse
        import csv
        import io
        from rescue.cli import run_transfer_command
        with tempfile.TemporaryDirectory() as root:
            out=os.path.join(root,'external')
            os.mkdir(out)
            listing=os.path.join(root,'list.csv')
            with open(listing,'w',newline='') as f:
                w=csv.writer(f)
                w.writerow(['path','size','md5','media_type'])
                w.writerow(['/a',3,hashlib.md5(b'abc').hexdigest(),''])
            client=FakeClient({'/a':b'abc'})
            def stream(*args,**kwargs):
                os.rename(out,out+'-detached')
                os.mkdir(out)
                return io.BytesIO(b'abc')
            client.open_stream=stream
            bind=Destination.bind
            volume=Volume()
            def fixture_bind(path):
                return bind(path,identify=volume.identify,capacity=volume.capacity)
            args=argparse.Namespace(list=listing,dest=out,workers=1,pause=0,stop_after=1,timeout=1,only_ext=None)
            with patch('rescue.cli.YandexClient',return_value=client),patch('rescue.cli.Destination.bind',side_effect=fixture_bind),patch('rescue.cli.cli_state_path',return_value=os.path.join(root,'internal.sqlite'),create=True):
                run_transfer_command(args,'fixture',False)
            self.assertEqual(list(Path(out).rglob('*')),[])
