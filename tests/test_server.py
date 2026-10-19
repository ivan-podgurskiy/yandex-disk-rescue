import contextlib
import http.client
import io
import json
import os
import tempfile
import threading
import time
import unittest
from fakes import FakeClient
from rescue.engine import Engine
from rescue.models import CloudFile
from rescue.store import Store
try:
    from rescue.server import create_server
except ImportError:
    create_server=None


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(create_server,'secure HTTP server must exist')
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store=Store(os.path.join(self.tmp.name,'db.sqlite'))
        self.engine=Engine(self.store,FakeClient())
        self.addCleanup(self.engine.close)
        self.server=create_server(self.engine,self.tmp.name,0)
        thread=threading.Thread(target=self.server.serve_forever,daemon=True)
        thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.port=self.server.server_port
        _,body=self.request('GET','/api/bootstrap',auth=False)
        self.secret=json.loads(body)['secret']

    def request(self,method,path,body=None,headers=None,auth=True):
        conn=http.client.HTTPConnection('127.0.0.1',self.port,timeout=2)
        h={'Origin':'http://127.0.0.1:'+str(self.port)}
        if auth:
            h['X-Session-Secret']=self.secret
        if body is not None:
            h['Content-Type']='application/json'
            body=json.dumps(body)
        h.update(headers or {})
        try:
            conn.request(method,path,body,h)
            result=conn.getresponse()
            return result.status,result.read()
        finally:
            conn.close()

    def test_second_process_rejected(self):
        with self.assertRaises(ValueError):
            Engine(self.store)

    def test_foreign_host_and_origin_rejected(self):
        self.assertEqual(self.request('GET','/api/bootstrap',headers={'Host':'evil.test'},auth=False)[0],403)
        self.assertEqual(self.request('GET','/api/bootstrap',headers={'Origin':'https://evil.test'},auth=False)[0],403)

    def test_mutation_requires_session_secret(self):
        self.assertEqual(self.request('POST','/api/pause',{},auth=False)[0],403)
        self.assertEqual(self.request('POST','/api/pause',{})[0],200)

    def test_token_not_in_any_response_or_log(self):
        buf=io.StringIO()
        with contextlib.redirect_stderr(buf),contextlib.redirect_stdout(buf):
            code,body=self.request('POST','/api/connect',{'token':'TOPSECRET\ninvalid'})
            self.assertEqual(code,202)
            time.sleep(.05)
            _,status=self.request('GET','/api/status')
        self.assertNotIn(b'TOPSECRET',body+status)
        self.assertNotIn('TOPSECRET',buf.getvalue())

    def test_request_size_limit(self):
        self.assertEqual(self.request('POST','/api/connect',{'token':'x'*17000})[0],413)

    def test_catalog_command_returns_before_scan(self):
        gate=threading.Event()
        self.addCleanup(gate.set)
        self.engine.client.list_page=lambda *a: (gate.wait(1) or [])
        started=time.monotonic()
        self.assertEqual(self.request('POST','/api/catalog',{})[0],202)
        self.assertLess(time.monotonic()-started,.5)
        self.assertEqual(self.request('GET','/api/status')[0],200)
        gate.set()

    def test_pagination_is_bounded(self):
        sid=self.store.begin_snapshot()
        self.store.record_page(sid,[CloudFile('/'+str(i),1) for i in range(210)],210)
        code,body=self.request('GET','/api/files?limit=9999')
        self.assertEqual(code,200)
        self.assertEqual(len(json.loads(body)['files']),200)
        self.assertEqual(self.request('GET','/api/files?offset=-1')[0],400)

    def test_csv_formula_is_escaped(self):
        sid=self.store.begin_snapshot()
        self.store.record_page(sid,[CloudFile('=DANGEROUS()',1)],1)
        code,body=self.request('GET','/api/export.csv')
        self.assertEqual(code,200)
        self.assertIn(b"'=DANGEROUS()",body)
        self.assertEqual(self.store.files()[0]['path'],'=DANGEROUS()')

    def test_status_remains_available_during_connection_check(self):
        gate=threading.Event()
        self.addCleanup(gate.set)
        slow=FakeClient()
        slow.check_access=lambda: (gate.wait(1) and 'fixture-account')
        self.server.app.submit('connect',lambda:self.engine.connect(slow))
        started=time.monotonic()
        self.assertEqual(self.request('GET','/api/status')[0],200)
        self.assertLess(time.monotonic()-started,.4)
        gate.set()
