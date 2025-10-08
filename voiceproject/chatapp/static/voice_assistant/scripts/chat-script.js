(() => {
  'use strict';
  // Config
  const CAPTURE_RATE = 16000, PLAYBACK_RATE = 24000, PROC_CHUNK = 2048, SEND_HZ = 50,
    SEND_PERIOD = 1000 / SEND_HZ, MAX_BATCH = Math.floor(CAPTURE_RATE / SEND_HZ) * 2,
    RECONNECT_MAX_DELAY = 8000;
  // DOM elements
  const statusDiv = document.getElementById('status'),
    enableBtn = document.getElementById('enableMic'),
    led = document.getElementById('led'),
    whoDiv = document.getElementById('whoSpeaking'),
    transcriptDiv = document.getElementById('transcript'),
    barViz = document.getElementById('assistant-bar-visualizer');
  if (!statusDiv || !enableBtn || !led || !whoDiv || !transcriptDiv) return;

  // --- Visualizer tracking variables ---
  let assistantAudioPending = 0;
  let needsHideBar = false;

  // Transcript management
  let activeRole = null,
    lastMsgEl = { user: null, assistant: null },
    buffers = { user: '', assistant: '' },
    needsFlush = false;
  function startNewTurn(role) {
    const p = document.createElement('p');
    p.innerHTML = `<strong>${role === 'user' ? 'You' : 'Assistant'}:</strong> <span class="msg-text"></span>`;
    lastMsgEl[role] = p;
    buffers[role] = '';
    activeRole = role;
    transcriptDiv.appendChild(p);
    needsFlush = true;
  }
  function smartMerge(cur, inc) {
    if (inc.startsWith(cur)) return inc;
    if (cur.startsWith(inc)) return cur;
    let mc = Math.min(80, cur.length, inc.length), k = 0;
    for (let l = mc; l > 0; l--) if (cur.slice(-l) === inc.slice(0, l)) { k = l; break; }
    const ns = k === 0 && /[\w]$/.test(cur) && /^\w/.test(inc);
    return cur + (ns ? ' ' : '') + inc.slice(k);
  }
  function upsertTranscript(role, t) {
    if (activeRole !== role || !lastMsgEl[role]) startNewTurn(role);
    const m = smartMerge(buffers[role] || '', String(t || ''));
    if (m !== buffers[role]) { buffers[role] = m; needsFlush = true; }
  }
  function rafFlush() {
    if (needsFlush) {
      for (const r of ['user', 'assistant']) {
        const p = lastMsgEl[r];
        if (p) {
          const s = p.querySelector('.msg-text');
          if (s) s.textContent = buffers[r];
        }
      }
      transcriptDiv.scrollTop = transcriptDiv.scrollHeight;
      needsFlush = false;
    }
    requestAnimationFrame(rafFlush);
  }
  requestAnimationFrame(rafFlush);

  // WebSocket connection
  let ws, reconnectDelay = 500, reconnectTimer = null;
  function connectWS() {
    const proto = location.protocol === 'https:' ? 'wss' : 'ws';
    ws = new WebSocket(`${proto}://${location.host}/ws/voice/`);
    ws.onopen = () => {
      statusDiv.textContent = 'Status: Connected';
      enableBtn.disabled = false;
      if (reconnectTimer) { clearTimeout(reconnectTimer); reconnectTimer = null; }
      reconnectDelay = 500;
    };
    ws.onclose = () => {
      statusDiv.textContent = 'Status: Disconnected — retrying…';
      stopMic();
      if (reconnectTimer) clearTimeout(reconnectTimer);
      reconnectTimer = setTimeout(connectWS, reconnectDelay);
      reconnectDelay = Math.min(RECONNECT_MAX_DELAY, reconnectDelay * 2);
    };
    ws.onerror = e => console.warn('[voice] WebSocket error', e);
    ws.onmessage = (e) => {
      let data; try { data = JSON.parse(e.data); } catch { return; }
      if (data.type === 'status') {
        whoDiv.textContent = data.speaking ? (data.role === 'user' ? 'You are speaking…' : 'Assistant is speaking…') : 'Ready.';
        // --- Improved visualizer visibility control ---
        if (barViz) {
          if (data.speaking && data.role === 'assistant') {
            barViz.style.display = '';
            needsHideBar = false;
          } else if (data.role === 'assistant') {
            needsHideBar = true;
            if (assistantAudioPending === 0) barViz.style.display = 'none';
          } else {
            barViz.style.display = 'none';
            needsHideBar = false;
          }
        }
        return;
      }
      if (data.type === 'audio' && data.data) { playPcmBase64(data.data, PLAYBACK_RATE); return; }
      if (data.role && typeof data.text === 'string') { upsertTranscript(data.role, data.text); return; }
    };
  }
  connectWS();
  enableBtn.disabled = true;

  // Audio Capture
  let mediaStream, audioCtx, processor, source, micEnabled = false, startingMic = false, batch = new Uint8Array(0), sendTimer = null;
  class PCM16Encoder {
    encode(float32) {
      let len = float32.length, out = new Int16Array(len);
      for (let i = 0; i < len; i++) {
        let s = float32[i];
        s = s < -1 ? -1 : (s > 1 ? 1 : s);
        out[i] = s < 0 ? s * 0x8000 : s * 0x7FFF;
      }
      return new Uint8Array(out.buffer);
    }
  }
  function concatU8(a, b) {
    if (!a || !a.length) return b || new Uint8Array(0);
    if (!b || !b.length) return a;
    let out = new Uint8Array(a.length + b.length);
    out.set(a, 0); out.set(b, a.length); return out;
  }
  function base64Encode(uint8) {
    let CHUNK = 0x8000, result = '';
    for (let i = 0; i < uint8.length; i += CHUNK) {
      result += String.fromCharCode.apply(null, uint8.subarray(i, i + CHUNK));
    }
    return btoa(result);
  }
  function startSendLoop() {
    if (sendTimer) return;
    sendTimer = setInterval(() => {
      if (!ws || ws.readyState !== 1 || !batch.length) return;
      const n = Math.min(MAX_BATCH, batch.length), toSend = batch.subarray(0, n);
      batch = batch.subarray(n);
      const payload = { type: 'audio', mime: `audio/pcm;rate=${CAPTURE_RATE}`, data: base64Encode(toSend) };
      ws.send(JSON.stringify(payload));
    }, SEND_PERIOD);
  }
  function stopSendLoop() { if (sendTimer) { clearInterval(sendTimer); sendTimer = null; } }
  async function startMic() {
    if (startingMic || micEnabled) return;
    startingMic = true; enableBtn.disabled = true;
    try {
      mediaStream = await navigator.mediaDevices.getUserMedia({ audio: { channelCount: 1, sampleRate: CAPTURE_RATE }, video: false });
      audioCtx = new (window.AudioContext || window.webkitAudioContext)({ sampleRate: CAPTURE_RATE });
      if (audioCtx.state !== 'running') await audioCtx.resume();
      source = audioCtx.createMediaStreamSource(mediaStream);
      processor = audioCtx.createScriptProcessor(PROC_CHUNK, 1, 1);
      const encoder = new PCM16Encoder();
      processor.onaudioprocess = (ev) => {
        batch = concatU8(batch, encoder.encode(ev.inputBuffer.getChannelData(0)));
      };
      source.connect(processor);
      processor.connect(audioCtx.destination);
      micEnabled = true; enableBtn.classList.remove('off'); led.classList.add('on');
      enableBtn.textContent = 'Disable Microphone ';
      if (led.parentNode !== enableBtn) enableBtn.appendChild(led);
      statusDiv.textContent = 'Microphone enabled';
      startSendLoop();
    } catch (e) {
      statusDiv.textContent = 'Mic error: ' + (e && e.message ? e.message : e);
    } finally {
      startingMic = false; enableBtn.disabled = false;
    }
  }
  function stopMic() {
    try {
      if (processor) { processor.disconnect(); processor.onaudioprocess = null; processor = null; }
      if (source) { source.disconnect(); source = null; }
      if (audioCtx) { audioCtx.close(); audioCtx = null; }
      if (mediaStream) { mediaStream.getTracks().forEach(t => t.stop()); mediaStream = null; }
      batch = new Uint8Array(0);
    } catch (_) {}
    micEnabled = false;
    enableBtn.classList.add('off'); led.classList.remove('on'); enableBtn.textContent = 'Enable Microphone ';
    if (led.parentNode !== enableBtn) enableBtn.appendChild(led);
    statusDiv.textContent = 'Microphone disabled';
    stopSendLoop();
  }
  enableBtn.addEventListener('click', () => {
    if (!ws || ws.readyState !== 1) {
      statusDiv.textContent = 'Status: Connecting… please wait';
      return;
    }
    if (!micEnabled) startMic(); else stopMic();
  });

  // Playback
  let playbackCtx, playTime = 0;
  function ensurePlaybackCtx(rate) {
    if (!playbackCtx || playbackCtx.sampleRate !== rate) {
      playbackCtx = new (window.AudioContext || window.webkitAudioContext)({ sampleRate: rate });
      playTime = playbackCtx.currentTime;
    }
    return playbackCtx;
  }
  function playPcmBase64(b64, sampleRate) {
    if (!b64) return;
    let bytes; try { bytes = Uint8Array.from(atob(b64), c => c.charCodeAt(0)); } catch { return; }
    if (!bytes.length) return;
    const view = new DataView(bytes.buffer), len = bytes.byteLength / 2,
      ctx = ensurePlaybackCtx(sampleRate), buffer = ctx.createBuffer(1, len, sampleRate), ch = buffer.getChannelData(0);
    for (let i = 0; i < len; i++) ch[i] = view.getInt16(i * 2, true) / 0x8000;
    const src = ctx.createBufferSource(); src.buffer = buffer; src.connect(ctx.destination);
    const now = ctx.currentTime; if (playTime < now) playTime = now;
    // Visualizer logic: Track when audio is playing
    assistantAudioPending = (assistantAudioPending || 0) + 1;
    src.onended = function () {
      assistantAudioPending = Math.max(0, assistantAudioPending - 1);
      if (needsHideBar && assistantAudioPending === 0 && barViz) barViz.style.display = 'none';
    };
    try { src.start(playTime); } catch {}
    playTime += buffer.duration;
  }

  // Lifecycle management
  document.addEventListener('visibilitychange', () => { if (document.hidden && micEnabled) stopMic(); });
  window.addEventListener('beforeunload', () => { stopMic(); try { ws && ws.close(); } catch (_) {} });
})();