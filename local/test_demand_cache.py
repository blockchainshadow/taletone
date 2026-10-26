"""Behavioral demand-cache tests, without downloading/loading a neural model.

Run after integration: python -m unittest -v test_demand_cache test_server.
Fixtures are real independently encoded PCM/AAC/H.264 media in temporary folders.
The deterministic renderer is a controlled audio producer, not an HTTP echo/mock;
production take reuse also runs the real renderer subprocess with cached PCM takes.
Actual neural inference is a separate short runtime smoke, not an accuracy claim.
"""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import http.client
import json
import math
import os
from pathlib import Path
import struct
import subprocess
import tempfile
import threading
import time
import unittest
import wave

from demand_cache import CacheController, file_sha256, tree_bytes
from server import Store, create_server
from voice_profiles import MLX_PYTHON


def ffmpeg(*args):
    return subprocess.run(['ffmpeg', '-v', 'error', '-nostdin', '-y', *map(str, args)],
                          capture_output=True, check=True, timeout=90).stdout


def pcm_take(path, frequency, seconds=.35):
    with wave.open(str(path), 'wb') as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(24000)
        wav.writeframes(b''.join(struct.pack('<h', round(9000 * math.sin(2 * math.pi * frequency * n / 24000)))
                                 for n in range(round(seconds * 24000))))


