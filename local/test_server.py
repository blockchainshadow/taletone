"""Behavioral API tests; run python -m unittest -v test_server after integration.

Fixtures are generated in temporary directories. No user media is read or modified.
ffprobe must be on PATH, as required by the application's upload validation.
"""
import copy
import hashlib
import http.client
import io
import json
import array
from pathlib import Path
import tempfile
import re
import threading
import unittest
from urllib.parse import quote
import wave
import math
import struct
import subprocess
import sys

from server import Store, create_server


def wav_bytes():
    output = io.BytesIO()
    with wave.open(output, 'wb') as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(8000)
        wav.writeframes(b'\x00\x00' * 8000)
    return output.getvalue()


class ServerBehavior(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.data = Path(self.temp.name)
        self.audio = wav_bytes()
        media = self.data / '只读测试音频.wav'
        media.write_bytes(self.audio)
        # The video registry fixture tests project references, not video decoding.
        # Media GET/Range and upload use the real, generated PCM WAV above.
        registry = {
            'video-fixture': {'path': str(media), 'asset': {'id': 'video-fixture', 'name': 'video registry fixture', 'kind': 'video', 'url': '/media/video-fixture', 'duration_ms': 1000, 'mime': 'video/mp4', 'sha256': hashlib.sha256(self.audio).hexdigest(), 'width': 1280, 'height': 720}},
            'audio-fixture': {'path': str(media), 'asset': {'id': 'audio-fixture', 'name': '音频.wav', 'kind': 'audio', 'url': '/media/audio-fixture', 'duration_ms': 1000, 'mime': 'audio/wav', 'sha256': hashlib.sha256(self.audio).hexdigest()}}
        }
        self.project = {'schema_version': 1, 'project_id': 'test', 'title': '测试项目', 'source_asset_id': 'video-fixture', 'selected_track_id': 'dub', 'tracks': [
            {'id': 'original', 'label': '原声', 'language': 'en', 'kind': 'embedded', 'revision': 1, 'audio_stream_index': 0, 'segments': []},
            {'id': 'dub', 'label': '中文', 'language': 'zh', 'kind': 'external', 'revision': 1, 'asset_id': 'audio-fixture', 'timeline_offset_ms': 0, 'segments': [{'id': '1', 'source_start_ms': 0, 'source_end_ms': 1000, 'published_text': '发布', 'draft_text': '发布'}]}
        ]}
        (self.data / 'assets.json').write_text(json.dumps(registry), encoding='utf-8')
        (self.data / 'project.json').write_text(json.dumps(self.project), encoding='utf-8')
        self.store = Store(self.data)
        self.server = create_server(0, self.store)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.temp.cleanup()

    def request(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=120)
        conn.request(method, path, body=body, headers=headers or {})
        response = conn.getresponse()
        result = response.status, dict(response.getheaders()), response.read()
        conn.close()
        return result

    def save(self, project, headers=None):
        return self.request('PUT', '/api/project', json.dumps({'project': project}).encode(), {'Content-Type': 'application/json', **(headers or {})})

    def test_save_and_reopen_preserves_draft_and_published_media(self):
        project = copy.deepcopy(self.project)
        project['tracks'][1]['label'] = '成都 <script>仅为文字</script>'
        project['tracks'][1]['segments'][0]['draft_text'] = '修改的草稿'
        self.assertEqual(self.save(project)[0], 200)
        reloaded = Store(self.data).project()
        self.assertEqual(reloaded, project)
        self.assertEqual(reloaded['tracks'][1]['segments'][0]['published_text'], '发布')
        self.assertEqual(reloaded['tracks'][1]['revision'], 1)
        self.assertEqual(json.loads(self.request('GET', '/api/project')[2])['project'], project)
        self.assertEqual(self.request('GET', '/media/audio-fixture')[2], self.audio)

    def test_invalid_projects_are_rejected_without_changing_saved_state(self):
        cases = []
        p = copy.deepcopy(self.project); p['tracks'][1]['asset_id'] = '/etc/passwd'; cases.append(p)
        p = copy.deepcopy(self.project); p['tracks'][1]['asset_id'] = 'video-fixture'; cases.append(p)
        p = copy.deepcopy(self.project); p['source_asset_id'] = 'audio-fixture'; cases.append(p)
        p = copy.deepcopy(self.project); p['tracks'][1]['id'] = 'original'; cases.append(p)
        p = copy.deepcopy(self.project); p['tracks'] = p['tracks'][1:]; cases.append(p)
        p = copy.deepcopy(self.project); p['selected_track_id'] = 'missing'; cases.append(p)
        p = copy.deepcopy(self.project); p['schema_version'] = 2; cases.append(p)
        p = copy.deepcopy(self.project); p['path'] = '/etc/passwd'; cases.append(p)
        p = copy.deepcopy(self.project); p['tracks'][1]['timeline_offset_ms'] = 600001; cases.append(p)
        p = copy.deepcopy(self.project); p['tracks'][1]['timeline_offset_ms'] = True; cases.append(p)
        p = copy.deepcopy(self.project); p['tracks'][1]['segments'] *= 2; cases.append(p)
        p = copy.deepcopy(self.project); p['tracks'][1]['segments'][0]['source_end_ms'] = 0; cases.append(p)
        for project in cases:
            with self.subTest(project=project):
                status, _, body = self.save(project)
                self.assertEqual(status, 400)
                self.assertIn('error', json.loads(body))
        self.assertEqual(self.store.project(), self.project)
        self.assertEqual(self.request('PUT', '/api/project', b'{broken')[0], 400)

    def test_ranges_and_head(self):
        for range_header, start, end in [('bytes=0-9', 0, 9), ('bytes=10-', 10, len(self.audio) - 1), ('bytes=-12', len(self.audio) - 12, len(self.audio) - 1), ('bytes=0-999999', 0, len(self.audio) - 1)]:
            status, headers, body = self.request('GET', '/media/audio-fixture', headers={'Range': range_header})
            self.assertEqual(status, 206)
            self.assertEqual(body, self.audio[start:end + 1])
            self.assertEqual(headers['Content-Range'], f'bytes {start}-{end}/{len(self.audio)}')
            head_status, head_headers, head_body = self.request('HEAD', '/media/audio-fixture', headers={'Range': range_header})
            self.assertEqual(head_status, 206)
            self.assertEqual(head_body, b'')
            self.assertEqual(head_headers['Content-Length'], str(end - start + 1))
        for invalid in ['bytes=999999-', 'bytes=5-2', 'bytes=-0', 'bytes=0-1,3-4', 'bananas', 'bytes=-']:
            status, headers, body = self.request('GET', '/media/audio-fixture', headers={'Range': invalid})
            self.assertEqual(status, 416)
            self.assertEqual(headers['Content-Range'], f'bytes */{len(self.audio)}')
            self.assertEqual(body, b'')
        self.assertEqual(self.request('HEAD', '/media/audio-fixture')[1]['Content-Length'], str(len(self.audio)))

    def test_upload_is_validated_persistent_and_playable(self):
        status, _, body = self.request('POST', '/api/assets', self.audio, {'X-Filename': quote('中文 配音.wav'), 'Content-Type': 'audio/wav'})
        self.assertEqual(status, 201, body)
        asset = json.loads(body)['asset']
        self.assertEqual(asset['name'], '中文 配音.wav')
        self.assertEqual(asset['kind'], 'audio')
        self.assertEqual(asset['mime'], 'audio/wav')
        self.assertEqual(asset['duration_ms'], 1000)
        self.assertEqual(asset['sha256'], hashlib.sha256(self.audio).hexdigest())
        self.assertEqual(self.request('GET', asset['url'])[2], self.audio)
        self.assertEqual(self.request('GET', asset['url'], headers={'Range': 'bytes=0-43'})[2], self.audio[:44])
        project = copy.deepcopy(self.project)
        project['tracks'][1]['asset_id'] = asset['id']
        self.assertEqual(self.save(project)[0], 200)
        reopened = Store(self.data)
        self.assertEqual(reopened.project()['tracks'][1]['asset_id'], asset['id'])
        self.assertEqual(Path(reopened.registry[asset['id']]['path']).read_bytes(), self.audio)
        self.assertIn(asset, json.loads(self.request('GET', '/api/assets')[2])['assets'])

    def test_invalid_upload_cleanup_and_write_origin(self):
        before = set((self.data / 'assets').iterdir())
        status, _, body = self.request('POST', '/api/assets', b'not an audio file', {'X-Filename': quote('假音频.mp3'), 'Content-Type': 'audio/mpeg'})
        self.assertEqual(status, 400)
        self.assertIn('error', json.loads(body))
        self.assertEqual(set((self.data / 'assets').iterdir()), before)
        self.assertEqual(len(self.store.registry), 2)
        self.assertEqual(self.save(self.project, {'Origin': 'https://evil.example'})[0], 403)
        self.assertEqual(self.request('POST', '/api/assets', self.audio, {'Origin': 'null'})[0], 403)
        origin = f'http://127.0.0.1:{self.server.server_port}'
        self.assertEqual(self.save(self.project, {'Origin': origin})[0], 200)
        self.assertEqual(self.request('POST', '/api/assets', b'x', {'Content-Length': str(512 * 1024 * 1024 + 1)})[0], 400)

    def test_static_and_media_paths_are_allowlisted(self):
        for path in ['/../server.py', '/data/project.json', '/server.py', '/media/../../etc/passwd', '/media/missing', '/%2e%2e/server.py', '/vendor/../server.py', '/vendor/hls.min.js.map']:
            self.assertEqual(self.request('GET', path)[0], 404)
        for path in ['/vendor/hls.min.js', '/vendor/hls.LICENSE']:
            status, _, body = self.request('GET', path)
            self.assertEqual(status, 200)
            self.assertGreater(len(body), 0)

    def test_invalid_playback_projects_never_prepare_media(self):
        before = set((self.data / 'hls').iterdir())
        cases = []
        p = copy.deepcopy(self.project); p['source_asset_id'] = '/etc/passwd'; cases.append({'project': p})
        p = copy.deepcopy(self.project); p['tracks'][1]['asset_id'] = '../private'; cases.append({'project': p})
        p = copy.deepcopy(self.project); p['tracks'][1]['path'] = '/etc/passwd'; cases.append({'project': p})
        p = copy.deepcopy(self.project); p['selected_track_id'] = 'unknown'; cases.append({'project': p})
        p = copy.deepcopy(self.project); p['tracks'][0]['audio_stream_index'] = 1; cases.append({'project': p})
        p = copy.deepcopy(self.project); p['tracks'][1]['label'] = '\ud800'; cases.append({'project': p})
        cases.extend([{'source_asset_id': 'video-fixture', 'audio_asset_id': 'audio-fixture', 'timeline_offset_ms': 0}, {}, {'project': []}])
        for payload in cases:
            with self.subTest(payload=payload):
                status, _, body = self.request('POST', '/api/playback', json.dumps(payload).encode())
                self.assertEqual(status, 400, body)
        self.assertEqual(set((self.data / 'hls').iterdir()), before)
        self.assertEqual(self.store.project(), self.project)

    def test_legacy_playback_registry_artifacts_are_not_source_assets(self):
        self.store.registry['playback-obsolete'] = copy.deepcopy(self.store.registry['video-fixture'])
        self.assertEqual(len(json.loads(self.request('GET', '/api/assets')[2])['assets']), 2)
        self.assertEqual(self.request('GET', '/media/playback-obsolete')[0], 404)
        project = copy.deepcopy(self.project)
        project['source_asset_id'] = 'playback-obsolete'
        self.assertEqual(self.save(project)[0], 400)
        self.assertEqual(self.request('POST', '/api/playback', json.dumps({'project': project}).encode())[0], 400)

    def test_untrusted_host_cannot_read_registered_private_media(self):
        for path in ['/api/assets', '/api/project', '/media/audio-fixture']:
            status, _, body = self.request('GET', path, headers={'Host': 'attacker.example'})
            self.assertEqual(status, 403)
            self.assertIn('error', json.loads(body))


class HlsBehavior(unittest.TestCase):
    request = ServerBehavior.request
    tearDown = ServerBehavior.tearDown

    @classmethod
    def setUpClass(cls):
        cls.fixtures = tempfile.TemporaryDirectory()
        folder = Path(cls.fixtures.name)
        cls.source = folder / 'source.mp4'
        base = folder / 'base.mp4'
        cover = folder / 'cover.png'
        subprocess.run(['ffmpeg', '-v', 'error', '-nostdin', '-y',
                        '-f', 'lavfi', '-i', 'testsrc2=size=160x90:rate=12:duration=14',
                        '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=48000:duration=18',
                        '-map', '0:v', '-map', '1:a', '-vf', 'setpts=PTS+3/TB', '-af', 'asetpts=PTS+3/TB',
                        '-c:v', 'libx264', '-preset', 'medium', '-g', '36', '-bf', '2', '-pix_fmt', 'yuv420p', '-fps_mode:v', 'passthrough',
                        '-c:a', 'aac', '-b:a', '192k', '-copyts', '-avoid_negative_ts', 'disabled', str(base)], check=True)
        subprocess.run(['ffmpeg', '-v', 'error', '-nostdin', '-y', '-f', 'lavfi', '-i',
                        'color=red:size=32x32', '-frames:v', '1', '-threads', '1', str(cover)], check=True)
        subprocess.run(['ffmpeg', '-v', 'error', '-nostdin', '-y', '-copyts', '-i', str(cover), '-i', str(base),
                        '-map', '0:v', '-map', '1:v', '-map', '1:a', '-c', 'copy',
                        '-disposition:v:0', 'attached_pic', '-avoid_negative_ts', 'disabled', str(cls.source)], check=True)
        cls.dub = folder / 'dub.wav'
        cls.other = folder / 'other.wav'
        for path, first, second in [(cls.dub, 660, 880), (cls.other, 990, 1100)]:
            with wave.open(str(path), 'wb') as wav:
                wav.setnchannels(1)
                wav.setsampwidth(2)
                wav.setframerate(48000)
                wav.writeframes(b''.join(struct.pack('<h', round(12000 * math.sin(2 * math.pi * (first if i < 24000 else second) * i / 48000))) for i in range(96000)))
        # A long, already-compatible dub catches full-file re-encoding without a
        # machine-dependent stopwatch. Original AAC has a sub-frame video offset.
        long_base = folder / 'long-base.mp4'
        cls.long_source = folder / 'long-source.mp4'
        cls.long_dub = folder / 'long-dub.m4a'
        subprocess.run(['ffmpeg', '-v', 'error', '-nostdin', '-y',
                        '-f', 'lavfi', '-i', 'color=size=64x36:rate=24:duration=120',
                        '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=44100:duration=120.065',
                        '-map', '0:v', '-map', '1:a', '-c:v', 'libx264', '-preset', 'ultrafast',
                        '-g', '144', '-bf', '2', '-c:a', 'aac', '-video_track_timescale', '90000',
                        str(long_base)], check=True)
        subprocess.run(['ffmpeg', '-v', 'error', '-nostdin', '-y', '-copyts',
                        '-itsoffset', '0.041', '-i', str(long_base), '-i', str(long_base),
                        '-map', '0:v', '-map', '1:a', '-c', 'copy', '-avoid_negative_ts', 'disabled',
                        '-video_track_timescale', '90000', str(cls.long_source)], check=True)
        subprocess.run(['ffmpeg', '-v', 'error', '-nostdin', '-y',
                        '-f', 'lavfi', '-i', 'sine=frequency=880:sample_rate=48000:duration=120',
                        '-ac', '2', '-c:a', 'aac', '-b:a', '128k', str(cls.long_dub)], check=True)
        cls.full_aac = folder / 'full-aac.m4a'
        subprocess.run(['ffmpeg', '-v', 'error', '-nostdin', '-y',
                        '-f', 'lavfi', '-i', 'sine=frequency=880:sample_rate=48000:duration=14',
                        '-ac', '2', '-c:a', 'aac', str(cls.full_aac)], check=True)

    @classmethod
    def tearDownClass(cls):
        cls.fixtures.cleanup()

    def setUp(self):
        ServerBehavior.setUp(self)
        self.store.register(self.source, 'video', 'real-video')
        self.store.register(self.dub, 'audio', 'real-dub')
        self.store.register(self.other, 'audio', 'other-dub')
        self.project['source_asset_id'] = 'real-video'
        self.project['tracks'][1]['asset_id'] = 'real-dub'
        self.store.save(self.project)

    def prepare(self, project=None):
        status, _, body = self.request('POST', '/api/playback', json.dumps({'project': project or self.project}).encode())
        self.assertEqual(status, 200, body)
        result = json.loads(body)
        self.assertEqual(set(result), {'manifest_url', 'tracks', 'video_key', 'video_bytes', 'audio_bytes', 'source_asset_id'})
        self.assertEqual(result['source_asset_id'], 'real-video')
        return result

    def master(self, result):
        status, headers, body = self.request('GET', result['manifest_url'])
        self.assertEqual(status, 200)
        self.assertEqual(headers['Content-Type'], 'application/vnd.apple.mpegurl')
        return body.decode()

    def rendition(self, result, index):
        lines = [line for line in self.master(result).splitlines() if line.startswith('#EXT-X-MEDIA:')]
        uri = re.search(r'URI="([^"]+)"', lines[index]).group(1)
        return self.data / uri.lstrip('/')

    def metadata(self, path, *extra):
        return json.loads(subprocess.check_output(['ffprobe', '-v', 'error', *extra, '-show_streams', '-show_format', '-of', 'json', str(path)]))

    def samples(self, path):
        pcm = subprocess.check_output(['ffmpeg', '-v', 'error', '-nostdin', '-i', str(path), '-map', '0:a:0',
                                       '-ar', '48000', '-ac', '1', '-f', 's16le', '-'])
        values = array.array('h')
        values.frombytes(pcm)
        if sys.byteorder != 'little':
            values.byteswap()
        return values

    def rms(self, values, start, end):
        selected = values[round(start * 48000):round(end * 48000)]
        self.assertGreater(len(selected), 0)
        return math.sqrt(sum(v * v for v in selected) / len(selected))

    def snapshot(self, folder):
        return {p.name: (hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_mtime_ns) for p in folder.iterdir()}

    def test_shared_video_is_immutable_across_track_selection_edits_and_offsets(self):
        original = (hashlib.sha256(self.source.read_bytes()).hexdigest(), self.source.stat().st_mtime_ns)
        registry_ids = set(self.store.registry)
        initial = self.prepare()
        video_folder = self.data / 'hls' / initial['video_key']
        snapshot = self.snapshot(video_folder)
        audio_folders = lambda: {p.name for p in (self.data / 'hls').glob('a-*')}
        baseline_audio = audio_folders()
        selected = copy.deepcopy(self.project)
        selected['selected_track_id'] = 'original'
        selected['title'] = 'Selection and draft do not change media'
        selected['tracks'][1]['segments'][0]['draft_text'] = 'new draft'
        original_ready = self.prepare(selected)
        self.assertEqual(original_ready['video_key'], initial['video_key'])
        self.assertNotEqual(original_ready['manifest_url'], initial['manifest_url'])
        self.assertEqual(len(audio_folders()), len(baseline_audio) + 1)
        added = copy.deepcopy(selected)
        added['tracks'].append({**copy.deepcopy(added['tracks'][1]), 'id': 'second', 'label': '成都', 'asset_id': 'other-dub'})
        added['selected_track_id'] = 'second'
        extra = self.prepare(added)
        self.assertNotEqual(extra['manifest_url'], initial['manifest_url'])
        self.assertEqual(extra['video_key'], initial['video_key'])
        self.assertEqual(len(audio_folders()), len(baseline_audio) + 2)
        added['tracks'][2]['timeline_offset_ms'] = -500
        offset = self.prepare(added)
        self.assertNotEqual(offset['manifest_url'], extra['manifest_url'])
        self.assertEqual(offset['video_key'], initial['video_key'])
        before_rename = audio_folders()
        added['tracks'][2]['label'] = '四川 "声轨"\n#EXT-X-STREAM-INF:injection'
        added['tracks'][2]['language'] = 'zh-CN'
        renamed = self.prepare(added)
        self.assertNotEqual(renamed['manifest_url'], offset['manifest_url'])
        self.assertEqual(audio_folders(), before_rename)
        self.assertEqual(self.master(renamed).count('#EXT-X-STREAM-INF:'), 2)  # One is harmless text inside NAME.
        self.assertEqual(sum(line.startswith('#EXT-X-STREAM-INF:') for line in self.master(renamed).splitlines()), 1)
        restored = self.prepare(self.project)
        self.assertEqual([t['id'] for t in restored['tracks']], ['original', 'dub'])
        self.assertEqual(restored['video_key'], initial['video_key'])
        self.assertEqual(self.snapshot(video_folder), snapshot)
        self.assertEqual(len(list((self.data / 'hls').glob('v-*'))), 1)
        self.assertEqual(set(self.store.registry), registry_ids)
        self.assertFalse((self.data / 'playback').exists())
        self.assertFalse(list((self.data / 'hls').glob('.stage-*')))
        self.assertEqual((hashlib.sha256(self.source.read_bytes()).hexdigest(), self.source.stat().st_mtime_ns), original)
        self.assertGreater(initial['video_bytes'], 0)
        self.assertGreater(initial['audio_bytes'], 0)
        self.assertEqual(initial['video_bytes'], sum(p.stat().st_size for p in video_folder.iterdir() if p.suffix in ('.mp4', '.m4s')))
        self.assertTrue(all(p.name == 'init.mp4' for p in (self.data / 'hls').rglob('*.mp4')))

    def test_master_only_prepares_selected_and_reuses_cached_ordered_alternatives(self):
        project = copy.deepcopy(self.project)
        project['tracks'].append({**copy.deepcopy(project['tracks'][1]), 'id': 'same-language', 'label': '中文', 'asset_id': 'other-dub'})
        cold = self.prepare(project)
        self.assertEqual([t['id'] for t in cold['tracks']], ['dub'])
        self.assertEqual(cold['tracks'][0]['audio_index'], 0)
        self.assertEqual(len(list((self.data / 'hls').glob('a-*'))), 1)
        self.assertIn('DEFAULT=YES,AUTOSELECT=YES', self.master(cold))
        for selected in ['original', 'same-language']:
            project['selected_track_id'] = selected
            result = self.prepare(project)
        text = self.master(result)
        self.assertEqual(sum(line.startswith('#EXT-X-STREAM-INF:') for line in text.splitlines()), 1)
        self.assertNotIn('CODECS=', text)
        audio = [line for line in text.splitlines() if line.startswith('#EXT-X-MEDIA:')]
        self.assertEqual(len(audio), 3)
        self.assertTrue(all('DEFAULT=NO,AUTOSELECT=NO' in line for line in audio[:2]))
        self.assertIn('DEFAULT=YES,AUTOSELECT=YES', audio[2])
        self.assertEqual(len(set(re.search(r'NAME="([^"]+)"', line).group(1) for line in audio)), 3)
        self.assertEqual(result['tracks'], [{'id': t['id'], 'audio_index': i, 'label': t['label'], 'language': t['language']} for i, t in enumerate(project['tracks'])])
        video = self.data / 'hls' / result['video_key'] / 'index.m3u8'
        self.assertEqual([s['codec_type'] for s in self.metadata(video)['streams']], ['video'])
        for i in range(3):
            streams = self.metadata(self.rendition(result, i))['streams']
            self.assertEqual([s['codec_type'] for s in streams], ['audio'])
            self.assertEqual(streams[0]['codec_name'], 'aac')
        master_metadata = self.metadata(f'http://127.0.0.1:{self.server.server_port}' + result['manifest_url'])
        self.assertEqual(sum(s['codec_type'] == 'video' for s in master_metadata['streams']), 1)
        self.assertEqual(sum(s['codec_type'] == 'audio' for s in master_metadata['streams']), 3)
        project['tracks'].reverse()
        reordered = self.prepare(project)
        self.assertEqual(reordered['video_key'], result['video_key'])
        self.assertEqual([t['id'] for t in reordered['tracks']], [t['id'] for t in project['tracks']])
        self.assertEqual([t['audio_index'] for t in reordered['tracks']], [0, 1, 2])
        reordered_audio = [line for line in self.master(reordered).splitlines() if line.startswith('#EXT-X-MEDIA:')]
        self.assertIn('DEFAULT=YES,AUTOSELECT=YES', reordered_audio[0])

    def test_cold_long_aac_preserves_packets_priming_clock_and_selected_only(self):
        self.store.register(self.long_source, 'video', 'long-video')
        self.store.register(self.long_dub, 'audio', 'long-dub')
        project = copy.deepcopy(self.project)
        project['source_asset_id'] = 'long-video'
        project['tracks'][1]['asset_id'] = 'long-dub'
        def packets(path, selection):
            return json.loads(subprocess.check_output([
                'ffprobe', '-v', 'error', '-select_streams', selection, '-show_packets',
                '-show_data_hash', 'sha256', '-show_entries', 'packet=pts_time,duration_time,data_hash',
                '-of', 'json', str(path)]))['packets']
        for selected, source in [('dub', self.long_dub), ('original', self.long_source)]:
            with self.subTest(selected=selected):
                project['selected_track_id'] = selected
                status, _, body = self.request('POST', '/api/playback', json.dumps({'project': project}).encode())
                self.assertEqual(status, 200, body)
                result = json.loads(body)
                expected_ids = ['dub'] if selected == 'dub' else ['original', 'dub']
                self.assertEqual([t['id'] for t in result['tracks']], expected_ids)
                self.assertEqual(len(list((self.data / 'hls').glob('a-*'))), len(expected_ids))
                index = next(t['audio_index'] for t in result['tracks'] if t['id'] == selected)
                output = self.rendition(result, index)
                source_packets = packets(source, 'a:0')
                output_packets = packets(output, 'a:0')
                # This is red for an AAC→AAC transcode, even on a fast machine.
                self.assertEqual([p['data_hash'] for p in output_packets],
                                 [p['data_hash'] for p in source_packets[:len(output_packets)]])
                self.assertEqual(output_packets[-1]['duration_time'],
                                 source_packets[len(output_packets) - 1]['duration_time'])
                stream = self.metadata(output)['streams'][0]
                rate = int(stream['sample_rate'])
                self.assertEqual(rate, 48000 if selected == 'dub' else 44100)
                self.assertEqual(int(stream['channels']), 2 if selected == 'dub' else 1)
                frames = json.loads(subprocess.check_output([
                    'ffprobe', '-v', 'error', '-read_intervals', '%+0.1', '-show_frames',
                    '-show_entries', 'frame=best_effort_timestamp_time,nb_samples',
                    '-of', 'json', str(output)]))['frames']
                self.assertAlmostEqual(float(frames[0]['best_effort_timestamp_time']), 0, delta=1 / rate)
                pcm = subprocess.check_output(['ffmpeg', '-v', 'error', '-i', str(output), '-f', 's16le', '-'])
                channels = int(stream['channels'])
                self.assertAlmostEqual(len(pcm) / (2 * channels * rate), 120, delta=1024 / rate)
                trim = ['-af', 'atrim=start_sample=' + str(round(0.041 * rate))] if selected == 'original' else []
                expected = subprocess.check_output(['ffmpeg', '-v', 'error', '-i', str(source),
                                                    '-map', '0:a:0', *trim, '-f', 's16le', '-'])
                common = min(len(pcm), len(expected))
                self.assertEqual(pcm[:common], expected[:common])
                self.assertEqual(self.master(result).count('DEFAULT=YES,AUTOSELECT=YES'), 1)
                video = self.data / 'hls' / result['video_key'] / 'index.m3u8'
                self.assertEqual([p['data_hash'] for p in packets(video, 'v:0')],
                                 [p['data_hash'] for p in packets(self.long_source, 'v:0')])

    def test_full_aac_with_user_offsets_still_trims_and_pads_decoded_audio(self):
        self.store.register(self.full_aac, 'audio', 'full-aac')
        for offset in (500, -500):
            with self.subTest(offset=offset):
                project = copy.deepcopy(self.project)
                project['tracks'][1]['asset_id'] = 'full-aac'
                project['tracks'][1]['timeline_offset_ms'] = offset
                result = self.prepare(project)
                self.assertEqual([t['id'] for t in result['tracks']], ['dub'])
                values = self.samples(self.rendition(result, 0))
                self.assertAlmostEqual(len(values) / 48000, 14, delta=1024 / 48000)
                if offset > 0:
                    self.assertLess(self.rms(values, 0.05, 0.4), 30)
                    self.assertGreater(self.rms(values, 0.6, 0.9), 1000)
                    self.assertGreater(self.rms(values, 13.6, 13.9), 1000)
                else:
                    self.assertGreater(self.rms(values, 0.05, 0.3), 1000)
                    self.assertGreater(self.rms(values, 13.1, 13.35), 1000)
                    self.assertLess(self.rms(values, 13.65, 13.9), 30)

    def test_positive_negative_and_entirely_out_of_range_offsets(self):
        for offset, audible_end in [(500, 2.5), (-500, 1.5), (20000, None), (-3000, None)]:
            with self.subTest(offset=offset):
                project = copy.deepcopy(self.project)
                project['tracks'][1]['timeline_offset_ms'] = offset
                values = self.samples(self.rendition(self.prepare(project), 0))
                self.assertGreaterEqual(len(values), round(13.99 * 48000))
                self.assertLessEqual(len(values), round(14.03 * 48000))
                if audible_end is None:
                    self.assertLess(self.rms(values, 0.05, 13.9), 30)
                else:
                    if offset > 0:
                        self.assertLess(self.rms(values, 0.05, 0.4), 30)
                        self.assertGreater(self.rms(values, 0.6, 0.9), 1000)
                    else:
                        self.assertGreater(self.rms(values, 0.05, 0.3), 1000)
                        # The first half-second (660 Hz) was removed, not just delayed.
                        window = values[2400:12000]
                        crossings = sum(a <= 0 < b for a, b in zip(window, window[1:]))
                        self.assertAlmostEqual(crossings / 0.2, 880, delta=15)
                    self.assertGreater(self.rms(values, audible_end - 0.25, audible_end - 0.1), 1000)
                    self.assertLess(self.rms(values, audible_end + 0.15, 13.9), 30)

    def test_video_duration_frames_timestamps_and_original_aac_are_complete(self):
        project = copy.deepcopy(self.project)
        project['selected_track_id'] = 'original'
        result = self.prepare(project)
        source_info = self.metadata(self.source)
        source_video = next(s for s in source_info['streams'] if s['codec_type'] == 'video' and not s.get('disposition', {}).get('attached_pic'))
        self.assertTrue(any(s.get('disposition', {}).get('attached_pic') for s in source_info['streams']))
        self.assertGreater(float(source_info['format']['duration']), 17)
        self.assertEqual(self.store.registry['real-video']['asset']['duration_ms'], 14000)
        self.assertGreater(float(source_video['start_time']), 2.9)
        video = self.data / 'hls' / result['video_key'] / 'index.m3u8'
        def frames(path, stream):
            return json.loads(subprocess.check_output(['ffprobe', '-v', 'error', '-select_streams', str(stream),
                                                      '-show_frames', '-show_entries', 'frame=best_effort_timestamp_time,pict_type,nb_samples',
                                                      '-of', 'json', str(path)]))['frames']
        source_frames = frames(self.source, source_video['index'])
        video_frames = frames(video, 0)
        self.assertEqual(len(source_frames), 168)
        self.assertTrue(any(f['pict_type'] == 'B' for f in source_frames))
        self.assertEqual(len(video_frames), len(source_frames))
        self.assertAlmostEqual(float(video_frames[0]['best_effort_timestamp_time']), 0, delta=0.001)
        self.assertAlmostEqual(float(video_frames[-1]['best_effort_timestamp_time']), 14 - 1 / 12, delta=0.001)
        def frame_hashes(path, stream):
            output = subprocess.check_output(['ffmpeg', '-v', 'error', '-nostdin', '-i', str(path), '-map', '0:' + str(stream), '-f', 'framemd5', '-'])
            return [line.rsplit(b',', 1)[-1].strip() for line in output.splitlines() if not line.startswith(b'#')]
        self.assertEqual(frame_hashes(video, 0), frame_hashes(self.source, source_video['index']))
        # Every video segment begins with an independently decodable keyframe.
        for segment in sorted(video.parent.glob('seg-*.m4s')):
            fragment = self.data / 'fragment.mp4'
            fragment.write_bytes((video.parent / 'init.mp4').read_bytes() + segment.read_bytes())
            packets = json.loads(subprocess.check_output(['ffprobe', '-v', 'error', '-show_packets', '-show_entries', 'packet=flags',
                                                          '-of', 'json', str(fragment)]))['packets']
            self.assertIn('K', packets[0]['flags'])
            fragment.unlink()
        original = self.rendition(result, 0)
        original_info = self.metadata(original)['streams'][0]
        self.assertEqual(original_info['codec_name'], 'aac')
        # HLS start_time may describe AAC's negative preroll packet. Presentation
        # starts with the first decoded frame after the edit-list priming skip.
        audio_frames = frames(original, 0)
        self.assertAlmostEqual(float(audio_frames[0]['best_effort_timestamp_time']),
                               float(video_frames[0]['best_effort_timestamp_time']), delta=1 / 48000)
        values = self.samples(original)
        self.assertGreaterEqual(len(values), round(13.99 * 48000))
        self.assertLessEqual(len(values), round(14.03 * 48000))
        self.assertGreater(self.rms(values, 0.002, 0.015), 500)  # No retained 1024-sample primer silence.
        self.assertGreater(self.rms(values, 0.05, 0.25), 1000)
        self.assertGreater(self.rms(values, 13.75, 13.95), 1000)
        window = values[2400:12000]
        crossings = sum(a <= 0 < b for a, b in zip(window, window[1:]))
        self.assertAlmostEqual(crossings / 0.2, 440, delta=15)

    def test_hls_serving_ranges_head_and_exact_generated_paths(self):
        result = self.prepare()
        folder = self.data / 'hls' / result['video_key']
        for filename, mime in [('index.m3u8', 'application/vnd.apple.mpegurl'), ('init.mp4', 'video/mp4'), ('seg-000000.m4s', 'video/iso.segment')]:
            url = '/hls/' + result['video_key'] + '/' + filename
            expected = (folder / filename).read_bytes()
            self.assertEqual(self.request('GET', url)[2], expected)
            status, headers, body = self.request('GET', url, headers={'Range': 'bytes=0-15'})
            self.assertEqual(status, 206)
            self.assertEqual(body, expected[:16])
            self.assertEqual(headers['Content-Type'], mime)
            self.assertEqual(headers['Content-Range'], f'bytes 0-15/{len(expected)}')
            status, headers, body = self.request('HEAD', url, headers={'Range': 'bytes=-12'})
            self.assertEqual(status, 206)
            self.assertEqual(headers['Content-Length'], '12')
            self.assertEqual(body, b'')
            status, headers, body = self.request('GET', url, headers={'Range': 'bytes=999999999-'})
            self.assertEqual(status, 416)
            self.assertEqual(headers['Content-Range'], f'bytes */{len(expected)}')
            self.assertEqual(body, b'')
        (folder / 'seg-999998.m4s').write_bytes(b'not generated by the packager')
        (folder / 'seg-999999.m4s').symlink_to(self.source)
        key = result['video_key']
        for path in [f'/hls/{key}/../assets.json', f'/hls/{key}/%2e%2e%2fassets.json',
                     f'/hls/{key}/.complete.json', f'/hls/{key}/seg-999998.m4s', f'/hls/{key}/seg-999999.m4s',
                     '/hls/../assets.json', '/hls/.stage-incomplete/index.m3u8',
                     f'/hls/{key}/index.m3u8/extra', '/hls/v-' + '0' * 64 + '/index.m3u8']:
            self.assertEqual(self.request('GET', path)[0], 404, path)
        alias = self.data / 'hls' / ('v-' + '1' * 64)
        alias.symlink_to(folder, target_is_directory=True)
        self.assertEqual(self.request('GET', '/hls/' + alias.name + '/init.mp4')[0], 404)


if __name__ == '__main__':
    unittest.main()
