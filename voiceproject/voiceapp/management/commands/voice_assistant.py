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

    def __init__(self, pya_instance: pyaudio.PyAudio, stdout):
        self.pya = pya_instance
        self.stdout = stdout

        self.to_send: asyncio.Queue = asyncio.Queue(maxsize=10)
        self.received: asyncio.Queue = asyncio.Queue()

        self.audio_in = None
        self._stop = asyncio.Event()
        self.session = None

        # transcript buffers + timers
        self.user_buf, self.bot_buf = "", ""
        self.user_t, self.bot_t = 0.0, 0.0
        self.flush_delay = 0.5

        self.conversation_id: str | None = None

        # channels broadcasting
        self.channel_layer = get_channel_layer()
        self.group_name = "voice_transcripts"

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
                            await self.received.put(audio)

                    # Transcripts
                    self._maybe_buffer_transcript(sc, attr="input_transcription", is_user=True)
                    self._maybe_buffer_transcript(sc, attr="output_transcription", is_user=False)

                await asyncio.sleep(0.05)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.stdout.write(f"📥 Receive error: {e}\n")
                await asyncio.sleep(0.3)

    def _maybe_buffer_transcript(self, sc, attr: str, is_user: bool):
        if not hasattr(sc, attr):
            return
        txt = getattr(getattr(sc, attr), "text", "") or ""
        if not txt:
            return
        nrm = normalize_transcript(txt.strip())
        if not nrm:
            return
        if is_user:
            self.user_buf += (" " + nrm)
            self.user_t = time.time()
            # speaking started/continuing
            asyncio.create_task(self._broadcast_status("user", True))
        else:
            self.bot_buf += (" " + nrm)
            self.bot_t = time.time()
            asyncio.create_task(self._broadcast_status("assistant", True))

    # --- Playback ---

    async def play_audio(self):
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
                    self.stdout.write("🔊 Playback active.\n")

                chunk = await self.received.get()
                if isinstance(chunk, bytes) and chunk:
                    await asyncio.to_thread(stream.write, chunk)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            self.stdout.write(f"🔈 Playback error: {e}\n")
        finally:
            if stream:
                stream.close()

    # --- Periodic transcript flush ---

    async def flush_transcripts(self):
        while not self._stop.is_set():
            await asyncio.sleep(self.flush_delay)
            now = time.time()

            for is_user in (True, False):
                buf = (self.user_buf if is_user else self.bot_buf).strip()
                last = self.user_t if is_user else self.bot_t
                if not buf or now - last <= self.flush_delay:
                    continue
                if len(buf) <= 3 and not buf.endswith((".", "?", "!")):
                    continue

                norm = normalize_transcript(buf)
                if not norm:
                    continue

                emoji, label, role = ("🧑", "You", "user") if is_user else ("🤖", "Gemini", "assistant")
                self.stdout.write(f"{emoji} {label}: {norm}\n")
                # Persist using the external helpers
                await getsave_message(self.conversation_id, role, norm)

                # Broadcast transcript to clients
                await self._broadcast({
                    "type": "transcript.message",
                    "role": role,
                    "text": norm,
                })

                if is_user:
                    self.user_buf = ""
                else:
                    self.bot_buf = ""

                # speaking ended for this turn
                await self._broadcast_status(role, False)

    # --- Orchestration ---

    async def run(self):
        try:
            # Conversation setup via helpers
            self.conversation_id = await getlatest()
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
                self.stdout.write("💬 Voice chat started — press Ctrl+C to stop.\n")

                tasks = [
                    asyncio.create_task(self.listen_audio()),
                    asyncio.create_task(self._gemini_sender()),
                    asyncio.create_task(self._gemini_receiver()),
                    asyncio.create_task(self.play_audio()),
                    asyncio.create_task(self.flush_transcripts()),
                ]

                await self._stop.wait()
                for t in tasks:
                    t.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)

        except Exception as e:
            self.stdout.write(f"💥 Run error: {e}\n")
        finally:
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