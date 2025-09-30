import asyncio
import logging
import time
import re
import base64
import pyaudio

from django.conf import settings
from channels.layers import get_channel_layer
from asgiref.sync import async_to_sync
from django.core.management.base import BaseCommand

from voiceapp.db_helpers import getlatest, gethistory, getsave_message
from google import genai

from .prompts import AGENT_PROMPT


for name in ("google.genai", "genai"):
    logging.getLogger(name).setLevel(logging.WARNING)


FORMAT, CHANNELS, CHUNK = pyaudio.paInt16, 1, 1024
SEND_RATE, RECV_RATE = 16000, 24000
MODEL = "models/gemini-2.0-flash-exp"

client = genai.Client(
    api_key=getattr(settings, "GEMINI_API_KEY", None),
    http_options={"api_version": "v1beta"},
)

def normalize_transcript(text: str | None) -> str | None:
    """Normalize spacing, merge letter-by-letter tokens, and tidy punctuation."""
    if not text:
        return text
    text = re.sub(r"\s+", " ", text).strip()
    tokens, out, i = text.split(), [], 0

    while i < len(tokens):
        t = tokens[i]
        if len(t) == 1 and t.isalpha():
            letters = [t]
            i += 1
            while i < len(tokens) and len(tokens[i]) == 1 and tokens[i].isalpha():
                letters.append(tokens[i])
                i += 1
            out.append("".join(letters) if len(letters) > 1 else letters[0])
        else:
            out.append(t)
            i += 1

    text = " ".join(out)
    for pat, rep in (
        (r"\s+([,?.!;:])", r"\1"),
        (r"\s+'", "'"),
        (r"'\s+", "'"),
        (r'\s+"', '"'),
        (r'"\s+', '"'),
        (r"\s{2,}", " "),
    ):
        text = re.sub(pat, rep, text)
    return text.strip()

