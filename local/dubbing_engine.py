#!/usr/bin/env python3
"""Real end-to-end voice-cloning dubbing engine:
1. Extract & transcribe source video A dialogue (Whisper ASR).
2. Translate Video A's dialogue into target language (Chinese).
3. Extract clean reference audio slice & transcribe reference B (Whisper ASR).
4. Clone Voice B speaking Video A's translated content (Qwen3-TTS Base ICL).
5. Align timeline to Video A and stream progressive preview / full AAC audio.
"""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.parse
import urllib.request
import uuid
import wave

RATE = 24000
BASE_MODEL = os.environ.get('TALETONE_CLONE_MODEL', 'mlx-community/Qwen3-TTS-12Hz-1.7B-Base-6bit')
BASE_REVISION = os.environ.get('TALETONE_CLONE_REVISION', '34ff5318365b59cba9c03ff729f2eee0814caf72')
WHISPER_MODEL = os.environ.get('TALETONE_WHISPER_MODEL', 'mlx-community/whisper-small-mlx')


def find_mlx_python():
    env_py = os.environ.get('TALETONE_MLX_PYTHON')
    if env_py and Path(env_py).expanduser().is_file():
        return str(Path(env_py).expanduser())
    default_uv = Path.home() / '.local/share/uv/tools/mlx-audio/bin/python'
    if default_uv.is_file():
        return str(default_uv)
    return 'python3'


def find_whisper_cmd():
    uv_cache = Path.home() / '.cache/uv/archive-v0'
    if uv_cache.is_dir():
        for sub in uv_cache.iterdir():
            cand = sub / 'bin/mlx_whisper'
            if cand.is_file():
                return [str(cand)]
    if shutil.which('mlx_whisper'):
        return ['mlx_whisper']
    return ['uvx', '--from', 'mlx-whisper', 'mlx_whisper']


def probe_duration_s(media_path):
    res = subprocess.run([
        'ffprobe', '-v', 'error', '-show_entries', 'format=duration',
        '-of', 'default=noprint_wrappers=1:nokey=1', str(media_path)
    ], capture_output=True, text=True, check=True)
    return float(res.stdout.strip())


def run_whisper_json(wav_path, out_dir):
    cmd = find_whisper_cmd() + [
        str(wav_path),
        '--model', WHISPER_MODEL,
        '--output-format', 'json',
        '--output-dir', str(out_dir)
    ]
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        # Retry without offline restriction if uvx was invoked
        raise RuntimeError(f"Whisper ASR 识别失败: {res.stderr[-500:]}")
    json_file = Path(out_dir) / (Path(wav_path).stem + '.json')
    if not json_file.is_file():
        raise RuntimeError("Whisper 未输出识别结果 JSON")
    return json.loads(json_file.read_text(encoding='utf-8'))


def merge_asr_segments(raw_segments, max_window_s=15.0, max_gap_s=1.2):
    """Merge short Whisper caption fragments into natural speaking sentences."""
    cleaned = []
    for seg in raw_segments:
        txt = (seg.get('text') or '').strip()
        if not txt or re.fullmatch(r'[\[\(（].*?[\]\)）]', txt):
            continue
        if seg.get('no_speech_prob', 0.0) > 0.8:
            continue
        start = float(seg.get('start', 0.0))
        end = float(seg.get('end', start + 1.0))
        if end <= start:
            continue
        cleaned.append({'start': start, 'end': end, 'text': txt})

    if not cleaned:
        return []

    merged = []
    cur = dict(cleaned[0])
    for nxt in cleaned[1:]:
        gap = nxt['start'] - cur['end']
        combined_dur = nxt['end'] - cur['start']
        ends_sentence = cur['text'].endswith(('.', '?', '!', '。', '？', '！'))
        if gap <= max_gap_s and combined_dur <= max_window_s and not (ends_sentence and (cur['end'] - cur['start']) >= 5.0):
            cur['end'] = nxt['end']
            cur['text'] = (cur['text'] + ' ' + nxt['text']).strip()
        else:
            merged.append(cur)
            cur = dict(nxt)
    merged.append(cur)
    return merged


def translate_text_to_zh(text, src_lang='auto'):
    if not text.strip():
        return ''
    if src_lang in ('zh', 'zh-cn', 'zh-tw', 'chinese'):
        return text.strip()
    url = (
        'https://translate.googleapis.com/translate_a/single?client=gtx'
        f'&sl=auto&tl=zh-CN&dt=t&q={urllib.parse.quote(text)}'
    )
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
    with urllib.request.urlopen(req, timeout=12) as resp:
        data = json.loads(resp.read().decode('utf-8'))
        return ''.join(part[0] for part in data[0] if part and part[0]).strip()


