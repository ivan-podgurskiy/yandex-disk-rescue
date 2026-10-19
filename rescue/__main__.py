"""Run with python3 -m rescue. Only loopback access is supported."""
import argparse
import os
import sys
from pathlib import Path
from .engine import Engine
from .server import create_server
from .store import Store


def main(argv=None):
    parser=argparse.ArgumentParser(description='Локальное восстановление Яндекс.Диска')
    parser.add_argument('--host',default='127.0.0.1',choices=['127.0.0.1','localhost'])
    parser.add_argument('--port',type=int,default=8765)
    parser.add_argument('--data-dir',default=str(Path.home()/'Library/Application Support/YandexDiskRescue'))
    args=parser.parse_args(argv)
    if not 0<=args.port<=65535:
        parser.error('invalid port')
    root=Path(args.data_dir).expanduser().absolute()
    if root.is_symlink():
        parser.error('data directory must not be a symlink')
    root.mkdir(parents=True,exist_ok=True,mode=0o700)
    os.chmod(root,0o700)
    os.umask(0o077)
    engine=None
    server=None
    try:
        engine=Engine(Store(str(root/'state.sqlite')))
        server=create_server(engine,str(root),args.port)
        print('Откройте http://127.0.0.1:%d — Ctrl+C для остановки' % server.server_port,flush=True)
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    except (OSError,ValueError) as error:
        print('Не удалось запустить локальный сервис (%s).' % ('application_busy' if str(error)=='application_busy' else 'local_error'),file=sys.stderr)
        return 1
    finally:
        if server:
            server.server_close()
        if engine:
            safe=engine.close()
            if safe and engine.destination:
                engine.destination.close()
    return 0


if __name__=='__main__':
    raise SystemExit(main())
