'use strict';

const $ = id => document.getElementById(id);
const video = $('video');
let project = null;
let savedProject = null;
let assets = new Map();
let busy = false;
let lastSave = '';
let player;
const clone = value => JSON.parse(JSON.stringify(value));
const selectedTrack = () => project?.tracks.find(track => track.id === project.selected_track_id);
const dirty = () => project && JSON.stringify(project) !== JSON.stringify(savedProject);
const time = seconds => {
  const value = Math.max(0, Math.floor(Number.isFinite(seconds) ? seconds : 0));
  const minutes = Math.floor(value / 60);
  return `${String(minutes).padStart(2, '0')}:${String(value % 60).padStart(2, '0')}`;
};
function notice(message, kind = '') {
  $('notice').textContent = message;
  $('notice').className = `notice ${kind}`;
  $('retryLoad').hidden = Boolean(project) || kind !== 'error';
}
async function api(path, options = {}) {
  const response = await fetch(path, {cache: 'no-store', ...options});
  let result;
  try { result = await response.json(); }
  catch { throw new Error(`服务器未返回有效 JSON（HTTP ${response.status}）`); }
  if (!response.ok) {
    const error = new Error(result.error || `请求失败（HTTP ${response.status}）`);
    Object.assign(error, {status: response.status, code: result.code, track_id: result.track_id});
    throw error;
  }
  return result;
}

function trackAudioConfiguration(track) {
  const common = {id: track.id, language: track.language, kind: track.kind};
  if (track.kind === 'generated') return {...common, profile_id: track.profile_id,
    timeline_offset_ms: track.timeline_offset_ms, revision: track.revision,
    segments: track.segments.map(segment => ({id: segment.id, source_start_ms: segment.source_start_ms,
      source_end_ms: segment.source_end_ms, published_text: segment.published_text}))};
  return {...common, asset_id: track.asset_id, audio_stream_index: track.audio_stream_index,
    timeline_offset_ms: track.timeline_offset_ms};
}
function generatedSignature(track, value = project) {
  const source = assets.get(value.source_asset_id);
  return JSON.stringify({source_asset_id: value.source_asset_id, source_sha256: value.source_sha256,
    registered_source_sha256: source?.sha256,
    track: trackAudioConfiguration(track)});
}
const isGenerating = entry => entry?.state === 'queued' || entry?.state === 'running';
const byteSize = bytes => `${(Number(bytes || 0) / 1048576).toFixed(1)} MiB`;
function abortableDelay(signal, milliseconds) {
  return new Promise((resolve, reject) => {
    const aborted = () => { clearTimeout(timer); reject(new DOMException('切换已取消', 'AbortError')); };
    const timer = setTimeout(() => { signal.removeEventListener('abort', aborted); resolve(); }, milliseconds);
    if (signal.aborted) return aborted();
    signal.addEventListener('abort', aborted, {once: true});
  });
}

// Reading status never starts synthesis. Only the explicit play action calls prepare.
class DemandCache {
  constructor() {
    this.entries = new Map();
    this.bytes = 0;
    this.known = false;
    this.maxBytes = 256 * 1048576;
    this.ttlSeconds = 86400;
    this.error = '';
    this.request = 0;
    this.clearing = false;
  }
  get(track) {
    const entry = this.entries.get(track.id);
    return entry?.signature === generatedSignature(track) ? entry : null;
  }
  reset() {
    clearTimeout(this.timer);
    this.request++;
    this.entries.clear();
    this.known = false;
    this.error = '';
  }
  schedule() {
    clearTimeout(this.timer);
    if (!this.clearing && [...this.entries.values()].some(isGenerating)) {
      this.timer = setTimeout(() => this.refresh().catch(() => this.schedule()), 2000);
    }
  }
  async refresh() {
    if (!project) return;
    const owner = project;
    const snapshot = clone(project);
    const request = ++this.request;
    try {
      const result = await api('/api/cache/status', {method: 'POST',
        headers: {'Content-Type': 'application/json'}, body: JSON.stringify({project: snapshot})});
      if (owner !== project || request !== this.request) return;
      const previous = this.entries;
      this.entries = new Map();
      for (const entry of result.tracks) {
        const track = snapshot.tracks.find(row => row.id === entry.id);
        if (track?.kind !== 'generated') continue;
        const signature = generatedSignature(track, snapshot);
        const old = previous.get(entry.id);
        const observed = old?.signature === signature && old.cache_key === entry.cache_key
          ? {origin: old.origin, cache_hit: old.cache_hit} : {};
        this.entries.set(entry.id, {...observed, ...entry, signature});
      }
      this.bytes = result.bytes;
      this.maxBytes = result.max_bytes;
      this.ttlSeconds = result.ttl_seconds;
      this.known = true;
      this.error = '';
      renderStatus();
      this.schedule();
    } catch (error) {
      if (owner === project && request === this.request) {
        this.error = `暂时无法查询缓存：${error.message}`;
        renderStatus();
      }
      throw error;
    }
  }
  updateJob(job, track, owner, signature, cacheHit) {
    if (owner !== project) return;
    this.request++;
    this.entries.set(track.id, {...job, id: track.id, job_id: job.id, signature, cache_hit: cacheHit});
    this.error = '';
    renderStatus();
    this.schedule();
  }
  async prepare(track, signal) {
    const owner = project;
    const signature = generatedSignature(track);
    const result = await api('/api/cache/prepare', {method: 'POST', signal,
      headers: {'Content-Type': 'application/json'}, body: JSON.stringify({project: clone(project), track_id: track.id})});
    let job = result.job;
    this.updateJob(job, track, owner, signature, result.cache_hit);
    let pollDelay = 200;
    while (isGenerating(job)) {
      await abortableDelay(signal, pollDelay);
      job = (await api(`/api/cache/jobs/${encodeURIComponent(job.id)}`, {signal})).job;
      this.updateJob(job, track, owner, signature, result.cache_hit);
      pollDelay = Math.min(pollDelay * 2, 1500);
    }
    if (signal.aborted) throw new DOMException('切换已取消', 'AbortError');
    if (job.state !== 'ready') throw new Error(job.error || '配音生成未完成，请重试');
    return job;
  }
}
const demandCache = new DemandCache();

