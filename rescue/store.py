"""Short independent SQLite transactions; never share connections across threads."""
import json
import os
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import asdict

from .models import CloudFile, Job, IMAGE_EXT


class Store:
    def __init__(self, db_path):
        self.path = os.path.abspath(db_path)
        with self.connect() as db:
            db.executescript('''
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS snapshots(
                    id INTEGER PRIMARY KEY, created REAL, complete INTEGER DEFAULT 0,
                    cursor INTEGER DEFAULT 0);
                CREATE TABLE IF NOT EXISTS files(
                    id INTEGER PRIMARY KEY, path TEXT, size INTEGER, md5 TEXT,
                    media_type TEXT, resource_id TEXT,
                    UNIQUE(path,size,md5,resource_id));
                CREATE TABLE IF NOT EXISTS members(
                    snapshot_id INTEGER, file_id INTEGER,
                    PRIMARY KEY(snapshot_id,file_id));
                CREATE TABLE IF NOT EXISTS jobs(
                    id INTEGER PRIMARY KEY, file_id INTEGER, kind TEXT,
                    status TEXT DEFAULT 'pending', attempts INTEGER DEFAULT 0,
                    retry_at REAL DEFAULT 0, error_code TEXT DEFAULT '',
                    verification TEXT DEFAULT '', written_bytes INTEGER DEFAULT 0,
                    UNIQUE(file_id,kind));
                CREATE TABLE IF NOT EXISTS mappings(
                    scope TEXT, file_id INTEGER, kind TEXT, rel TEXT, key TEXT,
                    PRIMARY KEY(scope,file_id,kind), UNIQUE(scope,key));
                CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
                PRAGMA user_version=1;
            ''')
        os.chmod(self.path, 0o600)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def begin_snapshot(self):
        with self.connect() as db:
            return db.execute('INSERT INTO snapshots(created) VALUES(?)', (time.time(),)).lastrowid

    def record_page(self, snapshot_id, files, next_offset):
        with self.connect() as db:
            for f in files:
                values = tuple(asdict(f).values())
                db.execute('INSERT OR IGNORE INTO files(path,size,md5,media_type,resource_id) VALUES(?,?,?,?,?)', values)
                fid = db.execute('SELECT id FROM files WHERE path=? AND size=? AND md5=? AND resource_id=?',
                                 (f.path, f.size, f.md5, f.resource_id)).fetchone()[0]
                db.execute('INSERT OR IGNORE INTO members VALUES(?,?)', (snapshot_id, fid))
            db.execute('UPDATE snapshots SET cursor=? WHERE id=?', (next_offset, snapshot_id))

    def finish_snapshot(self, snapshot_id):
        with self.connect() as db:
            db.execute('UPDATE snapshots SET complete=1 WHERE id=?', (snapshot_id,))

    def snapshot(self, snapshot_id=None):
        with self.connect() as db:
            row = db.execute('SELECT * FROM snapshots ORDER BY id DESC LIMIT 1' if snapshot_id is None
                             else 'SELECT * FROM snapshots WHERE id=?', () if snapshot_id is None else (snapshot_id,)).fetchone()
            return dict(row) if row else None

    def enqueue(self, snapshot_id, previews, file_ids=None):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            rows = db.execute('SELECT f.* FROM files f JOIN members m ON f.id=m.file_id WHERE snapshot_id=?', (snapshot_id,)).fetchall()
            if file_ids is not None:
                if not set(file_ids).issubset({r['id'] for r in rows}):
                    raise ValueError('invalid_selection')
                rows = [r for r in rows if r['id'] in file_ids]
            for row in rows:
                kinds = ['original']
                if previews and (row['media_type'] == 'image' or row['path'].lower().endswith(IMAGE_EXT)):
                    kinds.append('preview')
                for kind in kinds:
                    db.execute('INSERT OR IGNORE INTO jobs(file_id,kind) VALUES(?,?)', (row['id'], kind))

    def claim_job(self, now, previews=True):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute("SELECT j.id FROM jobs j WHERE status IN ('pending','deferred') AND retry_at<=? "
                             "AND (? OR kind='original') ORDER BY attempts,id LIMIT 1", (now, previews)).fetchone()
            if not row:
                return None
            db.execute("UPDATE jobs SET status='active', attempts=attempts+1 WHERE id=?", (row[0],))
            return self._job(db, row[0])

    def _job(self, db, job_id):
        row = db.execute('SELECT j.*, f.path,f.size,f.md5,f.media_type,f.resource_id FROM jobs j JOIN files f ON f.id=j.file_id WHERE j.id=?', (job_id,)).fetchone()
        return Job(row['id'], row['file_id'], CloudFile(*(row[k] for k in ('path','size','md5','media_type','resource_id'))),
                   row['kind'], '', row['attempts'])

    def complete_job(self, job_id, result):
        with self.connect() as db:
            db.execute('UPDATE jobs SET status=?,written_bytes=?,verification=?,error_code=?,retry_at=? WHERE id=?',
                       (result.status, result.written_bytes, result.verification, result.error_code, result.retry_at, job_id))

    def recover_interrupted(self):
        with self.connect() as db:
            db.execute("UPDATE jobs SET status='pending' WHERE status='active'")

    def mapping(self, scope, file_id, kind):
        with self.connect() as db:
            row = db.execute('SELECT rel FROM mappings WHERE scope=? AND file_id=? AND kind=?', (scope,file_id,kind)).fetchone()
            return row[0] if row else None

    def allocate_mapping(self, scope, file_id, kind, candidates):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            for rel, key in candidates:
                try:
                    db.execute('INSERT INTO mappings VALUES(?,?,?,?,?)', (scope,file_id,kind,rel,key))
                    return rel
                except sqlite3.IntegrityError:
                    existing = db.execute('SELECT rel FROM mappings WHERE scope=? AND file_id=? AND kind=?', (scope,file_id,kind)).fetchone()
                    if existing:
                        return existing[0]
            raise ValueError('path_collision')

    def set_setting(self, key, value):
        with self.connect() as db:
            db.execute('INSERT OR REPLACE INTO settings VALUES(?,?)', (key,json.dumps(value)))

    def get_setting(self, key, default=None):
        with self.connect() as db:
            row = db.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()
            return json.loads(row[0]) if row else default

    def files(self, snapshot_id=None, offset=0, limit=200, query='', state=''):
        sql = '''SELECT f.*, o.status AS original_status, p.status AS preview_status,
                 o.error_code, o.attempts, o.verification FROM files f
                 LEFT JOIN jobs o ON o.file_id=f.id AND o.kind='original'
                 LEFT JOIN jobs p ON p.file_id=f.id AND p.kind='preview' WHERE 1=1'''
        args = []
        if snapshot_id is not None:
            sql += ' AND f.id IN (SELECT file_id FROM members WHERE snapshot_id=?)'
            args.append(snapshot_id)
        if query:
            sql += ' AND instr(lower(f.path),lower(?))>0'
            args.append(query)
        if state:
            sql += ' AND (o.status=? OR p.status=?)'
            args.extend([state,state])
        sql += ' ORDER BY f.id LIMIT ? OFFSET ?'
        args.extend([limit,offset])
        with self.connect() as db:
            return [dict(r) for r in db.execute(sql,args)]

    def summary(self):
        with self.connect() as db:
            counts = {'original': {}, 'preview': {}}
            for row in db.execute('SELECT kind,status,count(*) n FROM jobs GROUP BY kind,status'):
                counts[row['kind']][row['status']] = row['n']
            total = db.execute('SELECT count(*),coalesce(sum(size),0) FROM files').fetchone()
            verified = db.execute("SELECT coalesce(sum(f.size),0) FROM jobs j JOIN files f ON j.file_id=f.id WHERE j.kind='original' AND j.status IN ('verified_md5','verified_size')").fetchone()[0]
            return {'counts':counts,'catalog_files':total[0],'catalog_bytes':total[1], 'verified_bytes':verified}

    def relocate_mapping(self, scope, file_id, kind):
        """Persist a fresh conflict name before writing; never overwrite the old copy."""
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            current = db.execute('SELECT rel FROM mappings WHERE scope=? AND file_id=? AND kind=?', (scope,file_id,kind)).fetchone()[0]
            stem,ext = os.path.splitext(current)
            for i in range(1,1000):
                rel = stem + '-recovered-' + str(i) + ext
                try:
                    db.execute('UPDATE mappings SET rel=?,key=? WHERE scope=? AND file_id=? AND kind=?',
                               (rel,rel.casefold(),scope,file_id,kind))
                    return rel
                except sqlite3.IntegrityError:
                    continue
            raise ValueError('path_collision')
