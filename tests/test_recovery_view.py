import os
import tempfile
import unittest
from rescue.store import Store
from rescue.models import CloudFile, TransferResult


class RecoveryViewTests(unittest.TestCase):
    def test_results_follow_selection_and_destination_not_cloud_catalog(self):
        with tempfile.TemporaryDirectory() as root:
            store = Store(os.path.join(root, 'state.sqlite'))
            sid = store.begin_snapshot()
            store.record_page(sid, [CloudFile('/Фото/a.jpg', 10), CloudFile('/Фото/Семья/b.jpg', 20),
                                    CloudFile('/Фото2/c.txt', 30), CloudFile('/unselected.txt', 40)], 4)
            ids = [row['id'] for row in store.files(sid)]
            store.enqueue(sid, False, ids[:3])
            store.set_setting('selection', ids[:2])
            store.set_active_selection(ids[:2])
            a = {'root':'/a', 'identity':{'uuid':'disk'}}
            store.switch_destination(a)
            job = store.claim_job(100, False, ids[:2])
            store.complete_job(job.id, TransferResult('verified_size', verification='size'))
            result = store.recovery_view(parent='/')
            self.assertEqual((result['total'], result['bytes'], result['saved']), (2, 30, 1))
            self.assertEqual([f['path'] for f in result['folders']], ['/Фото'])
            self.assertEqual(result['files'], [])
            self.assertEqual([f['path'] for f in store.recovery_view(parent='/Фото')['files']], ['/Фото/a.jpg'])
            self.assertEqual([f['path'] for f in store.recovery_view()['files']], ['/Фото/a.jpg', '/Фото/Семья/b.jpg'])
            store.switch_destination({'root':'/b', 'identity':{'uuid':'disk'}})
            self.assertEqual(store.recovery_view()['saved'], 0)
            store.switch_destination(a)
            self.assertEqual(store.recovery_view()['saved'], 1)
            filtered = store.recovery_view(query='Семья')
            self.assertEqual(filtered['total'], 1)
            self.assertEqual(store.recovery_view(state='verified_size')['total'], 1)
            store.set_setting('selection', None)
            self.assertEqual(store.recovery_view()['total'], 3)
            self.assertEqual(store.recovery_view(parent='/Фото')['total'], 2)

    def test_folder_totals_include_all_pages_and_preview_errors(self):
        with tempfile.TemporaryDirectory() as root:
            store = Store(os.path.join(root, 'state.sqlite'))
            sid = store.begin_snapshot()
            store.record_page(sid, [CloudFile('/Фото/%03d.jpg'%i, 10, media_type='image') for i in range(205)], 205)
            store.enqueue(sid, True)
            original = store.claim_job(100)
            store.complete_job(original.id, TransferResult('verified_size', verification='size'))
            preview = store.claim_job(100)
            store.complete_job(preview.id, TransferResult('deferred', error_code='timeout'))
            result = store.recovery_view(parent='/Фото', offset=100, limit=100)
            self.assertEqual(result['total'], 205)
            self.assertEqual(result['file_total'], 205)
            self.assertEqual(len(result['files']), 100)
            self.assertEqual(result['files'][0]['path'], '/Фото/100.jpg')
            self.assertEqual(result['errors'], 1)
            store.set_setting('previews', False)
            self.assertEqual(store.recovery_view()['errors'], 0)

    def test_catalog_without_jobs_has_no_recovery_results(self):
        with tempfile.TemporaryDirectory() as root:
            store = Store(os.path.join(root, 'state.sqlite'))
            sid = store.begin_snapshot()
            store.record_page(sid, [CloudFile('/a.txt', 10)], 1)
            self.assertEqual(store.recovery_view()['total'], 0)
