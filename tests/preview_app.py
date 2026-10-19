"""Test-only UI harness: generated content, no real credentials or Yandex requests."""
import hashlib
import io
import os
import sys
import tempfile
import time
sys.path.insert(0,os.path.dirname(os.path.dirname(__file__)))
from fakes import Volume,FakeClient
from rescue.api import ApiError
from rescue.destination import Destination
from rescue.engine import Engine
from rescue.server import create_server
from rescue.store import Store
import rescue.server as web


def main():
    tmp=tempfile.TemporaryDirectory(prefix='disk-rescue-preview-')
    root=os.path.join(tmp.name,'external-disk')
    os.mkdir(root)
    data={'/Фото/Путешествия/Стамбул.jpg':b'photo-original'*20000,
          '/Фото/Семья/Лето.jpg':b'family-original'*20000,
          '/Фото/<img onerror=alert(1)>.jpg':b'text-only'*100,
          '/Документы/Архив.zip':b'archive'*30000,
          '/Документы/Паспорт.pdf':b'pdf'*20000,
          '/Видео/Семейный фильм.mp4':b'movie'*30000,
          '/Заметки/Пустой файл.txt':b''}
    for i in range(24):
        data['/Фото/Архив/Кадр-%02d.jpg'%i]=bytes([i])*100000
    client=FakeClient(data)
    client.errors['/Видео/Семейный фильм.mp4']=ApiError('unavailable')
    class Slow(io.BytesIO):
        def read(self,n):
            time.sleep(.15)
            return super().read(min(n,20000))
    client.open_stream=lambda url,authenticated=False:Slow(b'\xff\xd8\xff'+b'preview'*100 if authenticated else data[url])
    client.preview_url=lambda file:file.path
    web.YandexClient=lambda *a,**k:client
    volume=Volume()
    real_bind=Destination.bind
    web.Destination.bind=lambda path:real_bind(path,identify=volume.identify,capacity=volume.capacity)
    class PreviewEngine(Engine):
        def status(self):
            value=super().status()
            value['fixture']=True
            return value
    engine=PreviewEngine(Store(os.path.join(tmp.name,'state.sqlite')))
    server=create_server(engine,tmp.name,8766)
    print('UI: http://127.0.0.1:8766\nTARGET: '+root,flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        if engine.close() and engine.destination:
            engine.destination.close()
        tmp.cleanup()

if __name__=='__main__':
    main()
