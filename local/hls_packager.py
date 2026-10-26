"""Immutable, independently cached video/audio HLS VOD renditions."""
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
from fractions import Fraction
from urllib.parse import quote


PACKAGER_VERSION = 'hls-fmp4-editlists-v1'
CACHE_KEY = re.compile(r'[vam]-[0-9a-f]{64}')
MEDIA_FILE = re.compile(r'(?:index\.m3u8|master\.m3u8|init\.mp4|seg-[0-9]{6,}\.m4s)')


def run(command):
    try:
        return subprocess.run(command, capture_output=True, check=True, timeout=900).stdout
    except (OSError, subprocess.SubprocessError) as exc:
        detail = getattr(exc, 'stderr', b'') or b''
        raise ValueError('Cannot prepare HLS media: ' + detail.decode('utf-8', errors='replace')[-2000:] + str(exc)) from exc


def inspect_stream(path, kind):
    try:
        info = json.loads(run(['ffprobe', '-v', 'error', '-show_format', '-show_streams', '-of', 'json', str(path)]))
        stream = next(s for s in info['streams'] if s.get('codec_type') == kind and not s.get('disposition', {}).get('attached_pic'))
        if stream.get('duration_ts') is not None and stream.get('time_base'):
            duration = float(int(stream['duration_ts']) * Fraction(stream['time_base']))
        else:
            duration = float(stream.get('duration', 0))
        if not math.isfinite(duration) or duration <= 0:
            # Container duration may include a longer audio stream or attached artwork.
            packets = json.loads(run(['ffprobe', '-v', 'error', '-select_streams', str(stream['index']),
                                      '-show_packets', '-show_entries', 'packet=pts_time,duration_time',
                                      '-of', 'json', str(path)]))['packets']
            times = [(float(p['pts_time']), float(p.get('duration_time', 0))) for p in packets if 'pts_time' in p]
            duration = max(pts + length for pts, length in times) - min(pts for pts, _ in times)
        if not math.isfinite(duration) or duration <= 0:
            raise ValueError('Media must contain a finite-duration ' + kind + ' stream')
        return stream, duration, info.get('format', {}).get('format_name', '')
    except (KeyError, TypeError, ValueError, StopIteration, OverflowError) as exc:
        raise ValueError('Invalid ' + kind + ' media: ' + str(exc)) from exc

def can_copy_aac(path, stream, audio_duration, duration, start, embedded, offset):
    """Accept packet-contiguous AAC with no resampling, padding or user offset.

    MOV edit lists preserve encoder priming and sample-accurately clip the small
    leading skew of an embedded track. The last AAC frame may decode past the
    presentation end, just as it does after an AAC encode; never drop that frame.
    Large trims, missing coverage and discontinuous clocks keep the PCM path.
    """
    if stream.get('codec_name') != 'aac' or stream.get('profile') != 'LC' or offset:
        return False
    rate = int(stream.get('sample_rate', 0))
    if rate not in (8000, 11025, 12000, 16000, 22050, 24000, 32000, 44100, 48000):
        return False
    if stream.get('channels') not in (1, 2) or Fraction(stream['time_base']) != Fraction(1, rate):
        return False
    audio_start = float(stream.get('start_time', 0))
    if not math.isfinite(audio_start) or audio_start != 0:
        return False
    end_sample = round(audio_duration * rate)
    target_end = round((start if embedded else 0) * rate) + round(duration * rate)
    if embedded:
        # At most two AAC frames of container skew, not arbitrary timeline trims.
        if not 0 <= round(start * rate) <= 2048 or not -1 <= end_sample - target_end <= 2048:
            return False
    elif abs(audio_duration - duration) > 1 / rate:
        return False
    packets = json.loads(run([
        'ffprobe', '-v', 'error', '-select_streams', str(stream['index']), '-show_packets',
        '-show_entries', 'packet=pts,duration,side_data_list', '-of', 'json', str(path)]))['packets']
    if not packets:
        return False
    expected = None
    for index, packet in enumerate(packets):
        pts, length = packet.get('pts'), packet.get('duration')
        if not isinstance(pts, int) or not isinstance(length, int) or not 0 < length <= 1024:
            return False
        if index < len(packets) - 1 and length != 1024:
            return False
        skip = 0
        for side in packet.get('side_data_list', []):
            if side.get('side_data_type') == 'Skip Samples':
                skip = side.get('skip_samples', 0)
                discard = side.get('discard_padding', 0)
                if discard and (index != len(packets) - 1 or discard != 1024 - length):
                    return False
                if index and skip:
                    return False
        if index == 0:
            if not 0 <= skip <= 2048 or pts + skip != 0:
                return False
        elif pts != expected:
            return False
        expected = pts + length
    return abs(expected - end_sample) <= 1


