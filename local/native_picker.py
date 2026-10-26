"""macOS chooser and ephemeral, origin-restricted loopback media tickets."""
import json
import mimetypes
import os
from pathlib import Path
import re
import secrets
import stat
import subprocess
import sys
import threading
from urllib.parse import urlsplit


PUBLIC_ORIGIN = 'https://papaw.cc'
PICKER_PATH = '/api/native-picker'
FILES_PATH = '/api/native-files/'
_CHOOSER_LOCK = threading.Lock()


class PickerError(Exception):
    """An actionable chooser failure that is safe to return to the browser."""


def choose_files(kind):
    """Return selected paths, or None for macOS user cancellation (-128)."""
    if sys.platform != 'darwin':
        raise PickerError('Native file selection requires the macOS TaleTone local helper.')
    script = r'''
function run(argv) {
    var app = Application.currentApplication();
    app.includeStandardAdditions = true;
    try {
        var audio = argv[0] === 'audio';
        var selected = app.chooseFile({
            withPrompt: audio ? '声伴：选择本地音频或配音成片（可多选）' : '声伴：选择本地视频',
            ofType: audio ? ['public.audio', 'public.movie'] : ['public.movie'],
            multipleSelectionsAllowed: audio
        });
        var files = audio ? selected : [selected];
        return JSON.stringify({cancelled: false, paths: files.map(function (file) { return file.toString(); })});
    } catch (error) {
        if (Number(error.errorNumber) === -128 || Number(error.number) === -128) {
            return JSON.stringify({cancelled: true});
        }
        throw error;
    }
}
'''
    try:
        result = subprocess.run(['/usr/bin/osascript', '-l', 'JavaScript', '-e', script, kind],
                                capture_output=True, text=True, check=False)
    except OSError as exc:
        raise PickerError('Cannot launch the macOS file chooser. Check that /usr/bin/osascript is available.') from exc
    if result.returncode:
        # Some macOS versions report Standard Additions cancellation outside JXA's catch.
        if re.search(r'\(-128\)\s*$', result.stderr.strip()):
            return None
        raise PickerError('The macOS file chooser failed. Check the local helper’s macOS Automation permissions and desktop session.')
    try:
        value = json.loads(result.stdout)
        if value == {'cancelled': True}:
            return None
        if (not isinstance(value, dict) or set(value) != {'cancelled', 'paths'} or
                value['cancelled'] is not False or not isinstance(value['paths'], list) or
                not value['paths'] or not all(isinstance(path, str) and path for path in value['paths'])):
            raise ValueError('Invalid chooser result')
        return value['paths']
    except (ValueError, TypeError) as exc:
        raise PickerError('The macOS file chooser returned an invalid result. Restart the local helper and try again.') from exc