// A paused, muted decoder supplies one real frame, never an alternate audio clock.
class SourcePoster {
  constructor() {
    this.key = '';
    this.epoch = 0;
    this.url = '';
    this.loading = false;
  }
  setSource(source) {
    const key = source ? `${source.id}:${source.sha256}:${source.url}` : '';
    if (key === this.key) return;
    this.key = key;
    const token = ++this.epoch;
    this.release?.();
    if (this.url) URL.revokeObjectURL(this.url);
    this.url = '';
    video.removeAttribute('poster');
    this.loading = Boolean(source?.url);
    if (!this.loading) return;
    const preview = document.createElement('video');
    preview.defaultMuted = true;
    preview.muted = true;
    preview.volume = 0;
    preview.autoplay = false;
    preview.playsInline = true;
    preview.disableRemotePlayback = true;
    preview.preload = 'auto';
    let timer;
    let captured = false;
    const release = () => {
      clearTimeout(timer);
      preview.onloadedmetadata = preview.onloadeddata = preview.onseeked = preview.onerror = null;
      preview.pause();
      preview.removeAttribute('src');
      preview.load();
      if (this.release === release) this.release = null;
    };
    this.release = release;
    const failed = () => {
      release();
      if (token !== this.epoch) return;
      this.loading = false;
      renderStatus();
    };
    const position = Math.min(1, Math.max(0, source.duration_ms / 2000 || 0));
    const capture = () => {
      if (captured || token !== this.epoch || preview.seeking || preview.readyState < 2 ||
          Math.abs(preview.currentTime - position) > 0.1 || !preview.videoWidth) return;
      captured = true;
      try {
        const canvas = document.createElement('canvas');
        const scale = Math.min(1, 960 / preview.videoWidth);
        canvas.width = Math.max(1, Math.round(preview.videoWidth * scale));
        canvas.height = Math.max(1, Math.round(preview.videoHeight * scale));
        canvas.getContext('2d').drawImage(preview, 0, 0, canvas.width, canvas.height);
        release();
        canvas.toBlob(blob => {
          if (token !== this.epoch) return;
          this.loading = false;
          if (blob) {
            this.url = URL.createObjectURL(blob);
            video.poster = this.url;
          }
          renderStatus();
        }, 'image/jpeg', 0.85);
      } catch { failed(); }
    };
    preview.onloadedmetadata = () => {
      if (position > 0) preview.currentTime = position;
      capture();
    };
    preview.onloadeddata = preview.onseeked = capture;
    preview.onerror = failed;
    timer = setTimeout(failed, 8000);
    preview.src = source.url;
    preview.load();
  }
}
const sourcePoster = new SourcePoster();
window.addEventListener('pagehide', () => sourcePoster.setSource(null));
window.addEventListener('pageshow', event => {
  if (event.persisted && project) sourcePoster.setSource(assets.get(project.source_asset_id));
});

