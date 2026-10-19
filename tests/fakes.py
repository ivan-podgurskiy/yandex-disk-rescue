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
