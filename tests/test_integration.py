import http.client
import json
import os
import tempfile
import threading
import unittest
from dataclasses import replace
from pathlib import Path
from fakes import MemoryYandex,Volume
from test_engine import wait_for
from rescue.api import YandexClient
from rescue.destination import Destination
from rescue.engine import Engine
from rescue.models import Job
from rescue.server import create_server
from rescue.store import Store


class RecoveryIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root=os.path.join(self.tmp.name,'external')
        os.mkdir(self.root)
        self.volume=Volume()
        self.transport=MemoryYandex({'/a?b.jpg':b'one','/a:b.jpg':b'two','/empty':b'','/flaky':b'eventually'})
        self.store=Store(os.path.join(self.tmp.name,'state.sqlite'))
        self.now=[100]
        self.engine=self.make_engine()
        self.server=create_server(self.engine,self.tmp.name,0)
        threading.Thread(target=self.server.serve_forever,daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.secret=self.server.app.secret
        self.request('/api/catalog',{})
        wait_for(lambda:not self.engine.status()['scanning'])

    def make_engine(self):
        dest=Destination.bind(self.root,identify=self.volume.identify,capacity=self.volume.capacity)
        self.addCleanup(dest.close)
        engine=Engine(self.store,YandexClient('test',opener=self.transport),dest,workers=1,clock=lambda:self.now[0],jitter=lambda:0)
        self.addCleanup(engine.close)
        return engine

    def request(self,path,data=None):
        conn=http.client.HTTPConnection('127.0.0.1',self.server.server_port,timeout=2)
        try:
            headers={'X-Session-Secret':self.secret,'Content-Type':'application/json'}
            conn.request('GET' if data is None else 'POST',path,None if data is None else json.dumps(data),headers)
            res=conn.getresponse()
            self.assertIn(res.status,(200,202))
            return json.loads(res.read())
        finally:
            conn.close()

    def test_recovery_survives_restart_and_preserves_conflicts(self):
        self.transport.fail_paths.add('/flaky')
        row=next(r for r in self.store.files() if r['path']=='/a?b.jpg')
        from rescue.models import CloudFile
        file=CloudFile(row['path'],row['size'],row['md5'],row['media_type'],row['resource_id'])
        rel=self.engine.destination.map_path(Job(1,row['id'],file,'original','',1),self.store)
        old=Path(self.root,rel)
        old.parent.mkdir(parents=True)
        old.write_bytes(b'bad')
        self.request('/api/start',{'previews':True})
        wait_for(lambda:self.store.summary()['counts']['original'].get('verified_md5')==3)
        wait_for(lambda:self.store.summary()['counts']['preview'].get('preview_saved')==2)
        self.engine.pause()
        self.assertTrue(self.engine.close())
        self.engine.destination.close()
        self.transport.fail_paths.clear()
        self.now[0]=500
        self.engine=self.make_engine()
        self.engine.start(True)
        wait_for(lambda:self.store.summary()['counts']['original'].get('verified_md5')==4)
        self.assertEqual(old.read_bytes(),b'bad')
        content=[p.read_bytes() for p in Path(self.root,'originals').rglob('*') if p.is_file()]
        for b in self.transport.data.values():
            self.assertIn(b,content)
        flaky=next(r for r in self.store.files() if r['path']=='/flaky')
        self.assertEqual(flaky['attempts'],2)
        self.assertEqual(self.store.summary()['verified_bytes'],len(b'one')+len(b'two')+len(b'eventually'))

    def test_disk_loss_creates_nothing_at_replacement_path(self):
        def replace_volume():
            self.transport.before_stream=None
            os.rename(self.root,self.root+'-detached')
            os.mkdir(self.root)
        self.transport.before_stream=replace_volume
        self.request('/api/start',{'previews':False})
        wait_for(lambda:self.engine.status()['pause_reason']=='wrong_volume')
        self.assertEqual(list(Path(self.root).rglob('*')),[])
        self.assertEqual(self.store.summary()['verified_bytes'],0)

    def test_incomplete_scan_then_rescan_preserves_old_observations(self):
        self.transport.fail_offset=2
        self.request('/api/catalog',{})
        wait_for(lambda:not self.engine.status()['scanning'])
        self.assertFalse(self.store.snapshot()['complete'])
        self.assertEqual(self.store.snapshot()['cursor'],2)
        self.transport.fail_offset=None
        self.request('/api/catalog',{})
        wait_for(lambda:not self.engine.status()['scanning'])
        self.assertTrue(self.store.snapshot()['complete'])
        del self.transport.data['/a?b.jpg']
        self.request('/api/catalog',{})
        wait_for(lambda:not self.engine.status()['scanning'])
        self.assertEqual(len(self.store.files()),4)
        self.assertEqual(len(self.store.files(snapshot_id=self.store.snapshot()['id'])),3)

    def test_selection_does_not_run_previously_queued_unselected_jobs(self):
        self.store.enqueue(None,False)
        selected=next(r['id'] for r in self.store.files() if r['path']=='/empty')
        self.engine.start(False,[selected],self.store.snapshot()['id'])
        wait_for(lambda:self.store.summary()['counts']['original'].get('verified_md5',0)>=1)
        self.engine.pause()
        wait_for(lambda:self.engine.status()['active_jobs']==0)
        self.assertEqual(sum(r['attempts'] or 0 for r in self.store.files() if r['id']!=selected),0)

    def test_report_includes_persisted_local_copy_paths(self):
        self.request('/api/start',{'previews':False})
        wait_for(lambda:self.store.summary()['counts']['original'].get('verified_md5')==4)
        result=self.request('/api/files')['files']
        self.assertTrue(all(r.get('local_original') for r in result))
