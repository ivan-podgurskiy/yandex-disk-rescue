import os
import tempfile
import unittest
from rescue.destination import Destination, DestinationError
from rescue.engine import Engine
from rescue.models import CloudFile, TransferResult
from rescue.server import Application, media_range
from rescue.store import Store
from fakes import Volume, FakeClient


class MediaTests(unittest.TestCase):
    def test_saved_preview_can_be_viewed_before_original_and_uses_actual_image_type(self):
        with tempfile.TemporaryDirectory() as root:
            store=Store(os.path.join(root,'state.sqlite'))
            volume=Volume()
            dest=Destination.bind(root,identify=volume.identify,capacity=volume.capacity)
            engine=Engine(store,FakeClient(),dest)
            try:
                app=Application(engine)
                sid=store.begin_snapshot()
                store.record_page(sid,[CloudFile('/raw.heic',100,media_type='image')],1)
                store.enqueue(sid,True)
                store.claim_job(100)
                preview=store.claim_job(100)
                rel=dest.map_path(preview,store)
                os.makedirs(os.path.dirname(os.path.join(root,rel)),exist_ok=True)
                with open(os.path.join(root,rel),'wb') as out:out.write(b'\x89PNG\r\n\x1a\nfixture')
                store.complete_job(preview.id,TransferResult('preview_saved'))
                info=app.media_info(1)
                self.assertEqual((info['kind'],info['mime']),('preview','image/png'))
            finally:
                engine.close()
                dest.close()

    def test_ranges_support_seeking_and_reject_invalid_offsets(self):
        for value,want in [(None,(0,9)),('bytes=2-4',(2,4)),('bytes=5-',(5,9)),('bytes=-3',(7,9)),('bytes=0-999',(0,9))]:
            self.assertEqual(media_range(value,10),want)
        for value in ('bytes=10-','bytes=4-2','bytes=-0','bytes=0-1,4-5','bytes=-','invalid'):
            with self.assertRaises(ValueError):media_range(value,10)

    def test_only_finished_mapped_files_in_current_destination_can_be_viewed(self):
        with tempfile.TemporaryDirectory() as root:
            store=Store(os.path.join(root,'state.sqlite'))
            volume=Volume()
            dest=Destination.bind(root,identify=volume.identify,capacity=volume.capacity)
            engine=Engine(store,FakeClient(),dest)
            try:
                app=Application(engine)
                sid=store.begin_snapshot()
                store.record_page(sid,[CloudFile('/photo.jpg',3)],1)
                store.enqueue(sid,False)
                job=store.claim_job(100)
                rel=dest.map_path(job,store)
                with open(os.path.join(root,rel),'wb') as out:out.write(b'abc')
                with self.assertRaises(FileNotFoundError):app.media_info(1)
                store.complete_job(job.id,TransferResult('verified_size',verification='size'))
                info=app.media_info(1)
                self.assertEqual(info['mime'],'image/jpeg')
                self.assertEqual(info['kind'],'original')
                with app.open_media(1,'original')[0] as src:self.assertEqual(src.read(),b'abc')
                os.unlink(os.path.join(root,rel))
                os.symlink(store.path,os.path.join(root,rel))
                with self.assertRaises(DestinationError):app.open_media(1,'original')
            finally:
                engine.close()
                dest.close()
