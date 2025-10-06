(() => {
  'use strict';

  // =================================================================
  // ====== Config: Constants for Audio Processing and Networking ======
  // =================================================================

  // The sample rate for capturing audio from the microphone. Must match server expectations.
  const CAPTURE_RATE = 16000;
  // The sample rate for playing back audio received from the server.
  const PLAYBACK_RATE = 24000;
  // The size of the audio processing buffer in samples. A lower value reduces latency but increases CPU load.
  const PROC_CHUNK = 2048;
  // How many times per second we send audio data to the server.
  const SEND_HZ = 50;
  // The interval in milliseconds between each audio data send.
  const SEND_PERIOD = 1000 / SEND_HZ;
  // The maximum number of bytes to send in a single WebSocket message to maintain the desired send rate.
  // Calculated as: (samples per second / sends per second) * 2 bytes per sample (for 16-bit audio).
  const MAX_BATCH = Math.floor(CAPTURE_RATE / SEND_HZ) * 2;
  // The maximum delay in milliseconds for the WebSocket reconnection logic.
  const RECONNECT_MAX_DELAY = 8000;


  // ==================================================
  // ====== DOM Element References & Initialization ======
  // ==================================================

  const statusDiv = document.getElementById('status');
  const enableBtn = document.getElementById('enableMic');
  const led = document.getElementById('led');
  const whoDiv = document.getElementById('whoSpeaking');
  const transcriptDiv = document.getElementById('transcript');

  // Abort initialization if any required HTML elements are missing.
  if (!statusDiv || !enableBtn || !led || !whoDiv || !transcriptDiv) {
    console.error('[voice] Required DOM elements missing. Script will not run.');
    return;
  }


  // ============================================================================
  // ====== Transcript Management: Efficiently updates the conversation UI ======
  // ============================================================================

  let activeRole = null; // Tracks who is currently speaking ('user' or 'assistant').
  const lastMsgEl = { user: null, assistant: null }; // Holds the last <p> element for each role.
  const buffers = { user: '', assistant: '' }; // Buffers the latest full transcript text for each role.
  let needsFlush = false; // A flag to indicate that the DOM needs to be updated.

  /**
   * Creates a new paragraph in the transcript for a new turn.
   * @param {'user' | 'assistant'} role The role taking the new turn.
   */
  function startNewTurn(role) {
    const speaker = role === 'user' ? 'You' : 'Assistant';
    const p = document.createElement('p');
    p.innerHTML = `<strong>${speaker}:</strong> <span class="msg-text"></span>`;
    lastMsgEl[role] = p;
    buffers[role] = '';
    activeRole = role;
    transcriptDiv.appendChild(p);
    needsFlush = true; // Mark the DOM as needing an update.
  }

  /**
   * Intelligently merges a new piece of text with the existing buffer.
   * This handles overlapping text from streaming transcription APIs.
   * @param {string} current The text currently in the buffer.
   * @param {string} incoming The new text fragment from the server.
   * @returns {string} The merged, most complete version of the text.
   */
  function smartMerge(current, incoming) {
    if (incoming.startsWith(current)) return incoming; // New text is a superset.
    if (current.startsWith(incoming)) return current; // New text is a subset (e.g., old update).

    // Find the longest overlapping suffix of `current` and prefix of `incoming`.
    const maxCheck = Math.min(80, current.length, incoming.length);
    let k = 0;
    for (let len = maxCheck; len > 0; len--) {
      if (current.slice(-len) === incoming.slice(0, len)) {
        k = len;
        break;
      }
    }
    // Add a space if merging two words without overlap.
    const needSpace = k === 0 && /[A-Za-z0-9]$/.test(current) && /^[A-Za-z0-9]/.test(incoming);
    return current + (needSpace ? ' ' : '') + incoming.slice(k);
  }

  /**
   * Main function to update the transcript. It starts a new turn if needed
   * and merges the incoming text into the appropriate buffer.
   * @param {'user' | 'assistant'} role The role whose transcript is being updated.
   * @param {string} incomingText The new text from the server.
   */
  // This main function, called from the WebSocket handler, first checks whether the speaker has changed — for instance, if the assistant was speaking (activeRole === 'assistant') and a new user message arrives, it recognizes the role switch and starts a new paragraph accordingly.
  function upsertTranscript(role, incomingText) {
    // startNewTurn(role) - The Paragraph Creator
    if (activeRole !== role || !lastMsgEl[role]) startNewTurn(role);
    const merged = smartMerge(buffers[role] || '', String(incomingText || ''));
    if (merged !== buffers[role]) {
      buffers[role] = merged;
      needsFlush = true;
    }
  }

  /**
   * Uses requestAnimationFrame to batch DOM updates for better performance.
   * This prevents the UI from re-rendering on every single message, which is crucial for streaming text.
   */
  function rafFlush() {
    if (needsFlush) {
      for (const role of ['user', 'assistant']) {
        const p = lastMsgEl[role];
        if (p) {
          const span = p.querySelector('.msg-text');
          if (span) span.textContent = buffers[role];
        }
      }
      // Auto-scroll to the bottom of the transcript.
      transcriptDiv.scrollTop = transcriptDiv.scrollHeight;
      needsFlush = false;
    }
    requestAnimationFrame(rafFlush);
  }
  requestAnimationFrame(rafFlush); // Start the flush loop.


  // =========================================================================
  // ====== WebSocket Management: Handles connection, messages, and retry ======
  // =========================================================================

  let ws;
  let reconnectDelay = 500;
  let reconnectTimer = null;

  /**
   * Establishes a WebSocket connection and defines its event handlers.
   * Implements an exponential backoff strategy for reconnection.
   */
  function connectWS() {
    const proto = location.protocol === 'https:' ? 'wss' : 'ws';
    ws = new WebSocket(`${proto}://${location.host}/ws/voice/`);

    ws.onopen = () => {
      statusDiv.textContent = 'Status: Connected';
      enableBtn.disabled = false;
      if (reconnectTimer) { clearTimeout(reconnectTimer); reconnectTimer = null; }
      reconnectDelay = 500; // Reset reconnect delay on successful connection.
    };

    ws.onclose = () => {
      statusDiv.textContent = 'Status: Disconnected — retrying…';
      stopMic(); // Ensure mic is off if connection is lost.
      if (reconnectTimer) clearTimeout(reconnectTimer);
      // Schedule the next reconnection attempt with an increased delay.
      reconnectTimer = setTimeout(connectWS, reconnectDelay);
      reconnectDelay = Math.min(RECONNECT_MAX_DELAY, reconnectDelay * 2);
    };

    ws.onerror = (e) => console.warn('[voice] WebSocket error', e);

    // This is the central message dispatcher from the server.
    ws.onmessage = (e) => {
      let data;
      // '{"type": "transcript.message", "role": "assistant", "text": "The weather is"}' <= The acutal JSON Parsed
      try { data = JSON.parse(e.data); } catch { return; }

      // Handle speaking status updates.
      if (data.type === 'status') {
        whoDiv.textContent = data.speaking
          ? (data.role === 'user' ? 'You are speaking…' : 'Assistant is speaking…')
          : 'Ready.';
        return;
      }

      // Handle incoming audio from the assistant.
      if (data.type === 'audio' && data.data) {
        playPcmBase64(data.data, PLAYBACK_RATE);
        return;
      }

      // Handle transcript updates.
      if (data.role && typeof data.text === 'string') {
        upsertTranscript(data.role, data.text);
        return;
      }
    };
  }
  connectWS(); // Initial connection attempt.
  enableBtn.disabled = true; // Disable mic button until connected.


  // ===================================================================
  // ====== Audio Capture: Manages microphone input and data sending ======
  // ===================================================================

  let mediaStream, audioCtx, processor, source;
  let micEnabled = false;
  let startingMic = false; // A lock to prevent multiple start attempts.
  let batch = new Uint8Array(0); // A buffer for audio data waiting to be sent.
  let sendTimer = null;

  /**
   * A class to convert audio from the browser's native 32-bit float format
   * to the 16-bit integer (PCM16) format expected by the server.
   */
  class PCM16Encoder {
    constructor(rate) { this.rate = rate; }
    encode(float32) {
      const len = float32.length;
      const out = new Int16Array(len);
      for (let i = 0; i < len; i++) {
        let s = float32[i];
        // Clamp the value to the [-1.0, 1.0] range.
        s = s < -1 ? -1 : (s > 1 ? 1 : s);
        // Convert to 16-bit integer.
        out[i] = s < 0 ? s * 0x8000 : s * 0x7FFF;
      }
      return new Uint8Array(out.buffer);
    }
  }

  /** Utility function to concatenate two Uint8Arrays. */
  function concatU8(a, b) {
    if (!a || a.length === 0) return b || new Uint8Array(0);
    if (!b || b.length === 0) return a;
    const out = new Uint8Array(a.length + b.length);
    out.set(a, 0);
    out.set(b, a.length);
    return out;
  }

  /**
   * Encodes a Uint8Array into a Base64 string.
   * Processes in chunks to avoid stack overflow on large arrays.
   */
  function base64Encode(uint8) {
    const CHUNK = 0x8000; // 32KB chunks.
    let result = '';
    for (let i = 0; i < uint8.length; i += CHUNK) {
      const slice = uint8.subarray(i, i + CHUNK);
      result += String.fromCharCode.apply(null, slice);
    }
    return btoa(result);
  }

  /**
   * Starts a timer that periodically sends buffered audio data to the server.
   * This decouples audio capture from network sending, smoothing out data flow.
   */
  function startSendLoop() {
    if (sendTimer) return;
    sendTimer = setInterval(() => {
      if (!ws || ws.readyState !== 1) return; // Don't send if WebSocket is not open.
      if (!batch || batch.length === 0) return; // Don't send if there's no data.

      // Take a chunk of data from the batch to send.
      const n = Math.min(MAX_BATCH, batch.length);
      const toSend = batch.subarray(0, n);
      batch = batch.subarray(n); // Keep the rest for the next interval.

      const b64 = base64Encode(toSend);
      const payload = { type: 'audio', mime: `audio/pcm;rate=${CAPTURE_RATE}`, data: b64 };
      ws.send(JSON.stringify(payload));
    }, SEND_PERIOD);
  }

  /** Stops the audio sending loop. */
  function stopSendLoop() {
    if (sendTimer) { clearInterval(sendTimer); sendTimer = null; }
  }

  /**
   * Initializes the microphone, Web Audio API, and starts capturing audio.
   */
  async function startMic() {
    if (startingMic || micEnabled) return;
    startingMic = true;
    enableBtn.disabled = true;

    try {
      // Get access to the user's microphone.
      mediaStream = await navigator.mediaDevices.getUserMedia({
        audio: { channelCount: 1, sampleRate: CAPTURE_RATE },
        video: false
      });

      // Create the Web Audio API context.
      audioCtx = new (window.AudioContext || window.webkitAudioContext)({ sampleRate: CAPTURE_RATE });
      // Resume context if it's suspended (required by some browsers' autoplay policies).
      if (audioCtx.state !== 'running') await audioCtx.resume();

      // Create the audio processing chain: source -> processor -> destination.
      source = audioCtx.createMediaStreamSource(mediaStream);
      processor = audioCtx.createScriptProcessor(PROC_CHUNK, 1, 1);
      const encoder = new PCM16Encoder(CAPTURE_RATE);

      // This event fires whenever a new chunk of audio data is available.
      processor.onaudioprocess = (ev) => {
        const input = ev.inputBuffer.getChannelData(0); // Get raw 32-bit float audio.
        const encoded = encoder.encode(input); // Convert to 16-bit integer PCM.
        batch = concatU8(batch, encoded); // Add the encoded data to our send buffer.
      };

      source.connect(processor);
      processor.connect(audioCtx.destination); // Connect to speakers to prevent garbage collection.

      // Update UI and state.
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

  /**
   * Stops the microphone and cleans up all associated Web Audio API resources.
   */
  function stopMic() {
    try {
      if (processor) { processor.disconnect(); processor.onaudioprocess = null; processor = null; }
      if (source) { source.disconnect(); source = null; }
      if (audioCtx) { audioCtx.close(); audioCtx = null; }
      if (mediaStream) { mediaStream.getTracks().forEach(t => t.stop()); mediaStream = null; }
      batch = new Uint8Array(0);
    } catch (_) {}

    // Update UI and state.
    micEnabled = false;
    enableBtn.classList.add('off');
    led.classList.remove('on');
    enableBtn.textContent = 'Enable Microphone ';
    if (led.parentNode !== enableBtn) enableBtn.appendChild(led);
    statusDiv.textContent = 'Microphone disabled';

    stopSendLoop();
  }

  // Add the main click event listener to the microphone button.
  enableBtn.addEventListener('click', () => {
    if (!ws || ws.readyState !== 1) {
      statusDiv.textContent = 'Status: Connecting… please wait';
      return;
    }
    if (!micEnabled) startMic(); else stopMic();
  });


  // ===============================================================
  // ====== Audio Playback: Plays audio received from the server ======
  // ===============================================================

  let playbackCtx; // The AudioContext for playback.
  let playTime = 0; // The scheduler time for queuing audio chunks seamlessly.

  /**
   * Ensures a playback AudioContext exists and has the correct sample rate.
   * This acts as a singleton manager for the playback context.
   */
  function ensurePlaybackCtx(rate) {
    if (!playbackCtx || playbackCtx.sampleRate !== rate) {
      playbackCtx = new (window.AudioContext || window.webkitAudioContext)({ sampleRate: rate });
      playTime = playbackCtx.currentTime;
    }
    return playbackCtx;
  }

  /**
   * Decodes, prepares, and schedules a chunk of Base64 PCM audio for playback.
   * @param {string} b64 The Base64 encoded audio data.
   * @param {number} sampleRate The sample rate of the audio.
   */
  // Plays actual decoded audio in the browser by capturing the upcoming data as JSON b64 [Extracts the data] 
  // Decodes and play the audio
  function playPcmBase64(b64, sampleRate) {
    if (!b64) return;
    let bytes;
    try {
      // Step 1: Decode Base64 string back into a byte array.
      bytes = Uint8Array.from(atob(b64), c => c.charCodeAt(0));
    } catch { return; }
    if (!bytes.length) return;

    // Step 2: Convert the 16-bit integer PCM data to the 32-bit float format required by Web Audio API.
    const view = new DataView(bytes.buffer);
    const len = bytes.byteLength / 2;
    const ctx = ensurePlaybackCtx(sampleRate);
    const buffer = ctx.createBuffer(1, len, sampleRate);
    const ch = buffer.getChannelData(0);
    for (let i = 0; i < len; i++) {
      // Read two bytes as a 16-bit signed integer and normalize to the [-1.0, 1.0] range.
      ch[i] = view.getInt16(i * 2, true) / 0x8000;
    }

    // Step 3: Create an audio source and connect it to the speakers.
    const src = ctx.createBufferSource();
    src.buffer = buffer;
    src.connect(ctx.destination);

    // Step 4: Schedule the playback to ensure seamless, gapless audio streaming.
    const now = ctx.currentTime;
    if (playTime < now) playTime = now; // Reset scheduler if it falls behind.
    try { src.start(playTime); } catch {} // Schedule this chunk to play at `playTime`.
    playTime += buffer.duration; // Advance the scheduler time by the duration of this chunk.
  }


  // ==============================================================
  // ====== Page Lifecycle Management: Handle tab visibility ======
  // ==============================================================

  // Stop the mic to save CPU and bandwidth if the user switches to another tab.
  document.addEventListener('visibilitychange', () => {
    if (document.hidden && micEnabled) {
      stopMic();
    }
  });

  // Ensure all resources are cleaned up before the page is closed.
  window.addEventListener('beforeunload', () => {
    stopMic();
    try { ws && ws.close(); } catch (_) {}
  });

})();