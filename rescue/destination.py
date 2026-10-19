"""Destination capabilities: all writes are relative to held no-follow descriptors."""
import ctypes
import errno
import sys
import hashlib
import os
import plistlib
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
            raw = subprocess.run(['/usr/sbin/diskutil','info','-plist',root], check=True,
                                 capture_output=True, timeout=5).stdout
            data = plistlib.loads(raw)
            if not data.get('VolumeUUID') or not data.get('MountPoint'):
                raise ValueError('unidentified volume')
            self.cached = {'uuid':data['VolumeUUID'],'mount':data['MountPoint'],'device':st.st_dev}
            self.key, self.checked = key, time.monotonic()
            return self.cached
        except (OSError, ValueError, subprocess.SubprocessError, plistlib.InvalidFileException):
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
        existing = store.mapping(self.scope,job.file_id,job.kind)
        if existing:
            return existing
        # Hash every raw component, including directories: file/directory and
        # normalization collisions cannot alias even on case-insensitive volumes.
        components = []
        for raw in job.file.path.split('/'):
            if not raw:
                continue
            if raw in ('.','..') or '\x00' in raw:
                raise DestinationError('unsafe_path')
            clean = ''.join('_' if c in '<>:"|?*\\' or ord(c)<32 else c for c in unicodedata.normalize('NFC',raw)).rstrip(' .') or '_'
            while len(clean.encode('utf-8')) > 140:
                clean = clean[:-1]
            stem,ext = os.path.splitext(clean)
            components.append(stem + '~' + hashlib.sha256(raw.encode()).hexdigest()[:12] + ext)
        prefix = 'originals' if job.kind == 'original' else 'previews'
        base = '/'.join([prefix] + components)
        if not components:
            raise DestinationError('unsafe_path')
        stem,ext = os.path.splitext(base)
        def candidates():
            for i in range(100):
                # file version suffix prevents changed contents from replacing old copy.
                rel = stem + '~v' + str(job.file_id) + (('-'+str(i)) if i else '') + ext
                yield rel, unicodedata.normalize('NFC',rel).casefold()
        return store.allocate_mapping(self.scope,job.file_id,job.kind,candidates())

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
            return Attempt(self,fd,name,job.id)
        except BaseException:
            os.close(fd)
            raise



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
    def __init__(self,destination,fd,name,job_id):
        self.destination,self.fd,self.name,self.job_id = destination,fd,name,job_id
        self.temp = '.yd-' + uuid.uuid4().hex + '.part'
        out = os.open(self.temp,os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,0o600,dir_fd=fd)
        self.writer = os.fdopen(out,'wb')

    def check(self):
        self.destination.check_space()
        # Verify this descriptor still belongs to the current root hierarchy.
        # Even if a directory is swapped, open descriptors cannot redirect writes.
        self.destination.check()

    def commit(self):
        self.check()
        self.writer.flush()
        os.fsync(self.writer.fileno())
        self.writer.close()
        publish_noreplace(self.fd,self.temp,self.name)
        try:
            os.fsync(self.fd)
        except OSError as error:
            if error.errno not in (errno.EINVAL,errno.ENOTSUP):
                raise
        os.close(self.fd)
        self.fd = None

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
