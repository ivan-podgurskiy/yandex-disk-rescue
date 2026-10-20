"""Destination capabilities: all writes are relative to held no-follow descriptors."""
import ctypes
import errno
import sys
import hashlib
import os
import plistlib
import re
import shutil
import stat
import subprocess
import threading
import time
import unicodedata
import uuid

RESERVE = 1073741824
DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


class DestinationError(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


class VolumeProbe:
    """diskutil establishes UUID; inode/device checks protect every write cheaply."""
    def __init__(self):
        self.cached = None
        self.checked = 0
        self.key = None

    def __call__(self, root):
        st = os.stat(root, follow_symlinks=False)
        key = (st.st_dev, st.st_ino)
        if self.cached and key == self.key and time.monotonic() - self.checked < 5:
            return self.cached
        try:
            usage = subprocess.run(['/bin/df','-P',root], check=True,capture_output=True,timeout=5).stdout.decode('utf-8')
            devices = [line.split()[0] for line in usage.splitlines() if line.startswith('/dev/disk')]
            if len(devices) != 1 or not re.fullmatch(r'/dev/disk[0-9]+(?:s[0-9]+)*',devices[0]):
                raise ValueError('unidentified device')
            raw = subprocess.run(['/usr/sbin/diskutil','info','-plist',devices[0]], check=True,
                                 capture_output=True, timeout=5).stdout
            data = plistlib.loads(raw)
            if not data.get('VolumeUUID') or not data.get('MountPoint'):
                raise ValueError('unidentified volume')
            self.cached = {'uuid':data['VolumeUUID'],'mount':data['MountPoint'],'device':st.st_dev}
            self.key, self.checked = key, time.monotonic()
            return self.cached
        except (OSError, ValueError, UnicodeError, subprocess.SubprocessError, plistlib.InvalidFileException):
            raise DestinationError('missing_volume') from None


class Destination:
    @classmethod
    def bind(cls, root, identify=None, capacity=None):
        if not os.path.isabs(root) or os.path.islink(root) or not os.path.isdir(root):
            raise DestinationError('missing_volume')
        return cls(os.path.realpath(root), identify or VolumeProbe(), capacity or (lambda p: shutil.disk_usage(p).free))

    def __init__(self, root, identify, capacity):
        self.root, self._identify, self._capacity = root, identify, capacity
        self.identity = identify(root)
        self._fd = os.open(root, DIR_FLAGS)
        self._stat = os.fstat(self._fd)
        self.scope = self.identity['uuid'] + ':' + root
        self._reservations = {}
        self._lock = threading.RLock()
        self._ownership = None

    def rebind(self):
        return type(self).bind(self.root,identify=self._identify,capacity=self._capacity)

    def close(self):
        if self._ownership is not None:
            os.close(self._ownership)
            self._ownership = None
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None

    def check(self):
        try:
            st = os.stat(self.root, follow_symlinks=False)
            if (st.st_dev,st.st_ino) != (self._stat.st_dev,self._stat.st_ino) or self._identify(self.root) != self.identity:
                raise DestinationError('wrong_volume')
            if not stat.S_ISDIR(st.st_mode):
                raise DestinationError('unsafe_path')
        except OSError:
            raise DestinationError('missing_volume') from None

    def free(self):
        self.check()
        return self._capacity(self.root)

    def reserve(self, job_id, size):
        with self._lock:
            free = self.free()
            other = sum(v for k,v in self._reservations.items() if k != job_id)
            if free - other - size < RESERVE:
                raise DestinationError('no_space')
            self._reservations[job_id] = size

    def consume(self, job_id, count):
        with self._lock:
            self._reservations[job_id] = max(0,self._reservations.get(job_id,0) - count)

    def check_space(self):
        with self._lock:
            if self.free() < RESERVE + sum(self._reservations.values()):
                raise DestinationError('no_space')

    def release(self, job_id):
        with self._lock:
            self._reservations.pop(job_id, None)

    def acquire(self):
        import fcntl
        self.check()
        if self._ownership is not None:
            return
        fd = os.open('.yd-rescue.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600, dir_fd=self._fd)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            raise DestinationError('destination_busy') from None
        self._ownership = fd

    def map_path(self, job, store):
        self.check()
        existing = store.mapping(self.scope,job.file_id,job.kind)
        if existing:
            return existing
        components=[]
        for raw in job.file.path.split('/'):
            if not raw:
                continue
            if raw in ('.','..') or '\x00' in raw:
                raise DestinationError('unsafe_path')
            clean=''.join('_' if c in '<>:"|?*\\' or ord(c)<32 else c for c in unicodedata.normalize('NFC',raw)).rstrip(' .') or '_'
            while len(clean.encode('utf-8'))>220:
                clean=clean[:-1]
            if clean.split('.')[0].upper() in {'CON','PRN','AUX','NUL'} | {'COM'+str(i) for i in range(1,10)} | {'LPT'+str(i) for i in range(1,10)}:
                clean='_'+clean
            components.append((raw,clean))
        if not components:
            raise DestinationError('unsafe_path')
        return store.allocate_natural_mapping(self.scope,job,components,self._path_conflict)

    def _path_conflict(self,rel,directory):
        try:
            fd,name=self._parent(rel)
        except FileNotFoundError:
            return False
        try:
            try:
                found=os.stat(name,dir_fd=fd,follow_symlinks=False)
            except FileNotFoundError:
                return False
            return not (stat.S_ISDIR(found.st_mode) if directory else stat.S_ISREG(found.st_mode))
        finally:
            os.close(fd)

    def _parent(self, rel, create=False):
        self.check()
        parts = rel.split('/')
        if any(p in ('','.','..') or '\x00' in p for p in parts):
            raise DestinationError('unsafe_path')
        fd = os.dup(self._fd)
        try:
            for part in parts[:-1]:
                if create:
                    try:
                        os.mkdir(part,0o700,dir_fd=fd)
                    except FileExistsError:
                        pass
                child = os.open(part, DIR_FLAGS, dir_fd=fd)
                os.close(fd)
                fd = child
            return fd, parts[-1]
        except OSError as e:
            os.close(fd)
            if e.errno == errno.ENOENT and not create:
                raise FileNotFoundError() from None
            raise DestinationError('unsafe_path') from None

    def open_existing(self, rel):
        fd,name = self._parent(rel)
        try:
            file_fd = os.open(name,os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,dir_fd=fd)
            if not stat.S_ISREG(os.fstat(file_fd).st_mode):
                os.close(file_fd)
                raise DestinationError('unsafe_path')
            return os.fdopen(file_fd,'rb')
        except OSError as e:
            if e.errno == errno.ENOENT:
                raise FileNotFoundError() from None
            raise DestinationError('unsafe_path') from None
        finally:
            os.close(fd)

    def open_attempt(self, job, rel):
        fd,name = self._parent(rel,create=True)
        try:
            return Attempt(self,fd,name,job.id,rel)
        except BaseException:
            os.close(fd)
            raise


    def write_report(self,name,payload):
        if name not in ('_pending_download.csv','_pending_previews.csv'):
            raise DestinationError('unsafe_path')
        self.reserve(-1,len(payload))
        attempt = None
        try:
            fd,leaf = self._parent(name)
            attempt = Attempt(self,fd,leaf,-1,name)
            attempt.check()
            attempt.writer.write(payload)
            attempt.writer.flush()
            os.fsync(attempt.writer.fileno())
            self.consume(-1,len(payload))
            attempt.check()
            attempt.writer.close()
            os.replace(attempt.temp,leaf,src_dir_fd=fd,dst_dir_fd=fd)
        finally:
            if attempt:
                attempt.discard()
            self.release(-1)


def publish_noreplace(fd, source, target):
    if sys.platform == 'darwin':
        # macOS RENAME_EXCL is atomic and works on volumes without hard links (exFAT).
        libc = ctypes.CDLL(None,use_errno=True)
        rename = libc.renameatx_np
        rename.argtypes = [ctypes.c_int,ctypes.c_char_p,ctypes.c_int,ctypes.c_char_p,ctypes.c_uint]
        rename.restype = ctypes.c_int
        if rename(fd,os.fsencode(source),fd,os.fsencode(target),4):
            code = ctypes.get_errno()
            raise OSError(code,os.strerror(code))
    else:
        os.link(source,target,src_dir_fd=fd,dst_dir_fd=fd,follow_symlinks=False)
        os.unlink(source,dir_fd=fd)


class Attempt:
    def __init__(self,destination,fd,name,job_id,rel):
        self.rel = rel
        self.cancel = None
        self.destination,self.fd,self.name,self.job_id = destination,fd,name,job_id
        self.temp = '.yd-' + uuid.uuid4().hex + '.part'
        out = os.open(self.temp,os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,0o600,dir_fd=fd)
        self.writer = os.fdopen(out,'wb')

    def check(self):
        if self.cancel is not None and self.cancel.is_set():
            raise DestinationError('cancelled')
        self.destination.check_space()
        # Reopen the current no-follow hierarchy and compare the held parent.
        # A moved/replaced child must not receive any more data or publication.
        try:
            current, _ = self.destination._parent(self.rel)
        except (FileNotFoundError,DestinationError):
            raise DestinationError('unsafe_path') from None
        try:
            before,now = os.fstat(self.fd),os.fstat(current)
            if (before.st_dev,before.st_ino) != (now.st_dev,now.st_ino):
                raise DestinationError('unsafe_path')
        finally:
            os.close(current)

    def commit(self):
        self.check()
        self.writer.flush()
        os.fsync(self.writer.fileno())
        self.writer.close()
        try:
            publish_noreplace(self.fd,self.temp,self.name)
        except OSError as error:
            if error.errno not in (errno.ENOTSUP,errno.EOPNOTSUPP):
                raise
            self._copy_exclusive()
        try:
            os.fsync(self.fd)
        except OSError as error:
            if error.errno not in (errno.EINVAL,errno.ENOTSUP):
                raise
        os.close(self.fd)
        self.fd = None

    def _copy_exclusive(self):
        """ExFAT may lack both exclusive rename and hard links.

        Create the final name exclusively, then copy the already verified temp.
        Completion is recorded only after fsync. A crash can leave an incomplete
        named file, which is reverified/preserved on retry, never blindly reused.
        """
        source = os.open(self.temp,os.O_RDONLY | os.O_NOFOLLOW,dir_fd=self.fd)
        target = None
        owned = None
        try:
            self.destination.reserve(self.job_id,os.fstat(source).st_size)
            self.check()
            target = os.open(self.name,os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,0o600,dir_fd=self.fd)
            owned = os.fstat(target)
            while True:
                self.check()
                data = os.read(source,1024*1024)
                if not data:
                    break
                remaining = memoryview(data)
                while remaining:
                    self.check()
                    n = os.write(target,remaining)
                    if n <= 0:
                        raise OSError(errno.EIO,'short copy write')
                    remaining = remaining[n:]
                    self.destination.consume(self.job_id,n)
            self.check()
            os.fsync(target)
            self.check()
            os.close(target)
            target = None
            os.unlink(self.temp,dir_fd=self.fd)
        except BaseException:
            if owned is not None:
                try:
                    current = os.stat(self.name,dir_fd=self.fd,follow_symlinks=False)
                    if (current.st_dev,current.st_ino)==(owned.st_dev,owned.st_ino):
                        os.unlink(self.name,dir_fd=self.fd)
                except OSError:
                    pass
            raise
        finally:
            if target is not None:
                os.close(target)
            os.close(source)
            self.destination.release(self.job_id)

    def discard(self):
        if self.fd is None:
            return
        self.writer.close()
        try:
            os.unlink(self.temp,dir_fd=self.fd)
        except FileNotFoundError:
            pass
        finally:
            os.close(self.fd)
            self.fd = None
