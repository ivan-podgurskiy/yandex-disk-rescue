"""Controlled external boundaries, with real store/transfer/engine in tests."""
import io
from rescue.models import CloudFile


class Volume:
    def __init__(self):
        self.uuid = 'test-volume'
        self.free = 10 * 1024**3

    def identify(self, root):
        import os
        return {'uuid':self.uuid,'mount':root,'device':os.stat(root).st_dev}

    def capacity(self, root):
        return self.free


class FakeClient:
    def __init__(self, data=None):
        self.data = data if data is not None else {'/photo.jpg': b'abc', '/empty':b''}
        self.errors = {}

    def check_access(self):
        return 'fixture-account'

    def list_page(self, offset, limit=1000):
        import hashlib
        return [CloudFile(p,len(b),hashlib.md5(b).hexdigest(),'image' if p.endswith('.jpg') else '',p)
                for p,b in sorted(self.data.items())][offset:offset+limit]

    def original_url(self, file):
        if file.path in self.errors:
            raise self.errors[file.path]
        return file.path

    def preview_url(self, file):
        return None

    def open_stream(self, url, authenticated=False):
        return io.BytesIO(self.data[url])


class MemoryYandex:
    """Actual YandexClient transport boundary; every endpoint is read-only and local."""
    def __init__(self,data):
        self.data=data
        self.fail_paths=set()
        self.fail_offset=None
        self.before_stream=None
        self.preview=b'\xff\xd8\xfffixture-preview'

    def open(self,req,timeout=None):
        import hashlib
        import json
        import urllib.error
        from urllib.parse import urlsplit,parse_qs,urlencode
        u=urlsplit(req.full_url)
        params=parse_qs(u.query)
        def response(value):
            return io.BytesIO(json.dumps(value).encode())
        if u.path=='/v1/disk/':
            return response({'user':{'uid':'fixture-account'}})
        if u.path=='/v1/disk/resources/files':
            offset=int(params['offset'][0])
            if offset==self.fail_offset:
                raise urllib.error.HTTPError(req.full_url,503,'unavailable',{},None)
            items=[{'path':'disk:'+p,'size':len(b),'md5':hashlib.md5(b).hexdigest(),'resource_id':p,
                    'media_type':'image' if p.endswith('.jpg') else ''} for p,b in sorted(self.data.items())]
            return response({'items':items[offset:offset+2]})
        if u.path=='/v1/disk/resources/download':
            path=params['path'][0][5:]
            if path in self.fail_paths:
                raise urllib.error.HTTPError(req.full_url,503,'unavailable',{},None)
            return response({'href':'https://downloader.disk.yandex.ru/original?'+urlencode({'path':path})})
        if u.path=='/v1/disk/resources':
            path=params['path'][0][5:]
            return response({'preview':'https://downloader.disk.yandex.ru/preview?'+urlencode({'path':path})} if path.endswith('.jpg') else {})
        if u.path in ('/original','/preview'):
            if self.before_stream:
                self.before_stream()
            return io.BytesIO(self.preview if u.path=='/preview' else self.data[params['path'][0]])
        raise AssertionError('unexpected endpoint: '+u.path)
