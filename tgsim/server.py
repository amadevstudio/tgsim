"""The model over HTTP, where a bot of any language finds api.telegram.org.

    server = Server(tg, port=8081).start()        # http://127.0.0.1:8081
    # the bot: API base http://127.0.0.1:8081/bot<token>/<method>, files at /file/bot<token>/<path>
    server.stop()

Requests are read as the Bot API reads them: query string, JSON, form-urlencoded or multipart (uploads). `GET /`
shows the chats as a page (`?refresh=2` reloads it). `POST /tgsim/action` with {"line": "send /start"} runs one
scenario action and answers with its step, so tests in another language can drive users.
"""
from __future__ import annotations

import json
import re
import threading
import urllib.parse
from email.parser import BytesParser
from email.policy import HTTP
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional

from .model import ApiError, Telegram
from .render import chat_page

_API = re.compile(r'^/bot([^/]+)/([A-Za-z]+)$')
_FILE = re.compile(r'^/file/bot([^/]+)/(.+)$')


def parse_body(content_type: str, body: bytes):
    """(params, files) of a Bot API request body."""
    params, files = {}, {}
    kind = (content_type or '').split(';')[0].strip().lower()
    if not body:
        return params, files
    if kind == 'application/json':
        data = json.loads(body.decode('utf-8'))
        if isinstance(data, dict):
            for key, value in data.items():
                params[key] = value
        return params, files
    if kind == 'multipart/form-data':
        message = BytesParser(policy=HTTP).parsebytes(
            b'Content-Type: ' + content_type.encode('latin-1') + b'\r\n\r\n' + body)
        for part in message.iter_parts():
            name = part.get_param('name', header='content-disposition')
            if not name:
                continue
            filename = part.get_filename()
            payload = part.get_payload(decode=True) or b''
            if filename is not None:
                files[name] = (filename, payload, part.get_content_type())
            else:
                charset = part.get_content_charset() or 'utf-8'
                params[name] = payload.decode(charset)
        return params, files
    for key, values in urllib.parse.parse_qs(body.decode('utf-8'), keep_blank_values=True).items():
        params[key] = values[-1]
    return params, files


class Server:
    def __init__(self, tg: Telegram, host: str = '127.0.0.1', port: int = 8081, runner=None):
        self.tg, self.host, self.port, self.runner = tg, host, port, runner
        self.httpd: Optional[ThreadingHTTPServer] = None
        self.thread: Optional[threading.Thread] = None
        if tg.quiet is None:
            tg.quiet = 0.5

    @property
    def url(self) -> str:
        return 'http://%s:%d' % (self.host, self.port)

    def start(self) -> 'Server':
        server = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'

            def log_message(self, *args):
                pass

            def do_GET(self):
                server._handle(self, b'')

            def do_POST(self):
                length = int(self.headers.get('Content-Length') or 0)
                server._handle(self, self.rfile.read(length) if length else b'')

        self.httpd = ThreadingHTTPServer((self.host, self.port), Handler)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, name='tgsim-http', daemon=True)
        self.thread.start()
        return self

    def stop(self):
        self.tg.close()
        if self.httpd is not None:
            self.httpd.shutdown()
            self.httpd.server_close()

    # ---------------------------------------------------------------------------------------
    def _handle(self, request: BaseHTTPRequestHandler, body: bytes):
        parsed = urllib.parse.urlsplit(request.path)
        path = parsed.path
        try:
            match = _API.match(path)
            if match:
                return self._api(request, match.group(1), match.group(2), parsed.query, body)
            match = _FILE.match(path)
            if match:
                return self._file(request, match.group(1), urllib.parse.unquote(match.group(2)))
            if path == '/tgsim/action' and request.command == 'POST':
                return self._action(request, body)
            if path in ('/', '/index.html'):
                query = urllib.parse.parse_qs(parsed.query)
                refresh = int(query['refresh'][0]) if query.get('refresh') else None
                return self._send(request, 200, chat_page(self.tg, refresh=refresh).encode('utf-8'),
                                  'text/html; charset=utf-8')
            return self._json(request, 404, {'ok': False, 'error_code': 404, 'description': 'Not Found'})
        except BrokenPipeError:
            pass

    def _api(self, request, token: str, method: str, query: str, body: bytes):
        params = {key: values[-1] for key, values in urllib.parse.parse_qs(query, keep_blank_values=True).items()}
        try:
            extra, files = parse_body(request.headers.get('Content-Type', ''), body)
        except ValueError:
            return self._json(request, 400, {'ok': False, 'error_code': 400,
                                             'description': "Bad Request: can't parse JSON"})
        params.update(extra)
        try:
            result = self.tg.call(method, params, files, token=token)
        except ApiError as e:
            return self._json(request, e.code, e.json())
        return self._json(request, 200, {'ok': True, 'result': result})

    def _file(self, request, token: str, file_path: str):
        if self.tg.token is not None and token != self.tg.token:
            return self._json(request, 401, {'ok': False, 'error_code': 401, 'description': 'Unauthorized'})
        data = self.tg.file_bytes(file_path)
        if data is None:
            return self._json(request, 404, {'ok': False, 'error_code': 404, 'description': 'Not Found'})
        return self._send(request, 200, data, 'application/octet-stream')

    def _action(self, request, body: bytes):
        if self.runner is None:
            return self._json(request, 404, {'ok': False, 'description': 'no scenario runner'})
        try:
            line = json.loads(body.decode('utf-8')).get('line', '')
            result = self.runner.run(line)
        except Exception as e:
            return self._json(request, 400, {'ok': False, 'description': '%s: %s' % (type(e).__name__, e)})
        text = str(result) if result is not None else ''
        failed = bool(getattr(result, 'failed', False))
        return self._json(request, 200, {'ok': not failed, 'step': text})

    @staticmethod
    def _json(request, status: int, data: dict):
        Server._send(request, status, json.dumps(data, ensure_ascii=False).encode('utf-8'),
                     'application/json; charset=utf-8')

    @staticmethod
    def _send(request, status: int, data: bytes, content_type: str):
        request.send_response(status)
        request.send_header('Content-Type', content_type)
        request.send_header('Content-Length', str(len(data)))
        request.end_headers()
        request.wfile.write(data)
