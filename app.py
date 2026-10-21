"""Warehouse Ops localhost server. Python 3.10+, no third-party packages."""
import argparse
import json
import secrets
import sqlite3
import tempfile
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs,urlsplit
from domain import Store,Problem,FIELDS,clock,csv_bytes,integer
from demo import seed_demo,sample_rows

ROOT=Path(__file__).resolve().parent

class Handler(BaseHTTPRequestHandler):
    def send(self,body,mime='application/json; charset=utf-8',status=200,filename=None):
        self.send_response(status)
        for k,v in {'Content-Type':mime,'Content-Length':str(len(body)),
                    'Cache-Control':'no-store','X-Content-Type-Options':'nosniff','Referrer-Policy':'no-referrer',
                    'Content-Security-Policy':"default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"}.items():
            self.send_header(k,v)
        if filename:
            self.send_header('Content-Disposition',f'attachment; filename="{filename}"')
        self.end_headers()
        self.wfile.write(body)

    def json(self,value,status=200):
        self.send(json.dumps(value,ensure_ascii=False,allow_nan=False).encode(),status=status)

    def host_valid(self):
        return self.headers.get('Host','') in (f'127.0.0.1:{self.server.server_port}',f'localhost:{self.server.server_port}')

    def do_GET(self):
        if not self.host_valid():
            return self.json({'error':'仅支持本机访问'},403)
        parts=urlsplit(self.path)
        q={k:v[0] for k,v in parse_qs(parts.query).items()}
        store=self.server.store
        try:
            if parts.path=='/api/meta':
                return self.json({**store.metadata(),'token':self.server.token})
            if parts.path=='/api/report':
                return self.json(store.report(q.get('start'),q.get('end'),q.get('station','')))
            if parts.path=='/api/history':
                return self.json(store.history(q.get('order_id','')))
            if parts.path=='/api/export':
                kind=q.get('kind','orders')
                body=store.export(kind,q.get('start'),q.get('end'),q.get('station',''))
                return self.send(body,'text/csv; charset=utf-8',filename=f'warehouse-{kind}.csv')
            if parts.path in ('/api/template','/api/sample'):
                kind=q.get('kind','orders')
                if kind not in FIELDS:
                    raise Problem('类型无效')
                rows=sample_rows()[kind] if parts.path=='/api/sample' else []
                return self.send(csv_bytes(FIELDS[kind],rows),'text/csv; charset=utf-8',filename=f'{kind}-{parts.path.split("/")[-1]}.csv')
            if parts.path=='/api/backup':
                with tempfile.TemporaryDirectory() as directory:
                    dest=Path(directory)/'backup.db'
                    with store.connect() as source:
                        target=sqlite3.connect(dest)
                        try:
                            source.backup(target)
                        finally:
                            target.close()
                    return self.send(dest.read_bytes(),'application/octet-stream',filename=f'warehouse-ops-{clock().date()}.db')
            static={'/':('index.html','text/html'),'/app.js':('app.js','text/javascript'),'/style.css':('style.css','text/css'),'/tokens.css':('../tokens.css','text/css'),'/favicon.svg':('favicon.svg','image/svg+xml')}
            if parts.path in static:
                file,mime=static[parts.path]
                return self.send((ROOT/'static'/file).read_bytes(),mime+'; charset=utf-8')
            return self.json({'error':'页面不存在'},404)
        except Problem as exc:
            self.json({'error':str(exc)},exc.status)
        except (sqlite3.Error,OSError):
            self.json({'error':'读取失败，请检查本地数据库后重试'},500)

    def do_POST(self):
        if not self.host_valid() or not secrets.compare_digest(self.headers.get('X-App-Token',''),self.server.token):
            return self.json({'error':'会话已过期，请刷新页面'},403)
        origin=self.headers.get('Origin')
        if origin and origin!=f"http://{self.headers['Host']}":
            return self.json({'error':'不支持跨站操作'},403)
        try:
            length=integer(self.headers.get('Content-Length','0'),'请求长度',1,8_000_000)
            if self.headers.get_content_type()!='application/json':
                raise Problem('请使用 JSON 请求',415)
            data=json.loads(self.rfile.read(length))
            if not isinstance(data,dict):
                raise Problem('请求须为对象')
            path=urlsplit(self.path).path
            store=self.server.store
            if path in ('/api/import/preview','/api/import/commit'):
                if not isinstance(data.get('filename'),str):
                    raise Problem('缺少文件名')
                result=store.ingest(data.get('kind'),data.get('content'),data['filename'],path.endswith('/commit'))
                return self.json(result)
            if path=='/api/followup':
                return self.json(store.followup(data))
            if path=='/api/staffing':
                return self.json(store.staffing(data))
            raise Problem('接口不存在',404)
        except (json.JSONDecodeError,UnicodeDecodeError):
            self.json({'error':'JSON 格式无效'},400)
        except Problem as exc:
            self.json({'error':str(exc)},exc.status)
        except sqlite3.Error:
            self.json({'error':'数据库忙或写入失败，本次操作未提交，请重试'},500)

def make_server(store,port=8767):
    server=ThreadingHTTPServer(('127.0.0.1',port),Handler)
    server.store=store
    server.token=secrets.token_hex(32)
    server.daemon_threads=True
    return server

def main():
    parser=argparse.ArgumentParser(description='仓衡：仓储运营分析与人效管理')
    parser.add_argument('--port',type=int,default=8767)
    parser.add_argument('--db',type=Path,default=ROOT/'data'/'operations.db')
    parser.add_argument('--demo',action='store_true')
    parser.add_argument('--open',action='store_true')
    args=parser.parse_args()
    store=Store(args.db)
    if args.demo:
        seed_demo(store)
    try:
        server=make_server(store,args.port)
    except OSError as exc:
        parser.exit(1,f'启动失败：{exc}；可用 --port 8768 更换端口。\n')
    url=f'http://127.0.0.1:{server.server_port}'
    print(f'Warehouse Ops: {url}\nDatabase: {store.path}\nPress Ctrl+C to stop.',flush=True)
    if args.open:
        threading.Timer(.5,lambda:webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()

if __name__=='__main__':
    main()
