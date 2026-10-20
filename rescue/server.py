"""Small loopback-only HTTP surface; all recovery work is owned by Engine."""
import csv
import hmac
import io
import json
import os
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs,urlsplit

from .api import YandexClient,ApiError
from .destination import Destination,DestinationError

WEB=Path(__file__).parent/'web'
SAFE_ERRORS={'auth','invalid_response','unavailable','timeout','throttled','missing_volume','wrong_volume',
             'unsafe_path','no_space','disk_io','application_busy','destination_busy','different_account',
             'different_destination','pause_required','writers_active','auth_required','destination_required',
             'confirmation_required','scan_active','invalid_selection'}


def error_code(error):
    value=getattr(error,'code',str(error))
    return value if value in SAFE_ERRORS else 'operation_failed'


class Application:
    def __init__(self,engine):
        self.engine=engine
        self.secret=secrets.token_urlsafe(32)
        self._lock=threading.Lock()
        self.operation={'kind':'','state':'idle','error':''}
        self.thread=None
        self.restoration_error=''
        if engine.store.has_saved_token() or (engine.destination is None and engine.store.get_setting('destination')):
            self.submit('restore',self.restore)

    def restore(self):
        engine=self.engine
        saved=engine.store.get_setting('destination')
        if saved and engine.destination is None:
            destination=None
            try:
                destination=Destination.bind(saved['root'])
                engine.set_destination(destination)
            except Exception as error:
                if destination:destination.close()
                self.restoration_error=error_code(error)
        token=engine.store.saved_token()
        if token:
            engine.connect(YandexClient(token))

    def submit(self,kind,fn):
        with self._lock:
            if self.thread and self.thread.is_alive():
                raise ValueError('operation_busy')
            self.operation={'kind':kind,'state':'running','error':''}
            def run():
                try:
                    fn()
                    result={'kind':kind,'state':'done','error':''}
                except Exception as error:
                    result={'kind':kind,'state':'failed','error':error_code(error)}
                with self._lock:
                    self.operation=result
            self.thread=threading.Thread(target=run,daemon=True)
            self.thread.start()

    def status(self):
        data=self.engine.status()
        data['token_saved']=self.engine.store.has_saved_token()
        data['restoration_error']=self.restoration_error
        data['folder_selection']=self.engine.store.get_setting('folder_selection')
        with self._lock:
            data['operation']=dict(self.operation)
        return data

    def command(self,path,data):
        engine=self.engine
        if path=='/api/connect':
            token=data.get('token')
            remember=data.get('remember',False)
            if not isinstance(remember,bool):raise TypeError('invalid_input')
            if token is not None and not isinstance(token,str):
                raise TypeError('invalid_input')
            def connect():
                selected=token
                if not selected:
                    selected=os.environ.get('YADISK_TOKEN','')
                    if not selected:
                        filename=os.environ.get('YADISK_TOKEN_FILE','token.txt')
                        try:
                            with open(filename,encoding='utf-8') as src:
                                selected=src.read(16384).strip()
                        except OSError:
                            raise ApiError('auth') from None
                engine.connect(YandexClient(selected))
                if remember:
                    engine.store.save_token(selected)
                else:
                    engine.store.forget_token()
            self.submit('connect',connect)
        elif path=='/api/forget-token':
            engine.store.forget_token()
            return 200
        elif path=='/api/catalog':
            self.submit('catalog',engine.scan_catalog)
        elif path=='/api/destination':
            root=data.get('path')
            if not isinstance(root,str) or not os.path.isabs(root):
                raise TypeError('invalid_input')
            def select():
                destination=Destination.bind(root)
                try:
                    engine.set_destination(destination)
                    self.restoration_error=''
                except BaseException:
                    destination.close()
                    raise
            self.submit('destination',select)
        elif path=='/api/start':
            previews=data.get('previews',True)
            ids=data.get('file_ids')
            sid=data.get('snapshot_id')
            folders=data.get('folders')
            excluded=data.get('excluded_folders',[])
            if folders is not None:
                if ids is not None or not isinstance(folders,list) or not isinstance(excluded,list) or not all(isinstance(p,str) for p in folders+excluded):
                    raise TypeError('invalid_input')
                if sid is None:
                    snap=engine.store.snapshot()
                    sid=snap['id'] if snap else -1
            if (not isinstance(previews,bool) or
                (ids is not None and (not isinstance(ids,list) or not all(type(i) is int and i>0 for i in ids))) or
                (sid is not None and (type(sid) is not int or sid<1))):
                raise TypeError('invalid_input')
            def start():
                chosen=engine.store.folder_file_ids(sid,folders,excluded) if folders is not None else ids
                if folders is not None and not chosen:raise ValueError('invalid_selection')
                engine.start(previews,chosen,sid)
                engine.store.set_setting('folder_selection',{'snapshot_id':sid,'included':folders,'excluded':excluded} if folders is not None else None)
            self.submit('start',start)
        elif path=='/api/resume':
            confirm=data.get('confirm_destination',False)
            if not isinstance(confirm,bool):
                raise TypeError('invalid_input')
            self.submit('resume',lambda:engine.resume(confirm))
        elif path=='/api/pause':
            engine.pause()
            return 200
        else:
            return 404
        return 202


class LocalServer(ThreadingHTTPServer):
    daemon_threads=True
    allow_reuse_address=True