// One video element consumes a shared HLS video rendition and alternative audio.
// Track switching changes the audio rendition, not video.src or a second clock.
class PlaybackController {
  constructor() {
    this.track = null;
    this.asset = null;
    this.source = null;
    this.desiredPlaying = false;
    this.loading = false;
    this.sourceLoading = false;
    this.error = '';
    this.epoch = 0;
    this.rate = 1;
    this.switchPosition = 0;
    this.silent = '';
    this.descriptorConfigurations = new Map();
    this.descriptor = null;
    this.hls = null;
    this.mode = '';
    this.activeIndex = -1;
    this.activeTrackId = '';
    this.pendingTrackId = '';
    this.waitingForGeneration = false;
    this.generating = false;
    this.phase = '';
    this.waitStarted = 0;
    for (const event of ['timeupdate', 'seeked', 'pause']) video.addEventListener(event, () => renderStatus());
    video.addEventListener('waiting', () => {
      if (this.desiredPlaying && !this.loading) this.setPhase('buffering');
    });
    video.addEventListener('playing', () => {
      if (!this.desiredPlaying || this.loading) { video.pause(); return; }
      this.setPhase('');
      notice(`正在播放「${this.track.label}」。`);
    });
    video.addEventListener('ended', () => {
      this.desiredPlaying = false;
      this.setPhase('');
      notice(`「${this.track.label}」播放完毕，可再次播放。`);
    });
    video.addEventListener('error', () => {
      if (!this.loading && this.descriptor) this.fail('HLS 媒体加载失败；不会回退其他语言。请重试播放以重新检查缓存。');
    });
  }
  canRun() { return !this.loading && !this.waitingForGeneration && !this.error && !video.seeking && video.readyState >= 3; }
  setPhase(phase) {
    this.phase = phase;
    if (phase) {
      if (!this.waitStarted) this.waitStarted = performance.now();
      if (!this.waitTimer) this.waitTimer = setInterval(() => renderStatus(), 1000);
    } else {
      clearInterval(this.waitTimer);
      this.waitTimer = null;
      this.waitStarted = 0;
    }
    renderStatus();
  }
  canReuse(track = this.track) {
    return !this.error && this.descriptor?.tracks.some(row => row.id === track.id) &&
      this.descriptorConfigurations.get(track.id) === generatedSignature(track) &&
      (this.mode === 'HLS.js / MSE' ? Boolean(this.hls) : Boolean(video.currentSrc));
  }
  fail(message) {
    this.error = message;
    this.desiredPlaying = false;
    this.loading = false;
    this.generating = false;
    this.setPhase('');
    video.pause();
    notice(message, 'error');
    renderStatus();
  }
  position() {
    return !this.descriptor || this.waitingForGeneration || (this.loading && this.rebuilding) ? this.switchPosition : video.currentTime;
  }
  detachMedia() {
    video.pause();
    video.muted = true;
    const instance = this.hls;
    this.hls = null;
    instance?.destroy();
    video.removeAttribute('src');
    video.load();
    this.descriptorConfigurations.clear();
    this.descriptor = null;
    this.activeIndex = -1;
    this.activeTrackId = '';
    this.pendingTrackId = '';
    this.mode = '';
  }
  suspend() {
    this.switchPosition = this.position();
    this.epoch++;
    this.abort?.abort();
    this.desiredPlaying = false;
    this.loading = false;
    this.generating = false;
    this.setPhase('');
    this.error = '';
    this.detachMedia();
    this.waitingForGeneration = this.track?.kind === 'generated';
    renderStatus();
  }
  waitFor(target, events, predicate, signal, timeout = 30000) {
    return new Promise((resolve, reject) => {
      let timer;
      const remove = () => {
        clearTimeout(timer);
        for (const event of events) target.removeEventListener(event, ready);
        target.removeEventListener('error', failed);
        signal.removeEventListener('abort', aborted);
      };
      const ready = () => { if (predicate()) { remove(); resolve(); } };
      const failed = () => { remove(); reject(new Error('媒体无法载入')); };
      const aborted = () => { remove(); reject(new DOMException('切换已取消', 'AbortError')); };
      if (signal.aborted) return aborted();
      for (const event of events) target.addEventListener(event, ready);
      target.addEventListener('error', failed);
      signal.addEventListener('abort', aborted, {once: true});
      timer = setTimeout(() => { remove(); reject(new Error('等待媒体超时')); }, timeout);
      ready();
    });
  }
  waitHls(instance, events, predicate, signal) {
    return new Promise((resolve, reject) => {
      let timer;
      const remove = () => {
        clearTimeout(timer);
        for (const event of events) instance.off(event, ready);
        instance.off(Hls.Events.ERROR, failed);
        signal.removeEventListener('abort', aborted);
      };
      const ready = () => { if (predicate()) { remove(); resolve(); } };
      const failed = (_, data) => { if (data.fatal) { remove(); reject(new Error(data.details || 'HLS 播放失败')); } };
      const aborted = () => { remove(); reject(new DOMException('切换已取消', 'AbortError')); };
      if (signal.aborted) return aborted();
      for (const event of events) instance.on(event, ready);
      instance.on(Hls.Events.ERROR, failed);
      signal.addEventListener('abort', aborted, {once: true});
      timer = setTimeout(() => { remove(); reject(new Error('HLS 音轨载入超时')); }, 30000);
      ready();
    });
  }
  async open(source, track) {
    this.suspend();
    this.source = source;
    this.switchPosition = 0;
    await this.choose(track, true);
  }
  async prepare(position, signal) {
    const snapshot = clone(project);
    const result = await api('/api/playback', {
      method: 'POST', signal, headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({project: snapshot})
    });
    if (signal.aborted) throw new DOMException('切换已取消', 'AbortError');
    this.hls?.destroy();
    this.hls = null;
    this.activeIndex = -1;
    this.activeTrackId = '';
    this.pendingTrackId = '';
    this.descriptor = result;
    this.descriptorConfigurations = new Map(result.tracks.map(mapping => {
      const track = snapshot.tracks.find(row => row.id === mapping.id);
      return [mapping.id, generatedSignature(track, snapshot)];
    }));
    this.setPhase('loading');
    notice(`正在加载「${this.track.label}」的视频与声音${this.desiredPlaying ? '…' : '；完成后保持暂停。'}`, 'pending');
    video.pause();
    video.muted = true;
    video.removeAttribute('src');
    video.load();
    if (typeof Hls !== 'undefined' && Hls.isSupported()) {
      this.mode = 'HLS.js / MSE';
      const instance = new Hls({enableWorker: true, startPosition: position, maxBufferLength: 18, maxMaxBufferLength: 36, backBufferLength: 18});
      this.hls = instance;
      instance.on(Hls.Events.AUDIO_TRACK_SWITCHED, (_, data) => {
        if (this.hls !== instance) return;
        this.activeIndex = data.id;
        this.activeTrackId = this.descriptor.tracks.find(t => t.audio_index === data.id)?.id || '';
        if (this.pendingTrackId === this.activeTrackId) {
          this.pendingTrackId = '';
          notice(`已选择「${this.track.label}」· 音轨已随播放/定位生效。`);
          renderStatus();
        }
      });
      instance.on(Hls.Events.ERROR, (_, data) => {
        if (this.hls === instance && data.fatal) this.fail(`HLS 错误：${data.details}；播放已暂停。`);
      });
      instance.attachMedia(video);
      instance.loadSource(result.manifest_url);
      await this.waitHls(instance, [Hls.Events.AUDIO_TRACKS_UPDATED, Hls.Events.MANIFEST_PARSED], () => instance.audioTracks.length >= result.tracks.length, signal);
    } else if (video.canPlayType('application/vnd.apple.mpegurl') && video.audioTracks) {
      this.mode = '原生 HLS';
      video.src = result.manifest_url;
      video.load();
      await this.waitFor(video, ['loadedmetadata', 'canplay'], () => video.audioTracks.length >= result.tracks.length, signal);
    } else {
      throw new Error('此浏览器不支持可切换音轨的 HLS，请使用支持 MSE 的 Chromium 或原生 HLS 浏览器');
    }
  }
  async choose(track, opening = false, allowGeneration = false) {
    if (!this.source) return;
    if (!opening) this.switchPosition = this.position();
    const token = ++this.epoch;
    this.abort?.abort();
    const abort = new AbortController();
    this.abort = abort;
    this.track = track;
    this.asset = track.kind === 'external' ? assets.get(track.asset_id) : track.kind === 'embedded' ? this.source : null;
    const reusable = this.canReuse(track);
    this.rebuilding = !reusable;
    this.loading = true;
    this.generating = false;
    this.waitingForGeneration = false;
    this.error = '';
    this.pendingTrackId = '';
    video.pause();
    video.muted = true;
    notice(`正在检查「${track.label}」的播放准备情况…`, 'pending');
    this.waitStarted = performance.now();
    this.setPhase('checking');
    try {
      // Yield without waiting for animation frames, which hidden tabs can suspend.
      await new Promise(resolve => setTimeout(resolve, 0));
      if (token !== this.epoch) return;
      if (track.kind === 'generated' && !reusable) {
        if (!allowGeneration || !demandCache.get(track)) await demandCache.refresh();
        if (token !== this.epoch) return;
        if (allowGeneration && demandCache.get(track)?.state === 'ready') demandCache.get(track).cache_hit = true;
        if (demandCache.get(track)?.state !== 'ready') {
          this.detachMedia();
          this.waitingForGeneration = true;
          this.rebuilding = true;
          if (!allowGeneration) {
            this.loading = false;
            this.desiredPlaying = false;
            notice(isGenerating(demandCache.get(track)) ? `「${track.label}」正在后台准备。点「等待并试听」可在完成后播放。` : `「${track.label}」尚未生成。点击「生成并试听」才会准备配音。`);
            this.setPhase('');
            return;
          }
          this.generating = true;
          notice('正在本机准备所选语言；匹配的已有已验证配音会直接复用。可暂停等待或稍后回来，完成后按你的播放/暂停选择继续。', 'pending');
          this.setPhase('generating');
          await demandCache.prepare(track, abort.signal);
          if (token !== this.epoch) return;
          this.generating = false;
          this.waitingForGeneration = false;
          this.setPhase('packaging');
        }
      }
      if (!allowGeneration && !this.desiredPlaying && !reusable) {
        this.detachMedia();
        this.loading = false;
        this.waitingForGeneration = false;
        notice(track.kind === 'generated' ? `「${track.label}」缓存就绪。点击播放才会载入视频与声音，不调用 AI。` : `已选择「${track.label}」。点击播放才会载入视频与声音。`);
        this.setPhase('');
        return;
      }
      const rebuild = !this.canReuse(track);
      this.rebuilding = rebuild;
      notice(rebuild ? `正在封装「${track.label}」的播放音轨${this.desiredPlaying ? '…' : '；完成后保持暂停。'}` : `正在切换到「${track.label}」…`, 'pending');
      this.setPhase(rebuild ? 'packaging' : 'switching');
      if (rebuild) await this.prepare(this.switchPosition, abort.signal);
      if (token !== this.epoch) return;
      const mapping = this.descriptor.tracks.find(t => t.id === track.id);
      if (!mapping) throw new Error('播放清单缺少目标音轨');
      if (this.hls) {
        const instance = this.hls;
        // An ended playhead cannot render a new fragment, so SWITCHED may not
        // fire until replay/seek. Keep that request visibly pending, not failed.
        const parkedAtEnd = video.ended || (Number.isFinite(video.duration) && video.currentTime >= video.duration - 0.05);
        if (parkedAtEnd && this.activeIndex !== mapping.audio_index) {
          this.pendingTrackId = track.id;
          instance.audioTrack = mapping.audio_index;
        } else {
          const ready = this.waitHls(instance, [Hls.Events.AUDIO_TRACK_SWITCHED], () => this.activeIndex === mapping.audio_index, abort.signal);
          instance.audioTrack = mapping.audio_index;
          await ready;
        }
      } else {
        const list = video.audioTracks;
        if (!list || list.length <= mapping.audio_index) throw new Error('原生播放器未暴露目标音轨');
        for (let i = 0; i < list.length; i++) list[i].enabled = i === mapping.audio_index;
        this.activeIndex = mapping.audio_index;
      }
      if (token !== this.epoch) return;
      if (rebuild) {
        await this.waitFor(video, ['canplay', 'loadeddata', 'seeked'], () => video.readyState >= 3 && !video.seeking, abort.signal);
        const position = Math.max(0, Math.min(this.switchPosition, Number.isFinite(video.duration) ? video.duration : this.switchPosition));
        if (Math.abs(video.currentTime - position) > 0.1) {
          video.currentTime = position;
          await this.waitFor(video, ['canplay', 'seeked'], () => video.readyState >= 3 && !video.seeking, abort.signal);
        }
      }
      if (token !== this.epoch) return;
      if (!this.pendingTrackId) this.activeTrackId = track.id;
      video.playbackRate = this.rate;
      video.muted = false;
      this.loading = false;
      this.rebuilding = false;
      if (this.desiredPlaying) {
        this.setPhase('starting');
        notice(`正在开始播放「${track.label}」…`, 'pending');
        if (video.paused) await video.play();
        if (token !== this.epoch) return;
        if (!this.desiredPlaying) video.pause();
      } else {
        this.setPhase('');
        notice(this.pendingTrackId ? `已选择「${track.label}」。片尾已暂停，播放或拖动进度后生效。` : `「${track.label}」已就绪，保持暂停。`);
      }
      renderStatus();
      if (token === this.epoch && track.kind === 'generated') demandCache.refresh().catch(() => {});
    } catch (error) {
      if (token !== this.epoch || error.name === 'AbortError') return;
      this.loading = false;
      this.generating = false;
      this.descriptorConfigurations.clear();
      this.setPhase('');
      if (error.code === 'generation_required') {
        this.detachMedia();
        this.waitingForGeneration = true;
        this.desiredPlaying = false;
        demandCache.entries.delete(track.id);
        demandCache.refresh().catch(() => {});
        notice('这份临时配音已过期或被清理，请点击「生成并试听」重新准备。');
        renderStatus();
        return;
      }
      this.fail(`${track.kind === 'generated' && this.waitingForGeneration ? '配音准备失败' : '音轨载入失败'}：${error.message}；不会回退原声。`);
    }
  }
  pause() {
    this.desiredPlaying = false;
    video.pause();
    if (!this.loading) this.setPhase('');
    notice(this.loading ? '已暂停等待；准备完成后不会自动播放。可继续等待或切换声音。' : `「${this.track.label}」已暂停。`);
    renderStatus();
  }
  async play() {
    if (!this.track) return;
    this.desiredPlaying = true;
    if (this.loading) {
      notice('已选择继续等待；准备完成后试听当前声音。', 'pending');
      renderStatus();
      return;
    }
    if (video.ended) {
      this.switchPosition = 0;
      video.currentTime = 0;
    }
    const mapping = this.descriptor?.tracks.find(row => row.id === this.track.id);
    if (!this.canReuse() || this.activeIndex !== mapping?.audio_index || this.pendingTrackId) {
      return this.choose(this.track, false, true);
    }
    const token = this.epoch;
    this.setPhase('starting');
    notice(`正在继续播放「${this.track.label}」…`, 'pending');
    try {
      await video.play();
      if (token !== this.epoch) return;
      if (!this.desiredPlaying) video.pause();
    } catch (error) {
      if (token === this.epoch && this.desiredPlaying && error.name !== 'AbortError') this.fail(error.message);
    }
    renderStatus();
  }
  seek(seconds) {
    if (!Number.isFinite(seconds)) return;
    const duration = Number.isFinite(video.duration) ? video.duration : (this.source?.duration_ms || 0) / 1000;
    this.switchPosition = Math.max(0, Math.min(seconds, duration || seconds));
    if (!this.descriptor || this.waitingForGeneration || (this.loading && this.rebuilding)) {
      renderStatus();
      return;
    }
    if (Number.isFinite(video.duration)) video.currentTime = this.switchPosition;
    renderStatus();
  }
  setRate(rate) { this.rate = rate; video.playbackRate = rate; }
}

