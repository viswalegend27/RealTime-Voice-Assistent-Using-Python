(() => {
  'use strict';

  // ====== Config ======
  const CAPTURE_RATE = 16000;
  const PLAYBACK_RATE = 24000;
  const PROC_CHUNK   = 2048;
  const SEND_HZ      = 50;
  const SEND_PERIOD  = 1000 / SEND_HZ;
  // frames per tick (samples) * 2 bytes (16-bit)
  const MAX_BATCH    = Math.floor(CAPTURE_RATE / SEND_HZ) * 2;
  const RECONNECT_MAX_DELAY = 8000;

  // ====== DOM ======
  const statusDiv     = document.getElementById('status');
  const enableBtn     = document.getElementById('enableMic');
  const led           = document.getElementById('led');
  const whoDiv        = document.getElementById('whoSpeaking');
  const transcriptDiv = document.getElementById('transcript');

  // Abort early if required nodes missing
  if (!statusDiv || !enableBtn || !led || !whoDiv || !transcriptDiv) {
    console.error('[voice] Required DOM elements missing.');
    return;
  }

  // ====== Transcript (batched DOM updates) ======
  let activeRole = null;
  const lastMsgEl = { user: null, assistant: null };
  const buffers   = { user: '',  assistant: ''  };
  let needsFlush = false;

  function startNewTurn(role) {
    const speaker = role === 'user' ? 'You' : 'Assistant';
    const p = document.createElement('p');
    p.innerHTML = `<strong>${speaker}:</strong> <span class="msg-text"></span>`;
    lastMsgEl[role] = p;
    buffers[role] = '';
    activeRole = role;
    transcriptDiv.appendChild(p);
    needsFlush = true;
  }

  function smartMerge(current, incoming) {
    if (incoming.startsWith(current)) return incoming;
    if (current.startsWith(incoming)) return current;
    const maxCheck = Math.min(80, current.length, incoming.length);
    let k = 0;
    for (let len = maxCheck; len > 0; len--) {
      if (current.slice(-len) === incoming.slice(0, len)) { k = len; break; }
    }
    const needSpace = k === 0 && /[A-Za-z0-9]$/.test(current) && /^[A-Za-z0-9]/.test(incoming);
    return current + (needSpace ? ' ' : '') + incoming.slice(k);
  }

  function upsertTranscript(role, incomingText) {
    if (activeRole !== role || !lastMsgEl[role]) startNewTurn(role);
    const merged = smartMerge(buffers[role] || '', String(incomingText || ''));
    if (merged !== buffers[role]) {
      buffers[role] = merged;
      needsFlush = true;
    }
  }

  function rafFlush() {
    if (needsFlush) {
      for (const role of ['user','assistant']) {
        const p = lastMsgEl[role];
        if (p) {
          const span = p.querySelector('.msg-text');
          if (span) span.textContent = buffers[role];
        }
      }
      transcriptDiv.scrollTop = transcriptDiv.scrollHeight;
      needsFlush = false;
    }
    requestAnimationFrame(rafFlush);
  }
  requestAnimationFrame(rafFlush);

  // ====== WebSocket with exponential backoff ======
  let ws;
  let reconnectDelay = 500;
  let reconnectTimer = null;

  function connectWS() {
    const proto = location.protocol === 'https:' ? 'wss' : 'ws';
    ws = new WebSocket(`${proto}://${location.host}/ws/voice/`);

    ws.onopen = () => {
      statusDiv.textContent = 'Status: Connected';
      enableBtn.disabled = false;
      if (reconnectTimer) { clearTimeout(reconnectTimer); reconnectTimer = null; }
      reconnectDelay = 500; // reset
    };

    ws.onclose = () => {
      statusDiv.textContent = 'Status: Disconnected — retrying…';
      stopMic();
      if (reconnectTimer) clearTimeout(reconnectTimer);
      reconnectTimer = setTimeout(connectWS, reconnectDelay);
      reconnectDelay = Math.min(RECONNECT_MAX_DELAY, reconnectDelay * 2);
    };

    ws.onerror = (e) => console.warn('[voice] WS error', e);

    ws.onmessage = (e) => {
      let data;
      try { data = JSON.parse(e.data); } catch { return; }

      if (data.type === 'status') {
        whoDiv.textContent = data.speaking
          ? (data.role === 'user' ? 'You are speaking…' : 'Assistant is speaking…')
          : 'Ready.';
        return;
      }

      if (data.type === 'audio' && data.data) {
        playPcmBase64(data.data, PLAYBACK_RATE);
        return;
      }

      if (data.role && typeof data.text === 'string') {
        upsertTranscript(data.role, data.text);
        return;
      }
    };
  }
  connectWS();
  enableBtn.disabled = true;

  // ====== Audio capture, batching & send ======
  let mediaStream, audioCtx, processor, source;
  let micEnabled = false;
  let startingMic = false; // prevent double-start
  let batch = new Uint8Array(0);
  let sendTimer = null;

  class PCM16Encoder {
    constructor(rate){ this.rate = rate; }
    encode(float32){
      const len = float32.length;
      const out = new Int16Array(len);
      for (let i = 0; i < len; i++){
        let s = float32[i];
        s = s < -1 ? -1 : (s > 1 ? 1 : s);
        out[i] = s < 0 ? s * 0x8000 : s * 0x7FFF;
      }
      return new Uint8Array(out.buffer);
    }
  }

  function concatU8(a,b){
    if (!a || a.length === 0) return b || new Uint8Array(0);
    if (!b || b.length === 0) return a;
    const out = new Uint8Array(a.length + b.length);
    out.set(a,0); out.set(b,a.length);
    return out;
  }

  function base64Encode(uint8){
    const CHUNK = 0x8000; // 32KB
    let result = '';
    for (let i=0; i<uint8.length; i+=CHUNK){
      const slice = uint8.subarray(i, i+CHUNK);
      result += String.fromCharCode.apply(null, slice);
    }
    return btoa(result);
  }

  function startSendLoop(){
    if (sendTimer) return;
    sendTimer = setInterval(() => {
      if (!ws || ws.readyState !== 1) return;
      if (!batch || batch.length === 0) return;

      const n = Math.min(MAX_BATCH, batch.length);
      const toSend = batch.subarray(0, n);
      batch = batch.subarray(n);

      const b64 = base64Encode(toSend);
      ws.send(JSON.stringify({ type:'audio', mime:`audio/pcm;rate=${CAPTURE_RATE}`, data:b64 }));
    }, SEND_PERIOD);
  }

  function stopSendLoop(){
    if (sendTimer){ clearInterval(sendTimer); sendTimer=null; }
  }

  async function startMic(){
    if (startingMic || micEnabled) return;
    startingMic = true;
    enableBtn.disabled = true;

    try {
      mediaStream = await navigator.mediaDevices.getUserMedia({
        audio: { channelCount: 1, sampleRate: CAPTURE_RATE },
        video: false
      });

      audioCtx = new (window.AudioContext||window.webkitAudioContext)({ sampleRate: CAPTURE_RATE });
      // Ensure context is running (Chrome autoplay policy)
      if (audioCtx.state !== 'running') {
        try { await audioCtx.resume(); } catch {}
      }

      source = audioCtx.createMediaStreamSource(mediaStream);
      processor = audioCtx.createScriptProcessor(PROC_CHUNK, 1, 1);
      const encoder = new PCM16Encoder(CAPTURE_RATE);

      processor.onaudioprocess = (ev) => {
        const input = ev.inputBuffer.getChannelData(0);
        const encoded = encoder.encode(input);
        batch = concatU8(batch, encoded);
      };

      source.connect(processor);
      processor.connect(audioCtx.destination);

      micEnabled = true;
      enableBtn.classList.remove('off');
      led.classList.add('on');
      enableBtn.textContent = 'Disable Microphone ';
      if (led.parentNode !== enableBtn) enableBtn.appendChild(led);
      statusDiv.textContent = 'Microphone enabled';

      startSendLoop();
    } catch (e) {
      console.error('[voice] Mic error', e);
      statusDiv.textContent = 'Mic error: ' + (e && e.message ? e.message : e);
    } finally {
      startingMic = false;
      enableBtn.disabled = false;
    }
  }

  function stopMic(){
    try {
      if (processor){ processor.disconnect(); processor.onaudioprocess = null; processor = null; }
      if (source){ source.disconnect(); source = null; }
      if (audioCtx){ audioCtx.close(); audioCtx = null; }
      if (mediaStream){ mediaStream.getTracks().forEach(t=>t.stop()); mediaStream = null; }
      batch = new Uint8Array(0);
    } catch(_){}

    micEnabled = false;
    enableBtn.classList.add('off');
    led.classList.remove('on');
    enableBtn.textContent = 'Enable Microphone ';
    if (led.parentNode !== enableBtn) enableBtn.appendChild(led);
    statusDiv.textContent = 'Microphone disabled';

    stopSendLoop();
  }

  // Toggle button
  enableBtn.addEventListener('click', () => {
    if (!ws || ws.readyState !== 1){
      statusDiv.textContent = 'Status: Connecting… please wait';
      return;
    }
    if (!micEnabled) startMic(); else stopMic();
  });

  // ====== Playback ======
  let playbackCtx; let playTime = 0;
  function ensurePlaybackCtx(rate){
    if (!playbackCtx || playbackCtx.sampleRate !== rate){
      playbackCtx = new (window.AudioContext||window.webkitAudioContext)({ sampleRate: rate });
      playTime = playbackCtx.currentTime;
    }
    return playbackCtx;
  }

  function playPcmBase64(b64, sampleRate){
    if (!b64) return;
    let bytes;
    try { bytes = Uint8Array.from(atob(b64), c => c.charCodeAt(0)); }
    catch { return; }
    if (!bytes.length) return;

    const view = new DataView(bytes.buffer);
    const len = bytes.byteLength >> 1; // /2
    const ctx = ensurePlaybackCtx(sampleRate);
    const buffer = ctx.createBuffer(1, len, sampleRate);
    const ch = buffer.getChannelData(0);
    for (let i = 0; i < len; i++) ch[i] = view.getInt16(i << 1, true) / 0x8000;

    const src = ctx.createBufferSource();
    src.buffer = buffer;
    src.connect(ctx.destination);

    const now = ctx.currentTime;
    if (playTime < now) playTime = now;
    try { src.start(playTime); } catch {}
    playTime += buffer.duration;
  }

  // ====== Page lifecycle ======
  document.addEventListener('visibilitychange', () => {
    if (document.hidden && micEnabled) {
      stopMic(); // save CPU/bandwidth
    }
  });

  window.addEventListener('beforeunload', () => {
    stopMic();
    try { ws && ws.close(); } catch(_){}
  });
})();
