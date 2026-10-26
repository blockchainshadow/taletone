'use strict';

(() => {
  const $ = (id) => document.getElementById(id);
  const video = $('video');
  const ui = Object.fromEntries([
    'notice', 'play', 'back', 'seek', 'clock', 'rate', 'volume', 'playState',
    'emptyVideo', 'videoLabel', 'videoFile', 'videoInfo', 'tracks', 'trackSettings',
    'trackName', 'offset', 'trackInfo', 'remapAudio', 'projectTitle',
  ].map((id) => [id, $(id)]));
  let tracks = [];
  let selected = 'original';
  let nextId = 0;
  let videoURL = null;
  let videoFile = null;
  let videoMetadata = null;
  let videoReady = false;
  let wanted = false;
  let waiting = false;
  let audioWaiting = false;
  let epoch = 0;
  let frame = null;
  let resumePosition = 0;
  let pendingImport = 0;
  const pendingAudio = new WeakSet();

  function notice(message, kind = '') {
    ui.notice.textContent = message;
    ui.notice.className = `notice ${kind}`.trim();
  }

  function formatTime(seconds) {
    if (!Number.isFinite(seconds) || seconds < 0) return '00:00';
    const whole = Math.floor(seconds);
    const h = Math.floor(whole / 3600);
    const m = Math.floor((whole % 3600) / 60);
    const s = whole % 60;
    return `${h ? `${h}:` : ''}${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`;
  }

  function fileMetadata(file, duration = null) {
    return { name: file.name, size: file.size, lastModified: file.lastModified, duration };
  }

  function describeFile(metadata) {
    if (!metadata) return '未选择文件';
    const size = `${(metadata.size / 1048576).toFixed(1)} MiB`;
    return `${metadata.name} · ${size}${metadata.duration ? ` · ${formatTime(metadata.duration)}` : ''}`;
  }

  function currentTrack() {
    return tracks.find((track) => track.id === selected);
  }

  // This is the only place that enables original sound. Missing or failed external
  // audio never falls back to the video's original soundtrack.
  function routeSound() {
    video.muted = selected !== 'original';
    video.volume = Number(ui.volume.value);
    for (const track of tracks) {
      if (!track.audio) continue;
      track.audio.volume = Number(ui.volume.value);
      if (track.id !== selected) track.audio.pause();
    }
  }

  function updateClock() {
    const duration = videoReady ? video.duration : 0;
    ui.clock.textContent = `${formatTime(video.currentTime)} / ${formatTime(duration)}`;
    ui.seek.value = videoReady ? video.currentTime : 0;
    ui.seek.setAttribute('aria-valuetext', `${formatTime(video.currentTime)}，总时长 ${formatTime(duration)}`);
  }

  function updateState() {
    ui.play.textContent = wanted ? '暂停' : '播放';
    ui.play.setAttribute('aria-label', wanted ? '暂停视频与配音' : '播放视频与所选声音');
    if (!videoReady) {
      ui.playState.textContent = videoFile ? '视频尚未就绪' : '等待视频';
    } else if (selected === 'original') {
      ui.playState.textContent = `${wanted ? (waiting || audioWaiting ? '缓冲中' : '播放中') : '已暂停'} · 视频原声`;
    } else {
      const track = currentTrack();
      const target = video.currentTime - (track?.offset || 0);
      let state = '配音未关联，原声静音';
      if (track?.state === 'ready') {
        if (target < 0) state = '配音尚未开始，原声静音';
        else if (target >= track.audio.duration) state = '配音已结束，余下画面静音';
        else state = `${wanted ? (waiting || audioWaiting ? '缓冲中' : '配音播放中') : '已暂停'} · 原声静音`;
      } else if (track?.state === 'loading') state = '正在读取配音，原声静音';
      else if (track?.state === 'error') state = '配音不可播放，原声静音';
      ui.playState.textContent = state;
    }
    updateClock();
  }

  function stopPlayback() {
    wanted = false;
    waiting = false;
    audioWaiting = false;
    epoch += 1;
    video.pause();
    for (const track of tracks) track.audio?.pause();
    if (frame !== null) cancelAnimationFrame(frame);
    frame = null;
    updateState();
  }

  function failPlayback(message, ticket = epoch) {
    if (ticket !== epoch) return;
    stopPlayback();
    notice(message, 'error');
  }

  function playFailure(error, ticket, source) {
    if (ticket !== epoch || !wanted || error.name === 'AbortError') return;
    const reason = error.name === 'NotAllowedError'
      ? '浏览器阻止了播放。请直接点击“播放”重试，并检查浏览器的声音权限。'
      : '请确认文件完整且编码受浏览器支持；可尝试 MP4（H.264 / AAC）视频或 MP3 / WAV 音频。';
    failPlayback(`${source}播放失败。${reason}原声不会自动替代外部配音。`, ticket);
  }

  function resumeAfterAudioBuffer() {
    if (!wanted || !audioWaiting) return;
    audioWaiting = false;
    // Keep audio held until the video actually emits playing. A queued pause
    // event from the buffering hold must not cancel the user's playback intent.
    waiting = true;
    const ticket = epoch;
    video.play().catch((error) => playFailure(error, ticket, '视频'));
    notice('配音数据已就绪，正在恢复同步播放。视频原声保持静音。');
    updateState();
  }

  function playAudio(track) {
    const audio = track.audio;
    if (!audio.paused || pendingAudio.has(audio)) return;
    const ticket = epoch;
    pendingAudio.add(audio);
    audio.play().then(() => {
      pendingAudio.delete(audio);
      if (!wanted || selected !== track.id || video.paused || waiting || audioWaiting || video.seeking) audio.pause();
    }).catch((error) => {
      pendingAudio.delete(audio);
      playFailure(error, ticket, '配音');
    });
  }

  function syncAudio(force = false) {
    if (selected === 'original') return;
    const track = currentTrack();
    if (!track || track.state !== 'ready') return;
    const audio = track.audio;
    const target = video.currentTime - track.offset;
    audio.playbackRate = video.playbackRate;
    if (target < 0 || target >= audio.duration) {
      audio.pause();
      const boundary = target < 0 ? 0 : audio.duration;
      if (Math.abs(audio.currentTime - boundary) > 0.1) audio.currentTime = boundary;
      if (audioWaiting) resumeAfterAudioBuffer();
      return;
    }
    // Assigning even the same currentTime initiates an asynchronous media seek.
    // Do not repeatedly restart decoding at bootstrap or on every playing event.
    const tolerance = force ? 0.025 : 0.18;
    if (audio.ended || Math.abs(audio.currentTime - target) > tolerance) {
      audio.currentTime = Math.max(0, Math.min(target, audio.duration));
    }
    if (audioWaiting) {
      audio.pause();
      if (audio.readyState >= HTMLMediaElement.HAVE_FUTURE_DATA && !audio.seeking) resumeAfterAudioBuffer();
      return;
    }
    if (wanted && !video.paused && !video.seeking && !waiting) playAudio(track);
    else audio.pause();
  }

  function tick() {
    frame = null;
    if (!wanted) return;
    syncAudio();
    updateState();
    frame = requestAnimationFrame(tick);
  }

  function startPlayback() {
    if (!videoReady) {
      notice('请先选择可播放的本地视频。', 'error');
      return;
    }
    const track = currentTrack();
    if (selected !== 'original' && track?.state !== 'ready') {
      stopPlayback();
      notice('当前配音尚未就绪。请在“重新关联当前配音”选择音频，或明确选择“视频原声”。不会自动切回原声。', 'error');
      return;
    }
    epoch += 1;
    const ticket = epoch;
    if (video.ended || video.currentTime >= video.duration) video.currentTime = 0;
    wanted = true;
    waiting = false;
    audioWaiting = false;
    routeSound();
    notice(selected === 'original' ? '正在播放视频原声。' : `正在试听「${track.name}」。视频原声始终静音；配音未开始或已结束的部分保持静音。`);
    // Start both media elements inside the click gesture. Subsequent seeks and
    // buffer events follow the video clock, never an independent audio clock.
    const videoPromise = video.play();
    syncAudio(true);
    videoPromise.catch((error) => playFailure(error, ticket, '视频'));
    if (frame === null) frame = requestAnimationFrame(tick);
    updateState();
  }

  function makeTrackButton(id, name, description, symbol) {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'voice-card';
    button.setAttribute('aria-pressed', String(selected === id));
    const icon = document.createElement('span');
    icon.className = 'voice-symbol';
    icon.textContent = symbol;
    icon.setAttribute('aria-hidden', 'true');
    const copy = document.createElement('span');
    copy.className = 'voice-copy';
    const title = document.createElement('span');
    title.className = 'voice-title';
    title.textContent = name;
    const info = document.createElement('span');
    info.className = 'voice-description';
    info.textContent = description;
    copy.append(title, info);
    const check = document.createElement('span');
    check.className = 'voice-check';
    check.textContent = selected === id ? '✓' : '○';
    check.setAttribute('aria-hidden', 'true');
    button.append(icon, copy, check);
    button.addEventListener('click', () => selectTrack(id));
    return button;
  }

  function renderTracks(focusSelected = false) {
    const original = makeTrackButton('original', '视频原声', '视频自带声音 · 只有明确选择时才启用', '原');
    const buttons = [original];
    for (const [index, track] of tracks.entries()) {
      const descriptions = { missing: '需要重新关联本地音频', loading: '正在读取本地音频…', ready: `音频已就绪 · ${formatTime(track.audio?.duration)}`, error: '无法解码 · 请重新关联音频' };
      buttons.push(makeTrackButton(track.id, track.name, descriptions[track.state], String(index + 1).padStart(2, '0')));
    }
    ui.tracks.replaceChildren(...buttons);
    if (focusSelected) buttons.find((button) => button.getAttribute('aria-pressed') === 'true')?.focus({ preventScroll: true });
    const track = currentTrack();
    ui.trackSettings.hidden = !track;
    if (track) {
      ui.trackName.value = track.name;
      ui.offset.value = track.offset;
      ui.trackInfo.textContent = `${track.state === 'missing' ? '待关联：' : ''}${describeFile(track.metadata)}`;
    }
    updateState();
  }

  function selectTrack(id) {
    if (id !== 'original' && !tracks.some((track) => track.id === id)) return;
    const wasPlaying = wanted;
    stopPlayback();
    selected = id;
    routeSound();
    renderTracks(true);
    syncAudio(true);
    const track = currentTrack();
    if (selected !== 'original' && track.state !== 'ready') {
      notice('已选择外部配音，视频原声已静音。请先在下方重新关联音频，等待它读取完成后再播放。', 'pending');
    } else if (wasPlaying) startPlayback();
    else notice(id === 'original' ? '已选择视频原声。点击播放开始试听。' : `已选择「${track.name}」，原声已静音。点击播放试听。`);
  }

  function releaseTrack(track) {
    if (track.audio) {
      track.audio.pause();
      track.audio.removeAttribute('src');
      track.audio.load();
    }
    if (track.url) URL.revokeObjectURL(track.url);
    track.audio = null;
    track.url = null;
  }

  function attachAudio(track, file) {
    if (selected === track.id) stopPlayback();
    releaseTrack(track);
    const audio = new Audio();
    track.audio = audio;
    track.metadata = fileMetadata(file);
    track.url = URL.createObjectURL(file);
    track.state = 'loading';
    audio.preload = 'auto';
    const isCurrent = () => tracks.includes(track) && track.audio === audio;
    audio.addEventListener('loadedmetadata', () => {
      if (!isCurrent()) return;
      if (!Number.isFinite(audio.duration) || audio.duration <= 0) {
        track.state = 'error';
        renderTracks();
        notice(`无法读取「${track.name}」的有效时长，请改用完整的 MP3 / WAV 文件。`, 'error');
        return;
      }
      track.state = 'ready';
      track.metadata.duration = audio.duration;
      audio.playbackRate = video.playbackRate;
      renderTracks();
      if (selected === track.id) syncAudio(true);
      notice(`「${track.name}」读取完成，可以选择试听。所有文件仍在本机。`);
    });
    audio.addEventListener('error', () => {
      if (!isCurrent()) return;
      track.state = 'error';
      if (selected === track.id) stopPlayback();
      renderTracks();
      notice(`「${track.name}」无法播放：文件可能损坏或编码不受支持。请重新关联 MP3 / WAV 等可解码音频。未切回原声。`, 'error');
    });
    audio.addEventListener('waiting', () => {
      if (!isCurrent() || selected !== track.id || !wanted) return;
      const target = video.currentTime - track.offset;
      if (target < 0 || target >= audio.duration) return;
      // waiting is a normal decoding/buffering state, not a terminal failure.
      // Hold both clocks but retain intent so canplay can resume automatically.
      audioWaiting = true;
      audio.pause();
      video.pause();
      notice('正在缓冲配音，已临时停住画面；数据就绪后会自动同步继续。视频原声保持静音。', 'pending');
      updateState();
    });
    audio.addEventListener('canplay', () => {
      if (!isCurrent() || selected !== track.id || !wanted || !audioWaiting) return;
      syncAudio(true);
    });
    audio.addEventListener('ended', () => {
      if (!isCurrent() || selected !== track.id) return;
      // No fallback: a shorter dub means silence for the remaining picture.
      updateState();
    });
    audio.src = track.url;
    audio.load();
    renderTracks();
  }

  function loadVideo(file) {
    stopPlayback();
    videoReady = false;
    video.pause();
    video.removeAttribute('src');
    video.load();
    if (videoURL) URL.revokeObjectURL(videoURL);
    videoURL = URL.createObjectURL(file);
    videoFile = file;
    videoMetadata = fileMetadata(file);
    ui.videoInfo.textContent = describeFile(videoMetadata);
    ui.emptyVideo.hidden = true;
    ui.videoLabel.hidden = false;
    ui.videoLabel.textContent = '正在读取本地视频…';
    setControls(false);
    routeSound();
    notice('正在读取本地视频元数据…没有上传或 AI 处理。', 'pending');
    video.src = videoURL;
    video.load();
  }

  function setControls(enabled) {
    for (const control of [ui.play, ui.back, ui.seek, ui.rate]) control.disabled = !enabled;
  }

  function seekTo(time) {
    if (!videoReady) return;
    for (const track of tracks) track.audio?.pause();
    video.currentTime = Math.max(0, Math.min(time, video.duration));
    syncAudio(true);
    updateState();
  }

  ui.play.addEventListener('click', () => wanted ? stopPlayback() : startPlayback());
  ui.back.addEventListener('click', () => seekTo(video.currentTime - 10));
  ui.seek.addEventListener('input', () => seekTo(Number(ui.seek.value)));
  ui.rate.addEventListener('change', () => {
    video.playbackRate = Number(ui.rate.value);
    syncAudio(true);
  });
  ui.volume.addEventListener('input', routeSound);
  video.addEventListener('volumechange', () => {
    if (selected !== 'original' && !video.muted) video.muted = true;
  });
  $('chooseVideo').addEventListener('click', () => {
    try {
      if (typeof ui.videoFile.showPicker === 'function') ui.videoFile.showPicker();
      else ui.videoFile.click();
    } catch (error) {
      notice('浏览器未能打开文件窗口。请使用画面下方的原生“选择文件”；若当前页面是内嵌浏览器，请在 Chrome、Edge 或 Safari 中打开 https://papaw.cc 再选文件。', 'error');
    }
  });
  ui.videoFile.addEventListener('change', () => {
    const file = ui.videoFile.files[0];
    if (file) {
      resumePosition = videoReady ? video.currentTime : resumePosition;
      loadVideo(file);
    }
    ui.videoFile.value = '';
  });
  video.addEventListener('loadedmetadata', () => {
    if (!videoFile) return;
    if (!Number.isFinite(video.duration) || video.duration <= 0) {
      notice('视频没有可读取的有效时长。请尝试完整的 MP4（H.264 / AAC）或 WebM 文件。', 'error');
      return;
    }
    videoReady = true;
    videoMetadata.duration = video.duration;
    ui.videoInfo.textContent = describeFile(videoMetadata);
    ui.videoLabel.textContent = '本地视频 · 不上传';
    ui.seek.max = video.duration;
    setControls(true);
    video.currentTime = Math.min(resumePosition, video.duration);
    resumePosition = 0;
    routeSound();
    updateState();
    notice(selected === 'original' ? '视频已就绪。可以播放原声，或在右侧添加你准备好的配音。' : '视频已关联。请确认每条配音也已关联，再点击播放；外部配音期间原声保持静音。');
  });
  video.addEventListener('error', () => {
    if (!videoFile || !video.getAttribute('src')) return;
    videoReady = false;
    stopPlayback();
    setControls(false);
    ui.videoLabel.textContent = '视频读取失败';
    notice('无法播放这个视频。文件可能损坏或编码不受浏览器支持，请重新选择 MP4（H.264 / AAC）或 WebM。没有上传文件。', 'error');
  });
  video.addEventListener('waiting', () => {
    if (!wanted) return;
    waiting = true;
    for (const track of tracks) track.audio?.pause();
    updateState();
  });
  video.addEventListener('playing', () => {
    if (!wanted) { video.pause(); return; }
    waiting = false;
    syncAudio(true);
    updateState();
  });
  video.addEventListener('seeking', () => {
    for (const track of tracks) track.audio?.pause();
  });
  video.addEventListener('seeked', () => { syncAudio(true); updateState(); });
  video.addEventListener('timeupdate', () => { syncAudio(); updateState(); });
  video.addEventListener('ratechange', () => syncAudio(true));
  video.addEventListener('pause', () => {
    if (!video.paused) return;
    if (wanted && !audioWaiting) stopPlayback();
  });
  video.addEventListener('ended', () => {
    stopPlayback();
    notice('视频播放结束，所有配音已停止。点击播放可从头重新试听。');
  });

  function addAudioFiles(files) {
    if (!files.length) return;
    if (tracks.length + files.length > 200) {
      notice('一个项目最多保存 200 条配音，请先移除不需要的声音。', 'error');
      return;
    }
    notice(`正在读取 ${files.length} 个本地音频…没有上传或 AI 处理。`, 'pending');
    const customName = $('audioName').value.trim();
    for (const file of files) {
      const name = (files.length === 1 && customName ? customName : file.name.replace(/\.[^.]+$/, '')).slice(0, 100) || '未命名配音';
      let id;
      do { id = `voice-${++nextId}`; } while (tracks.some((track) => track.id === id));
      const track = { id, name, offset: 0, metadata: null, state: 'loading', audio: null, url: null };
      tracks.push(track);
      attachAudio(track, file);
    }
    $('audioFile').value = '';
    $('audioName').value = '';
  }
  $('addTrack').addEventListener('submit', (event) => {
    event.preventDefault();
    addAudioFiles(Array.from($('audioFile').files));
  });
  function fileDrop(element, consume) {
    element.addEventListener('dragover', (event) => {
      if (!Array.from(event.dataTransfer?.types || []).includes('Files')) return;
      event.preventDefault();
      event.dataTransfer.dropEffect = 'copy';
    });
    element.addEventListener('drop', (event) => {
      const files = Array.from(event.dataTransfer?.files || []);
      if (!files.length) return;
      event.preventDefault();
      event.stopPropagation();
      consume(files);
    });
  }
  fileDrop(document.querySelector('.video-stage'), (files) => {
    if (files.length !== 1) {
      notice('画面区域一次只能放入一个视频。配音请拖到右侧“添加已有配音”区域。', 'error');
      return;
    }
    resumePosition = videoReady ? video.currentTime : resumePosition;
    loadVideo(files[0]);
  });
  fileDrop($('addTrack'), addAudioFiles);
  window.addEventListener('dragover', (event) => {
    if (Array.from(event.dataTransfer?.types || []).includes('Files')) event.preventDefault();
  });
  window.addEventListener('drop', (event) => {
    if (!Array.from(event.dataTransfer?.types || []).includes('Files')) return;
    event.preventDefault();
    notice('请把视频拖到画面区域，把配音拖到右侧“添加已有配音”区域。', 'error');
  });
  ui.trackName.addEventListener('change', () => {
    const track = currentTrack();
    if (!track) return;
    track.name = ui.trackName.value.trim().slice(0, 100) || '未命名配音';
    renderTracks();
  });
  ui.offset.addEventListener('change', () => {
    const track = currentTrack();
    if (!track) return;
    if (!ui.offset.checkValidity() || ui.offset.value === '') {
      ui.offset.reportValidity();
      ui.offset.value = track.offset;
      return;
    }
    track.offset = Number(ui.offset.value);
    syncAudio(true);
    updateState();
    notice(`「${track.name}」时间偏移已设为 ${track.offset} 秒。请导出项目保存设置。`);
  });
  ui.remapAudio.addEventListener('change', () => {
    const file = ui.remapAudio.files[0];
    const track = currentTrack();
    if (track && file) {
      notice(`正在重新关联「${track.name}」…请确认所选文件是正确素材。`, 'pending');
      attachAudio(track, file);
    }
    ui.remapAudio.value = '';
  });
  $('removeTrack').addEventListener('click', () => {
    const track = currentTrack();
    if (!track) return;
    stopPlayback();
    releaseTrack(track);
    tracks = tracks.filter((item) => item !== track);
    // Removing a dub must not implicitly enable original audio.
    selected = 'original';
    routeSound();
    renderTracks(true);
    notice('配音已移除，播放已暂停。现在选择的是视频原声；只有再次点击播放才会发声。原始文件未被删除。');
  });

  $('exportProject').addEventListener('click', () => {
    const project = {
      format: 'taletone-project', version: 1,
      title: ui.projectTitle.value.trim() || '未命名的故事',
      video: videoMetadata,
      tracks: tracks.map(({ id, name, offset, metadata }) => ({ id, name, offset, file: metadata })),
      playback: { selected, position: videoReady ? video.currentTime : resumePosition, rate: Number(ui.rate.value) },
    };
    const blob = new Blob([JSON.stringify(project, null, 2)], { type: 'application/json' });
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = 'taletone-project.json';
    document.body.append(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
    notice('项目 JSON 已交给浏览器下载（不含任何媒体文件）。请保留本机原始视频和音频，并检查浏览器下载记录。');
  });

  function checkedNumber(value, min, max) {
    if (typeof value !== 'number' || !Number.isFinite(value) || value < min || value > max) throw new Error('项目中的时间或数值无效。');
    return value;
  }

  function checkedText(value, max) {
    if (typeof value !== 'string' || !value.trim() || value.length > max) throw new Error('项目中的名称无效或过长。');
    return value;
  }

  function checkedFile(value) {
    if (value === null) return null;
    if (!value || typeof value !== 'object') throw new Error('媒体元数据无效。');
    return {
      name: checkedText(value.name, 1024),
      size: checkedNumber(value.size, 0, Number.MAX_SAFE_INTEGER),
      lastModified: checkedNumber(value.lastModified, 0, Number.MAX_SAFE_INTEGER),
      duration: value.duration === null ? null : checkedNumber(value.duration, 0, 1e9),
    };
  }

  function parseProject(text) {
    const value = JSON.parse(text);
    if (!value || value.format !== 'taletone-project' || value.version !== 1) throw new Error('请选择 TaleTone 导出的版本 1 项目 JSON。');
    if (!Array.isArray(value.tracks) || value.tracks.length > 200) throw new Error('项目配音列表无效（最多 200 条）。');
    const ids = new Set(['original']);
    const importedTracks = value.tracks.map((track) => {
      if (!track || typeof track !== 'object') throw new Error('配音记录无效。');
      const id = checkedText(track.id, 100);
      if (ids.has(id)) throw new Error('项目包含重复的配音标识。');
      ids.add(id);
      return { id, name: checkedText(track.name, 100), offset: checkedNumber(track.offset, -86400, 86400), metadata: checkedFile(track.file), state: 'missing', audio: null, url: null };
    });
    if (!value.playback || !ids.has(value.playback.selected)) throw new Error('所选声音不存在。');
    const rate = checkedNumber(value.playback.rate, 0.75, 2);
    if (![0.75, 1, 1.25, 1.5, 2].includes(rate)) throw new Error('项目播放速度不受支持。');
    return { title: checkedText(value.title, 160), video: checkedFile(value.video), tracks: importedTracks, selected: value.playback.selected, position: checkedNumber(value.playback.position, 0, 1e9), rate };
  }

  $('importProject').addEventListener('change', async (event) => {
    const file = event.target.files[0];
    event.target.value = '';
    if (!file) return;
    const request = ++pendingImport;
    try {
      if (file.size > 2 * 1024 * 1024) throw new Error('项目 JSON 超过 2 MiB，请选择不含媒体内容的 TaleTone 项目文件。');
      const project = parseProject(await file.text());
      if (request !== pendingImport) return;
      if ((videoFile || tracks.length) && !window.confirm('导入会替换当前项目和媒体关联。尚未导出的设置将丢失。继续导入？')) return;
      stopPlayback();
      for (const track of tracks) releaseTrack(track);
      videoReady = false;
      videoFile = null;
      video.removeAttribute('src');
      video.load();
      if (videoURL) URL.revokeObjectURL(videoURL);
      videoURL = null;
      tracks = project.tracks;
      selected = project.selected;
      videoMetadata = project.video;
      resumePosition = project.position;
      ui.projectTitle.value = project.title;
      ui.rate.value = String(project.rate);
      video.playbackRate = project.rate;
      ui.seek.value = 0;
      ui.seek.max = 1;
      ui.emptyVideo.hidden = false;
      ui.videoLabel.hidden = true;
      ui.videoInfo.textContent = project.video ? `待关联：${describeFile(project.video)}` : '请关联本地视频';
      ui.remapAudio.value = '';
      setControls(false);
      routeSound();
      renderTracks();
      notice('项目设置已导入，媒体尚未恢复。请先“打开 / 重新关联视频”，再逐条选择配音并“重新关联当前配音”。JSON 只有文件信息，不含文件内容；关联后不会自动播放。', 'pending');
    } catch (error) {
      if (request !== pendingImport) return;
      notice(`导入失败，当前项目未改变：${error instanceof SyntaxError ? 'JSON 格式不正确。' : error.message}`, 'error');
    }
  });

  $('openGuide').addEventListener('click', () => {
    $('guide').open = true;
    $('guide').scrollIntoView({ behavior: 'smooth', block: 'center' });
    $('guide').querySelector('summary').focus({ preventScroll: true });
  });
  window.addEventListener('pagehide', () => {
    // Keep object URLs on back/forward-cache entries; release on a real unload.
    stopPlayback();
  });
  routeSound();
  renderTracks();
})();