function identity(asset) {
  if (!asset) return '素材未注册';
  const dimensions = asset.kind === 'video' ? `${asset.width} × ${asset.height} · ` : '';
  return `${asset.name}\n${asset.id}\n${dimensions}${time(asset.duration_ms / 1000)} · ${asset.mime}\nSHA-256 ${asset.sha256}`;
}
function updateControls() {
  const unavailable = !project || busy;
  for (const id of ['track', 'trackName', 'title', 'save', 'reopen', 'export', 'import', 'upload', 'saveDraft', 'openEditor']) $(id).disabled = unavailable;
  for (const node of $('voiceChoices').querySelectorAll('button')) node.disabled = unavailable;
  $('voiceChoices').setAttribute('aria-busy', String(busy));
  $('save').textContent = busy ? '处理中…' : dirty() ? '保存修改' : '保存项目';
  $('offset').disabled = unavailable || selectedTrack()?.kind !== 'external';
  $('remove').disabled = unavailable || selectedTrack()?.kind !== 'external';
  const generated = selectedTrack()?.kind === 'generated';
  $('publishDraft').hidden = !generated;
  $('publishDraft').disabled = unavailable || !generated || !selectedTrack().segments.some(segment => segment.draft_text !== segment.published_text);
  for (const node of $('segments').querySelectorAll('textarea,button')) node.disabled = unavailable;
  $('clearCache').disabled = unavailable || demandCache.clearing;
  const mediaUnavailable = !project || busy || demandCache.clearing || player?.sourceLoading;
  for (const id of ['play', 'back', 'seek', 'rate']) $(id).disabled = mediaUnavailable;
}
function renderDirty() {
  if (!project) return;
  $('saveState').textContent = `${dirty() ? '有未保存的修改' : '已保存到本机'}${lastSave ? ` · ${lastSave}` : ''}`;
  $('saveState').classList.toggle('unsaved', Boolean(dirty()));
  $('save').classList.toggle('has-changes', Boolean(dirty()));
  renderStatus();
}
function renderStatus() {
  if (!project || !player) return;
  const track = selectedTrack();
  if (!track) return;
  const drafts = track.segments.filter(segment => segment.draft_text !== segment.published_text).length;
  const savedTrack = savedProject?.tracks.find(row => row.id === track.id);
  const unsavedDraft = track.segments.some(segment => savedTrack?.segments.find(row => row.id === segment.id)?.draft_text !== segment.draft_text);
  const unsavedPublished = track.kind === 'generated' && track.segments.some(segment => savedTrack?.segments.find(row => row.id === segment.id)?.published_text !== segment.published_text);
  const active = project.tracks.find(row => row.id === player.activeTrackId);
  $('sTrack').textContent = player.pendingTrackId ? `${track.label}（等待播放/定位生效）` : player.loading && active ? `${active.label} → 正在切换到 ${track.label}` : track.label;
  $('sRevision').textContent = `已发布 r${track.revision} · 草稿不替换音频${unsavedPublished ? ' · 应用后的文本未保存' : ''}`;
  $('sDraft').textContent = track.segments.length ? `${drafts} / ${track.segments.length} 段不同于已发布${unsavedDraft ? ' · 草稿未保存' : ' · 草稿已保存'}` : '此轨没有示例文字片段';
  $('editorStatus').textContent = `正在编辑「${track.label}」。${drafts ? `${drafts} 段文字已修改，试听仍使用已发布文本。${track.kind === 'generated' ? '确认后点「应用修改并生成试听」才会更换声音。' : ''}` : '文字与已发布文本一致。'}${unsavedDraft ? '草稿尚未保存。' : ''}${unsavedPublished ? '应用后的文本尚未保存到项目。' : ''}`;
  $('editorScope').textContent = `${track.segments.length} 段译文`;
  $('editorDescription').textContent = track.kind === 'generated'
    ? `项目保存了 ${track.segments.length} 段译文。保存草稿不改变声音；「应用修改并生成试听」会应用当前语言的草稿并准备配音，之后请保存项目。`
    : `当前声音附有 ${track.segments.length} 段译文。保存文字不会重新生成或替换配音。`;
  const cacheEntry = track.kind === 'generated' ? demandCache.get(track) : null;
  const progressText = cacheEntry && Number.isFinite(cacheEntry.total) && cacheEntry.total > 0 ? `${cacheEntry.done} / ${cacheEntry.total} 段` : '';
  const originText = cacheEntry?.origin === 'tts' ? 'TTS 生成' : '准备配音';
  const phase = player.phase === 'generating' && cacheEntry?.state === 'ready' ? 'packaging' : player.phase;
  const phaseText = {
    checking: track.kind === 'generated' ? '正在检查配音缓存' : '正在检查播放准备',
    generating: `${originText}${progressText ? ` · ${progressText}` : ''}`,
    packaging: `${cacheEntry?.state === 'ready' ? '配音已就绪 · ' : ''}正在封装播放音轨`,
    loading: '正在加载视频与所选声音',
    switching: `正在切换到「${track.label}」`,
    starting: '正在开始播放',
    buffering: '缓冲 / 定位中'
  }[phase] || '正在准备所选声音';
  const elapsed = player.waitStarted ? Math.floor((performance.now() - player.waitStarted) / 1000) : 0;
  const elapsedText = elapsed >= 2 ? ` · 已等待 ${elapsed} 秒` : '';
  let state = '已暂停';
  if (player.error) state = '错误 · 已暂停';
  else if (player.sourceLoading) state = '正在加载源视频…';
  else if (player.loading) state = `${phaseText}${player.desiredPlaying ? '' : ' · 完成后保持暂停'}${elapsedText}`;
  else if (player.waitingForGeneration) state = cacheEntry?.state === 'ready' ? '静音画面预览 · 配音缓存就绪' : isGenerating(cacheEntry) ? '静音画面预览 · 后台正在准备配音' : '静音画面预览 · 尚未生成配音';
  else if (player.desiredPlaying && !player.canRun()) state = `缓冲 / 定位中 · 等待后自动继续${elapsedText}`;
  else if (player.desiredPlaying) state = video.paused ? `正在开始播放${elapsedText}` : '播放中';
  else if (!player.descriptor) state = sourcePoster.loading ? '正在读取静音预览画面' : sourcePoster.url ? '静音画面预览 · 尚未播放' : '尚未开始播放';
  else if (video.ended) state = '播放完毕';
  if (player.silent) state += ` · ${player.silent}`;
  $('sPlay').textContent = state;
  $('mediaState').textContent = state;
  $('videoLabel').textContent = player.desiredPlaying && !video.paused ? `正在试听 · ${track.label}`
    : player.loading ? '正在准备所选声音' : video.ended ? '播放完毕' : player.descriptor ? '已暂停' : '静音画面预览';
  $('sOriginal').textContent = track.kind === 'embedded' ? '视频内嵌原声' : '目标语言替代音轨，不叠加原声';
  $('sDrift').textContent = player.loading ? '正在准备替代音轨' : `单媒体时钟 · ${player.mode || '等待载入'}`;
  const bundle = player.descriptor;
  $('sStorage').textContent = bundle ? `共享视频 ${(bundle.video_bytes / 1048576).toFixed(1)} MiB；所有音轨 ${(bundle.audio_bytes / 1048576).toFixed(1)} MiB；${bundle.video_key.slice(0, 14)}` : '尚未准备分片';
  const needsGeneration = track.kind === 'generated' && cacheEntry?.state !== 'ready' && !player.canReuse(track);
  const playLabel = player.desiredPlaying ? player.loading ? '暂停等待' : '暂停播放'
    : player.loading ? '继续等待并试听' : player.error ? '重试播放'
    : needsGeneration ? isGenerating(cacheEntry) ? '等待并试听' : '生成并试听'
    : video.ended ? '重新播放' : player.position() > 0 ? '继续播放' : '播放预览';
  $('play').textContent = playLabel;
  $('play').setAttribute('aria-label', `${playLabel}：${track.label}`);
  const blocking = player.error || player.sourceLoading || player.loading || player.waitingForGeneration ||
    !player.descriptor || (player.desiredPlaying && (video.paused || !player.canRun()));
  $('playerOverlay').hidden = !blocking;
  $('playerOverlay').classList.toggle('is-error', Boolean(player.error));
  $('playerOverlay').classList.toggle('is-idle', !player.loading && !player.desiredPlaying);
  $('playerOverlay').classList.toggle('has-preview', Boolean(sourcePoster.url) || video.readyState >= 2);
  $('playerOverlayText').textContent = player.error ? `暂时无法播放：${player.error}`
    : player.loading || player.desiredPlaying ? `${phaseText}${elapsedText}。${player.desiredPlaying ? '可暂停等待，或切换声音。' : '已暂停等待，完成后不会自动播放。'}`
    : player.waitingForGeneration ? cacheEntry?.state === 'ready' ? '配音缓存已就绪。点击播放，载入当前语言。' : isGenerating(cacheEntry) ? '配音仍在后台准备。点击「等待并试听」选择完成后播放。' : `「${track.label}」尚未生成。点击下方「生成并试听」开始；不会播放原声。`
    : sourcePoster.loading ? '正在读取原视频的静音预览画面…'
    : `已选择「${track.label}」。${sourcePoster.url ? '当前为静音画面预览。' : ''}点击播放开始试听。`;
  $('video').setAttribute('aria-busy', String(Boolean(player.loading)));
  for (const card of $('voiceChoices').querySelectorAll('button')) {
    const cardTrack = project.tracks.find(row => row.id === card.dataset.trackId);
    const chosen = cardTrack?.id === track.id;
    card.setAttribute('aria-pressed', String(chosen));
    const status = card.querySelector('.voice-status');
    const entry = cardTrack?.kind === 'generated' ? demandCache.get(cardTrack) : null;
    const cacheState = cardTrack?.kind !== 'generated' ? ''
      : entry?.state === 'ready' ? ' · 缓存就绪' : isGenerating(entry) ? ` · ${entry.state === 'queued' ? '排队中' : '准备中'}${entry.total > 0 ? ` ${entry.done}/${entry.total} 段` : ''}`
      : entry?.state === 'error' ? ' · 生成失败，可重试' : demandCache.error ? ' · 缓存状态待确认' : ' · 尚未生成';
    const selectionState = !chosen ? '' : player.error ? ' · 播放未就绪' : player.generating ? ' · 完成后可试听' : player.loading ? ' · 检查/载入中…' : player.pendingTrackId ? ' · 播放后生效' : ' · 当前选择';
    const text = cacheState + selectionState;
    if (status.textContent !== text) status.textContent = text;
  }
  $('cacheSummary').textContent = demandCache.known ? `已用 ${byteSize(demandCache.bytes)} / ${byteSize(demandCache.maxBytes)}` : demandCache.error ? '用量暂不可用' : '正在读取用量…';
  $('cachePolicy').textContent = `临时保存约 ${Math.round(demandCache.ttlSeconds / 3600)} 小时；下次使用时清理过期内容，空间满时先清理较久未用的缓存。`;
  $('generationStatus').textContent = demandCache.error || (track.kind !== 'generated' ? '当前声音来自已有媒体，无需 AI 生成。'
    : cacheEntry?.state === 'ready' ? `${cacheEntry.cache_hit ? '已命中缓存 · ' : ''}缓存就绪${cacheEntry.origin ? ` · ${originText}` : ''}${player.loading ? '，正在准备播放。' : player.desiredPlaying && !video.paused ? '，正在试听。' : '，播放时无需再次生成。'}`
    : isGenerating(cacheEntry) ? `${originText} · ${cacheEntry.state === 'queued' ? '排队中' : '进行中'}${progressText ? ` · ${progressText}` : ''}`
    : cacheEntry?.state === 'error' ? `配音准备失败：${cacheEntry.error || '请点击「生成并试听」重试。'}`
    : '尚未生成。只在点击「生成并试听」时准备当前语言。');
  $('preparationHint').textContent = track.kind !== 'generated' ? '当前声音来自已有媒体，播放时不调用语音合成。'
    : cacheEntry?.state === 'ready' || player.canReuse(track) ? player.loading ? '声音已就绪，正在准备播放音轨，不会重新生成。' : '声音已就绪，直接播放，不重新生成。'
    : isGenerating(cacheEntry) ? '正在本机准备所选声音；进度来自实际完成段数，可暂停等待，稍后回来。'
    : '首次需要在本机合成当前声音；需先安装可选 MLX 环境，可暂停等待，稍后回来。';
  $('generationProgress').hidden = !isGenerating(cacheEntry);
  if (isGenerating(cacheEntry) && cacheEntry.total > 0) {
    $('generationProgress').max = cacheEntry.total;
    $('generationProgress').value = cacheEntry.done;
    $('generationProgress').setAttribute('aria-valuetext', progressText);
  } else {
    $('generationProgress').removeAttribute('value');
    $('generationProgress').removeAttribute('aria-valuetext');
  }
  const duration = Number.isFinite(video.duration) ? video.duration : (assets.get(project.source_asset_id)?.duration_ms || 0) / 1000;
  const position = player.position();
  $('clock').textContent = `${time(position)} / ${time(duration)}`;
  $('seek').max = duration || 1;
  $('seek').value = position || 0;
  $('seek').setAttribute('aria-valuetext', `${time(position)}，共 ${time(duration)}`);
  updateControls();
}
function chooseVoice(id) {
  if (!project || busy || id === project.selected_track_id || !project.tracks.some(track => track.id === id)) return;
  project.selected_track_id = id;
  renderTrack();
  player.choose(selectedTrack());
}
function renderVoiceChoices() {
  const focusedId = document.activeElement?.closest('[data-track-id]')?.dataset.trackId;
  const cards = project.tracks.map(track => {
    const button = document.createElement('button');
    button.className = 'voice-card';
    button.type = 'button';
    button.dataset.trackId = track.id;
    button.setAttribute('aria-pressed', String(track.id === project.selected_track_id));
    const original = track.kind === 'embedded';
    const mandarin = track.profile_id === 'zh-beijing-v1';
    const chengdu = track.profile_id === 'zh-chengdu-v1';
    const japanese = track.profile_id === 'ja-playful-v1';
    const symbol = document.createElement('span');
    symbol.className = 'voice-symbol';
    symbol.setAttribute('aria-hidden', 'true');
    symbol.textContent = original ? '原' : mandarin ? '普' : chengdu ? '蜀' : japanese ? 'JP' : '音';
    const copy = document.createElement('span');
    copy.className = 'voice-copy';
    const title = document.createElement('span');
    title.className = 'voice-title';
    title.textContent = track.label;
    const description = document.createElement('span');
    description.className = 'voice-description';
    const voiceDescription = original ? '视频原始声音' : mandarin ? '温和男声 · 普通话' : chengdu ? '评书式讲述 · 成都话' : japanese ? '调皮、兴奋的卡通讲述 · 原创合成音色' : '你添加的真实音频';
    description.textContent = `${voiceDescription}${track.kind === 'generated' ? ` · ${track.segments.length} 段译文` : ''}`;
    const status = document.createElement('span');
    status.className = 'voice-status';
    description.append(status);
    copy.append(title, description);
    const check = document.createElement('span');
    check.className = 'voice-check';
    check.setAttribute('aria-hidden', 'true');
    const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.setAttribute('viewBox', '0 0 16 16');
    const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
    path.setAttribute('d', 'm3 8 3 3 7-7');
    svg.append(path);
    check.append(svg);
    button.append(symbol, copy, check);
    button.onclick = () => chooseVoice(track.id);
    return button;
  });
  $('voiceChoices').replaceChildren(...cards);
  if (focusedId) cards.find(card => card.dataset.trackId === focusedId)?.focus({preventScroll: true});
}
function renderTrack() {
  const track = selectedTrack();
  $('track').replaceChildren(...project.tracks.map(row => new Option(row.label, row.id)));
  $('track').value = track.id;
  $('trackName').value = track.label;
  $('offset').value = track.timeline_offset_ms || 0;
  $('trackInfo').textContent = track.kind === 'embedded' ? '内嵌原声 · 不可移除' : track.kind === 'generated' ? `${track.language} · 已发布 r${track.revision} · 按需生成，时间由已发布文本固定` : `${track.language} · 已发布 r${track.revision}`;
  $('audioIdentity').textContent = track.kind === 'embedded' ? `此轨使用源视频的内嵌 audio_stream_index: ${track.audio_stream_index ?? 0}。`
    : track.kind === 'generated' ? `按需配音 · ${track.profile_id}\n${track.segments.length} 段已发布译文 · 时间偏移 ${track.timeline_offset_ms} ms\n项目 JSON 不引用临时音频文件。缓存被清理后需重新准备；草稿不会用于合成。`
    : identity(assets.get(track.asset_id));
  renderVoiceChoices();
  renderSegments();
  renderDirty();
  updateControls();
}
function renderSegments() {
  const track = selectedTrack();
  const container = $('segments');
  container.replaceChildren();
  if (!track.segments.length) {
    const empty = document.createElement('p');
    empty.className = 'empty-editor';
    empty.textContent = track.kind === 'embedded' ? '原声没有译文草稿。可上传已有配音，或按 README 导入自己的片段项目；不会自动转写。' : '此音轨未附带文字片段。上传仅挂载真实音频，不会自动转写或生成译文。';
    container.append(empty);
    return;
  }
  track.segments.forEach((segment, index) => {
    const section = document.createElement('article');
    section.className = 'segment';
    const heading = document.createElement('div');
    heading.className = 'segment-heading';
    const label = document.createElement('label');
    label.htmlFor = `draft-${index}`;
    label.textContent = `片段 ${index + 1} · ${time(segment.source_start_ms / 1000)} – ${time(segment.source_end_ms / 1000)}`;
    const jump = document.createElement('button');
    jump.textContent = '跳到这一段';
    jump.onclick = () => player.seek(segment.source_start_ms / 1000);
    heading.append(label, jump);
    const input = document.createElement('textarea');
    input.id = `draft-${index}`;
    input.value = segment.draft_text;
    input.setAttribute('aria-label', `片段 ${index + 1} 译文草稿`);
    const controls = document.createElement('div');
    controls.className = 'row';
    const undo = document.createElement('button');
    undo.textContent = '撤销到已保存草稿';
    const restore = document.createElement('button');
    restore.textContent = '恢复已发布文本';
    const status = document.createElement('p');
    status.className = 'segment-state';
    const update = (notify = true) => {
      const saved = savedProject?.tracks.find(row => row.id === track.id)?.segments.find(row => row.id === segment.id);
      status.textContent = `${segment.draft_text === segment.published_text ? '与已发布文本一致' : '文字已改 · 音频未更新'}${saved?.draft_text === segment.draft_text ? ' · 已保存' : ' · 未保存'}`;
      if (notify) renderDirty();
    };
    input.oninput = () => { segment.draft_text = input.value; update(); };
    undo.onclick = () => {
      const saved = savedProject?.tracks.find(row => row.id === track.id)?.segments.find(row => row.id === segment.id);
      input.value = segment.draft_text = saved ? saved.draft_text : segment.published_text;
      update();
    };
    restore.onclick = () => { input.value = segment.draft_text = segment.published_text; update(); };
    controls.append(undo, restore);
    const details = document.createElement('details');
    const summary = document.createElement('summary');
    summary.textContent = `查看已发布 r${track.revision} 文本（不随草稿改变）`;
    const published = document.createElement('p');
    published.className = 'published-text';
    published.textContent = segment.published_text;
    details.append(summary, published);
    section.append(heading, input, controls, status, details);
    container.append(section);
    update(false);
  });
}
async function installProject(value) {
  player.suspend();
  demandCache.reset();
  project = clone(value);
  savedProject = clone(value);
  const source = assets.get(project.source_asset_id);
  sourcePoster.setSource(source);
  $('title').value = project.title;
  $('sourceIdentity').textContent = identity(assets.get(project.source_asset_id));
  renderTrack();
  if (!source) { player.fail(`源视频未注册：${project.source_asset_id}`); return; }
  if (selectedTrack()?.kind !== 'generated') await demandCache.refresh().catch(() => {});
  await player.open(source, selectedTrack());
}
function validateInputs() {
  if (!$('offset').disabled && !$('offset').reportValidity()) return false;
  return true;
}
async function operation(work) {
  if (busy) return;
  busy = true;
  updateControls();
  try { await work(); }
  catch (error) { notice(error.message, 'error'); }
  finally { busy = false; updateControls(); }
}
async function saveProject() {
  if (!project || !validateInputs()) return;
  await operation(async () => {
    notice('正在保存项目与文字草稿…', 'pending');
    const snapshot = clone(project);
    const result = await api('/api/project', {method: 'PUT', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({project: snapshot})});
    savedProject = clone(result.project);
    lastSave = new Date().toLocaleTimeString([], {hour: '2-digit', minute: '2-digit'});
    renderSegments();
    renderDirty();
    notice('已保存。你的文字草稿和声音选择已保留，现有配音不变。');
  });
}

