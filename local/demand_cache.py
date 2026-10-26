"""Lazy TTL/LRU temporary media, real single-flight jobs, and private playback resolution.

Only prepare() schedules synthesis. status()/job() inspect filesystem metadata; they
never invoke a model, FFmpeg, or ffprobe. A single worker keeps model memory bounded.
All paths passed to the renderer originate here, never from client JSON.
"""
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile
import threading
import time
import uuid

from hls_packager import inspect_stream
from voice_profiles import MLX_PYTHON, profile

DEFAULT_TTL = 24 * 60 * 60
DEFAULT_MAX_BYTES = 256 * 1024 * 1024


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(',', ':')).encode('utf-8')).hexdigest()


def file_sha256(path):
    result = hashlib.sha256()
    with Path(path).open('rb') as src:
        for block in iter(lambda: src.read(1024 * 1024), b''):
            result.update(block)
    return result.hexdigest()


def tree_bytes(path):
    # The renderer can finish/rename scratch files while a status request scans.
    # lstat avoids following any symlink outside the managed temporary roots.
    try:
        info = path.lstat()
        if stat.S_ISREG(info.st_mode):
            return info.st_size
        if not stat.S_ISDIR(info.st_mode):
            return 0
        size = 0
        for child in path.rglob('*'):
            try:
                info = child.lstat()
                if stat.S_ISREG(info.st_mode):
                    size += info.st_size
            except FileNotFoundError:
                continue
        return size
    except FileNotFoundError:
        return 0


class CacheError(ValueError):
    def __init__(self, message, status=409, code=None, track_id=None):
        super().__init__(message)
        self.status, self.code, self.track_id = status, code, track_id

    def payload(self):
        result = {'error': str(self)}
        if self.code:
            result['code'] = self.code
        if self.track_id:
            result['track_id'] = self.track_id
        return result