class NativePicker:
    """One helper session's tickets. No paths or selections are persisted."""

    def __init__(self, select=None):
        self._select = select if select is not None else choose_files
        self._tickets = {}
        self._tickets_lock = threading.Lock()

    def handle(self, handler):
        target = urlsplit(handler.path)
        picker = target.path == PICKER_PATH
        media = target.path.startswith(FILES_PATH)
        if not picker and not media:
            return False
        hosts = {f'127.0.0.1:{handler.server.server_port}', f'localhost:{handler.server.server_port}'}
        host = handler.headers.get('Host')
        origins = handler.headers.get_all('Origin', [])
        origin = origins[0] if len(origins) == 1 else None
        allowed_origins = {PUBLIC_ORIGIN, f'http://{host}'}
        trusted = len(origins) == 1 and origin in allowed_origins
        if len(handler.headers.get_all('Host', [])) != 1 or host not in hosts:
            self._json(handler, 403, {'error': 'Loopback Host is required.'})
            return True
        # POST and preflight always require an explicit trusted browser origin.
        if (origins and not trusted) or (not trusted and
                (handler.command in ('POST', 'OPTIONS') or handler.headers.get('Sec-Fetch-Site') == 'cross-site')):
            self._json(handler, 403, {'error': 'Origin is not allowed.'})
            return True
        cors_origin = origin if trusted else None
        if target.query or target.fragment:
            self._json(handler, 400, {'error': 'Native picker routes do not accept query parameters.'}, cors_origin)
        elif handler.command == 'OPTIONS':
            self._options(handler, picker, cors_origin)
        elif picker and handler.command == 'POST':
            self._pick(handler, cors_origin, host)
        elif media and handler.command in ('GET', 'HEAD'):
            self._media(handler, target.path[len(FILES_PATH):], cors_origin)
        else:
            self._json(handler, 405, {'error': 'Method is not allowed.'}, cors_origin)
        return True

    @staticmethod
    def _headers(handler, origin):
        handler.send_header('Cache-Control', 'no-store')
        handler.send_header('X-Content-Type-Options', 'nosniff')
        handler.send_header('Vary', 'Origin')
        if origin:
            handler.send_header('Access-Control-Allow-Origin', origin)
            handler.send_header('Access-Control-Expose-Headers', 'Content-Length, Content-Range, Accept-Ranges')

    def _json(self, handler, status, value, origin=None):
        body = json.dumps(value, ensure_ascii=False).encode('utf-8')
        # Invalid POSTs may leave an unread body; never treat it as another request.
        if status >= 400:
            handler.close_connection = True
        handler.send_response(status)
        self._headers(handler, origin)
        handler.send_header('Content-Type', 'application/json; charset=utf-8')
        handler.send_header('Content-Length', str(len(body)))
        handler.end_headers()
        if handler.command != 'HEAD':
            handler.wfile.write(body)

    def _options(self, handler, picker, origin):
        methods = ('POST',) if picker else ('GET', 'HEAD')
        allowed_headers = {'content-type'} if picker else {'range'}
        requested = {part.strip().lower() for part in handler.headers.get('Access-Control-Request-Headers', '').split(',') if part.strip()}
        private_network = handler.headers.get('Access-Control-Request-Private-Network')
        if (handler.headers.get('Access-Control-Request-Method') not in methods or
                not requested <= allowed_headers or private_network not in (None, 'true')):
            self._json(handler, 403, {'error': 'Preflight method or headers are not allowed.'}, origin)
            return
        handler.send_response(204)
        self._headers(handler, origin)
        handler.send_header('Vary', 'Access-Control-Request-Method, Access-Control-Request-Headers, Access-Control-Request-Private-Network')
        handler.send_header('Access-Control-Allow-Methods', ', '.join(methods))
        if requested:
            handler.send_header('Access-Control-Allow-Headers', ', '.join(sorted(requested)))
        if private_network == 'true':
            handler.send_header('Access-Control-Allow-Private-Network', 'true')
        handler.send_header('Content-Length', '0')
        handler.end_headers()

    def _pick(self, handler, origin, host):
        if (len(handler.headers.get_all('Content-Type', [])) != 1 or
                handler.headers.get_content_type() != 'application/json'):
            return self._json(handler, 415, {'error': 'Content-Type must be application/json.'}, origin)
        try:
            if handler.headers.get('Transfer-Encoding') or len(handler.headers.get_all('Content-Length', [])) != 1:
                raise ValueError('A single Content-Length is required; Transfer-Encoding is not supported.')
            length = handler.headers.get('Content-Length', '')
            if not length.isdigit() or not 0 < int(length) <= 1024:
                raise ValueError('Content-Length must be between 1 and 1024.')
            body = handler.rfile.read(int(length))
            if len(body) != int(length):
                raise ValueError('Incomplete JSON request body.')
            value = json.loads(body)
            if not isinstance(value, dict) or set(value) != {'kind'} or value['kind'] not in ('audio', 'video'):
                raise ValueError('Expected only {kind: "audio" | "video"}; filesystem paths are not accepted.')
        except (ValueError, TypeError, UnicodeError) as exc:
            return self._json(handler, 400, {'error': str(exc)}, origin)
        if not _CHOOSER_LOCK.acquire(blocking=False):
            return self._json(handler, 409, {'error': 'A native file chooser is already open. Finish or cancel it first.'}, origin)
        try:
            paths = self._select(value['kind'])
            if paths is None:
                return self._json(handler, 200, {'cancelled': True, 'files': []}, origin)
            if not paths or (value['kind'] == 'video' and len(paths) != 1):
                raise PickerError('The macOS file chooser returned an invalid selection.')
            files, tickets = [], {}
            for selected in paths:
                path = Path(selected).resolve(strict=True)
                info = path.stat()
                if not stat.S_ISREG(info.st_mode):
                    raise PickerError('Choose regular audio or video files, not folders or devices.')
                # Open now to fail clearly for unavailable files before returning a ticket.
                with path.open('rb'):
                    pass
                token = secrets.token_urlsafe(32)
                mime = mimetypes.guess_type(path.name)[0] or 'application/octet-stream'
                tickets[token] = (path, info.st_dev, info.st_ino, mime)
                files.append({'name': path.name, 'size': info.st_size,
                              'lastModified': info.st_mtime_ns // 1000000, 'type': mime,
                              'url': f'http://{host}{FILES_PATH}{token}'})
            with self._tickets_lock:
                self._tickets.update(tickets)
            return self._json(handler, 200, {'cancelled': False, 'files': files}, origin)
        except PickerError as exc:
            return self._json(handler, 503, {'error': str(exc)}, origin)
        except (OSError, ValueError, TypeError):
            return self._json(handler, 503, {'error': 'A selected file is unavailable or unreadable. Choose it again after checking local file permissions.'}, origin)
        finally:
            _CHOOSER_LOCK.release()

    def _media(self, handler, token, origin):
        with self._tickets_lock:
            ticket = self._tickets.get(token) if re.fullmatch(r'[A-Za-z0-9_-]{43}', token) else None
        if ticket is None:
            return self._json(handler, 404, {'error': 'Unknown native file ticket. Choose the file again.'}, origin)
        path, device, inode, mime = ticket
        try:
            fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0))
            src = os.fdopen(fd, 'rb')
        except OSError:
            return self._json(handler, 404, {'error': 'Selected file is unavailable. Choose it again.'}, origin)
        with src:
            info = os.fstat(src.fileno())
            if not stat.S_ISREG(info.st_mode) or (info.st_dev, info.st_ino) != (device, inode):
                return self._json(handler, 404, {'error': 'Selected file changed location or was replaced. Choose it again.'}, origin)
            size = info.st_size
            start, end, status = 0, size - 1, 200
            header = handler.headers.get('Range')
            if header is not None:
                try:
                    match = re.fullmatch(r'bytes=(\d*)-(\d*)', header)
                    if not match or not any(match.groups()) or not size or len(handler.headers.get_all('Range', [])) != 1:
                        raise ValueError()
                    first, last = match.groups()
                    if first:
                        start = int(first)
                        end = min(int(last), size - 1) if last else size - 1
                    else:
                        suffix = int(last)
                        if suffix <= 0:
                            raise ValueError()
                        start = max(0, size - suffix)
                    if start >= size or end < start:
                        raise ValueError()
                    status = 206
                except ValueError:
                    handler.send_response(416)
                    self._headers(handler, origin)
                    handler.send_header('Accept-Ranges', 'bytes')
                    handler.send_header('Content-Range', f'bytes */{size}')
                    handler.send_header('Content-Length', '0')
                    handler.end_headers()
                    return
            handler.send_response(status)
            self._headers(handler, origin)
            handler.send_header('Content-Type', mime)
            handler.send_header('Content-Length', str(max(0, end - start + 1)))
            handler.send_header('Accept-Ranges', 'bytes')
            if status == 206:
                handler.send_header('Content-Range', f'bytes {start}-{end}/{size}')
            handler.end_headers()
            if handler.command == 'HEAD':
                return
            src.seek(start)
            remaining = end - start + 1
            try:
                while remaining > 0:
                    block = src.read(min(256 * 1024, remaining))
                    if not block:
                        handler.close_connection = True
                        break
                    handler.wfile.write(block)
                    remaining -= len(block)
            except (BrokenPipeError, ConnectionResetError):
                pass
