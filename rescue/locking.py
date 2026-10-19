"""Kernel-owned lock released on process exit, never by stale-file deletion."""
import fcntl
import os


class ProcessLock:
    def __init__(self,path):
        self.fd = os.open(path,os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW,0o600)
        try:
            fcntl.flock(self.fd,fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(self.fd)
            self.fd = None
            raise ValueError('application_busy') from None

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
