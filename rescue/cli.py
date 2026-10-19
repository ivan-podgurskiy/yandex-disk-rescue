"""Existing CSV commands backed by the same guarded transfer primitives."""
import csv
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

from .api import YandexClient
from .destination import Destination
from .locking import ProcessLock
from .models import CloudFile, Job, FINISHED, IMAGE_EXT
from .store import Store
from .transfer import transfer


def run_transfer_command(args,token,preview=False):
    with open(args.list,encoding='utf-8-sig') as source:
        rows=list(csv.DictReader(source))
    if preview:
        rows=[r for r in rows if r['path'].lower().endswith(IMAGE_EXT)]
    elif args.only_ext:
        rows=[r for r in rows if r['path'].lower().endswith(tuple(e.lower() for e in args.only_ext))]
    # Do not manufacture a missing /Volumes mount. Local folders retain CLI convenience.
    absolute=os.path.abspath(args.dest)
    if absolute.startswith('/Volumes/'):
        mount='/'.join(absolute.split('/')[:3])
        if not os.path.ismount(mount):
            raise ValueError('missing_volume')
    os.makedirs(absolute,exist_ok=True)
    destination=Destination.bind(absolute)
    owner=None
    try:
        destination.acquire()
        path=os.path.join(absolute,'.yd-rescue-state.sqlite')
        owner=ProcessLock(path+'.lock')
        store=Store(path)
        client=YandexClient(token,timeout=args.timeout)
        # Keep preview-size CLI flag, without duplicating transport security.
        if preview and hasattr(client,'preview_size'):
            client.preview_size=args.size
        sid=store.begin_snapshot()
        files=[CloudFile(r['path'],int(r['size']),r.get('md5',''),r.get('media_type',''), '') for r in rows]
        store.record_page(sid,files,len(files))
        store.finish_snapshot(sid)
        ids={r['path']:r['id'] for r in store.files(snapshot_id=sid,limit=len(files) or 1)}
        pending=list(zip(rows,files))
        stop=threading.Event()
        empty=0
        with ThreadPoolExecutor(max_workers=max(1,min(args.workers,8))) as pool:
            while pending:
                def work(pair):
                    row,file=pair
                    fid=ids[file.path]
                    job=Job(fid,fid,file,'preview' if preview else 'original','',1)
                    rel=destination.map_path(job,store)
                    return transfer(replace(job,local_rel=rel),client,destination,stop,lambda n:None,store=store)
                results=list(pool.map(work,pending))
                left=[pair for pair,result in zip(pending,results) if result.status not in FINISHED]
                fatal=any(r.error_code in ('auth','no_space','wrong_volume','missing_volume','disk_io','unsafe_path') for r in results)
                empty=empty+1 if len(left)==len(pending) else 0
                pending=left
                target=os.path.join(absolute,'_pending_previews.csv' if preview else '_pending_download.csv')
                temp=target+'.part'
                with open(temp,'w',newline='',encoding='utf-8-sig') as out:
                    writer=csv.DictWriter(out,fieldnames=['path','size','md5','media_type'],extrasaction='ignore')
                    writer.writeheader()
                    writer.writerows(r for r,_ in pending)
                os.replace(temp,target)
                print('%d remaining' % len(pending),flush=True)
                if not pending or fatal or empty>=args.stop_after:
                    break
                time.sleep(max(args.pause,max((r.retry_at for r in results),default=0)))
    finally:
        if owner:
            owner.close()
        destination.close()
