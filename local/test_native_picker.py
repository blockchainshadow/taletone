"""Native-picker security and real-file HTTP boundaries, without opening a GUI.

Run: python -m unittest -v test_native_picker
Only the OS selection boundary is injected; requests and file/range I/O are real.
"""
import http.client
import json
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

from native_picker import NativePicker, PickerError, choose_files
from server import create_server


class NativePickerBehavior(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.folder = Path(self.temp.name)
        self.media = self.folder / '本地 audio.wav'
        self.content = bytes(range(256)) * 20
        self.media.write_bytes(self.content)
        self.calls = []
        self.selected = [self.media]

        def select(kind):
            self.calls.append(kind)
            return self.selected

        self.picker = NativePicker(select)
        # No Store is needed: these routes must not read or mutate project state.
        self.server = create_server(0, store=object(), native_picker=self.picker)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.origin = 'https://papaw.cc'

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.temp.cleanup()

    def request(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=10)
        try:
            conn.request(method, path, body=body, headers=headers or {})
            response = conn.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            conn.close()

    def pick(self, kind='audio', **headers):
        return self.request('POST', '/api/native-picker', json.dumps({'kind': kind}),
                            {'Origin': self.origin, 'Content-Type': 'application/json', **headers})

    def selected_url(self):
        status, headers, body = self.pick()
        self.assertEqual(status, 200, body)
        self.assertEqual(headers['Access-Control-Allow-Origin'], self.origin)
        value = json.loads(body)
        self.assertFalse(value['cancelled'])
        file = value['files'][0]
        self.assertEqual(set(file), {'name', 'size', 'lastModified', 'type', 'url'})
        self.assertEqual(file['name'], self.media.name)
        self.assertEqual(file['size'], len(self.content))
        self.assertEqual(file['lastModified'], self.media.stat().st_mtime_ns // 1000000)
        self.assertIn(file['type'], ('audio/wav', 'audio/x-wav'))
        self.assertNotIn(str(self.folder), body.decode())
        url = urlsplit(file['url'])
        self.assertEqual(url.netloc, f'127.0.0.1:{self.server.server_port}')
        self.assertRegex(url.path, r'^/api/native-files/[A-Za-z0-9_-]{43}$')
        return url.path

    def test_origin_host_and_payload_rejected_before_chooser(self):
        body = b'{"kind":"audio"}'
        for origin in [None, 'null', 'https://evil.example', 'https://papaw.cc.evil.example', 'https://papaw.cc/',
                       f'http://localhost:{self.server.server_port}']:
            with self.subTest(origin=origin):
                headers = {'Content-Type': 'application/json'}
                if origin is not None:
                    headers['Origin'] = origin
                status, response_headers, _ = self.request('POST', '/api/native-picker', body, headers)
                self.assertEqual(status, 403)
                self.assertNotIn('Access-Control-Allow-Origin', response_headers)
        self.assertEqual(self.pick(Host='evil.example')[0], 403)
        for payload in [{'kind': 'audio', 'path': str(self.media)}, {'kind': 'video', 'files': [str(self.media)]},
                        {'kind': 'document'}, {'kind': []}, ['audio']]:
            self.assertEqual(self.request('POST', '/api/native-picker', json.dumps(payload),
                                         {'Origin': self.origin, 'Content-Type': 'application/json'})[0], 400)
        self.assertEqual(self.pick(**{'Content-Type': 'text/plain'})[0], 415)
        self.assertEqual(self.request('POST', '/api/native-picker?path=private', body,
                                     {'Origin': self.origin, 'Content-Type': 'application/json'})[0], 400)
        self.assertEqual(self.calls, [])

    def test_loopback_origin_and_public_cross_site_request(self):
        loopback = f'localhost:{self.server.server_port}'
        status, headers, _ = self.pick(Host=loopback, Origin='http://' + loopback)
        self.assertEqual(status, 200)
        self.assertEqual(headers['Access-Control-Allow-Origin'], 'http://' + loopback)
        self.assertEqual(self.pick(**{'Sec-Fetch-Site': 'cross-site'})[0], 200)
        self.assertEqual(self.calls, ['audio', 'audio'])

    def test_preflight_is_limited_to_native_routes(self):
        headers = {'Origin': self.origin, 'Access-Control-Request-Method': 'POST',
                   'Access-Control-Request-Headers': 'content-type', 'Access-Control-Request-Private-Network': 'true'}
        status, result, body = self.request('OPTIONS', '/api/native-picker', headers=headers)
        self.assertEqual((status, body), (204, b''))
        self.assertEqual(result['Access-Control-Allow-Origin'], self.origin)
        self.assertEqual(result['Access-Control-Allow-Private-Network'], 'true')
        self.assertEqual(result['Access-Control-Allow-Methods'], 'POST')
        for changes in [{'Origin': 'https://evil.example'}, {'Access-Control-Request-Method': 'PUT'},
                        {'Access-Control-Request-Headers': 'content-type,x-path'}]:
            status, result, _ = self.request('OPTIONS', '/api/native-picker', headers={**headers, **changes})
            self.assertEqual(status, 403)
            self.assertNotIn('Access-Control-Allow-Private-Network', result)
        for path, method in [('/api/assets', 'OPTIONS'), ('/api/assets', 'POST'), ('/api/project', 'PUT')]:
            status, result, _ = self.request(method, path, headers=headers)
            self.assertIn(status, (403, 404))
            self.assertNotIn('Access-Control-Allow-Origin', result)
        self.assertEqual(self.calls, [])

    def test_cancel_preserves_existing_tickets_and_returns_no_files(self):
        path = self.selected_url()
        self.selected = None
        status, _, body = self.pick()
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {'cancelled': True, 'files': []})
        self.assertEqual(len(self.picker._tickets), 1)
        self.assertEqual(self.request('GET', path)[2], self.content)
        self.assertEqual(sorted(self.folder.iterdir()), [self.media])

    def test_only_one_chooser_can_be_open(self):
        entered, release = threading.Event(), threading.Event()

        def wait_for_user(kind):
            entered.set()
            if not release.wait(10):
                raise PickerError('Test chooser timed out')
            return None

        self.picker._select = wait_for_user
        results = []
        worker = threading.Thread(target=lambda: results.append(self.pick()))
        worker.start()
        try:
            self.assertTrue(entered.wait(5))
            status, _, body = self.pick()
            self.assertEqual(status, 409)
            self.assertIn('already open', json.loads(body)['error'])
        finally:
            release.set()
            worker.join(10)
        self.assertFalse(worker.is_alive())
        self.assertEqual(results[0][0], 200)
        self.assertEqual(self.pick()[0], 200)  # Cancellation released the chooser lock.

    def test_multiple_audio_single_video_and_no_partial_tickets(self):
        second = self.folder / 'second.wav'
        second.write_bytes(b'other')
        self.selected = [self.media, second]
        self.assertEqual(len(json.loads(self.pick()[2])['files']), 2)
        count = len(self.picker._tickets)
        self.assertEqual(self.pick('video')[0], 503)
        self.assertEqual(len(self.picker._tickets), count)
        self.selected = [self.media, self.folder / 'missing.wav']
        status, _, body = self.pick()
        self.assertEqual(status, 503)
        self.assertNotIn(str(self.folder), body.decode())
        self.assertEqual(len(self.picker._tickets), count)
        self.selected = [self.media]
        self.assertEqual(self.pick('video')[0], 200)
        self.assertEqual(self.calls, ['audio', 'video', 'audio', 'video'])

    def test_real_media_ranges_head_and_origin_boundaries(self):
        path = self.selected_url()
        headers = {'Origin': self.origin}
        for span, start, end in [('bytes=0-9', 0, 9), ('bytes=12-', 12, len(self.content) - 1),
                                 ('bytes=-12', len(self.content) - 12, len(self.content) - 1),
                                 ('bytes=0-999999', 0, len(self.content) - 1)]:
            for method in ['GET', 'HEAD']:
                status, result, body = self.request(method, path, headers={**headers, 'Range': span})
                self.assertEqual(status, 206)
                self.assertEqual(result['Content-Range'], f'bytes {start}-{end}/{len(self.content)}')
                self.assertEqual(result['Content-Length'], str(end - start + 1))
                self.assertEqual(result['Access-Control-Allow-Origin'], self.origin)
                self.assertEqual(body, self.content[start:end + 1] if method == 'GET' else b'')
        for invalid in ['bytes=999999-', 'bytes=9-1', 'bytes=-0', 'bytes=0-1,4-6', 'invalid', 'bytes=-']:
            status, result, body = self.request('GET', path, headers={**headers, 'Range': invalid})
            self.assertEqual((status, body), (416, b''))
            self.assertEqual(result['Content-Range'], f'bytes */{len(self.content)}')
            self.assertEqual(result['Access-Control-Allow-Origin'], self.origin)
        for blocked in [{'Origin': 'https://evil.example'}, {'Origin': 'null'}, {'Sec-Fetch-Site': 'cross-site'}]:
            self.assertEqual(self.request('GET', path, headers=blocked)[0], 403)
        for unknown in ['/api/native-files/', '/api/native-files/../../etc/passwd', '/api/native-files/' + 'a' * 43]:
            self.assertEqual(self.request('GET', unknown, headers=headers)[0], 404)
        status, result, _ = self.request('OPTIONS', path, headers={**headers,
            'Access-Control-Request-Method': 'GET', 'Access-Control-Request-Headers': 'range'})
        self.assertEqual(status, 204)
        self.assertEqual(result['Access-Control-Allow-Headers'], 'range')
        self.assertNotIn('Access-Control-Allow-Private-Network', result)

    def test_media_is_original_not_a_copy_and_replacement_is_not_exposed(self):
        path = self.selected_url()
        self.media.write_bytes(b'changed in place')
        self.assertEqual(self.request('GET', path)[2], b'changed in place')
        replacement = self.folder / 'replacement.wav'
        replacement.write_bytes(b'not selected')
        replacement.replace(self.media)
        status, _, body = self.request('GET', path)
        self.assertEqual(status, 404)
        self.assertNotIn(b'not selected', body)
        self.media.unlink()
        self.assertEqual(self.request('GET', path)[0], 404)
        fresh = NativePicker(lambda kind: None)
        self.server.native_picker = fresh
        self.assertEqual(self.request('GET', path)[0], 404)

    def test_empty_media_and_platform_error_have_explicit_boundaries(self):
        self.media.write_bytes(b'')
        value = json.loads(self.pick()[2])
        path = urlsplit(value['files'][0]['url']).path
        self.assertEqual(value['files'][0]['size'], 0)
        for method in ('GET', 'HEAD'):
            status, headers, body = self.request(method, path)
            self.assertEqual((status, body), (200, b''))
            self.assertEqual(headers['Content-Length'], '0')
        self.assertEqual(self.request('GET', path, headers={'Range': 'bytes=0-'})[0], 416)
        self.picker._select = choose_files
        with patch('native_picker.sys.platform', 'linux'):
            status, _, body = self.pick()
        self.assertEqual(status, 503)
        self.assertIn('requires the macOS', json.loads(body)['error'])
        self.assertEqual(len(self.picker._tickets), 1)
        self.picker._select = lambda kind: None
        self.assertEqual(self.pick()[0], 200)


class MacOSSelectionBoundary(unittest.TestCase):
    def test_platform_failure_does_not_launch_a_substitute(self):
        with patch('native_picker.sys.platform', 'linux'), patch('native_picker.subprocess.run') as run:
            with self.assertRaisesRegex(PickerError, 'requires the macOS'):
                choose_files('audio')
            run.assert_not_called()

    def test_explicit_macos_cancellation_and_errors(self):
        cases = [(subprocess.CompletedProcess([], 0, '{"cancelled":true}', ''), None),
                 (subprocess.CompletedProcess([], 1, '', 'execution error: User canceled. (-128)\n'), None)]
        for result, expected in cases:
            with self.subTest(result=result), patch('native_picker.sys.platform', 'darwin'), \
                    patch('native_picker.subprocess.run', return_value=result):
                self.assertIs(choose_files('audio'), expected)
        for result in [subprocess.CompletedProcess([], 1, '', 'execution error: Not authorized. (-1743)'),
                       subprocess.CompletedProcess([], 0, 'invalid output', ''),
                       subprocess.CompletedProcess([], 0, '{"cancelled":false,"paths":[]}', '')]:
            with self.subTest(result=result), patch('native_picker.sys.platform', 'darwin'), \
                    patch('native_picker.subprocess.run', return_value=result):
                with self.assertRaises(PickerError):
                    choose_files('video')


if __name__ == '__main__':
    unittest.main()