class CacheController:
    def __init__(self, root, hls_root, *, max_bytes=DEFAULT_MAX_BYTES,
                 ttl_seconds=DEFAULT_TTL, renderer=None, clock=time.time):
        self.root, self.hls_root = Path(root), Path(hls_root)
        self.audio_root, self.takes_root, self.work_root = (self.root / name for name in ('audio', 'takes', 'work'))
        for path in (self.root, self.audio_root, self.takes_root, self.work_root, self.hls_root):
            if path.is_symlink():
                raise ValueError('Temporary cache directories must not be symlinks')
            path.mkdir(parents=True, exist_ok=True)
        self.max_bytes, self.ttl_seconds = max_bytes, ttl_seconds
        self.clock = clock
        self.lock = threading.RLock()
        self.pins = {}
        self.packaging = 0
        self.jobs, self.by_key = OrderedDict(), {}
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='demand-dub')
        self.renderer = renderer or self._render_tts
        # Staged files are never visible after a process restart.
        for path in self.work_root.iterdir():
            self._remove(path)
        with self.lock:
            self._cleanup_locked()

    @staticmethod
    def snapshot(project, registry, track):
        source = registry[project['source_asset_id']]
        params = profile(track['profile_id'])
        segments = [{'id': s['id'], 'source_start_ms': s['source_start_ms'],
                     'source_end_ms': s['source_end_ms'], 'published_text': s['published_text']}
                    for s in track['segments']]
        value = {'source_sha256': source['asset']['sha256'],
                 'source_duration_ms': source['asset']['duration_ms'],
                 'profile_id': track['profile_id'], 'profile': params,
                 'timeline_offset_ms': track['timeline_offset_ms'], 'segments': segments}
        cache_key = digest(value)
        # Paths and IDs are not synthesis inputs. They remain server-owned job metadata.
        return {**value, 'cache_key': cache_key, 'source_path': source['path'], 'track_id': track['id']}

    @staticmethod
    def take_key(snapshot, text):
        # Timing/source changes can reuse unchanged natural speech, but never a
        # different voice, instruction, model, seed, or algorithm.
        return digest({'profile': snapshot['profile'], 'published_text': text})

    def _touch(self, path):
        now = self.clock()
        os.utime(path, (now, now))

    @staticmethod
    def _remove(path):
        if path.is_symlink() or path.is_file():
            path.unlink(missing_ok=True)
        elif path.exists():
            shutil.rmtree(path)

    def _pin(self, path):
        self.pins[path] = self.pins.get(path, 0) + 1

    def _unpin(self, path):
        count = self.pins.get(path, 0)
        if count <= 1:
            self.pins.pop(path, None)
        else:
            self.pins[path] = count - 1

    def _entries(self):
        entries = []
        for root in (self.audio_root, self.takes_root):
            for path in root.iterdir():
                try:
                    entries.append((path, tree_bytes(path), path.lstat().st_mtime))
                except FileNotFoundError:
                    continue
        # Evict HLS as one coherent group: retaining a master while deleting a
        # referenced audio/video rendition would create a broken published URL.
        if any(self.hls_root.iterdir()):
            entries.append((self.hls_root, tree_bytes(self.hls_root), self.hls_root.stat().st_mtime))
        return entries

    def _protected(self, path):
        return bool(self.pins.get(path) or (path == self.hls_root and self.packaging))

    def _evict(self, path):
        if path == self.hls_root:
            for child in path.iterdir():
                self._remove(child)
        else:
            self._remove(path)

    def _cleanup_locked(self):
        now = self.clock()
        entries = self._entries()
        for path, _, accessed in entries:
            if now - accessed >= self.ttl_seconds and not self._protected(path):
                self._evict(path)
        entries = self._entries()
        size = sum(row[1] for row in entries) + tree_bytes(self.work_root)
        for path, length, _ in sorted(entries, key=lambda item: item[2]):
            if size <= self.max_bytes:
                break
            if not self._protected(path):
                self._evict(path)
                size -= length
        return size

    def _enforce_locked(self):
        if self._cleanup_locked() > self.max_bytes:
            raise CacheError('临时缓存空间不足；请清空缓存或缩短配音片段', 507, 'cache_quota')

    def _ready(self, cache_key):
        path = self.audio_root / cache_key
        try:
            marker = path / 'complete.json'
            audio = path / 'audio.m4a'
            if path.is_symlink() or marker.is_symlink() or audio.is_symlink():
                return None
            meta = json.loads(marker.read_text(encoding='utf-8'))
            if meta['cache_key'] != cache_key or audio.stat().st_size != meta['bytes'] or not audio.is_file():
                return None
            return path, meta
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def _view(self, job):
        return {k: deepcopy(job[k]) for k in ('id', 'track_id', 'cache_key', 'state', 'done', 'total', 'error', 'origin') if k in job}

    def status(self, project, registry):
        with self.lock:
            size = self._cleanup_locked()
            tracks = []
            for track in project['tracks']:
                if track['kind'] != 'generated':
                    continue
                snap = self.snapshot(project, registry, track)
                total = sum(bool(s['published_text'].strip()) for s in snap['segments'])
                cache_key = snap['cache_key']
                current = self.jobs.get(self.by_key.get(cache_key))
                ready = self._ready(cache_key)
                if ready:
                    state, done, error = 'ready', total, None
                elif current and current['state'] in ('queued', 'running', 'error'):
                    state, done, error = current['state'], current['done'], current.get('error')
                else:
                    state, done, error = 'missing', 0, None
                row = {'id': track['id'], 'cache_key': cache_key, 'state': state, 'done': done, 'total': total}
                if ready:
                    row['origin'] = ready[1]['origin']
                elif current and current.get('origin') and state in ('queued', 'running', 'error'):
                    row['origin'] = current['origin']
                if error:
                    row['error'] = error
                tracks.append(row)
            return {'tracks': tracks, 'bytes': size, 'max_bytes': self.max_bytes, 'ttl_seconds': self.ttl_seconds}

    def prepare(self, project, registry, track_id):
        track = next((t for t in project['tracks'] if t['id'] == track_id), None)
        if not track or track['kind'] != 'generated':
            raise ValueError('Select a generated track to prepare')
        if project['selected_track_id'] != track_id:
            raise ValueError('Only the explicitly selected generated track may be prepared')
        snap = self.snapshot(project, registry, track)
        cache_key = snap['cache_key']
        with self.lock:
            self._cleanup_locked()
            ready = self._ready(cache_key)
            current = self.jobs.get(self.by_key.get(cache_key))
            if current and current['state'] in ('queued', 'running'):
                return {'job': self._view(current), 'cache_hit': False}
            if ready:
                self._touch(ready[0])
                if not current or current['state'] != 'ready':
                    current = self._new_job(snap)
                    current.update(state='ready', done=current['total'], origin=ready[1]['origin'])
                return {'job': self._view(current), 'cache_hit': True}
            if sum(j['state'] in ('queued', 'running') for j in self.jobs.values()) >= 8:
                raise CacheError('配音任务队列已满', 429, 'queue_full')
            current = self._new_job(snap)
            # Snapshot was built from validated primitive values, detached from
            # project mutations and subsequent draft edits before queueing.
            self.executor.submit(self._generate, current['id'], deepcopy(snap))
            return {'job': self._view(current), 'cache_hit': False}

    def _new_job(self, snap):
        job = {'id': uuid.uuid4().hex, 'track_id': snap['track_id'], 'cache_key': snap['cache_key'],
               'state': 'queued', 'done': 0,
               'total': sum(bool(s['published_text'].strip()) for s in snap['segments'])}
        self.jobs[job['id']] = job
        self.by_key[snap['cache_key']] = job['id']
        while len(self.jobs) > 256:
            oldest = next((key for key, value in self.jobs.items() if value['state'] not in ('queued', 'running')), None)
            if oldest is None:
                break
            removed = self.jobs.pop(oldest)
            if self.by_key.get(removed['cache_key']) == oldest:
                self.by_key.pop(removed['cache_key'], None)
        return job

    def job(self, job_id):
        with self.lock:
            self._cleanup_locked()
            value = self.jobs.get(job_id)
            if not value:
                raise CacheError('Unknown cache job', 404)
            # An expired ready file is not a usable cache hit.
            if value['state'] == 'ready' and not self._ready(value['cache_key']):
                return {**self._view(value), 'state': 'missing', 'done': 0}
            return self._view(value)

    def _generate(self, job_id, snap):
        work = None
        failure = None
        take_paths = {self.takes_root / self.take_key(snap, s['published_text'])
                      for s in snap['segments'] if s['published_text'].strip()}
        destination = self.audio_root / snap['cache_key']
        with self.lock:
            job = self.jobs[job_id]
            job['state'] = 'running'
            self._pin(destination)
            for path in take_paths:
                self._pin(path)
        try:
            work = Path(tempfile.mkdtemp(prefix=job_id + '-', dir=self.work_root))
            result = work / 'result'
            result.mkdir()
            output = result / 'audio.m4a'

            def progress(done):
                with self.lock:
                    # Called only after a completed utterance (or verified extraction).
                    if not isinstance(done, int) or not job['done'] <= done <= job['total']:
                        raise ValueError('Invalid renderer progress')
                    job['done'] = done
                    self._enforce_locked()

            # A registered asset's identity must still describe its real bytes.
            if file_sha256(snap['source_path']) != snap['source_sha256']:
                raise ValueError('Source media changed since registration; register it again')
            with self.lock:
                job['origin'] = 'tts'
            self.renderer(snap, output, self.takes_root, work, progress)
            audio, duration, _ = inspect_stream(output, 'audio')
            if audio.get('codec_name') != 'aac' or abs(duration * 1000 - snap['source_duration_ms']) > 100:
                raise ValueError('Renderer did not produce a complete source-length AAC audio stream')
            meta = {'cache_key': snap['cache_key'], 'origin': job['origin'], 'bytes': output.stat().st_size,
                    'sha256': file_sha256(output), 'duration_ms': round(duration * 1000), 'created': self.clock()}
            (result / 'complete.json').write_text(json.dumps(meta), encoding='utf-8')
            with self.lock:
                # Rendering no longer needs takes. LRU may discard older takes
                # to make room for the final audio, preserving as many as fit.
                for path in take_paths:
                    self._unpin(path)
                take_paths.clear()
                self._enforce_locked()
                if destination.exists() or destination.is_symlink():
                    self._remove(destination)
                os.replace(result, destination)
                self._touch(destination)
                job['done'] = job['total']
        except Exception as exc:
            failure = str(exc)[-2000:] or type(exc).__name__
        finally:
            with self.lock:
                if work:
                    self._remove(work)
                for path in take_paths:
                    self._unpin(path)
                self._unpin(destination)
                self._cleanup_locked()
                if failure is None:
                    job['state'] = 'ready'
                else:
                    job.update(state='error', error=failure)

    def _render_tts(self, snap, output, takes_root, work, progress):
        config = {**snap, 'output': str(output), 'takes_root': str(takes_root), 'work': str(work)}
        config_path = work / 'request.json'
        config_path.write_text(json.dumps(config, ensure_ascii=False), encoding='utf-8')
        log_path = work / 'renderer.log'
        with log_path.open('wb') as log:
            process = subprocess.Popen([MLX_PYTHON, '-u', str(Path(__file__).with_name('tts_cache_worker.py')),
                                        '--request', str(config_path)], stdout=subprocess.PIPE, stderr=log,
                                       text=True, encoding='utf-8', errors='replace')
            # Model libraries may print diagnostics to stdout. Only this private
            # event prefix is interpreted as measured progress.
            # Bound stalled synthesis, not a long video that keeps making progress.
            timer = threading.Timer(3600, process.kill)
            timer.start()
            try:
                for line in process.stdout:
                    if line.startswith('DUB_PROGRESS '):
                        event = json.loads(line[len('DUB_PROGRESS '):])
                        progress(event['done'])
                        timer.cancel()
                        timer = threading.Timer(3600, process.kill)
                        timer.start()
                code = process.wait()
                if code:
                    with log_path.open('rb') as errors:
                        errors.seek(max(0, log_path.stat().st_size - 2000))
                        detail = errors.read().decode('utf-8', errors='replace')
                    raise ValueError('Real speech generation failed: ' + detail)
            except BaseException:
                process.kill()
                process.wait()
                raise
            finally:
                timer.cancel()
                process.stdout.close()

    @contextmanager
    def playback(self, project, registry):
        private, assets = deepcopy(project), dict(registry)
        pinned = []
        selected_folder = None
        with self.lock:
            self._cleanup_locked()
            resolved = []
            for track in private['tracks']:
                if track['kind'] != 'generated':
                    resolved.append(track)
                    continue
                snap = self.snapshot(private, assets, track)
                ready = self._ready(snap['cache_key'])
                if not ready:
                    if track['id'] == private['selected_track_id']:
                        raise CacheError('配音尚未生成', 409, 'generation_required', track['id'])
                    continue
                folder, meta = ready
                pinned.append(folder)
                if track['id'] == private['selected_track_id']:
                    selected_folder = folder
                asset_id = 'private-generated-' + snap['cache_key']
                assets[asset_id] = {'path': str(folder / 'audio.m4a'), 'asset': {
                    'id': asset_id, 'kind': 'audio', 'sha256': meta['sha256'], 'duration_ms': meta['duration_ms']}}
                resolved.append({k: v for k, v in track.items() if k not in ('profile_id', 'kind')} |
                                {'kind': 'external', 'asset_id': asset_id})
            private['tracks'] = resolved
            for folder in pinned:
                self._pin(folder)
                self._touch(folder)
            self.packaging += 1
        try:
            yield private, assets
            with self.lock:
                self._touch(self.hls_root)
                # Packager has finished reading inputs. Keep the selected track
                # pinned; other ready audio may be evicted to fit the HLS group.
                for folder in pinned[:]:
                    if folder != selected_folder:
                        self._unpin(folder)
                        pinned.remove(folder)
                self._enforce_locked()
        finally:
            with self.lock:
                self.packaging -= 1
                for path in pinned:
                    self._unpin(path)
                self._cleanup_locked()

    @contextmanager
    def media(self, packager, cache_key, filename):
        with self.lock:
            self._cleanup_locked()
            path = packager.media_path(cache_key, filename)
            if path:
                self._pin(self.hls_root)
                self._touch(self.hls_root)
            self._cleanup_locked()
        try:
            yield path
        finally:
            if path:
                with self.lock:
                    self._unpin(self.hls_root)
                    self._cleanup_locked()

    def clear(self):
        with self.lock:
            if self.packaging or self.pins or any(j['state'] in ('queued', 'running') for j in self.jobs.values()):
                raise CacheError('缓存正在生成或读取；请暂停播放后重试', 409, 'cache_busy')
            before = tree_bytes(self.root) + tree_bytes(self.hls_root)
            for root in (self.audio_root, self.takes_root, self.work_root, self.hls_root):
                for path in root.iterdir():
                    self._remove(path)
            self.jobs.clear()
            self.by_key.clear()
            return {'removed_bytes': before}

    def close(self):
        self.executor.shutdown(wait=True)
