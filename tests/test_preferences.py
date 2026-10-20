import os
import tempfile
import unittest
from dataclasses import replace
from rescue.store import Store
from rescue.models import CloudFile,Job
from rescue.destination import Destination
from fakes import Volume


class PreferenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store=Store(os.path.join(self.tmp.name,'state.sqlite'))
        self.volume=Volume()
        self.dest=Destination.bind(self.tmp.name,identify=self.volume.identify,capacity=self.volume.capacity)
        self.addCleanup(self.dest.close)

    def test_new_paths_preserve_full_cloud_structure(self):
        job=Job(1,1,CloudFile('/Фото/Семья/2024/снимок.jpg',3),'original','',1)
        self.assertEqual(self.dest.map_path(job,self.store),'Фото/Семья/2024/снимок.jpg')

    def test_colliding_directories_get_suffix_only_when_needed(self):
        def job(path,i):return Job(i,i,CloudFile(path,3),'original','',1)
        a=self.dest.map_path(job('/Фото/a',1),self.store)
        b=self.dest.map_path(job('/фото/b',2),self.store)
        self.assertEqual(a,'Фото/a')
        self.assertNotEqual(b.split('/')[0].casefold(),'фото')

    def test_legacy_mapping_is_retained(self):
        self.store.allocate_mapping(self.dest.scope,1,'original',[('originals/legacy~hash.jpg','originals/legacy~hash.jpg')])
        job=Job(1,1,CloudFile('/Фото/a.jpg',3),'original','',1)
        self.assertEqual(self.dest.map_path(job,self.store),'originals/legacy~hash.jpg')

    def test_folder_selection_matches_descendants_not_similar_prefix(self):
        sid=self.store.begin_snapshot()
        self.store.record_page(sid,[CloudFile('/Фото/a',2),CloudFile('/Фото/Семья/b',3),CloudFile('/Фото2/c',5)],3)
        ids=self.store.folder_file_ids(sid,['/Фото'],['/Фото/Семья'])
        self.assertEqual(ids,[1])
        folders=self.store.folders(sid)
        row=next(f for f in folders if f['path']=='/Фото')
        self.assertEqual((row['files'],row['bytes']),(2,5))

    def test_secret_not_exposed_in_summary_or_files(self):
        self.store.save_token('fixture-secret')
        self.assertEqual(Store(self.store.path).saved_token(),'fixture-secret')
        self.assertNotIn('fixture-secret',str(self.store.summary()))
        self.store.forget_token()
        self.assertIsNone(self.store.saved_token())

    def test_connection_and_destination_restore_after_engine_restart(self):
        from unittest.mock import patch
        from rescue.engine import Engine
        from rescue.server import Application
        from fakes import FakeClient
        initial=Engine(self.store,FakeClient(),self.dest)
        initial.close()
        self.dest.close()
        self.store.save_token('fixture-secret')
        engine=Engine(Store(self.store.path))
        self.addCleanup(engine.close)
        bind=Destination.bind
        def fixture_bind(root):
            return bind(root,identify=self.volume.identify,capacity=self.volume.capacity)
        with patch('rescue.server.YandexClient',return_value=FakeClient()),patch('rescue.server.Destination.bind',side_effect=fixture_bind):
            app=Application(engine)
            app.thread.join(2)
        self.assertTrue(engine.connected)
        self.assertEqual(engine.destination.root,os.path.realpath(self.tmp.name))
        self.assertFalse(engine.status()['running'])
        self.addCleanup(engine.destination.close)

    def test_nested_folder_can_be_included_again_below_exclusion(self):
        sid=self.store.begin_snapshot()
        self.store.record_page(sid,[CloudFile('/Фото/Семья/2024/a',3),CloudFile('/Фото/Семья/2023/b',4)],2)
        self.assertEqual(self.store.folder_file_ids(sid,['/Фото','/Фото/Семья/2024'],['/Фото/Семья']),[1])

    def test_large_selection_does_not_exceed_sqlite_parameter_limit(self):
        sid=self.store.begin_snapshot()
        self.store.record_page(sid,[CloudFile('/Фото/a',3)],1)
        self.store.enqueue(sid,False)
        ids=list(range(1,40001))
        self.store.set_active_selection(ids)
        self.assertEqual(self.store.claim_job(10,False,ids).file.path,'/Фото/a')

    def test_cli_report_names_are_reserved(self):
        job=Job(1,1,CloudFile('/_pending_download.csv',3),'original','',1)
        self.assertNotEqual(self.dest.map_path(job,self.store),'_pending_download.csv')

    def test_relocated_file_is_not_reused_as_directory(self):
        first=Job(1,1,CloudFile('/a',3),'original','',1)
        self.dest.map_path(first,self.store)
        moved=self.store.relocate_mapping(self.dest.scope,1,'original')
        second=Job(2,2,CloudFile('/'+moved+'/b',3),'original','',1)
        self.assertNotEqual(self.dest.map_path(second,self.store).split('/')[0],moved)

    def test_legacy_ancestors_are_reserved_even_if_files_are_missing(self):
        self.store.allocate_mapping(self.dest.scope,1,'original',[('originals/legacy.jpg','originals/legacy.jpg')])
        job=Job(2,2,CloudFile('/originals',3),'original','',1)
        self.assertNotEqual(self.dest.map_path(job,self.store),'originals')

    def test_existing_local_directory_is_not_used_as_a_file(self):
        os.mkdir(os.path.join(self.tmp.name,'existing'))
        job=Job(1,1,CloudFile('/existing',3),'original','',1)
        self.assertNotEqual(self.dest.map_path(job,self.store),'existing')