def key(prefix, value):
    encoded = json.dumps([PACKAGER_VERSION, value], sort_keys=True, separators=(',', ':')).encode()
    return prefix + '-' + hashlib.sha256(encoded).hexdigest()


def quoted(value):
    # RFC 8216 quoted-string cannot contain quotes or CR/LF; backslash is not an escape.
    return '"' + ''.join("'" if c == '"' else ' ' if ord(c) < 32 or ord(c) == 127 or 0xD800 <= ord(c) <= 0xDFFF else c for c in value) + '"'


class Packager:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def publish(self, cache_key, build):
        destination = self.root / cache_key
        if destination.exists() or destination.is_symlink():
            playlist = 'master.m3u8' if cache_key.startswith('m-') else 'index.m3u8'
            if self.media_path(cache_key, playlist):
                return destination
            raise ValueError('Incomplete HLS cache; remove the damaged generated directory: ' + cache_key)
        temporary = Path(tempfile.mkdtemp(prefix='.stage-', dir=self.root))
        try:
            build(temporary)
            # This exact allowlist also prevents serving partially written or extra files.
            files = sorted(p.name for p in temporary.iterdir() if MEDIA_FILE.fullmatch(p.name))
            if not files:
                raise ValueError('HLS packaging produced no media')
            (temporary / '.complete.json').write_text(json.dumps(files), encoding='utf-8')
            os.rename(temporary, destination)
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)
        return destination

    def media_path(self, cache_key, filename):
        if not CACHE_KEY.fullmatch(cache_key) or not MEDIA_FILE.fullmatch(filename):
            return None
        folder = self.root / cache_key
        if folder.is_symlink() or not folder.is_dir():
            return None
        try:
            marker = folder / '.complete.json'
            if marker.is_symlink():
                return None
            files = json.loads(marker.read_text(encoding='utf-8'))
            if not isinstance(files, list) or not all(isinstance(name, str) and MEDIA_FILE.fullmatch(name) for name in files):
                return None
        except (OSError, ValueError):
            return None
        path = folder / filename
        if filename not in files or path.is_symlink() or not path.is_file():
            return None
        return path

    def hls(self, folder, inputs, mapping, codecs):
        # Keep negative decode timestamps: MOV edit lists remove AAC priming and
        # video reorder delay instead of shifting each rendition by a different amount.
        run(['ffmpeg', '-v', 'error', '-nostdin', '-y', *inputs, *mapping,
             '-map_metadata', '-1', '-map_chapters', '-1', *codecs,
             '-avoid_negative_ts', 'disabled', '-f', 'hls', '-hls_time', '6',
             '-hls_playlist_type', 'vod', '-hls_segment_type', 'fmp4',
             '-hls_segment_options', 'use_editlist=1:avoid_negative_ts=disabled',
             '-hls_flags', 'independent_segments', '-hls_fmp4_init_filename', 'init.mp4',
             '-hls_segment_filename', str(folder / 'seg-%06d.m4s'), str(folder / 'index.m3u8')])
        playlist = folder / 'index.m3u8'
        text = playlist.read_text(encoding='utf-8')
        if '#EXT-X-ENDLIST' not in text or not any(folder.glob('seg-*.m4s')):
            raise ValueError('Incomplete HLS rendition')

    def prepare(self, project, registry):
        source_id = project['source_asset_id']
        source = registry[source_id]
        video, duration, _ = inspect_stream(source['path'], 'video')
        if video.get('codec_name') != 'h264':
            raise ValueError('Shared-video HLS currently requires an H.264 source')
        start = float(video.get('start_time', 0))
        if not math.isfinite(start):
            raise ValueError('Invalid video start timestamp')
        video_key = key('v', source['asset']['sha256'])
        video_folder = self.publish(video_key, lambda folder: self.hls(
            folder, ['-copyts', '-itsoffset', str(-start), '-i', source['path']],
            ['-map', '0:' + str(video['index'])], ['-c:v', 'copy']))
        samples = round(duration * 48000)
        audio_keys, tracks, available = [], [], []
        for track in project['tracks']:
            embedded = track['kind'] == 'embedded'
            entry = source if embedded else registry[track['asset_id']]
            offset = 0 if embedded else track['timeline_offset_ms']
            identity = [source['asset']['sha256'] if embedded else None, duration,
                        entry['asset']['sha256'], track.get('audio_stream_index'), offset, start if embedded else 0]
            # Keep valid old normalized renditions and the shared-video key reusable.
            normalized_key = key('a', identity)
            copy_key = key('a', [*identity, 'aac-editlist-copy-v1'])
            audio_key = next((candidate for candidate in (normalized_key, copy_key)
                              if self.media_path(candidate, 'index.m3u8')), None)
            if audio_key is None:
                if track['id'] != project['selected_track_id']:
                    continue
                audio_stream, audio_duration, _ = inspect_stream(entry['path'], 'audio')
                copy_audio = can_copy_aac(entry['path'], audio_stream, audio_duration, duration, start, embedded, offset)
                audio_key = copy_key if copy_audio else normalized_key
                def build_audio(folder):
                    if copy_audio:
                        inputs = ['-copyts']
                        if embedded:
                            inputs += ['-itsoffset', str(-start)]
                        inputs += ['-i', entry['path']]
                        # Output -t keeps the final overlapping AAC packet. MOV
                        # edit lists trim leading samples without altering packets.
                        self.hls(folder, inputs, ['-map', '0:' + str(audio_stream['index'])],
                                 ['-c:a', 'copy', '-t', str(duration)])
                        return
                    if embedded:
                        timing = f'asetpts=PTS-({start})/TB,aresample=48000:async=1:first_pts=0'
                    else:
                        timing = 'asetpts=PTS-STARTPTS,aresample=48000'
                        if offset < 0:
                            timing += f',atrim=start_sample={-offset * 48},asetpts=PTS-STARTPTS'
                        elif offset > 0:
                            timing += f',adelay={offset}:all=1'
                    # A finite silence bed survives even when atrim consumes the entire input.
                    filters = (f'[0:{audio_stream["index"]}]{timing},aformat=sample_fmts=fltp:channel_layouts=stereo[a];'
                               f'anullsrc=r=48000:cl=stereo,atrim=end_sample={samples}[silence];'
                               f'[silence][a]amix=inputs=2:duration=first:dropout_transition=0:normalize=0,'
                               f'atrim=end_sample={samples},asetpts=N/SR/TB[out]')
                    self.hls(folder, ['-copyts', '-i', entry['path']], ['-filter_complex', filters, '-map', '[out]'],
                             ['-c:a', 'aac', '-b:a', '192k', '-ar', '48000'])
                self.publish(audio_key, build_audio)
            audio_keys.append(audio_key)
            available.append(track)
            tracks.append({'id': track['id'], 'audio_index': len(tracks), 'label': track['label'], 'language': track['language']})
        master_key = key('m', [video_key, [[audio_key, track['id'], track['label'], track['language'], track['kind']]
                                        for audio_key, track in zip(audio_keys, available)], project['selected_track_id']])
        def build_master(folder):
            lines = ['#EXTM3U', '#EXT-X-VERSION:7', '#EXT-X-INDEPENDENT-SEGMENTS']
            for audio_key, track in zip(audio_keys, available):
                selected = track['id'] == project['selected_track_id']
                lines.append('#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="audio",NAME=' + quoted(track['label'] + ' [' + quote(track['id'], safe='') + ']') +
                             ',LANGUAGE=' + quoted(track['language']) + ',DEFAULT=' + ('YES' if selected else 'NO') +
                             ',AUTOSELECT=' + ('YES' if selected else 'NO') + ',URI="/hls/' + audio_key + '/index.m3u8"')
            # A conservative peak bandwidth derived from segment bytes, not a hardcoded codec profile.
            playlist = (video_folder / 'index.m3u8').read_text(encoding='utf-8')
            lengths = [float(v) for v in re.findall(r'#EXTINF:([0-9.]+)', playlist)]
            segments = sorted(video_folder.glob('seg-*.m4s'))
            bandwidth = math.ceil(max(p.stat().st_size * 8 / d for p, d in zip(segments, lengths)) + 256000)
            lines += [f'#EXT-X-STREAM-INF:BANDWIDTH={bandwidth},RESOLUTION={video["width"]}x{video["height"]},AUDIO="audio"',
                      '/hls/' + video_key + '/index.m3u8']
            (folder / 'master.m3u8').write_text('\n'.join(lines) + '\n', encoding='utf-8')
        self.publish(master_key, build_master)
        def media_bytes(cache_key):
            return sum(p.stat().st_size for p in (self.root / cache_key).iterdir() if p.suffix in ('.mp4', '.m4s'))
        return {'manifest_url': '/hls/' + master_key + '/master.m3u8', 'tracks': tracks, 'video_key': video_key,
                'video_bytes': media_bytes(video_key), 'audio_bytes': sum(media_bytes(k) for k in set(audio_keys)),
                'source_asset_id': source_id}
