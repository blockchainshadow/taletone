#!/usr/bin/env python3
"""Loopback-only TaleTone editor. Fresh project: python server.py --source VIDEO."""
import argparse
import hashlib
import json
import mimetypes
import os
from pathlib import Path
import re
import tempfile
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlsplit

from hls_packager import Packager, inspect_stream
from demand_cache import CacheController, CacheError
from voice_profiles import profile
from native_picker import NativePicker

ROOT = Path(__file__).resolve().parent
WEB_ROOT = Path(os.environ.get('TALETONE_WEB_ROOT', ROOT.parent / 'web')).resolve()
MAX_UPLOAD = 512 * 1024 * 1024


def atomic_json(path, value):
    fd, name = tempfile.mkstemp(dir=path.parent, prefix='.json-')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as out:
            json.dump(value, out, ensure_ascii=False, indent=2)
            out.flush()
            os.fsync(out.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def probe(path, kind):
    stream, duration, formats = inspect_stream(path, kind)
    return stream, round(duration * 1000), formats


class Store:
    def __init__(self, data_dir=None, source=None, cache_options=None):
        self.data = Path(data_dir).expanduser().resolve() if data_dir else ROOT / 'data'
        self.data.mkdir(parents=True, exist_ok=True)
        (self.data / 'assets').mkdir(exist_ok=True)
        self.lock = threading.RLock()
        self.playback_lock = threading.Lock()
        self.hls = Packager(self.data / 'hls')
        self.cache = CacheController(self.data / 'cache', self.hls.root, **(cache_options or {}))
        self.registry_path = self.data / 'assets.json'
        self.project_path = self.data / 'project.json'
        self.registry = json.loads(self.registry_path.read_text(encoding='utf-8')) if self.registry_path.exists() else {}
        if not self.project_path.exists():
            if source is None:
                raise ValueError('A fresh project requires --source VIDEO; no sample media is bundled')
            source = Path(source).expanduser().resolve(strict=True)
            probe(source, 'audio')  # The original track requires a real embedded audio stream.
            asset = self.register(source, 'video', 'source')
            original = {'id': 'original', 'label': '原声', 'language': 'und', 'kind': 'embedded',
                        'revision': 1, 'audio_stream_index': 0, 'segments': []}
            self.save({'schema_version': 1, 'project_id': uuid.uuid4().hex, 'title': source.stem,
                       'source_asset_id': asset['id'], 'source_sha256': asset['sha256'],
                       'selected_track_id': 'original', 'tracks': [original]})
        elif source is not None:
            registered = self.registry[self.project()['source_asset_id']]['path']
            if Path(source).expanduser().resolve(strict=True) != Path(registered):
                raise ValueError('This data directory already has another source; use a new --data-dir')

    def register(self, path, kind, asset_id=None, name=None):
        stream, duration, formats = probe(path, kind)
        digest = hashlib.sha256()
        with path.open('rb') as src:
            for chunk in iter(lambda: src.read(1024 * 1024), b''):
                digest.update(chunk)
        mime = {'mp3': 'audio/mpeg', 'wav': 'audio/wav', 'flac': 'audio/flac', 'ogg': 'audio/ogg', 'mov,mp4,m4a,3gp,3g2,mj2': 'video/mp4' if kind == 'video' else 'audio/mp4', 'matroska,webm': 'video/webm' if kind == 'video' else 'audio/webm'}.get(formats, mimetypes.guess_type(str(path))[0] or 'application/octet-stream')
        asset_id = asset_id or uuid.uuid4().hex
        asset = {'id': asset_id, 'name': name or path.name, 'kind': kind, 'url': '/media/' + asset_id, 'duration_ms': duration, 'mime': mime, 'sha256': digest.hexdigest()}
        if kind == 'video':
            asset.update(width=stream['width'], height=stream['height'])
        with self.lock:
            self.registry[asset_id] = {'asset': asset, 'path': str(path.resolve())}
            atomic_json(self.registry_path, self.registry)
        return asset

    def validate(self, p):
        def require(ok, message):
            if not ok:
                raise ValueError(message)
        def text(v):
            return isinstance(v, str) and not any(0xD800 <= ord(c) <= 0xDFFF for c in v)
        def integer(v):
            return type(v) is int
        def fields(obj, required, optional=()):
            require(isinstance(obj, dict) and set(required) <= set(obj) and set(obj) <= set(required) | set(optional), 'Missing or unsupported fields; server paths are not accepted')
        fields(p, ('schema_version', 'project_id', 'title', 'source_asset_id', 'selected_track_id', 'tracks'), ('source_sha256',))
        require(type(p['schema_version']) is int and p['schema_version'] == 1, 'Unsupported schema_version')
        require(all(text(p[k]) for k in ('project_id', 'title', 'source_asset_id', 'selected_track_id')) and p['project_id'], 'Invalid project text fields')
        require(p['source_asset_id'] in self.registry and not p['source_asset_id'].startswith('playback-') and self.registry[p['source_asset_id']]['asset']['kind'] == 'video', 'Missing registered video source asset')
        require(isinstance(p['tracks'], list), 'tracks must be an array')
        if 'source_sha256' in p or any(isinstance(t, dict) and t.get('kind') == 'generated' for t in p['tracks']):
            expected = p.get('source_sha256')
            require(text(expected) and re.fullmatch(r'[0-9a-f]{64}', expected) is not None and
                    expected == self.registry[p['source_asset_id']]['asset']['sha256'],
                    'Generated project source_sha256 must match the registered source video')
        ids, embedded = set(), 0
        for t in p['tracks']:
            require(isinstance(t, dict), 'Invalid track')
            kind = t.get('kind')
            require(kind in ('embedded', 'external', 'generated'), 'Invalid track kind')
            extra = {'embedded': ('audio_stream_index',), 'external': ('asset_id', 'timeline_offset_ms'),
                     'generated': ('profile_id', 'timeline_offset_ms')}[kind]
            fields(t, ('id', 'label', 'language', 'kind', 'revision', 'segments') + extra)
            require(all(text(t[k]) for k in ('id', 'label', 'language')) and t['id'] and t['id'] not in ids, 'Invalid or duplicate track ID')
            ids.add(t['id'])
            require(type(t['revision']) is int and t['revision'] == 1, 'P0 supports published revision 1 only')
            if kind == 'embedded':
                embedded += 1
                require(type(t['audio_stream_index']) is int and t['audio_stream_index'] == 0, 'Invalid embedded audio stream')
            else:
                if kind == 'external':
                    require(text(t['asset_id']) and t['asset_id'] in self.registry and not t['asset_id'].startswith('playback-') and self.registry[t['asset_id']]['asset']['kind'] == 'audio', 'Missing registered audio asset')
                else:
                    settings = profile(t['profile_id'])
                    require(t['language'].split('-')[0].lower() == settings['language'], 'Generated profile language does not match track')
                require(integer(t['timeline_offset_ms']) and abs(t['timeline_offset_ms']) <= 600000, 'Invalid timeline offset')
            require(isinstance(t['segments'], list), 'segments must be an array')
            segment_ids = set()
            cursor = 0
            for s in t['segments']:
                fields(s, ('id', 'source_start_ms', 'source_end_ms', 'published_text', 'draft_text'))
                require(text(s['id']) and s['id'] and s['id'] not in segment_ids, 'Invalid or duplicate segment ID')
                segment_ids.add(s['id'])
                require(integer(s['source_start_ms']) and integer(s['source_end_ms']) and 0 <= s['source_start_ms'] < s['source_end_ms'], 'Invalid segment time')
                require(text(s['published_text']) and text(s['draft_text']), 'Invalid segment text')
                if kind == 'generated':
                    require(cursor <= s['source_start_ms'] and s['source_end_ms'] <= self.registry[p['source_asset_id']]['asset']['duration_ms'], 'Generated segments must be ordered, nonoverlapping, and within the source video')
                    cursor = s['source_end_ms']
        require(embedded == 1, 'Exactly one embedded track required')
        require(p['selected_track_id'] in ids, 'Selected track does not exist')
        return p

    def save(self, project):
        with self.lock:
            self.validate(project)
            atomic_json(self.project_path, project)
        return project

    def project(self):
        with self.lock:
            return json.loads(self.project_path.read_text(encoding='utf-8'))

    def cache_status(self, project):
        with self.lock:
            self.validate(project)
            registry = dict(self.registry)
        return self.cache.status(project, registry)

    def cache_prepare(self, project, track_id):
        with self.lock:
            self.validate(project)
            registry = dict(self.registry)
        return self.cache.prepare(project, registry, track_id)

    def playback(self, project):
        with self.lock:
            self.validate(project)
            registry = dict(self.registry)
        with self.playback_lock:
            try:
                with self.cache.playback(project, registry) as (private, assets):
                    return self.hls.prepare(private, assets)
            except OSError as exc:
                raise ValueError('Cannot publish HLS cache: ' + str(exc)) from exc


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def parse_request(self):
        if not super().parse_request():
            return False
        allowed = {f'127.0.0.1:{self.server.server_port}', f'localhost:{self.server.server_port}'}
        if self.headers.get('Host') not in allowed:
            self.fail(403, 'Host is not allowed')
            return False
        return True

    def json_response(self, status, value):
        body = json.dumps(value, ensure_ascii=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        if self.command != 'HEAD':
            self.wfile.write(body)

    def fail(self, status, message):
        self.close_connection = True
        self.json_response(status, {'error': message})

    def write_allowed(self):
        origin = self.headers.get('Origin')
        allowed = {f'http://127.0.0.1:{self.server.server_port}', f'http://localhost:{self.server.server_port}'}
        if origin is not None and origin not in allowed:
            self.fail(403, 'Origin is not allowed')
            return False
        if self.headers.get('Sec-Fetch-Site') == 'cross-site':
            self.fail(403, 'Cross-site writes are not allowed')
            return False
        return True

    def body_length(self, maximum):
        if self.headers.get('Transfer-Encoding'):
            raise ValueError('Transfer-Encoding is not supported; provide Content-Length')
        value = self.headers.get('Content-Length', '')
        if not value.isdigit() or not 0 < int(value) <= maximum:
            raise ValueError(f'Content-Length must be between 1 and {maximum}')
        return int(value)

    def do_PUT(self):
        if not self.write_allowed():
            return
        if self.path != '/api/project':
            return self.fail(404, 'Not found')
        try:
            length = self.body_length(8 * 1024 * 1024)
            body = self.rfile.read(length)
            if len(body) != length:
                raise ValueError('Incomplete request body')
            payload = json.loads(body)
            if not isinstance(payload, dict) or set(payload) != {'project'}:
                raise ValueError('Expected {project}')
            self.json_response(200, {'project': self.server.store.save(payload['project'])})
        except (ValueError, TypeError, UnicodeError) as exc:
            self.fail(400, str(exc))

    def do_POST(self):
        if self.server.native_picker.handle(self):
            return
        if not self.write_allowed():
            return
        if self.path in ('/api/playback', '/api/cache/status', '/api/cache/prepare'):
            try:
                length = self.body_length(8 * 1024 * 1024)
                body = self.rfile.read(length)
                if len(body) != length:
                    raise ValueError('Incomplete request body')
                payload = json.loads(body)
                required = {'project', 'track_id'} if self.path == '/api/cache/prepare' else {'project'}
                if not isinstance(payload, dict) or set(payload) != required:
                    raise ValueError('Expected ' + ', '.join(sorted(required)))
                if self.path == '/api/cache/status':
                    return self.json_response(200, self.server.store.cache_status(payload['project']))
                if self.path == '/api/cache/prepare':
                    if not isinstance(payload['track_id'], str):
                        raise ValueError('Invalid track_id')
                    result = self.server.store.cache_prepare(payload['project'], payload['track_id'])
                    return self.json_response(200 if result['cache_hit'] else 202, result)
                return self.json_response(200, self.server.store.playback(payload['project']))
            except CacheError as exc:
                return self.json_response(exc.status, exc.payload())
            except (ValueError, TypeError, UnicodeError) as exc:
                return self.fail(400, str(exc))
        if self.path != '/api/assets':
            return self.fail(404, 'Not found')
        temporary = None
        final = None
        try:
            length = self.body_length(MAX_UPLOAD)
            name = unquote(self.headers.get('X-Filename', 'audio'), errors='strict')
            name = name.replace('\\', '/').rsplit('/', 1)[-1]
            if not name or any(ord(c) < 32 for c in name):
                raise ValueError('Invalid filename')
            fd, temporary = tempfile.mkstemp(dir=self.server.store.data / 'assets', prefix='.upload-')
            with os.fdopen(fd, 'wb') as out:
                remaining = length
                while remaining:
                    block = self.rfile.read(min(1024 * 1024, remaining))
                    if not block:
                        raise ValueError('Incomplete upload')
                    out.write(block)
                    remaining -= len(block)
            asset_id = uuid.uuid4().hex
            final = self.server.store.data / 'assets' / asset_id
            os.replace(temporary, final)
            temporary = None
            asset = self.server.store.register(final, 'audio', asset_id, name)
            final = None
            self.json_response(201, {'asset': asset})
        except (ValueError, UnicodeError) as exc:
            self.fail(400, str(exc))
        finally:
            for path in (temporary, final):
                if path and os.path.exists(path):
                    os.unlink(path)

    def do_DELETE(self):
        if not self.write_allowed():
            return
        if self.path != '/api/cache':
            return self.fail(404, 'Not found')
        if self.headers.get('Transfer-Encoding') or self.headers.get('Content-Length', '0') != '0':
            return self.fail(400, 'Cache clear does not accept a request body')
        try:
            return self.json_response(200, self.server.store.cache.clear())
        except CacheError as exc:
            return self.json_response(exc.status, exc.payload())

    def do_OPTIONS(self):
        if not self.server.native_picker.handle(self):
            self.fail(404, 'Not found')

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        if self.server.native_picker.handle(self):
            return
        path = urlsplit(self.path).path
        if path == '/api/project':
            return self.json_response(200, {'project': self.server.store.project()})
        if path == '/api/assets':
            with self.server.store.lock:
                assets = [r['asset'] for asset_id, r in self.server.store.registry.items() if not asset_id.startswith('playback-')]
            return self.json_response(200, {'assets': assets})
        if path.startswith('/api/cache/jobs/'):
            job_id = path[len('/api/cache/jobs/'):]
            if not re.fullmatch(r'[0-9a-f]{32}', job_id):
                return self.fail(404, 'Unknown cache job')
            try:
                return self.json_response(200, {'job': self.server.store.cache.job(job_id)})
            except CacheError as exc:
                return self.json_response(exc.status, exc.payload())
        if path.startswith('/media/'):
            entry = self.server.store.registry.get(path[len('/media/'):])
            if not entry or path[len('/media/'):].startswith('playback-'):
                return self.fail(404, 'Unknown media asset')
            return self.send_file(Path(entry['path']), entry['asset']['mime'], ranges=True)
        if path.startswith('/hls/'):
            parts = path.split('/')
            cache_key, filename = (parts[2], parts[3]) if len(parts) == 4 else ('', '')
            with self.server.store.cache.media(self.server.store.hls, cache_key, filename) as media:
                if media is None:
                    return self.fail(404, 'Unknown HLS media')
                mime = 'application/vnd.apple.mpegurl' if media.suffix == '.m3u8' else 'video/mp4' if media.suffix == '.mp4' else 'video/iso.segment'
                return self.send_file(media, mime, ranges=True)
        workbench = {'/workbench/': 'index.html', '/workbench/app.js': 'app.js', '/workbench/style.css': 'style.css'}
        if path in workbench:
            file = WEB_ROOT / workbench[path]
            return self.send_file(file, mimetypes.guess_type(file.name)[0] or 'application/octet-stream')
        static = {'/': 'index.html', '/index.html': 'index.html', '/app.js': 'app.js', '/style.css': 'style.css',
                  '/vendor/hls.min.js': 'vendor/hls.min.js', '/vendor/hls.LICENSE': 'vendor/hls.LICENSE'}
        if path not in static:
            return self.fail(404, 'Not found')
        return self.send_file(ROOT / static[path], mimetypes.guess_type(static[path])[0] or 'application/octet-stream')

    def send_file(self, path, mime, ranges=False):
        try:
            src = path.open('rb')
        except OSError:
            return self.fail(404, 'Media or static file is unavailable')
        with src:
            size = os.fstat(src.fileno()).st_size
            start, end, status = 0, size - 1, 200
            header = self.headers.get('Range') if ranges else None
            if header:
                match = re.fullmatch(r'bytes=(\d*)-(\d*)', header)
                try:
                    if not match or not any(match.groups()) or size == 0:
                        raise ValueError()
                    a, b = match.groups()
                    if not a:
                        suffix = int(b)
                        if suffix <= 0:
                            raise ValueError()
                        start = max(0, size - suffix)
                    else:
                        start = int(a)
                        end = min(int(b), size - 1) if b else size - 1
                    if start >= size or end < start:
                        raise ValueError()
                    status = 206
                except ValueError:
                    self.send_response(416)
                    self.send_header('Content-Range', f'bytes */{size}')
                    self.send_header('Content-Length', '0')
                    self.end_headers()
                    return
            self.send_response(status)
            self.send_header('Content-Type', mime)
            self.send_header('Content-Length', str(max(0, end - start + 1)))
            self.send_header('X-Content-Type-Options', 'nosniff')
            if ranges:
                self.send_header('Accept-Ranges', 'bytes')
                if status == 206:
                    self.send_header('Content-Range', f'bytes {start}-{end}/{size}')
            self.end_headers()
            if self.command == 'HEAD':
                return
            src.seek(start)
            remaining = end - start + 1
            try:
                while remaining > 0:
                    block = src.read(min(256 * 1024, remaining))
                    if not block:
                        break
                    self.wfile.write(block)
                    remaining -= len(block)
            except (BrokenPipeError, ConnectionResetError):
                pass


def create_server(port=8877, store=None, native_picker=None):
    server = ThreadingHTTPServer(('127.0.0.1', port), Handler)
    server.daemon_threads = True
    server.store = store if store is not None else Store()
    server.native_picker = native_picker if native_picker is not None else NativePicker()
    return server


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=8877)
    parser.add_argument('--source', type=Path, help='User-owned source video, required for a fresh data directory')
    parser.add_argument('--data-dir', type=Path, default=ROOT / 'data', help='Private project/assets/cache directory')
    args = parser.parse_args()
    try:
        app = create_server(args.port, Store(args.data_dir, source=args.source))
    except (ValueError, OSError) as exc:
        parser.error(str(exc))
    print(f'TaleTone 声伴 · local only: http://127.0.0.1:{app.server_port}', flush=True)
    try:
        app.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        app.server_close()
        app.store.cache.close()