player = new PlaybackController();
$('play').onclick = () => player.desiredPlaying ? player.pause() : player.play();
$('back').onclick = () => player.seek(player.position() - 10);
$('seek').oninput = event => player.seek(Number(event.target.value));
$('rate').onchange = event => player.setRate(Number(event.target.value));
$('volume').oninput = event => { video.volume = Number(event.target.value); };
$('track').onchange = () => chooseVoice($('track').value);
$('clearCache').onclick = () => {
  if (!project || busy || demandCache.clearing) return;
  if (!window.confirm('清理所有临时配音与播放分片？播放会先暂停，之后需要重新准备配音。原视频、上传的已有音频、已导出成片和项目译文都不会删除。正在生成时暂时无法清理。')) return;
  operation(async () => {
    demandCache.clearing = true;
    clearTimeout(demandCache.timer);
    player.suspend();
    notice('播放已暂停，正在清理临时配音与播放缓存…', 'pending');
    try {
      const result = await api('/api/cache', {method: 'DELETE'});
      demandCache.reset();
      notice(`已清理 ${byteSize(result.removed_bytes)} 临时缓存。原始文件与译文保持不变；试听生成型声音需重新准备。`);
    } catch (error) {
      throw new Error(error.status === 409 ? `暂时无法清理：${error.message}。播放已暂停，相关读取或配音准备结束后可再次清理。` : `清理失败：${error.message}。播放已暂停，原始文件未改变。`);
    } finally {
      demandCache.clearing = false;
      await demandCache.refresh().catch(() => {});
      renderStatus();
    }
  });
};
$('openEditor').onclick = () => {
  $('editorPanel').open = true;
  $('editorHeading').focus({preventScroll: true});
  $('editorPanel').scrollIntoView({behavior: window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 'instant' : 'smooth', block: 'start'});
};
$('publishDraft').onclick = () => {
  const track = selectedTrack();
  if (!project || busy || track?.kind !== 'generated' || !track.segments.some(segment => segment.draft_text !== segment.published_text)) return;
  player.suspend();
  for (const segment of track.segments) segment.published_text = segment.draft_text;
  renderSegments();
  renderDirty();
  player.play();
};
$('title').oninput = event => { project.title = event.target.value; renderDirty(); };
$('trackName').oninput = event => {
  const track = selectedTrack();
  track.label = event.target.value;
  $('track').selectedOptions[0].textContent = track.label;
  const card = [...$('voiceChoices').querySelectorAll('button')].find(node => node.dataset.trackId === track.id);
  if (card) card.querySelector('.voice-title').textContent = track.label;
  renderDirty();
};
$('offset').onchange = event => {
  const input = event.target;
  if (!input.checkValidity() || input.value === '') return;
  selectedTrack().timeline_offset_ms = Number(input.value);
  player.choose(selectedTrack());
  renderDirty();
};
$('remove').onclick = () => {
  const track = selectedTrack();
  if (track.kind !== 'external') return;
  if (!window.confirm(`从项目中移除「${track.label}」及其文字草稿？原始音频文件不会删除。保存后此改动生效。`)) return;
  project.tracks = project.tracks.filter(row => row.id !== track.id);
  project.selected_track_id = project.tracks.find(row => row.kind === 'embedded').id;
  renderTrack();
  player.choose(selectedTrack());
  $('track').focus();
};
$('save').onclick = saveProject;
$('saveDraft').onclick = saveProject;
$('reopen').onclick = () => {
  if (dirty() && !window.confirm('放弃当前未保存修改，重新打开本机已保存项目？')) return;
  operation(async () => {
    player.pause();
    notice('正在重新打开已保存项目…', 'pending');
    const [registry, result] = await Promise.all([api('/api/assets'), api('/api/project')]);
    assets = new Map(registry.assets.map(asset => [asset.id, asset]));
    lastSave = '已重新打开';
    await installProject(result.project);
  });
};
$('export').onclick = () => {
  if (!project || !validateInputs()) return;
  const blob = new Blob([JSON.stringify(project, null, 2) + '\n'], {type: 'application/json;charset=utf-8'});
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = url;
  link.download = 'dubbing-project.json';
  document.body.append(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
  notice('已导出当前项目 JSON（包含未保存草稿与注册素材 ID，不包含媒体）。浏览器负责下载文件。');
};
$('import').onchange = event => {
  const file = event.target.files[0];
  event.target.value = '';
  if (!file) return;
  if (!window.confirm('导入会替换当前项目与本机已保存项目。JSON 不含媒体，只能恢复已注册素材。确定继续？')) return;
  operation(async () => {
    player.pause();
    notice('正在读取 JSON，并由本机后端校验协议与素材引用…', 'pending');
    let value;
    try { value = JSON.parse(await file.text()); }
    catch { throw new Error('导入失败：文件不是有效 JSON；原项目未替换。'); }
    // Never adopt client JSON first: the backend validates registered asset IDs,
    // exact project shape and identities before persisting or installing it.
    const result = await api('/api/project', {method: 'PUT', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({project: value})});
    const registry = await api('/api/assets');
    assets = new Map(registry.assets.map(asset => [asset.id, asset]));
    lastSave = '导入并保存成功';
    await installProject(result.project);
    if (!player.error) notice('JSON 已通过后端校验并保存；已恢复注册素材、声轨选择与文字草稿。');
  });
};
$('upload').onchange = event => {
  const file = event.target.files[0];
  event.target.value = '';
  if (!file) return;
  operation(async () => {
    if (file.size > 512 * 1024 * 1024) throw new Error('音频超过 512 MiB 上传上限。');
    notice('正在添加你的音频，请稍等…', 'pending');
    const result = await api('/api/assets', {method: 'POST', headers: {'Content-Type': file.type || 'application/octet-stream', 'X-Filename': encodeURIComponent(file.name)}, body: file});
    assets.set(result.asset.id, result.asset);
    const track = {id: `track-${crypto.randomUUID()}`, label: file.name, language: 'und', kind: 'external', revision: 1, asset_id: result.asset.id, timeline_offset_ms: 0, segments: []};
    project.tracks.push(track);
    project.selected_track_id = track.id;
    renderTrack();
    await player.choose(track);
    if (!player.error) notice('新声音已添加，可以直接试听。请保存项目以保留它；音频没有自动转写或翻译。');
  });
};
window.addEventListener('beforeunload', event => {
  if (dirty() || busy) { event.preventDefault(); event.returnValue = ''; }
});
function loadProject() {
  return operation(async () => {
    $('retryLoad').hidden = true;
    notice('正在打开你的项目…', 'pending');
    const [registry, result] = await Promise.all([api('/api/assets'), api('/api/project')]);
    assets = new Map(registry.assets.map(asset => [asset.id, asset]));
    await installProject(result.project);
  });
}
$('retryLoad').onclick = loadProject;
loadProject();

class NoviceGuide {
  constructor() {
    this.currentStep = 0;
    this.interacted = false;
    const interacted = () => {
      this.interacted = true;
      clearTimeout(this.autoTimer);
    };
    for (const event of ['pointerdown', 'keydown', 'input']) {
      document.addEventListener(event, interacted, {capture: true, once: true});
    }
    this.steps = [
      {
        target: () => $('voiceChoices'),
        title: '1. 选择你想听的声音',
        text: '打开页面会显示静音画面，不调用 AI；暂停时选择声音卡片只检查状态，不生成配音或视频分片。点击播放才开始准备。“尚未生成”表示只有译文，“缓存就绪”表示无需再次调用 AI。播放中切换声音会保留进度并载入所选声音。'
      },
      {
        target: () => $('playback'),
        title: '2. 视频与试听控制',
        text: '画面会先显示原视频的静音预览，不会播放原声。点击播放才在本机准备所选声音；缓存就绪时不重新生成。等待状态显示真实完成段数或已等待秒数，可暂停等待、稍后回来或切换声音，不会偷播原声。'
      },
      {
        target: () => $('openEditor'),
        title: '3. 查看与打磨译文',
        text: '展开文字工作区可查看实际译文段数。保存草稿不会改变声音。生成型声音另有“应用修改并生成试听”：明确点击才会应用当前语言的修改并准备新配音，之后请保存项目；已有音频的文字仍只是草稿。'
      },
      {
        target: () => document.querySelector('.project-bar'),
        title: '4. 项目保存与备份',
        text: '保存项目或导出 JSON，保留译文、声音设置与原视频引用，不把临时音频打包进去。配音和播放缓存约保存 24 小时，下次使用时清理；也可手动清理，之后需重新准备。原视频和已导出成片不会删除。'
      }
    ];

    $('startGuide').onclick = () => this.start();
    $('guideClose').onclick = () => this.hide();
    $('guideSkip').onclick = () => this.dismiss();
    $('guidePrev').onclick = () => this.navigate(-1);
    $('guideNext').onclick = () => this.navigate(1);

    window.addEventListener('keydown', event => {
      if (!$('guideOverlay').hidden) {
        if (event.key === 'Escape') this.hide();
        else if (event.key === 'ArrowRight') this.navigate(1);
        else if (event.key === 'ArrowLeft') this.navigate(-1);
      }
    });

    window.addEventListener('resize', () => {
      if (!$('guideOverlay').hidden) this.position();
    });
  }

  start() {
    clearTimeout(this.autoTimer);
    this.currentStep = 0;
    $('guideOverlay').hidden = false;
    this.render();
  }

  hide() {
    $('guideOverlay').hidden = true;
  }

  dismiss() {
    try { localStorage.setItem('novice_guide_done', '1'); } catch {}
    this.hide();
  }

  navigate(delta) {
    const next = this.currentStep + delta;
    if (next < 0) return;
    if (next >= this.steps.length) {
      this.dismiss();
      return;
    }
    this.currentStep = next;
    this.render();
  }

  render() {
    const step = this.steps[this.currentStep];
    $('guideStepBadge').textContent = `第 ${this.currentStep + 1} / ${this.steps.length} 步`;
    $('guideTitle').textContent = step.title;
    $('guideText').textContent = step.text;
    $('guidePrev').disabled = this.currentStep === 0;
    $('guideNext').textContent = this.currentStep === this.steps.length - 1 ? '完成引导' : '下一步';
    this.position();
  }

  position() {
    const step = this.steps[this.currentStep];
    const target = step.target();
    if (!target) return;
    target.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    const rect = target.getBoundingClientRect();
    const pad = 6;
    const spot = $('guideSpotlight');
    spot.style.top = `${Math.max(0, rect.top - pad)}px`;
    spot.style.left = `${Math.max(0, rect.left - pad)}px`;
    spot.style.width = `${rect.width + pad * 2}px`;
    spot.style.height = `${rect.height + pad * 2}px`;

    const card = $('guideCard');
    const cardWidth = card.offsetWidth || 340;
    const cardHeight = card.offsetHeight || 190;
    
    let top = rect.bottom + 16;
    let left = rect.left;
    if (top + cardHeight > window.innerHeight - 20) {
      top = Math.max(20, rect.top - cardHeight - 16);
    }
    if (left + cardWidth > window.innerWidth - 20) {
      left = Math.max(20, window.innerWidth - cardWidth - 20);
    }
    card.style.top = `${top}px`;
    card.style.left = `${left}px`;
  }
}

const guide = new NoviceGuide();
try {
  if (!localStorage.getItem('novice_guide_done')) {
    guide.autoTimer = setTimeout(() => { if (!guide.interacted) guide.start(); }, 800);
  }
} catch {}
