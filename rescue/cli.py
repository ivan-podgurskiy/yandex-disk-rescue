"""Existing CSV commands backed by the same guarded transfer primitives."""
import csv
import hashlib
import io
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

from .api import YandexClient
from .destination import Destination, DestinationError
from .locking import ProcessLock
from .models import CloudFile, Job, TransferResult, FINISHED, IMAGE_EXT
from .store import Store
from .transfer import transfer


def cli_state_path(destination):
    root=Path.home()/'Library/Application Support/YandexDiskRescue/cli'
    root.mkdir(parents=True,exist_ok=True,mode=0o700)
    os.chmod(root,0o700)
    key=hashlib.sha256(destination.scope.encode()).hexdigest()
    return str(root/(key+'.sqlite'))


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
        path=cli_state_path(destination)
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
                    try:
                        rel=destination.map_path(job,store)
                    except DestinationError as error:
                        return TransferResult('deferred',error_code=error.code)
                    return transfer(replace(job,local_rel=rel),client,destination,stop,lambda n:None,store=store)
                results=list(pool.map(work,pending))
                left=[pair for pair,result in zip(pending,results) if result.status not in FINISHED]
                fatal=any(r.error_code in ('auth','no_space','wrong_volume','missing_volume','disk_io','unsafe_path') for r in results)
                empty=empty+1 if len(left)==len(pending) else 0
                pending=left
                name='_pending_previews.csv' if preview else '_pending_download.csv'
                report=io.StringIO(newline='')
                writer=csv.DictWriter(report,fieldnames=['path','size','md5','media_type'],extrasaction='ignore')
                writer.writeheader()
                writer.writerows(r for r,_ in pending)
                payload=report.getvalue().encode('utf-8-sig')
                internal=path+'.'+name
                temp=internal+'.part'
                with open(temp,'wb') as out:
                    out.write(payload)
                os.replace(temp,internal)
                if not fatal:
                    try:
                        destination.write_report(name,payload)
                    except (DestinationError,OSError):
                        fatal=True
                if fatal:
                    print('Destination paused; pending list retained internally: '+internal,flush=True)
                print('%d remaining' % len(pending),flush=True)
                if not pending or fatal or empty>=args.stop_after:
                    break
                time.sleep(max(args.pause,max((r.retry_at for r in results),default=0)))
    finally:
        if owner:
            owner.close()
        destination.close()
