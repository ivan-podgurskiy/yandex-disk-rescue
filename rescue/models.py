from dataclasses import dataclass


@dataclass(frozen=True)
class CloudFile:
    path: str
    size: int
    md5: str = ''
    media_type: str = ''
    resource_id: str = ''


@dataclass(frozen=True)
class Job:
    id: int
    file_id: int
    file: CloudFile
    kind: str
    local_rel: str
    attempts: int


@dataclass(frozen=True)
class TransferResult:
    status: str
    written_bytes: int = 0
    verification: str = ''
    error_code: str = ''
    retry_at: float = 0


IMAGE_EXT = ('.jpg', '.jpeg', '.png', '.heic', '.heif', '.gif', '.bmp', '.tif',
             '.tiff', '.webp', '.cr2', '.nef', '.dng', '.arw', '.orf', '.rw2')
FINISHED = ('verified_md5', 'verified_size', 'preview_saved', 'preview_absent')