def csv_cell(value):
    text=str(value if value is not None else '')
    return "'"+text if text.lstrip().startswith(('=','+','-','@','\t','\r','\n')) else text


def create_server(engine,data_dir,port=8765):
    app=Application(engine)
    class Handler(BaseHTTPRequestHandler):
        server_version='LocalRecovery'
        def log_message(self,*args):
            pass

        def reply(self,status,body,content_type='application/json; charset=utf-8'):
            if isinstance(body,dict):
                body=json.dumps(body,ensure_ascii=False).encode()
            if isinstance(body,str):
                body=body.encode()
            self.send_response(status)
            self.send_header('Content-Type',content_type)
            self.send_header('Content-Length',str(len(body)))
            self.send_header('Cache-Control','no-store')
            self.send_header('X-Content-Type-Options','nosniff')
            self.send_header('X-Frame-Options','DENY')
            self.send_header('Referrer-Policy','no-referrer')
            self.send_header('Content-Security-Policy',"default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError,ConnectionResetError):
                pass

        def secure(self,authenticated):
            host=self.headers.get('Host','')
            valid={'127.0.0.1:'+str(self.server.server_port),'localhost:'+str(self.server.server_port)}
            origin=self.headers.get('Origin')
            if (host not in valid or (origin and origin!='http://'+host)
                or self.headers.get('Sec-Fetch-Site')=='cross-site'):
                self.reply(403,{'error':'forbidden'})
                return False
            if authenticated and not hmac.compare_digest(self.headers.get('X-Session-Secret',''),app.secret):
                self.reply(403,{'error':'forbidden'})
                return False
            return True

        def do_GET(self):
            u=urlsplit(self.path)
            if not self.secure(u.path.startswith('/api/') and u.path!='/api/bootstrap'):
                return
            try:
                if u.path=='/api/bootstrap':
                    self.reply(200,{'secret':app.secret})
                elif u.path=='/api/status':
                    self.reply(200,app.status())
                elif u.path=='/api/folders':
                    params=parse_qs(u.query)
                    value=params.get('snapshot_id',[None])[0]
                    snapshot=engine.store.snapshot(int(value) if value else None)
                    self.reply(200,{'snapshot_id':snapshot['id'] if snapshot else None,
                                    'folders':engine.store.folders(snapshot['id']) if snapshot else []})
                elif u.path=='/api/files':
                    params=parse_qs(u.query)
                    offset=int(params.get('offset',['0'])[0])
                    limit=min(200,int(params.get('limit',['100'])[0]))
                    if offset<0 or limit<1:
                        raise ValueError('invalid_input')
                    sid=params.get('snapshot_id',[None])[0]
                    sid=int(sid) if sid else None
                    rows=engine.store.files(sid,offset,limit,params.get('query',[''])[0],params.get('state',[''])[0])
                    self.reply(200,{'files':rows,'offset':offset,'limit':limit})
                elif u.path=='/api/export.csv':
                    # Build pages, not a single giant DB fetch. Typical catalogs fit in memory
                    # as a CSV; file contents never pass through the HTTP server.
                    out=io.StringIO(newline='')
                    writer=csv.writer(out)
                    fields=['path','size','md5','original_status','preview_status','verification','error_code','attempts','preview_error','preview_attempts','local_original','local_preview']
                    writer.writerow(fields)
                    offset=0
                    while True:
                        rows=engine.store.files(offset=offset,limit=1000)
                        if not rows:
                            break
                        for row in rows:
                            writer.writerow([csv_cell(row.get(k)) for k in fields])
                        offset+=len(rows)
                    self.reply(200,out.getvalue().encode('utf-8-sig'),'text/csv; charset=utf-8')
                elif u.path in ('/','/app.js','/styles.css'):
                    name={'/':'index.html','/app.js':'app.js','/styles.css':'styles.css'}[u.path]
                    content={'index.html':'text/html; charset=utf-8','app.js':'application/javascript; charset=utf-8','styles.css':'text/css; charset=utf-8'}[name]
                    try:
                        self.reply(200,(WEB/name).read_bytes(),content)
                    except FileNotFoundError:
                        self.reply(404,{'error':'not_found'})
                else:
                    self.reply(404,{'error':'not_found'})
            except (ValueError,TypeError):
                self.reply(400,{'error':'invalid_input'})
            except Exception:
                self.reply(500,{'error':'local_error'})

        def do_POST(self):
            if not self.secure(True):
                return
            if self.headers.get('Content-Type','').split(';')[0]!='application/json' or self.headers.get('Transfer-Encoding'):
                self.reply(400,{'error':'invalid_input'})
                return
            try:
                length=int(self.headers.get('Content-Length','-1'))
                if length>16384:
                    self.close_connection=True
                    self.reply(413,{'error':'request_too_large'})
                    return
                if length<0:
                    raise TypeError('invalid_input')
                data=json.loads(self.rfile.read(length))
                if not isinstance(data,dict):
                    raise TypeError('invalid_input')
                status=app.command(urlsplit(self.path).path,data)
                self.reply(status,{'accepted':status in (200,202)})
            except (TypeError,json.JSONDecodeError,UnicodeError):
                self.reply(400,{'error':'invalid_input'})
            except ValueError as error:
                self.reply(409,{'error':error_code(error)})
            except Exception:
                self.reply(500,{'error':'local_error'})

        def setup(self):
            super().setup()
            self.connection.settimeout(10)

    server=LocalServer(('127.0.0.1',port),Handler)
    server.app=app
    return server