WORKER_SCRIPT = r'''
import json, math, subprocess, sys, wave
from pathlib import Path
import numpy as np
import mlx.core as mx
from mlx_audio.tts.utils import load_model
from mlx_audio.utils import load_audio
from huggingface_hub import snapshot_download

RATE = 24000

def read_pcm(path):
    with wave.open(str(path), 'rb') as wf:
        raw = wf.readframes(wf.getnframes())
        return np.frombuffer(raw, dtype='<i2').astype(np.float32) / 32768.0

def write_pcm(path, audio):
    audio = np.asarray(audio, dtype=np.float32).reshape(-1)
    peak = float(np.max(np.abs(audio))) if len(audio) else 0.0
    if peak > 0.99:
        audio = audio * (0.99 / peak)
    with wave.open(str(path), 'wb') as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(RATE)
        wf.writeframes(np.rint(np.clip(audio, -0.99, 0.99) * 32768.0).astype('<i2').tobytes())

def encode_master(master, out_path):
    pcm = np.rint(np.clip(master, -0.99, 0.99) * 32768.0).astype('<i2')
    tmp_out = Path(str(out_path) + '.tmp.m4a')
    res = subprocess.run([
        'ffmpeg', '-v', 'error', '-nostdin', '-y',
        '-f', 's16le', '-ar', str(RATE), '-ac', '1', '-i', 'pipe:0',
        '-af', 'loudnorm=I=-16:TP=-1.5:LRA=7',
        '-ar', str(RATE), '-ac', '1', '-c:a', 'aac', '-b:a', '160k',
        '-movflags', '+faststart', str(tmp_out)
    ], input=memoryview(pcm).cast('B'), capture_output=True)
    if res.returncode != 0:
        raise RuntimeError('AAC 编码失败: ' + res.stderr.decode('utf-8', 'replace')[-500:])
    tmp_out.replace(out_path)

def main():
    cfg = json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))
    work = Path(cfg['work_dir'])
    out_audio = Path(cfg['output_audio'])
    preview_audio = Path(cfg['preview_audio'])
    segments = cfg['segments']
    total_dur_s = float(cfg['source_duration_s'])

    local_ckpt = snapshot_download(cfg['model'], revision=cfg['revision'])
    model = load_model(str(local_ckpt))
    ref_audio = load_audio(str(cfg['ref_wav']), sample_rate=RATE)
    ref_text = cfg['ref_text']

    master = np.zeros(max(RATE, round(total_dur_s * RATE)), dtype=np.float32)
    total = len(segments)
    preview_emitted = False

    batch_size = 2
    done = 0
    for offset in range(0, total, batch_size):
        batch = segments[offset:offset + batch_size]
        texts = [seg['zh_text'] for seg in batch]
        mx.random.seed(17)
        results = list(model.batch_generate(
            texts,
            ref_audio=ref_audio,
            ref_text=ref_text,
            temperature=0.65,
            lang_code='Chinese',
            max_tokens=900,
            top_k=50,
            top_p=1.0,
            repetition_penalty=1.45
        ))
        by_idx = {r.sequence_idx: r for r in results}
        for i, seg in enumerate(batch):
            res = by_idx.get(i)
            if res is None:
                continue
            raw_wav = work / f"raw_{offset + i}.wav"
            write_pcm(raw_wav, res.audio)
            audio_arr = read_pcm(raw_wav)
            nat_dur = len(audio_arr) / RATE
            slot_s = max(0.4, float(seg['end']) - float(seg['start']) - 0.05)
            speed = max(1.0, min(1.45, nat_dur / slot_s))
            if speed > 1.02:
                aligned_wav = work / f"aligned_{offset + i}.wav"
                subprocess.run([
                    'ffmpeg', '-v', 'error', '-nostdin', '-y', '-i', str(raw_wav),
                    '-af', f'atempo={speed:.6f}', '-ar', str(RATE), '-ac', '1', str(aligned_wav)
                ], check=True)
                audio_arr = read_pcm(aligned_wav)
                aligned_wav.unlink(missing_ok=True)
            raw_wav.unlink(missing_ok=True)

            start_idx = max(0, round(float(seg['start']) * RATE))
            end_idx = min(len(master), start_idx + len(audio_arr))
            if end_idx > start_idx:
                master[start_idx:end_idx] = audio_arr[:end_idx - start_idx]

            done += 1
            print('CLONE_PROGRESS ' + json.dumps({
                'done': done,
                'total': total,
                'latest_zh': seg['zh_text'],
                'end_s': float(seg['end'])
            }, ensure_ascii=False), flush=True)

        mx.clear_cache()

        # Emit an early playable preview as soon as first batch completes
        if not preview_emitted and done >= min(2, total):
            encode_master(master, preview_audio)
            preview_emitted = True
            print('CLONE_PREVIEW ' + json.dumps({'done': done, 'total': total}), flush=True)

    encode_master(master, out_audio)
    print('CLONE_COMPLETE ' + json.dumps({'done': done, 'total': total}), flush=True)

if __name__ == '__main__':
    main()
'''


