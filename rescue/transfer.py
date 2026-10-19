"""Stream, verify, and publish one copy; scheduling belongs to Engine."""
import errno
import hashlib
import os
import socket
from .api import ApiError
from .destination import DestinationError
from .models import TransferResult

CHUNK = 1024 * 1024
MAX_PREVIEW = 64 * CHUNK


def image_bytes(data):
    return (data.startswith((b'\xff\xd8\xff',b'\x89PNG\r\n\x1a\n',b'GIF87a',b'GIF89a',b'BM',b'II*\x00',b'MM\x00*'))
            or (data.startswith(b'RIFF') and data[8:12] == b'WEBP'))


def verify_existing(destination, rel, file, cancel=None):
    try:
        with destination.open_existing(rel) as src:
            if os.fstat(src.fileno()).st_size != file.size:
                return 'conflict'
            if not file.md5:
                return 'verified_size'
            h = hashlib.md5()
            while True:
                destination.check()
                if cancel and cancel.is_set():
                    raise DestinationError('cancelled')
                chunk = src.read(CHUNK)
                if not chunk:
                    break
                h.update(chunk)
            return 'verified_md5' if h.hexdigest() == file.md5 else 'conflict'
    except FileNotFoundError:
        return 'missing'


def transfer(job, client, destination, cancel, on_bytes, store=None):
    attempt = None
    written = 0
    rel = job.local_rel
    try:
        destination.check()
        if cancel.is_set():
            return TransferResult('deferred', error_code='cancelled')
        # Re-verify copied originals before reusing them. A conflict gets another name.
        for _ in range(100):
            if job.kind == 'original':
                existing = verify_existing(destination,rel,job.file,cancel)
            else:
                try:
                    with destination.open_existing(rel) as src:
                        existing = 'preview_saved' if image_bytes(src.read(32)) else 'conflict'
                except FileNotFoundError:
                    existing = 'missing'
            if existing == 'conflict':
                if store is None:
                    return TransferResult('conflict',error_code='local_conflict')
                rel = store.relocate_mapping(destination.scope,job.file_id,job.kind)
                continue
            if existing != 'missing':
                return TransferResult(existing,verification='md5' if existing == 'verified_md5' else 'size' if existing == 'verified_size' else '')
            break
        else:
            return TransferResult('conflict',error_code='local_conflict')
        preview = job.kind == 'preview'
        url = client.preview_url(job.file) if preview else client.original_url(job.file)
        if preview and url is None:
            return TransferResult('preview_absent')
        destination.reserve(job.id, MAX_PREVIEW if preview else job.file.size)
        attempt = destination.open_attempt(job,rel)
        h = hashlib.md5()
        first = b''
        with client.open_stream(url,authenticated=preview) as src:
            while True:
                if cancel.is_set():
                    return TransferResult('deferred',written,error_code='cancelled')
                attempt.check()
                try:
                    chunk = src.read(CHUNK)
                except (TimeoutError,socket.timeout):
                    raise ApiError('timeout') from None
                except OSError:
                    raise ApiError('network_io') from None
                if not chunk:
                    break
                # Recheck after network read: the volume may disappear while blocked.
                attempt.check()
                if not first:
                    first = chunk[:32]
                written += len(chunk)
                if written > (MAX_PREVIEW if preview else job.file.size):
                    return TransferResult('deferred',written,error_code='size_mismatch')
                attempt.writer.write(chunk)
                attempt.writer.flush()
                destination.consume(job.id,len(chunk))
                h.update(chunk)
                on_bytes(len(chunk))
        if cancel.is_set():
            return TransferResult('deferred',written,error_code='cancelled')
        if preview:
            if not written or not image_bytes(first):
                return TransferResult('deferred',written,error_code='invalid_preview')
            result = TransferResult('preview_saved',written)
        else:
            if written != job.file.size:
                return TransferResult('deferred',written,error_code='size_mismatch')
            if job.file.md5 and h.hexdigest() != job.file.md5:
                return TransferResult('deferred',written,error_code='checksum_mismatch')
            result = TransferResult('verified_md5' if job.file.md5 else 'verified_size',written,
                                    verification='md5' if job.file.md5 else 'size')
        attempt.commit()
        return result
    except (ApiError,DestinationError) as e:
        return TransferResult('deferred',written,error_code=e.code,
                              retry_at=e.retry_after or 0 if isinstance(e,ApiError) else 0)
    except FileExistsError:
        return TransferResult('deferred',written,error_code='local_conflict')
    except OSError as e:
        return TransferResult('deferred',written,error_code='no_space' if e.errno == errno.ENOSPC else 'disk_io')
    finally:
        if attempt:
            try:
                attempt.discard()
            except OSError:
                pass  # Detached/unreachable destination; never substitute another path.
        destination.release(job.id)
