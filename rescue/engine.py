"""Bounded persistent workers. No abandoned writer is retried concurrently."""
import random
import math
import threading
import time
from collections import deque
from dataclasses import replace
from .api import ApiError
from .destination import DestinationError
from .locking import ProcessLock
from .models import TransferResult
from .transfer import transfer

DISK_ERRORS = {'missing_volume','wrong_volume','unsafe_path','no_space','disk_io','destination_busy'}
TRANSFER_DEFAULTS={'workers':4,'timeout':12,'request_interval':0,'catalog_auto_retry':False}


def transfer_settings(value):
    if not isinstance(value,dict) or set(value)!=set(TRANSFER_DEFAULTS):raise ValueError('invalid_input')
    for key,low,high in (('workers',1,8),('timeout',5,120),('request_interval',0,10)):
        number=value[key]
        if type(number) not in (int,float) or not math.isfinite(number) or not low<=number<=high:raise ValueError('invalid_input')
    if type(value['workers']) is not int or type(value['catalog_auto_retry']) is not bool:raise ValueError('invalid_input')
    return dict(value)


class Engine:
    def __init__(self,store,client=None,destination=None,workers=4,clock=time.time,jitter=random.random,transfer_fn=transfer):
        self.store,self.client,self.destination = store,None,None
        self._owner = ProcessLock(store.path+'.lock')
        self._lock = threading.RLock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._cancel = threading.Event()
        self._cancel.set()
        self._active = {}
        self._threads = []
        self._catalog_thread = None
        self._catalog_cancel = threading.Event()
        self._scanning = False
        self._scan_error = ''
        self._pause_reason = 'user'
        self._running = False
        self.connected = False
        self._connecting = False
        self._previews = self.store.get_setting('previews',True)
        self._selection = self.store.get_setting('selection')
        self.store.set_active_selection(self._selection)
        self._throttle_until = 0
        self._transferred = 0
        self._samples = deque()
        self.clock,self.jitter,self.transfer_fn = clock,jitter,transfer_fn
        self.transfer_settings=transfer_settings(self.store.get_setting('transfer_settings',{**TRANSFER_DEFAULTS,'workers':max(1,min(int(workers),8))}))
        self.worker_count = self.transfer_settings['workers']
        self._catalog_retry_at=0
        self.store.recover_interrupted()
        try:
            if client:
                self.connect(client)
            if destination:
                self.set_destination(destination)
        except BaseException:
            self._owner.close()
            raise

    def configure_transfer(self,value):
        chosen=transfer_settings(value)
        with self._lock:
            self.store.set_setting('transfer_settings',chosen)
            self.transfer_settings=chosen
            self.worker_count=chosen['workers']
            if self.client:
                self.client.timeout=chosen['timeout']
                self.client.request_interval=chosen['request_interval']
            if self._threads:self._ensure_workers()
            self._wake.set()

    def connect(self,client):
        with self._lock:
            if self._active or self._scanning or self._running or self._connecting:
                raise ValueError('pause_required')
            self._connecting = True
            self.connected = False
        try:
            client.timeout=self.transfer_settings['timeout']
            client.request_interval=self.transfer_settings['request_interval']
            account = client.check_access()
            if not account:
                raise ApiError('invalid_response')
            with self._lock:
                if self._stop.is_set():
                    raise ValueError('closed')
                client.timeout=self.transfer_settings['timeout']
                client.request_interval=self.transfer_settings['request_interval']
                previous = self.store.get_setting('account')
                if previous and previous != account:
                    raise ValueError('different_account')
                self.store.set_setting('account',account)
                self.client,self.connected = client,True
        finally:
            with self._lock:
                self._connecting = False

    def set_destination(self,destination,confirm_change=False):
        with self._lock:
            if self._active or self._running:
                raise ValueError('pause_required')
            destination.check()
            previous = self.store.get_setting('destination')
            chosen = {'root':destination.root,'identity':destination.identity}
            changed=bool(previous and (previous['root'] != chosen['root'] or previous['identity']['uuid'] != chosen['identity']['uuid']))
            if changed and not confirm_change:
                # A new target must not inherit verification results from another disk.
                raise ValueError('different_destination')
            if self.destination and self.destination is not destination:
                self.destination.close()
                self.destination = None
            destination.acquire()
            self.store.switch_destination(chosen)
            self.destination = destination
            if changed:
                self._pause_reason = 'user'

    def scan_catalog(self):
        with self._lock:
            if not self.connected:
                raise ValueError('auth_required')
            if self._scanning:
                raise ValueError('scan_active')
            if self.clock() < self._throttle_until:
                raise ValueError('throttled')
            self._scanning,self._scan_error = True,''
            self._catalog_cancel.clear()
            snapshot = self.store.snapshot()
            sid = snapshot['id'] if snapshot and not snapshot['complete'] else self.store.begin_snapshot()
            self._catalog_thread = threading.Thread(target=self._scan,args=(sid,),daemon=True)
            self._catalog_thread.start()

    def _scan(self,sid):
        try:
            failures=0
            offset = self.store.snapshot(sid)['cursor']
            progress = self.store.get_setting('scan_recheck')
            if not progress or progress['snapshot_id'] != sid:
                progress = {'snapshot_id':sid,'start_count':self.store.snapshot_count(sid),'passes':0}
                self.store.set_setting('scan_recheck',progress)
            while not self._catalog_cancel.is_set() and not self._stop.is_set():
                if self._catalog_retry_at:
                    if not self.transfer_settings['catalog_auto_retry']:break
                    if self.clock()<self._catalog_retry_at:
                        self._catalog_cancel.wait(min(1,self._catalog_retry_at-self.clock()))
                        continue
                    self._catalog_retry_at=0
                if self.clock()<self._throttle_until:
                    self._catalog_cancel.wait(min(1,self._throttle_until-self.clock()))
                    continue
                try:
                    files = self.client.list_page(offset)
                except ApiError as error:
                    if not self.transfer_settings['catalog_auto_retry'] or error.code not in ('timeout','unavailable','throttled'):raise
                    failures+=1
                    with self._lock:
                        self._scan_error=error.code
                        self._catalog_retry_at=self.clock()+max(min(300,5*2**min(failures-1,6)),error.retry_after or 0)
                        if error.code=='throttled':self._throttle_until=max(self._throttle_until,self._catalog_retry_at)
                    continue
                failures=0
                self._scan_error=''
                if not files:
                    observed = self.store.snapshot_count(sid)
                    if progress['passes'] >= 1 and observed == progress['start_count']:
                        self.store.finish_snapshot(sid)
                        break
                    progress = {'snapshot_id':sid,'start_count':observed,'passes':progress['passes']+1}
                    self.store.rewind_snapshot(sid)
                    self.store.set_setting('scan_recheck',progress)
                    offset = 0
                    if progress['passes'] >= 3:
                        self._scan_error = 'catalog_changing'
                        break
                    continue
                offset += len(files)
                self.store.record_page(sid,files,offset)
        except ApiError as e:
            with self._lock:
                self._scan_error = e.code
                if e.requires_auth:
                    self.connected = False
                    self._pause('auth')
                if e.code == 'throttled':
                    self._throttle_until = self.clock()+max(5,e.retry_after or 0)
        except Exception:
            self._scan_error = 'catalog_error'
        finally:
            with self._lock:
                self._scanning = False
                self._catalog_retry_at=0

    def start(self,previews=True,file_ids=None,snapshot_id=None):
        with self._lock:
            if not self.connected:
                raise ValueError('auth_required')
            if not self.destination:
                raise ValueError('destination_required')
            if self._active:
                raise ValueError('writers_active')
            if file_ids is not None and snapshot_id is None:
                snapshot = self.store.snapshot()
                snapshot_id = snapshot['id'] if snapshot else -1
            self.store.enqueue(snapshot_id,previews,file_ids)
            self._previews = previews
            self._selection = file_ids
            self.store.set_active_selection(file_ids)
            self.store.set_setting('previews',previews)
            self.store.set_setting('selection',file_ids)
            try:
                self.destination.check()
            except DestinationError as e:
                self._pause(e.code)
                return
            self._running = True
            self._pause_reason = ''
            self._cancel.clear()
            self._ensure_workers()
            self._wake.set()

    def _ensure_workers(self):
        while len(self._threads)<self.worker_count:
            worker=threading.Thread(target=self._worker,daemon=True)
            self._threads.append(worker)
            worker.start()

    def _pause(self,reason):
        self._running = False
        self._pause_reason = reason
        self._cancel.set()
        self._wake.set()

    def pause(self):
        with self._lock:
            self._pause('user')
            self._catalog_cancel.set()

    def resume(self,confirm_destination=False):
        with self._lock:
            if self._active:
                raise ValueError('writers_active')
            if not self.connected:
                raise ValueError('auth_required')
            if not self.destination:
                raise ValueError('destination_required')
            if self._pause_reason in DISK_ERRORS and not confirm_destination:
                raise ValueError('confirmation_required')
            if confirm_destination:
                refreshed = self.destination.rebind()
                try:
                    self.set_destination(refreshed)
                except BaseException:
                    refreshed.close()
                    raise
            else:
                self.destination.check()
            self._pause_reason = ''
            self._running = True
            self._cancel.clear()
            self._ensure_workers()
            self._wake.set()

    def _bytes(self,n):
        with self._lock:
            self._transferred += n
            now = self.clock()
            self._samples.append((now,n))
            while self._samples and self._samples[0][0] < now-10:
                self._samples.popleft()

    def _worker(self):
        while not self._stop.is_set():
            job = None
            with self._lock:
                now = self.clock()
                if self._running and self.connected and now>=self._throttle_until and len(self._active)<self.worker_count:
                    job=self.store.claim_job(now,self._previews,self._selection)
                    if job:
                        self._active[job.id]=job
            if not job:
                self._wake.wait(.15)
                self._wake.clear()
                continue
            try:
                rel=self.destination.map_path(job,self.store)
                result=self.transfer_fn(replace(job,local_rel=rel),self.client,self.destination,self._cancel,self._bytes,store=self.store)
            except DestinationError as e:
                result=TransferResult('deferred',error_code=e.code)
            except Exception:
                result=TransferResult('deferred',error_code='internal_error')
            with self._lock:
                if result.status == 'deferred':
                    delay=min(300,5*2**min(job.attempts-1,6))*(1+.2*self.jitter())
                    delay=max(delay,result.retry_at)
                    if result.error_code == 'cancelled':
                        delay=0
                    result=replace(result,retry_at=self.clock()+delay)
                    if result.error_code=='throttled':
                        self._throttle_until=max(self._throttle_until,result.retry_at)
                    elif result.error_code=='auth':
                        self.connected=False
                        self._catalog_cancel.set()
                        self._pause('auth')
                    elif result.error_code in DISK_ERRORS or result.error_code=='internal_error':
                        self._pause(result.error_code)
                try:
                    self.store.complete_job(job.id,result)
                except Exception:
                    self._pause('state_error')
                finally:
                    self._active.pop(job.id,None)

    def status(self):
        with self._lock:
            data=self.store.summary()
            unfinished = self.store.unfinished(self._previews,self._selection)
            if self._running and not self._active and not unfinished and not self._scanning:
                self._pause('complete')
            free=None
            volume_error=''
            if self.destination:
                try:
                    free=self.destination.free()
                except DestinationError as e:
                    volume_error=e.code
                    if self._running:
                        self._pause(e.code)
            now=self.clock()
            samples=[(t,n) for t,n in self._samples if t>=now-10]
            data.update(connected=self.connected,scanning=self._scanning,scan_error=self._scan_error,
                        snapshot=self.store.snapshot(),running=self._running,previews=self._previews,selection_count=len(self._selection) if self._selection is not None else None,pause_reason=self._pause_reason,
                        active_jobs=len(self._active),safe_to_disconnect=not self._running and not self._active,
                        transferred_bytes=self._transferred,speed_bps=sum(n for _,n in samples)/10,
                        retry_in=max(0,self._throttle_until-now),catalog_retry_in=max(0,self._catalog_retry_at-now),
                        transfer_settings=dict(self.transfer_settings),free_bytes=free,
                        destination=self.destination.root if self.destination else None,
                        expected_destination=self.store.get_setting('destination'),volume_error=volume_error)
            return data

    def close(self):
        self.pause()
        self._stop.set()
        self._catalog_cancel.set()
        self._wake.set()
        for thread in self._threads+[self._catalog_thread]:
            if thread:
                thread.join(2)
        if any(t.is_alive() for t in self._threads+[self._catalog_thread] if t):
            return False
        # Destination's descriptor ownership belongs to the caller/server lifecycle.
        self._owner.close()
        return True