class DubbingEngine:
    """Manages asynchronous ASR + Translation + Voice-Cloning synthesis jobs."""

    def __init__(self, data_dir):
        self.root = Path(data_dir) / 'dub_jobs'
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()
        self.jobs = {}
        self.executor = ThreadPoolExecutor(max_workers=1)

    def start_job(self, video_path, ref_audio_path, ref_name='参考音色', max_duration_s=None):
        video_path = Path(video_path).resolve(strict=True)
        ref_audio_path = Path(ref_audio_path).resolve(strict=True)
        job_id = uuid.uuid4().hex
        job_dir = self.root / job_id
        job_dir.mkdir(parents=True, exist_ok=True)

        job = {
            'id': job_id,
            'state': 'queued',
            'phase': '排队等待执行…',
            'done': 0,
            'total': 0,
            'ref_name': ref_name,
            'video_name': video_path.name,
            'preview_ready': False,
            'preview_url': None,
            'audio_url': None,
            'duration_ms': 0,
            'segments': [],
            'latest_text': '',
            'error': None,
            'created_at': time.time(),
            'job_dir': str(job_dir),
        }
        with self.lock:
            self.jobs[job_id] = job

        self.executor.submit(self._run_pipeline, job_id, video_path, ref_audio_path, max_duration_s)
        return self.get_job(job_id)

    def get_job(self, job_id):
        with self.lock:
            job = self.jobs.get(job_id)
            if not job:
                return None
            return {k: v for k, v in job.items() if k != 'job_dir'}

    def media_path(self, job_id, kind='final'):
        with self.lock:
            job = self.jobs.get(job_id)
            if not job:
                return None
            job_dir = Path(job['job_dir'])
        target = job_dir / ('preview.m4a' if kind == 'preview' else 'dubbed.m4a')
        return target if target.is_file() else None

    def _update(self, job_id, **kwargs):
        with self.lock:
            if job_id in self.jobs:
                self.jobs[job_id].update(kwargs)

    def _run_pipeline(self, job_id, video_path, ref_audio_path, max_duration_s):
        job_dir = Path(self.jobs[job_id]['job_dir'])
        try:
            full_dur_s = probe_duration_s(video_path)
            target_dur_s = min(full_dur_s, float(max_duration_s)) if max_duration_s else full_dur_s
            self._update(job_id, state='running', duration_ms=round(full_dur_s * 1000),
                         phase='第 1/4 步：正在识别源视频 A 的英语语音内容 (Whisper ASR)…')

            # 1. Extract & ASR Video A
            src_wav = job_dir / 'video_a_16k.wav'
            trim_args = ['-t', str(target_dur_s)] if max_duration_s else []
            subprocess.run([
                'ffmpeg', '-v', 'error', '-nostdin', '-y',
                '-i', str(video_path), *trim_args,
                '-vn', '-ar', '16000', '-ac', '1', str(src_wav)
            ], check=True)

            asr_a = run_whisper_json(src_wav, job_dir)
            src_lang = asr_a.get('language', 'en')
            merged = merge_asr_segments(asr_a.get('segments', []))
            if not merged:
                raise RuntimeError('未能从源视频 A 中识别到有效的人声台词，请确认视频包含清晰语音。')

            # 2. Translate Video A's segments to Chinese
            self._update(job_id, phase=f'第 2/4 步：已识别出 {len(merged)} 段源视频台词，正在翻译为自然中文…',
                         total=len(merged))
            translated_segments = []
            for idx, seg in enumerate(merged):
                zh = translate_text_to_zh(seg['text'], src_lang=src_lang)
                translated_segments.append({
                    'id': f'seg-{idx+1:03d}',
                    'start': round(seg['start'], 2),
                    'end': round(seg['end'], 2),
                    'src_text': seg['text'],
                    'zh_text': zh or seg['text']
                })

            self._update(
                job_id,
                segments=translated_segments,
                latest_text=translated_segments[0]['zh_text'],
                phase='第 3/4 步：正在提取音频 B 的音色特征与参考文本…'
            )

            # 3. Extract 9s clean voice slice from Reference B & transcribe for ICL prompt
            ref_dur_s = probe_duration_s(ref_audio_path)
            ref_start = 1.0 if ref_dur_s > 12.0 else 0.0
            ref_len = min(9.5, max(2.5, ref_dur_s - ref_start))
            ref_24k = job_dir / 'ref_b_24k.wav'
            ref_16k = job_dir / 'ref_b_16k.wav'
            subprocess.run([
                'ffmpeg', '-v', 'error', '-nostdin', '-y',
                '-ss', str(ref_start), '-i', str(ref_audio_path), '-t', str(ref_len),
                '-vn', '-ar', str(RATE), '-ac', '1', str(ref_24k)
            ], check=True)
            subprocess.run([
                'ffmpeg', '-v', 'error', '-nostdin', '-y',
                '-i', str(ref_24k), '-ar', '16000', '-ac', '1', str(ref_16k)
            ], check=True)

            asr_b = run_whisper_json(ref_16k, job_dir)
            ref_text = (asr_b.get('text') or '').strip()
            if not ref_text:
                ref_text = '这是一段用于提取说话人声音特征的参考语音样本。'

            # 4. Synthesize Video A's Chinese script using Voice B (Qwen3-TTS Base ICL)
            self._update(
                job_id,
                phase=f'第 4/4 步：正在用「{self.jobs[job_id]["ref_name"]}」的声音朗诵视频 A 的内容 (0/{len(translated_segments)})…'
            )

            worker_py = job_dir / 'run_clone_worker.py'
            worker_py.write_text(WORKER_SCRIPT, encoding='utf-8')
            out_audio = job_dir / 'dubbed.m4a'
            preview_audio = job_dir / 'preview.m4a'
            cfg_path = job_dir / 'request.json'
            cfg_path.write_text(json.dumps({
                'model': BASE_MODEL,
                'revision': BASE_REVISION,
                'work_dir': str(job_dir),
                'ref_wav': str(ref_24k),
                'ref_text': ref_text,
                'source_duration_s': full_dur_s,
                'segments': translated_segments,
                'preview_audio': str(preview_audio),
                'output_audio': str(out_audio),
            }, ensure_ascii=False, indent=2), encoding='utf-8')

            mlx_py = find_mlx_python()
            proc = subprocess.Popen(
                [mlx_py, '-u', str(worker_py), str(cfg_path)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding='utf-8',
                errors='replace'
            )

            for line in proc.stdout:
                line = line.strip()
                if line.startswith('CLONE_PROGRESS '):
                    ev = json.loads(line[len('CLONE_PROGRESS '):])
                    d, t = ev['done'], ev['total']
                    self._update(
                        job_id,
                        done=d,
                        total=t,
                        latest_text=ev.get('latest_zh', ''),
                        phase=f'正在用「{self.jobs[job_id]["ref_name"]}」朗诵视频 A 台词 ({d}/{t})：{ev.get("latest_zh", "")[:28]}…'
                    )
                elif line.startswith('CLONE_PREVIEW '):
                    self._update(
                        job_id,
                        preview_ready=True,
                        preview_url=f'/api/dub-jobs/{job_id}/preview.m4a'
                    )

            stderr_out = proc.stderr.read()
            code = proc.wait()
            if code != 0 or not out_audio.is_file():
                raise RuntimeError(f'音色克隆合成失败: {stderr_out[-600:]}')

            # Cleanup temporary WAVs
            for tmp_wav in (src_wav, ref_16k, ref_24k, worker_py):
                tmp_wav.unlink(missing_ok=True)

            self._update(
                job_id,
                state='ready',
                done=len(translated_segments),
                audio_url=f'/api/dub-jobs/{job_id}/audio.m4a',
                phase=f'已完成！共将视频 A 的 {len(translated_segments)} 段内容用「{self.jobs[job_id]["ref_name"]}」的声音完整说出。'
            )
        except Exception as exc:
            self._update(
                job_id,
                state='error',
                error=str(exc),
                phase=f'译配生成出错：{exc}'
            )
