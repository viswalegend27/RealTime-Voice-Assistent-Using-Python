# voiceapp/utils.py
import asyncio
import time
import base64
from django.conf import settings
from channels.layers import get_channel_layer
from voiceapp.db_helpers import getlatest, gethistory, getsave_message
from google import genai

# Try both locations for AGENT_PROMPT (project or app), fallback to settings
try:
    from voiceproject.constants import AGENT_PROMPT  # project-level
except Exception:
    try:
        from .constants import AGENT_PROMPT  # app-level
    except Exception:
        AGENT_PROMPT = getattr(settings, "AGENT_PROMPT", "You are a helpful voice assistant.")

# ====== AUDIO & MODEL CONFIG (browser-only) ======
SEND_RATE = 16000   # browser -> server mic
RECV_RATE = 24000   # server -> browser TTS
MODEL = "models/gemini-2.0-flash-exp"

client = genai.Client(
    api_key=getattr(settings, "GEMINI_API_KEY", None),
    http_options={"api_version": "v1beta"},
)

# Silence windows (ms) to commit rolling transcripts
USER_SILENCE_MS = 300
ASSIST_SILENCE_MS = 250
HEARTBEAT_PERIOD_S = 0.2


class AudioLoop:

    def __init__(self, pya_instance, stdout, browser_mode=False, group_name="voice_transcripts"):
        self.stdout = stdout
        self.browser_mode = True  # force browser mode
        self.group_name = group_name

        # Queues + state
        self.to_send = asyncio.Queue(maxsize=20)
        self._stop = asyncio.Event()

        self.user_speaking = False
        self.bot_speaking = False
        self._last_user_audio_ts = 0.0
        self._last_tts_audio_ts = 0.0

        self.user_text = ""
        self.assistant_text = ""
        self._saved_user_text = ""
        self._saved_assistant_text = ""

        self.conversation_id = None
        self.channel_layer = get_channel_layer()

        # browser playback coalescing
        self._out_buf = bytearray()
        self._last_emit = 0.0

        self.session = None

    # ---------------- Channels helpers ----------------
    async def _broadcast(self, event: dict):
        try:
            await self.channel_layer.group_send(self.group_name, event)
        except Exception:
            pass

    async def _broadcast_status(self, role: str, speaking: bool):
        await self._broadcast({
            "type": "status.message",
            "role": role,
            "speaking": speaking,
        })

    # ---------------- Public API (used by your consumer) ----------------
    async def push_client_audio(self, pcm_bytes: bytes, mime_type: str = f"audio/pcm;rate={SEND_RATE}"):
        if not pcm_bytes:
            return
        self._last_user_audio_ts = time.time()
        if not self.user_speaking:
            self.user_speaking = True
            await self._broadcast_status("user", True)
        await self.to_send.put({"data": pcm_bytes, "mime_type": mime_type})

    async def stop(self):
        self._stop.set()

    # ---------------- Internal: Gemini I/O ----------------
    async def _gemini_sender(self):
        while not self._stop.is_set():
            try:
                item = await self.to_send.get()
                await self.session.send(input=item)
            except asyncio.CancelledError:
                break
            except Exception:
                await asyncio.sleep(0.04)

    async def _gemini_receiver(self):
        while not self._stop.is_set():
            try:
                async for resp in self.session.receive():
                    sc = getattr(resp, "server_content", None)
                    if not sc:
                        continue

                    # Rolling input (user) transcript
                    input_trans = getattr(sc, "input_transcription", None)
                    if input_trans and getattr(input_trans, "text", None):
                        self.user_text = (input_trans.text or "").strip()
                        await self._broadcast({
                            "type": "transcript.message",
                            "role": "user",
                            "text": self.user_text,
                        })

                    # Rolling output (assistant) transcript
                    output_trans = getattr(sc, "output_transcription", None)
                    if output_trans and getattr(output_trans, "text", None):
                        self.assistant_text = (output_trans.text or "").strip()
                        await self._broadcast({
                            "type": "transcript.message",
                            "role": "assistant",
                            "text": self.assistant_text,
                        })

                    # Audio chunks from model (TTS)
                    mt = getattr(sc, "model_turn", None)
                    if mt:
                        for part in getattr(mt, "parts", []):
                            blob = getattr(part, "inline_data", None) or getattr(part, "inlineData", None)
                            if not blob:
                                continue
                            data = getattr(blob, "data", None)
                            if not data:
                                continue
                            audio = data if isinstance(data, bytes) else base64.b64decode(data)

                            if not self.bot_speaking:
                                self.bot_speaking = True
                                await self._broadcast_status("assistant", True)

                            self._last_tts_audio_ts = time.time()
                            await self._emit_audio_to_clients(audio)

                await asyncio.sleep(0.02)
            except asyncio.CancelledError:
                break
            except Exception:
                await asyncio.sleep(0.08)

    # ---------------- Emit audio to browser (24 kHz PCM) ----------------
    async def _emit_audio_to_clients(self, pcm_bytes: bytes):
        # coalesce small chunks for smoother playback
        self._out_buf += pcm_bytes
        elapsed = time.time() - (self._last_emit or 0.0)
        if len(self._out_buf) >= 4800 or elapsed > 0.2:
            b64 = base64.b64encode(self._out_buf).decode("ascii")
            await self._broadcast({
                "type": "audio.message",
                "mime": f"audio/pcm;rate={RECV_RATE}",
                "data": b64,
            })
            self._out_buf = bytearray()
            self._last_emit = time.time()

    # ---------------- Commit transcripts (optional persistence) ----------------
    async def _commit_user_if_ready(self):
        text = (self.user_text or "").strip()
        if text and text != self._saved_user_text:
            try:
                await getsave_message(self.conversation_id, "user", text)
                self._saved_user_text = text
            except Exception:
                pass

    async def _commit_assistant_if_ready(self):
        text = (self.assistant_text or "").strip()
        if text and text != self._saved_assistant_text:
            try:
                await getsave_message(self.conversation_id, "assistant", text)
                self._saved_assistant_text = text
            except Exception:
                pass

    # ---------------- Heartbeat: detect silence / commit turns ----------------
    async def _status_heartbeat(self):
        while not self._stop.is_set():
            now = time.time()

            # USER speaking end by silence
            if self.user_speaking and (now - self._last_user_audio_ts) * 1000 > USER_SILENCE_MS:
                self.user_speaking = False
                await self._broadcast_status("user", False)
                await self._commit_user_if_ready()

            # ASSISTANT speaking end by silence
            if self.bot_speaking:
                quiet_ms = (now - (self._last_tts_audio_ts or now)) * 1000
                if quiet_ms > ASSIST_SILENCE_MS:
                    self.bot_speaking = False
                    await self._broadcast_status("assistant", False)
                    await self._commit_assistant_if_ready()

            await asyncio.sleep(HEARTBEAT_PERIOD_S)

    # ---------------- Main loop ----------------
    async def run(self):
        try:
            self.conversation_id = await getlatest()
            if self.stdout:
                self.stdout.write(f"📝 Session ID: {self.conversation_id}\n")

            history = await gethistory(self.conversation_id)

            config = {
                "generation_config": {"response_modalities": ["AUDIO"]},
                "speech_config": {
                    "voice_config": {"prebuilt_voice_config": {"voice_name": "Puck"}}
                },
                "input_audio_transcription": {},
                "output_audio_transcription": {},
                "system_instruction": {
                    "parts": [
                        {
                            "text": f"{AGENT_PROMPT.strip()}\n\n{history}\n\nNow continue the conversation naturally."
                        }
                    ]
                },
            }

            async with client.aio.live.connect(model=MODEL, config=config) as session:
                self.session = session
                try:
                    await self.session.send(input={"text": "Start the conversation with a brief greeting and the first question."})
                except Exception as e:
                    if self.stdout:
                        self.stdout.write(f"[init] failed to request first response: {e}\n")
                if self.stdout:
                    self.stdout.write("💬 Voice chat started — browser mode.\n")

                tasks = [
                    asyncio.create_task(self._gemini_sender()),
                    asyncio.create_task(self._gemini_receiver()),
                    asyncio.create_task(self._status_heartbeat()),
                ]
                await self._stop.wait()
                for t in tasks:
                    t.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)

        except Exception as e:
            if self.stdout:
                self.stdout.write(f"💥 Run error: {e}\n")
        finally:
            try:
                await self._commit_user_if_ready()
                await self._commit_assistant_if_ready()
            except Exception:
                pass
            if self.stdout:
                self.stdout.write("👋 Session ended.\n")