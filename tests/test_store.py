import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor

try:
    from rescue.models import CloudFile, TransferResult
    from rescue.store import Store
except ImportError:
    Store = None


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(Store, 'durable store must exist')
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, 'state.sqlite')
        self.store = Store(self.path)

    def snapshot(self, files=None):
        sid = self.store.begin_snapshot()
        self.store.record_page(sid, files or [CloudFile('/a.jpg', 3, '', 'image', '')], 1)
        return sid

    def test_reopen_retains_jobs(self):
        sid = self.snapshot()
        self.store.enqueue(sid, False)
        job = self.store.claim_job(100)
        self.assertEqual(job.file.path, '/a.jpg')
        reopened = Store(self.path)
        reopened.recover_interrupted()
        self.assertEqual(reopened.claim_job(101).id, job.id)

    def test_changed_content_is_new_version(self):
        sid = self.snapshot()
        self.store.enqueue(sid, False)
        job = self.store.claim_job(100)
        self.store.complete_job(job.id, TransferResult('verified_size', verification='size'))
        newer = self.snapshot([CloudFile('/a.jpg', 4, '', 'image', '')])
        self.store.enqueue(newer, False)
        self.assertEqual(self.store.claim_job(101).file.size, 4)
        self.assertEqual(self.store.summary()['counts']['original']['verified_size'], 1)

    def test_partial_snapshot_keeps_previous_files(self):
        old = self.snapshot()
        self.store.finish_snapshot(old)
        new = self.store.begin_snapshot()
        self.store.record_page(new, [CloudFile('/b', 1, '', '', '')], 1)
        self.assertFalse(self.store.snapshot(new)['complete'])
        self.assertEqual(self.store.snapshot(old)['complete'], 1)
        self.assertEqual(len(self.store.files()), 2)

    def test_original_and_preview_results_are_independent(self):
        sid = self.snapshot()
        self.store.enqueue(sid, True)
        jobs = [self.store.claim_job(100), self.store.claim_job(100)]
        for job in jobs:
            state = 'verified_size' if job.kind == 'original' else 'preview_absent'
            self.store.complete_job(job.id, TransferResult(state))
        counts = self.store.summary()['counts']
        self.assertEqual(counts['original']['verified_size'], 1)
        self.assertEqual(counts['preview']['preview_absent'], 1)

    def test_claim_is_atomic(self):
        self.store.enqueue(self.snapshot(), False)
        with ThreadPoolExecutor(max_workers=6) as pool:
            jobs = list(pool.map(lambda _: self.store.claim_job(100), range(6)))
        self.assertEqual(sum(j is not None for j in jobs), 1)

    def test_selection_rejects_other_snapshot(self):
        a = self.snapshot()
        b = self.snapshot([CloudFile('/other', 2, '', '', '')])
        with self.assertRaises(ValueError):
            self.store.enqueue(a, False, [self.store.files(snapshot_id=b)[0]['id']])

    def test_failed_attempt_records_error_and_delay(self):
        self.store.enqueue(self.snapshot(), False)
        job = self.store.claim_job(10)
        self.store.complete_job(job.id, TransferResult('deferred', error_code='unavailable', retry_at=50))
        self.assertIsNone(self.store.claim_job(49))
        retried = self.store.claim_job(50)
        self.assertEqual(retried.attempts, 2)