def audio_values(path, start=0, duration=.25):
    raw = ffmpeg('-ss', start, '-i', path, '-t', duration, '-vn', '-ar', '24000', '-ac', '1', '-f', 's16le', '-')
    return struct.unpack('<' + 'h' * (len(raw) // 2), raw)


def energy_at(values, frequency):
    return abs(sum(value * complex(math.cos(2 * math.pi * frequency * n / 24000),
                                   math.sin(2 * math.pi * frequency * n / 24000))
                   for n, value in enumerate(values)))


class ToneRenderer:
    """Audio fixture producer with controllable scheduling/failure, no network/model."""
    def __init__(self):
        self.calls = 0
        self.started, self.release = threading.Event(), threading.Event()
        self.block = False
        self.fail = False
        self.truncated = False
        self.snapshots = []

    def __call__(self, snap, output, takes_root, work, progress):
        self.calls += 1
        self.snapshots.append(deepcopy(snap))
        self.started.set()
        if self.block and not self.release.wait(15):
            raise RuntimeError('Fixture release timed out')
        if self.fail:
            output.write_bytes(b'partial, unpublished data')
            raise RuntimeError('Controlled synthesis failure')
        duration = .2 if self.truncated else snap['source_duration_ms'] / 1000
        words = ''.join(s['published_text'] for s in snap['segments'])
        frequency = 440 if words == 'こんにちは。' else 880
        ffmpeg('-f', 'lavfi', '-i', f'sine=frequency={frequency}:sample_rate=24000:duration={duration}',
               '-c:a', 'aac', '-b:a', '32k', output)
        total = sum(bool(s['published_text'].strip()) for s in snap['segments'])
        for index in range(total):
            progress(index + 1)


class DemandCacheBehavior(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.media_temp = tempfile.TemporaryDirectory()
        cls.source = Path(cls.media_temp.name) / 'source.mp4'
        ffmpeg('-f', 'lavfi', '-i', 'color=c=blue:s=160x90:r=24:d=4',
               '-f', 'lavfi', '-i', 'sine=frequency=330:sample_rate=48000:duration=4',
               '-map', '0:v', '-map', '1:a', '-c:v', 'libx264', '-preset', 'ultrafast',
               '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-t', '4', cls.source)

    @classmethod
    def tearDownClass(cls):
        cls.media_temp.cleanup()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.data = Path(self.temp.name)
        self.renderer = ToneRenderer()
        self.now = time.time()
        self.store = Store(self.data, source=self.source,
                           cache_options={'renderer': self.renderer, 'clock': lambda: self.now})
        self.project = self.store.project()
        self.project['source_sha256'] = self.store.registry['source']['asset']['sha256']
        self.project['tracks'].append({'id': 'dub', 'label': '日本語', 'language': 'ja-JP', 'kind': 'generated',
                                      'revision': 1, 'profile_id': 'ja-playful-v1', 'timeline_offset_ms': 0,
                                      'segments': [{'id': 'one', 'source_start_ms': 0, 'source_end_ms': 3900,
                                                    'published_text': 'こんにちは。', 'draft_text': 'こんにちは。'}]})
        self.project['selected_track_id'] = 'dub'
        self.store.save(self.project)
        self.server = create_server(0, self.store)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.renderer.release.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.store.cache.close()
        self.temp.cleanup()

    def request(self, method, path, value=None, headers=None):
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=90)
        body = json.dumps(value).encode() if value is not None else None
        connection.request(method, path, body, headers or {})
        response = connection.getresponse()
        result = response.status, response.read()
        connection.close()
        return result

    def status(self, project=None):
        status, body = self.request('POST', '/api/cache/status', {'project': project or self.project})
        self.assertEqual(status, 200, body)
        return json.loads(body)

    def prepare(self, project=None):
        project = project or self.project
        status, body = self.request('POST', '/api/cache/prepare', {'project': project, 'track_id': project['selected_track_id']})
        self.assertIn(status, (200, 202), body)
        return status, json.loads(body)

    def finish(self, job):
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            status, body = self.request('GET', '/api/cache/jobs/' + job['id'])
            self.assertEqual(status, 200, body)
            current = json.loads(body)['job']
            if current['state'] not in ('queued', 'running'):
                return current
            time.sleep(.02)
        self.fail('Real asynchronous cache job did not settle')

    def ready(self, project=None):
        _, result = self.prepare(project)
        job = self.finish(result['job'])
        self.assertEqual(job['state'], 'ready', job)
        return job

    def test_fresh_checkout_requires_user_source_and_has_only_empty_original(self):
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaisesRegex(ValueError, '--source'):
                Store(folder)
            store = Store(folder, source=self.source)
            try:
                project = store.project()
                self.assertEqual(set(store.registry), {'source'})
                self.assertEqual(project['source_asset_id'], 'source')
                self.assertEqual(project['source_sha256'], file_sha256(self.source))
                self.assertEqual(project['selected_track_id'], 'original')
                self.assertEqual(len(project['tracks']), 1)
                self.assertEqual(project['tracks'][0]['kind'], 'embedded')
                self.assertEqual(project['tracks'][0]['segments'], [])
                self.assertEqual(store.cache_status(project)['tracks'], [])
                self.assertEqual(Path(store.registry['source']['path']), self.source.resolve())
                self.assertEqual(list((Path(folder) / 'assets').iterdir()), [])
            finally:
                store.cache.close()

    def test_existing_project_rejects_another_explicit_source(self):
        with tempfile.TemporaryDirectory() as folder:
            alternate = Path(folder) / 'another.mp4'
            alternate.write_bytes(self.source.read_bytes())
            with self.assertRaisesRegex(ValueError, 'another source'):
                Store(self.data, source=alternate)

    def test_status_and_original_playback_never_generate_missing_languages(self):
        current = self.status()
        self.assertEqual(current['tracks'][0]['state'], 'missing')
        self.assertEqual(current['tracks'][0]['done'], 0)
        self.assertEqual(current['tracks'][0]['total'], 1)
        self.assertEqual(current['max_bytes'], 256 * 1024 * 1024)
        self.assertEqual(current['ttl_seconds'], 86400)
        self.assertEqual(self.renderer.calls, 0)
        status, body = self.request('POST', '/api/playback', {'project': self.project})
        self.assertEqual(status, 409)
        self.assertEqual(json.loads(body), {'error': '配音尚未生成', 'code': 'generation_required', 'track_id': 'dub'})
        original = deepcopy(self.project)
        original['selected_track_id'] = 'original'
        status, body = self.request('POST', '/api/playback', {'project': original})
        self.assertEqual(status, 200, body)
        self.assertEqual([t['id'] for t in json.loads(body)['tracks']], ['original'])
        self.assertEqual(self.renderer.calls, 0)
        status, _ = self.request('POST', '/api/cache/prepare', {'project': original, 'track_id': 'dub'})
        self.assertEqual(status, 400)

    def test_miss_then_real_media_hit_without_permanent_registration(self):
        status, result = self.prepare()
        self.assertEqual(status, 202)
        self.assertFalse(result['cache_hit'])
        job = self.finish(result['job'])
        self.assertEqual(job['state'], 'ready', job)
        self.assertEqual((job['done'], job['total'], job['origin']), (1, 1, 'tts'))
        status, repeated = self.prepare()
        self.assertEqual(status, 200)
        self.assertTrue(repeated['cache_hit'])
        self.assertEqual(repeated['job']['cache_key'], job['cache_key'])
        self.assertEqual(self.renderer.calls, 1)
        cache_status = self.status()
        self.assertEqual(cache_status['tracks'][0]['origin'], 'tts')
        self.assertGreater(cache_status['bytes'], 0)
        audio = self.store.cache.audio_root / job['cache_key'] / 'audio.m4a'
        values = audio_values(audio)
        self.assertGreater(energy_at(values, 440), 20 * energy_at(values, 330))
        status, body = self.request('POST', '/api/playback', {'project': self.project})
        self.assertEqual(status, 200, body)
        descriptor = json.loads(body)
        self.assertEqual([t['id'] for t in descriptor['tracks']], ['dub'])
        self.assertEqual(descriptor['tracks'][0]['audio_index'], 0)
        self.assertGreater(descriptor['video_bytes'], 0)
        master_status, master_body = self.request('GET', descriptor['manifest_url'])
        self.assertEqual(master_status, 200)
        media_lines = [line for line in master_body.decode().splitlines() if line.startswith('#EXT-X-MEDIA:')]
        self.assertEqual(len(media_lines), 1)
        self.assertIn('DEFAULT=YES,AUTOSELECT=YES', media_lines[0])
        uri = media_lines[0].split('URI="', 1)[1].split('"', 1)[0]
        rendition = self.data / uri.lstrip('/')
        def audio_hash(path):
            return ffmpeg('-i', path, '-map', '0:a:0', '-c:a', 'copy',
                          '-f', 'hash', '-hash', 'sha256', '-')
        self.assertEqual(audio_hash(rendition), audio_hash(audio))
        streams = json.loads(subprocess.check_output([
            'ffprobe', '-v', 'error', '-show_streams', '-of', 'json', str(rendition)]))['streams']
        self.assertEqual(streams[0]['sample_rate'], '24000')
        self.assertEqual(streams[0]['channels'], 1)
        self.assertEqual(set(self.store.registry), {'source'})
        assets = json.loads(self.request('GET', '/api/assets')[1])['assets']
        self.assertEqual([a['id'] for a in assets], ['source'])
        self.assertEqual(self.request('GET', '/media/private-generated-' + job['cache_key'])[0], 404)
        self.assertEqual(self.request('GET', '/cache/audio/' + job['cache_key'] + '/audio.m4a')[0], 404)

    def test_concurrent_identical_requests_join_one_measured_job(self):
        self.renderer.block = True
        with ThreadPoolExecutor(max_workers=8) as callers:
            results = list(callers.map(lambda _: self.prepare()[1], range(12)))
        self.assertTrue(self.renderer.started.wait(5))
        self.assertEqual(len({r['job']['id'] for r in results}), 1)
        self.assertEqual(self.renderer.calls, 1)
        current = json.loads(self.request('GET', '/api/cache/jobs/' + results[0]['job']['id'])[1])['job']
        self.assertEqual((current['state'], current['done']), ('running', 0))
        self.assertEqual(self.request('DELETE', '/api/cache')[0], 409)
        self.renderer.release.set()
        self.assertEqual(self.finish(results[0]['job'])['done'], 1)

    def test_distinct_jobs_have_one_worker_and_a_bounded_queue(self):
        self.renderer.block = True
        jobs = []
        for index in range(8):
            project = deepcopy(self.project)
            project['tracks'][1]['segments'][0]['published_text'] = f'文章{index}。'
            jobs.append(self.prepare(project)[1]['job'])
        self.assertTrue(self.renderer.started.wait(5))
        self.assertEqual(self.renderer.calls, 1)
        extra = deepcopy(self.project)
        extra['tracks'][1]['segments'][0]['published_text'] = '列からあふれる文章。'
        status, body = self.request('POST', '/api/cache/prepare', {'project': extra, 'track_id': 'dub'})
        self.assertEqual(status, 429, body)
        self.assertEqual(json.loads(body)['code'], 'queue_full')
        self.renderer.release.set()
        for job in jobs:
            self.assertEqual(self.finish(job)['state'], 'ready')
        self.assertEqual(self.renderer.calls, 8)

    def test_key_tracks_published_inputs_not_drafts_labels_or_selection(self):
        initial = self.status()['tracks'][0]['cache_key']
        unchanged = deepcopy(self.project)
        unchanged['title'] = 'new title'
        unchanged['selected_track_id'] = 'original'
        unchanged['tracks'][1]['label'] = 'new label'
        unchanged['tracks'][1]['segments'][0]['draft_text'] = 'unpublished changes'
        self.assertEqual(self.status(unchanged)['tracks'][0]['cache_key'], initial)
        variants = []
        changed = deepcopy(self.project); changed['tracks'][1]['segments'][0]['published_text'] = '別の言葉。'; variants.append(changed)
        changed = deepcopy(self.project); changed['tracks'][1]['segments'][0]['source_end_ms'] = 3800; variants.append(changed)
        changed = deepcopy(self.project); changed['tracks'][1]['timeline_offset_ms'] = 10; variants.append(changed)
        changed = deepcopy(self.project); changed['tracks'][1].update(profile_id='zh-beijing-v1', language='zh-CN'); variants.append(changed)
        changed = deepcopy(self.project); changed['tracks'][1].update(profile_id='zh-chengdu-v1', language='zh-CN'); variants.append(changed)
        for changed in variants:
            self.assertNotEqual(self.status(changed)['tracks'][0]['cache_key'], initial)
        self.assertEqual(self.renderer.calls, 0)
        # Source identity is content, not just a display label/asset identifier.
        other = self.data / 'other-source.mp4'
        other.write_bytes(self.source.read_bytes() + b'\0')
        self.store.register(other, 'video', 'other-source')
        changed = deepcopy(self.project); changed['source_asset_id'] = 'other-source'
        changed['source_sha256'] = self.store.registry['other-source']['asset']['sha256']
        self.assertNotEqual(self.status(changed)['tracks'][0]['cache_key'], initial)

    def test_queued_job_uses_an_immutable_published_snapshot(self):
        self.renderer.block = True
        result = self.store.cache_prepare(self.project, 'dub')
        self.assertTrue(self.renderer.started.wait(5))
        self.project['tracks'][1]['segments'][0]['published_text'] = '変更された文章。'
        self.renderer.release.set()
        job = self.finish(result['job'])
        self.assertEqual(job['state'], 'ready', job)
        audio = self.store.cache.audio_root / job['cache_key'] / 'audio.m4a'
        values = audio_values(audio)
        self.assertGreater(energy_at(values, 440), 20 * energy_at(values, 880))
        self.assertEqual(self.status()['tracks'][0]['state'], 'missing')

    def test_failure_and_truncated_audio_never_publish_partial_results(self):
        for truncated in (False, True):
            self.renderer.fail = not truncated
            self.renderer.truncated = truncated
            _, result = self.prepare()
            job = self.finish(result['job'])
            self.assertEqual(job['state'], 'error', job)
            self.assertIn('error', job)
            self.assertEqual(self.status()['tracks'][0]['state'], 'error')
            self.assertEqual(self.request('POST', '/api/playback', {'project': self.project})[0], 409)
            self.assertFalse((self.store.cache.audio_root / job['cache_key']).exists())
            self.assertEqual(list(self.store.cache.work_root.iterdir()), [])
        self.renderer.fail = self.renderer.truncated = False
        job = self.ready()
        self.assertEqual(job['state'], 'ready')

    def test_lazy_ttl_and_lru_quota_remove_only_temporary_media(self):
        first = self.ready()
        first_size = self.status()['bytes']
        self.assertGreater(first_size, 1000)
        self.now += 1
        self.store.cache.max_bytes = round(first_size * 1.5)
        changed = deepcopy(self.project)
        changed['tracks'][1]['segments'][0]['published_text'] = '別の文章。'
        self.ready(changed)
        self.assertEqual(self.status()['tracks'][0]['state'], 'missing')
        self.assertEqual(self.status(changed)['tracks'][0]['state'], 'ready')
        self.assertLessEqual(self.status(changed)['bytes'], self.store.cache.max_bytes)
        self.now += 86401
        expired = self.status(changed)
        self.assertEqual((expired['tracks'][0]['state'], expired['bytes']), ('missing', 0))
        self.assertEqual(file_sha256(self.source), self.store.registry['source']['asset']['sha256'])
        self.assertTrue(self.source.exists())
        self.assertFalse((self.store.cache.audio_root / first['cache_key']).exists())

    def test_quota_failure_does_not_publish_audio(self):
        self.store.cache.max_bytes = 100
        _, result = self.prepare()
        job = self.finish(result['job'])
        self.assertEqual(job['state'], 'error', job)
        self.assertEqual(self.status()['bytes'], 0)
        self.assertFalse((self.store.cache.audio_root / job['cache_key']).exists())

    def test_clear_hls_audio_and_takes_without_restarting_or_deleting_assets(self):
        self.ready()
        status, body = self.request('POST', '/api/playback', {'project': self.project})
        self.assertEqual(status, 200, body)
        descriptor = json.loads(body)
        source_before = file_sha256(self.source)
        self.assertGreater(tree_bytes(self.store.hls.root), 0)
        status, body = self.request('DELETE', '/api/cache')
        self.assertEqual(status, 200, body)
        self.assertGreater(json.loads(body)['removed_bytes'], 0)
        self.assertEqual(self.status()['bytes'], 0)
        self.assertEqual(self.request('GET', descriptor['manifest_url'])[0], 404)
        self.assertEqual(self.request('GET', '/media/source')[0], 200)
        self.assertEqual(file_sha256(self.source), source_before)
        self.assertEqual(self.request('POST', '/api/playback', {'project': self.project})[0], 409)
        self.ready()
        self.assertEqual(self.renderer.calls, 2)

    def test_read_lease_protects_media_from_clear_ttl_and_quota(self):
        original = deepcopy(self.project); original['selected_track_id'] = 'original'
        status, body = self.request('POST', '/api/playback', {'project': original})
        self.assertEqual(status, 200, body)
        parts = json.loads(body)['manifest_url'].split('/')
        with self.store.cache.media(self.store.hls, parts[2], parts[3]) as path:
            self.assertIsNotNone(path)
            self.now += 86401
            self.store.cache.max_bytes = 1
            self.status()
            self.assertTrue(path.exists())
            self.assertEqual(self.request('DELETE', '/api/cache')[0], 409)
        self.assertFalse(path.exists())
        self.assertEqual(self.request('DELETE', '/api/cache')[0], 200)

    def test_cache_symlinks_are_not_served_or_followed_by_clear(self):
        outside = self.data / 'outside-managed-cache'
        outside.mkdir()
        protected = outside / 'init.mp4'
        protected.write_bytes(b'not temporary cache media')
        (outside / '.complete.json').write_text(json.dumps(['init.mp4']))
        bad_key = 'v-' + 'a' * 64
        (self.store.hls.root / bad_key).symlink_to(outside, target_is_directory=True)
        (self.store.cache.takes_root / ('b' * 64)).symlink_to(outside, target_is_directory=True)
        self.assertEqual(self.request('GET', '/hls/' + bad_key + '/init.mp4')[0], 404)
        self.assertEqual(self.request('DELETE', '/api/cache')[0], 200)
        self.assertEqual(protected.read_bytes(), b'not temporary cache media')
        self.assertTrue((outside / '.complete.json').exists())

    def test_generated_validation_and_origin_path_security(self):
        cases = []
        changed = deepcopy(self.project); changed.pop('source_sha256'); cases.append(changed)
        changed = deepcopy(self.project); changed['source_sha256'] = '0' * 64; cases.append(changed)
        changed = deepcopy(self.project); changed['source_sha256'] = '/etc/passwd'; cases.append(changed)
        for field, value in [('asset_id', '/etc/passwd'), ('path', '/etc/passwd'),
                             ('audio_stream_index', 0), ('profile_id', '../other'), ('profile_id', []),
                             ('language', 'en'), ('revision', 2), ('timeline_offset_ms', True), ('segments', None)]:
            changed = deepcopy(self.project); changed['tracks'][1][field] = value; cases.append(changed)
        changed = deepcopy(self.project); changed['tracks'][1]['segments'][0]['source_end_ms'] = 4001; cases.append(changed)
        changed = deepcopy(self.project); changed['tracks'][1]['segments'].append({**changed['tracks'][1]['segments'][0], 'id': 'overlap'}); cases.append(changed)
        for project in cases:
            status, body = self.request('POST', '/api/cache/status', {'project': project})
            self.assertEqual(status, 400, body)
        for method, path, payload in [('POST', '/api/cache/status', {'project': self.project}),
                                      ('POST', '/api/cache/prepare', {'project': self.project, 'track_id': 'dub'}),
                                      ('DELETE', '/api/cache', None)]:
            self.assertEqual(self.request(method, path, payload, {'Origin': 'https://evil.example'})[0], 403)
        for path in ['/api/cache/jobs/../../etc/passwd', '/api/cache/jobs/missing', '/hls/../audio.m4a',
                     '/data/cache/audio/private/audio.m4a', '/media/private-generated-anything']:
            self.assertEqual(self.request('GET', path)[0], 404)
        self.assertEqual(self.renderer.calls, 0)
        self.assertEqual(self.store.project(), self.project)

    def test_generated_track_always_uses_explicit_renderer(self):
        self.ready()
        self.assertEqual(self.renderer.calls, 1)
        self.assertEqual(self.status()['tracks'][0]['origin'], 'tts')

    @unittest.skipUnless(os.environ.get('TALETONE_TEST_MLX') == '1' and Path(MLX_PYTHON).is_file(),
                         'Set TALETONE_TEST_MLX=1 with the optional MLX environment installed')
    def test_production_worker_reuses_unchanged_real_pcm_takes_after_text_edit(self):
        self.store.cache.renderer = self.store.cache._render_tts
        self.project['tracks'][1]['segments'] = [
            {'id': 'one', 'source_start_ms': 0, 'source_end_ms': 1900, 'published_text': '一。', 'draft_text': '一。'},
            {'id': 'two', 'source_start_ms': 2000, 'source_end_ms': 3900, 'published_text': '二。', 'draft_text': '二。'}]

        def seed(text, frequency):
            snap = self.store.cache.snapshot(self.project, self.store.registry, self.project['tracks'][1])
            key = CacheController.take_key(snap, text)
            folder = self.store.cache.takes_root / key
            folder.mkdir()
            pcm_take(folder / 'take.wav', frequency)
            checksum = file_sha256(folder / 'take.wav')
            (folder / 'complete.json').write_text(json.dumps({'key': key, 'sha256': checksum}))
            return folder, checksum

        first_take, checksum = seed('一。', 440)
        seed('二。', 660)
        initial = self.ready()
        first_audio = self.store.cache.audio_root / initial['cache_key'] / 'audio.m4a'
        values = audio_values(first_audio, 2.02, .2)
        self.assertGreater(energy_at(values, 660), 10 * energy_at(values, 880))
        self.project['tracks'][1]['segments'][1].update(published_text='三。', draft_text='三。')
        seed('三。', 880)
        changed = self.ready()
        self.assertNotEqual(initial['cache_key'], changed['cache_key'])
        self.assertEqual(file_sha256(first_take / 'take.wav'), checksum)
        self.assertEqual(len(list(self.store.cache.takes_root.iterdir())), 3)
        second_audio = self.store.cache.audio_root / changed['cache_key'] / 'audio.m4a'
        values = audio_values(second_audio, .02, .2)
        self.assertGreater(energy_at(values, 440), 10 * energy_at(values, 880))
        values = audio_values(second_audio, 2.02, .2)
        self.assertGreater(energy_at(values, 880), 10 * energy_at(values, 660))
        self.assertEqual(list(self.store.cache.work_root.iterdir()), [])



if __name__ == '__main__':
    unittest.main()
