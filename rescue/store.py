"""Short independent SQLite transactions; never share connections across threads."""
import json
import hashlib
import unicodedata
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
                CREATE TABLE IF NOT EXISTS secrets(key TEXT PRIMARY KEY,value TEXT);
                CREATE TABLE IF NOT EXISTS path_nodes(scope TEXT,kind TEXT,cloud_path TEXT,rel TEXT,key TEXT,
                    PRIMARY KEY(scope,kind,cloud_path),UNIQUE(scope,key));
                CREATE TABLE IF NOT EXISTS job_selection(file_id INTEGER PRIMARY KEY);
                CREATE TABLE IF NOT EXISTS destination_jobs(
                    scope TEXT,job_id INTEGER,status TEXT,attempts INTEGER,retry_at REAL,
                    error_code TEXT,verification TEXT,written_bytes INTEGER,
                    PRIMARY KEY(scope,job_id));
                PRAGMA user_version=3;
            ''')
        os.chmod(self.path, 0o600)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA secure_delete=ON")
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
            rows = (db.execute('SELECT * FROM files').fetchall() if snapshot_id is None else
                    db.execute('SELECT f.* FROM files f JOIN members m ON f.id=m.file_id WHERE snapshot_id=?', (snapshot_id,)).fetchall())
            if file_ids is not None:
                if not set(file_ids).issubset({r['id'] for r in rows}):
                    raise ValueError('invalid_selection')
                selected=set(file_ids)
                rows = [r for r in rows if r['id'] in selected]
            for row in rows:
                kinds = ['original']
                if previews and (row['media_type'] == 'image' or row['path'].lower().endswith(IMAGE_EXT)):
                    kinds.append('preview')
                for kind in kinds:
                    db.execute('INSERT OR IGNORE INTO jobs(file_id,kind) VALUES(?,?)', (row['id'], kind))

    def claim_job(self, now, previews=True, file_ids=None):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            sql = "SELECT j.id FROM jobs j WHERE status IN ('pending','deferred') AND retry_at<=? AND (? OR kind='original')"
            args = [now,previews]
            if file_ids is not None:
                if not file_ids:
                    return None
                sql += ' AND file_id IN (SELECT file_id FROM job_selection)'
            row = db.execute(sql+' ORDER BY attempts,id LIMIT 1',args).fetchone()
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
                 o.error_code, o.attempts, o.verification, p.error_code AS preview_error, p.attempts AS preview_attempts,
                 mo.rel AS local_original, mp.rel AS local_preview FROM files f
                 LEFT JOIN jobs o ON o.file_id=f.id AND o.kind='original'
                 LEFT JOIN jobs p ON p.file_id=f.id AND p.kind='preview'
                 LEFT JOIN mappings mo ON mo.file_id=f.id AND mo.kind='original' AND mo.scope=?
                 LEFT JOIN mappings mp ON mp.file_id=f.id AND mp.kind='preview' AND mp.scope=? WHERE 1=1'''
        destination = self.get_setting('destination')
        scope = destination['identity']['uuid'] + ':' + destination['root'] if destination else ''
        args = [scope,scope]
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
            rows = [dict(r) for r in db.execute(sql,args)]
        if destination:
            for row in rows:
                for field in ('local_original','local_preview'):
                    if row[field]:
                        row[field] = os.path.join(destination['root'],row[field])
        return rows

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
                key=unicodedata.normalize('NFC',rel).casefold()
                prefix=key+'/'
                if db.execute('SELECT 1 FROM path_nodes WHERE scope=? AND key=?',(scope,key)).fetchone():
                    continue
                if db.execute('SELECT 1 FROM mappings WHERE scope=? AND key>=? AND key<? LIMIT 1',(scope,prefix,prefix+'\U0010ffff')).fetchone():
                    continue
                try:
                    db.execute('UPDATE mappings SET rel=?,key=? WHERE scope=? AND file_id=? AND kind=?',
                               (rel,key,scope,file_id,kind))
                    return rel
                except sqlite3.IntegrityError:
                    continue
            raise ValueError('path_collision')

    def unfinished(self, previews=True, file_ids=None):
        sql = "SELECT count(*) FROM jobs WHERE status NOT IN ('verified_md5','verified_size','preview_saved','preview_absent') AND (? OR kind='original')"
        args = [previews]
        if file_ids is not None:
            if not file_ids:
                return 0
            sql += ' AND file_id IN (SELECT file_id FROM job_selection)'
        with self.connect() as db:
            return db.execute(sql,args).fetchone()[0]

    def snapshot_count(self, snapshot_id):
        with self.connect() as db:
            return db.execute('SELECT count(*) FROM members WHERE snapshot_id=?',(snapshot_id,)).fetchone()[0]

    def rewind_snapshot(self, snapshot_id):
        with self.connect() as db:
            db.execute('UPDATE snapshots SET cursor=0 WHERE id=?',(snapshot_id,))

    def set_active_selection(self, file_ids):
        with self.connect() as db:
            db.execute('DELETE FROM job_selection')
            if file_ids is not None:
                db.executemany('INSERT OR IGNORE INTO job_selection VALUES(?)',((i,) for i in file_ids))

    def save_token(self, token):
        if not isinstance(token,str) or not token.strip() or any(c in token for c in '\r\n'):
            raise ValueError('invalid_input')
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO secrets VALUES('oauth',?)",(token.strip(),))

    def saved_token(self):
        with self.connect() as db:
            row=db.execute("SELECT value FROM secrets WHERE key='oauth'").fetchone()
            return row[0] if row else None

    def forget_token(self):
        with self.connect() as db:
            db.execute("DELETE FROM secrets WHERE key='oauth'")
        with self.connect() as db:
            db.execute('PRAGMA wal_checkpoint(TRUNCATE)')

    def folders(self, snapshot_id):
        with self.connect() as db:
            rows=db.execute('SELECT f.path,f.size FROM files f JOIN members m ON m.file_id=f.id WHERE m.snapshot_id=?',(snapshot_id,)).fetchall()
        folders={'/':{'path':'/','files':0,'bytes':0}}
        for row in rows:
            parts=row['path'].strip('/').split('/')
            for n in range(len(parts)):
                path='/'+'/'.join(parts[:n])
                item=folders.setdefault(path,{'path':path,'files':0,'bytes':0})
                item['files']+=1
                item['bytes']+=row['size']
        return sorted(folders.values(),key=lambda r:r['path'])

    def folder_file_ids(self, snapshot_id, included, excluded=()):
        def valid(path):
            return isinstance(path,str) and path.startswith('/') and not any(p in ('.','..') for p in path.split('/'))
        if not included or not all(valid(p) for p in list(included)+list(excluded)):
            raise ValueError('invalid_selection')
        def matches(path,folder):
            return folder=='/' or path.startswith(folder.rstrip('/')+'/')
        with self.connect() as db:
            rows=db.execute('SELECT f.id,f.path FROM files f JOIN members m ON m.file_id=f.id WHERE m.snapshot_id=? ORDER BY f.id',(snapshot_id,)).fetchall()
        rules=sorted([(p,True) for p in included]+[(p,False) for p in excluded],key=lambda r:len(r[0]),reverse=True)
        return [r['id'] for r in rows if next((chosen for p,chosen in rules if matches(r['path'],p)),False)]

    def allocate_natural_mapping(self, scope, job, components, conflicts=None):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            existing=db.execute('SELECT rel FROM mappings WHERE scope=? AND file_id=? AND kind=?',(scope,job.file_id,job.kind)).fetchone()
            if existing:
                return existing[0]
            # Reserve app namespaces even when mapping an original first.
            for reserved in ('.rescue-previews','.yd-rescue.lock','_pending_download.csv','_pending_previews.csv'):
                db.execute('INSERT OR IGNORE INTO path_nodes VALUES(?,?,?,?,?)',(scope,'internal',reserved,reserved,reserved))
            parent='.rescue-previews' if job.kind=='preview' else ''
            cloud=[]
            for index,(raw,clean) in enumerate(components):
                cloud.append(raw)
                last=index==len(components)-1
                node='/'+'/'.join(cloud)+(('\0v'+str(job.file_id)) if last else '')
                kind=job.kind
                existing=db.execute('SELECT rel FROM path_nodes WHERE scope=? AND kind=? AND cloud_path=?',(scope,kind,node)).fetchone()
                if existing:
                    parent=existing[0]
                    continue
                stem,ext=os.path.splitext(clean)
                digest=hashlib.sha256(raw.encode()).hexdigest()[:8]
                for attempt in range(1000):
                    suffix='' if attempt==0 else '~'+digest+('-'+str(attempt) if attempt>1 else '')
                    name=stem+suffix+ext
                    rel=(parent+'/' if parent else '')+name
                    key=unicodedata.normalize('NFC',rel).casefold()
                    if db.execute('SELECT 1 FROM mappings WHERE scope=? AND key=?',(scope,key)).fetchone():
                        continue
                    prefix=key+'/'
                    if last and db.execute('SELECT 1 FROM mappings WHERE scope=? AND key>=? AND key<? LIMIT 1',(scope,prefix,prefix+'\U0010ffff')).fetchone():
                        continue
                    if conflicts and conflicts(rel,not last):
                        continue
                    try:
                        db.execute('INSERT INTO path_nodes VALUES(?,?,?,?,?)',(scope,kind,node,rel,key))
                        parent=rel
                        break
                    except sqlite3.IntegrityError:
                        continue
                else:
                    raise ValueError('path_collision')
            db.execute('INSERT INTO mappings VALUES(?,?,?,?,?)',(scope,job.file_id,job.kind,parent,unicodedata.normalize('NFC',parent).casefold()))
            return parent

    def has_saved_token(self):
        with self.connect() as db:
            return db.execute("SELECT 1 FROM secrets WHERE key='oauth'").fetchone() is not None

    def switch_destination(self, chosen):
        """Atomically switch queue results; files/mappings in other roots stay intact."""
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute("SELECT value FROM settings WHERE key='destination'").fetchone()
            previous=json.loads(row[0]) if row else None
            def scope(value):return value['identity']['uuid']+':'+value['root']
            if previous and scope(previous)!=scope(chosen):
                db.execute('INSERT OR REPLACE INTO destination_jobs SELECT ?,id,status,attempts,retry_at,error_code,verification,written_bytes FROM jobs',(scope(previous),))
                db.execute("UPDATE jobs SET status='pending',attempts=0,retry_at=0,error_code='',verification='',written_bytes=0")
                saved=db.execute('SELECT status,attempts,retry_at,error_code,verification,written_bytes,job_id FROM destination_jobs WHERE scope=?',(scope(chosen),)).fetchall()
                db.executemany('UPDATE jobs SET status=?,attempts=?,retry_at=?,error_code=?,verification=?,written_bytes=? WHERE id=?',saved)
            db.execute("INSERT OR REPLACE INTO settings VALUES('destination',?)",(json.dumps(chosen),))