class AudioLoop:
    """Minimal, robust audio <-> Gemini live loop with transcript persistence."""

    def __init__(self, pya_instance: pyaudio.PyAudio, stdout, browser_mode: bool = False, group_name: str = "voice_transcripts"):
        self.pya = pya_instance
        self.stdout = stdout
        self.browser_mode = browser_mode

        self.to_send: asyncio.Queue = asyncio.Queue(maxsize=10)
        self.received: asyncio.Queue = asyncio.Queue()

        self.audio_in = None
        self._stop = asyncio.Event()
        self.session = None

        # speaking status debounce
        self.user_speaking = False
        self.bot_speaking = False

        self.conversation_id: str | None = None

        # channels broadcasting
        self.channel_layer = get_channel_layer()
        self.group_name = group_name or "voice_transcripts"

    async def _broadcast(self, event: dict):
        if not self.channel_layer:
            return
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

    # --- Mic capture ---

    async def listen_audio(self):
        if self.browser_mode:
            return  # mic comes from browser via push_client_audio
        mic_info = self.pya.get_default_input_device_info()
        self.audio_in = await asyncio.to_thread(
            self.pya.open,
            format=FORMAT,
            channels=CHANNELS,
            rate=SEND_RATE,
            input=True,
            input_device_index=mic_info.get("index"),
            frames_per_buffer=CHUNK,
        )
        if self.stdout:
                self.stdout.write("🎙️ Microphone ready. Listening...\n")

        try:
            while not self._stop.is_set():
                try:
                    data = await asyncio.to_thread(
                        self.audio_in.read, CHUNK, exception_on_overflow=False
                    )
                    if data:
                        await self.to_send.put(
                            {"data": data, "mime_type": f"audio/pcm;rate={SEND_RATE}"}
                        )
                except Exception as e:
                    if self.stdout:
                        self.stdout.write(f"⚠️ Mic error: {e}\n")
                    await asyncio.sleep(0.1)
        finally:
            if self.audio_in:
                self.audio_in.close()

    # --- Gemini send/receive ---

    async def _gemini_sender(self):
        while not self._stop.is_set():
            try:
                item = await self.to_send.get()
                await self.session.send(input=item)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                if self.stdout:
                    self.stdout.write(f"📤 Send error: {e}\n")
                await asyncio.sleep(0.1)

    async def _gemini_receiver(self):
        while not self._stop.is_set():
            try:
                async for resp in self.session.receive():
                    sc = getattr(resp, "server_content", None)
                    if not sc:
                        continue

                    # Audio chunks
                    mt = getattr(sc, "model_turn", None)
                    if mt:
                        for part in getattr(mt, "parts", []):
                            blob = getattr(part, "inline_data", None) or getattr(
                                part, "inlineData", None
                            )
                            if not blob:
                                continue
                            data = getattr(blob, "data", None)
                            if not data:
                                continue
                            audio = data if isinstance(data, bytes) else base64.b64decode(data)
                            # mark assistant speaking on first chunk
                            if not self.bot_speaking:
                                self.bot_speaking = True
                                await self._broadcast_status("assistant", True)
                            if self.browser_mode:
                                await self._emit_audio_to_clients(audio)
                            else:
                                await self.received.put(audio)

                    # No transcript handling

                await asyncio.sleep(0.05)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                if self.stdout:
                    self.stdout.write(f"📥 Receive error: {e}\n")
                await asyncio.sleep(0.3)

    # --- Playback ---

    async def play_audio(self):
        if self.browser_mode:
            return  # playback happens in browser
        stream = None
        try:
            while not self._stop.is_set():
                if not stream:
                    stream = await asyncio.to_thread(
                        self.pya.open,
                        format=FORMAT,
                        channels=CHANNELS,
                        rate=RECV_RATE,
                        output=True,
                    )
                    if self.stdout:
                        self.stdout.write("🔊 Playback active.\n")

                chunk = await self.received.get()
                if isinstance(chunk, bytes) and chunk:
                    await asyncio.to_thread(stream.write, chunk)
                    # heuristic: assistant finished after short pause; mark not speaking in flush task below
        except asyncio.CancelledError:
            pass
        except Exception as e:
            if self.stdout:
                self.stdout.write(f"🔈 Playback error: {e}\n")
        finally:
            if stream:
                stream.close()

    # --- Periodic transcript flush ---

    async def _status_heartbeat(self):
        # Periodically mark end of speaking if no audio being produced
        while not self._stop.is_set():
            await asyncio.sleep(0.6)
            if self.bot_speaking and ((self.browser_mode) or self.received.empty()):
                self.bot_speaking = False
                await self._broadcast_status("assistant", False)

    # --- Browser streaming helpers ---
    async def push_client_audio(self, pcm_bytes: bytes, mime_type: str = f"audio/pcm;rate={SEND_RATE}"):
        # Called by consumer when receiving audio from browser
        if not pcm_bytes:
            return
        if not self.user_speaking:
            self.user_speaking = True
            await self._broadcast_status("user", True)
        await self.to_send.put({"data": pcm_bytes, "mime_type": mime_type})

    async def _emit_audio_to_clients(self, pcm_bytes: bytes):
        # Aggregate into ~100ms chunks to avoid choppy playback
        if not hasattr(self, "_out_buf"):
            self._out_buf = bytearray()
            self._last_emit = time.time()
        self._out_buf += pcm_bytes
        should_emit = len(self._out_buf) >= 4800 or (time.time() - self._last_emit) > 0.2
        if should_emit:
            b64 = base64.b64encode(self._out_buf).decode("ascii")
            await self._broadcast({
                "type": "audio.message",
                "mime": f"audio/pcm;rate={RECV_RATE}",
                "data": b64,
            })
            self._out_buf = bytearray()
            self._last_emit = time.time()

    # --- Orchestration ---

    async def run(self):
        try:
            # Conversation setup via helpers
            self.conversation_id = await getlatest()
            if self.stdout:
                self.stdout.write(f"📝 Session ID: {self.conversation_id}\n")

            history = await gethistory(self.conversation_id)
            config = {
                "generation_config": {"response_modalities": ["AUDIO"]},
                "speech_config": {
                    "voice_config": {"prebuilt_voice_config": {"voice_name": "Puck"}}
                },
                # no transcription requested
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
                if self.stdout:
                    self.stdout.write("💬 Voice chat started — press Ctrl+C to stop.\n")

                tasks = [
                    asyncio.create_task(self.listen_audio()),
                    asyncio.create_task(self._gemini_sender()),
                    asyncio.create_task(self._gemini_receiver()),
                    asyncio.create_task(self.play_audio()),
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
            if self.stdout:
                self.stdout.write("👋 Session ended.\n")

    async def stop(self):
        self._stop.set()

class Command(BaseCommand):
    help = "Starts a real-time voice chat with Gemini AI."

    def handle(self, *args, **options):
        pya = pyaudio.PyAudio()
        try:
            loop = AudioLoop(pya, self.stdout)
            asyncio.run(loop.run())
        except KeyboardInterrupt:
            self.stdout.write(self.style.SUCCESS("\n🛑 Chat terminated by user."))
        except Exception as e:
            self.stdout.write(self.style.ERROR(f"Error: {e}"))
        finally:
            pya.terminate()
            self.stdout.write(self.style.SUCCESS("✅ Audio resources released."))