#!/usr/bin/env python3
"""Private subprocess renderer: immutable published JSON -> isolated AAC timeline.

Invoked only by CacheController using the installed mlx-audio interpreter. Natural
PCM takes are temporary cache entries; no output is registered as a permanent asset.
"""
import argparse
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import wave

from demand_cache import CacheController, file_sha256
from voice_profiles import RATE


def read_pcm(path):
    import numpy as np
    with wave.open(str(path), 'rb') as wav:
        if wav.getnchannels() != 1 or wav.getsampwidth() != 2 or wav.getframerate() != RATE:
            raise ValueError('Natural speech must have a true mono 16-bit 24000 Hz PCM header')
        frames = wav.getnframes()
        raw = wav.readframes(frames)
        if frames <= 0 or len(raw) != frames * 2:
            raise ValueError('Incomplete PCM take')
        return np.frombuffer(raw, dtype='<i2').astype(np.float32) / 32768


def write_pcm(path, audio):
    import numpy as np
    peak = float(np.max(np.abs(audio)))
    if not np.isfinite(audio).all() or peak < 0.0001:
        raise ValueError('Invalid or silent generated speech')
    audio = audio * min(1, 0.99 / peak)
    with wave.open(str(path), 'wb') as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(RATE)
        wav.writeframes(np.rint(audio * 32768).astype('<i2').tobytes())


def natural_take(snap, text, root, work, model_state):
    import numpy as np
    take_key = CacheController.take_key(snap, text)
    folder, path = root / take_key, root / take_key / 'take.wav'
    try:
        if folder.is_symlink() or path.is_symlink() or (folder / 'complete.json').is_symlink():
            raise ValueError('Unsafe cached take')
        meta = json.loads((folder / 'complete.json').read_text(encoding='utf-8'))
        if meta['key'] != take_key or meta['sha256'] != file_sha256(path):
            raise ValueError('Cached take integrity mismatch')
        audio = read_pcm(path)
        os.utime(folder, None)
        return audio, path
    except (OSError, ValueError, KeyError, TypeError, wave.Error):
        pass
    if folder.is_symlink():
        folder.unlink()
    elif folder.exists():
        shutil.rmtree(folder)
    import mlx.core as mx
    if not model_state:
        from importlib.metadata import version
        from mlx_audio.tts.utils import load_model
        params = snap['profile']
        if version('mlx-audio') != params['mlx_audio_version'] or version('mlx') != params['mlx_version']:
            raise ValueError('Installed synthesis runtime differs from the immutable profile')
        from huggingface_hub import snapshot_download
        # Respect HF_HOME/HF_HUB_CACHE and HF_HUB_OFFLINE rather than a personal cache path.
        local = Path(snapshot_download(params['model'], revision=params['model_revision']))
        model_state.append(load_model(str(local)))
    params = snap['profile']
    mx.random.seed(params['seed'])
    results = list(model_state[0].generate(text=text, voice=params['voice'], instruct=params['instruction'],
                                           lang_code=params['lang_code'], temperature=params['temperature'],
                                           max_tokens=params['max_tokens']))
    if not results or any(result.token_count >= params['max_tokens'] for result in results):
        raise ValueError('Speech generation was absent or reached its token limit; nothing will be published')
    if any(result.sample_rate != RATE for result in results):
        raise ValueError('Unexpected native TTS sample rate; refusing to relabel frame headers')
    audio = np.concatenate([np.asarray(result.audio).reshape(-1) for result in results])
    if not len(audio):
        raise ValueError('Speech generation returned no PCM frames')
    stage = Path(tempfile.mkdtemp(prefix='take-', dir=work))
    try:
        temporary = stage / 'take.wav'
        write_pcm(temporary, audio)
        (stage / 'complete.json').write_text(json.dumps({'key': take_key, 'sha256': file_sha256(temporary)}), encoding='utf-8')
        os.replace(stage, folder)
    finally:
        if stage.exists():
            shutil.rmtree(stage)
    del results, audio
    mx.clear_cache()
    return read_pcm(path), path


def render(snap):
    import numpy as np
    output, root, work = (Path(snap[name]) for name in ('output', 'takes_root', 'work'))
    params = snap['profile']
    if params['rate'] != RATE:
        raise ValueError('Unsupported renderer sample rate')
    master = np.zeros(round(snap['source_duration_ms'] * RATE / 1000), dtype=np.float32)
    model_state = []
    done = 0
    for segment in snap['segments']:
        text = segment['published_text']
        if not text.strip():
            continue
        audio, natural = natural_take(snap, text, root, work, model_state)
        slot = (segment['source_end_ms'] - segment['source_start_ms'] - params['slot_margin_ms']) / 1000
        speed = max(1, len(audio) / RATE / slot) if slot > 0 else math.inf
        if not math.isfinite(speed) or speed > params['max_tempo']:
            raise ValueError(f"Segment {segment['id']} needs {speed:.3f}x tempo (maximum 1.2x); shorten its published text or extend its slot")
        aligned = work / 'aligned.wav'
        completed = subprocess.run(['ffmpeg', '-v', 'error', '-nostdin', '-y', '-i', str(natural),
                                    '-af', f'atempo={speed:.9f}', '-ar', str(RATE), '-ac', '1', str(aligned)],
                                   capture_output=True, timeout=300)
        if completed.returncode:
            raise ValueError('Pitch-preserving alignment failed: ' + completed.stderr.decode('utf-8', errors='replace')[-1500:])
        audio = read_pcm(aligned)
        start = round(segment['source_start_ms'] * RATE / 1000)
        end = start + len(audio)
        if end > round(segment['source_end_ms'] * RATE / 1000) or end > len(master):
            raise ValueError('Aligned audio overran its published slot: ' + segment['id'])
        master[start:end] = audio
        aligned.unlink()
        done += 1
        print('DUB_PROGRESS ' + json.dumps({'done': done}), flush=True)
    # No source soundtrack is an input: silence between speech is intentional.
    # Encode directly from PCM memory rather than retaining master WAV/MP3 copies.
    pcm = np.rint(np.clip(master, -0.99, 0.99) * 32768).astype('<i2')
    del master
    encoded = subprocess.run(['ffmpeg', '-v', 'error', '-nostdin', '-y', '-f', 's16le', '-ar', str(RATE),
                              '-ac', '1', '-i', 'pipe:0', '-af', params['normalization'], '-ar', str(RATE),
                              '-ac', '1', '-c:a', params['codec'], '-b:a', params['bitrate'], str(output)],
                             input=memoryview(pcm).cast('B'), capture_output=True, timeout=900)
    if encoded.returncode:
        raise ValueError('Dialogue encoding failed: ' + encoded.stderr.decode('utf-8', errors='replace')[-1500:])


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--request', type=Path, required=True)
    args = parser.parse_args()
    render(json.loads(args.request.read_text(encoding='utf-8')))
