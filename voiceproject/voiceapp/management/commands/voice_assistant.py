import asyncio
import time
import base64
import pyaudio
from django.conf import settings
from channels.layers import get_channel_layer
from django.core.management.base import BaseCommand
from voiceapp.db_helpers import getlatest, gethistory, getsave_message
from google import genai
from .prompts import AGENT_PROMPT

FORMAT, CHANNELS, CHUNK = pyaudio.paInt16, 1, 1024
SEND_RATE, RECV_RATE = 16000, 24000
MODEL = "models/gemini-2.0-flash-exp"

client = genai.Client(
    api_key=getattr(settings, "GEMINI_API_KEY", None),
    http_options={"api_version": "v1beta"},
)

USER_SILENCE_MS = 700          # if no mic frames in this window, we commit user's utterance
ASSIST_SILENCE_MS = 250        # if no TTS frames in this window, we commit assistant's utterance
HEARTBEAT_PERIOD_S = 0.2       # tighter heartbeat for snappier state transitions

class AudioLoop:
    """Live audio loop with transcription enabled and DB persistence."""
    def __init__(self, pya_instance, stdout, browser_mode=False, group_name="voice_transcripts"):
        self.pya = pya_instance
        self.stdout = stdout
        self.browser_mode = browser_mode
        self.group_name = group_name

        # Queue variable for for capturing content
        self.to_send = asyncio.Queue(maxsize=10)
        self.received = asyncio.Queue()

        self.audio_in = None
        self._stop = asyncio.Event()

        # speaking state + timers
        self.user_speaking = False
        self.bot_speaking = False
        self._last_user_audio_ts = 0.0
        self._last_tts_audio_ts = 0.0

        # transcript buffers (latest cumulative strings)
        self.user_text = ""
        self.assistant_text = ""
        self._saved_user_text = ""
        self._saved_assistant_text = ""

        self.conversation_id = None
        self.channel_layer = get_channel_layer()

        # browser playback buffer variables
        self._out_buf = bytearray()
        self._last_emit = 0.0

    async def _broadcast(self, event: dict):
        try:
            # print(f"==> BROADCASTING TO BROWSER: {event}") 
            await self.channel_layer.group_send(self.group_name, event)
        except Exception:
            pass

    async def _broadcast_status(self, role: str, speaking: bool):
        await self._broadcast({
            "type": "status.message",
            "role": role,
            "speaking": speaking,
        })

    # Reads mic using PyAudio at 16 khz
    async def listen_audio(self):
        if self.browser_mode:
            return  # browser provides audio
        mic_info = self.pya.get_default_input_device_info()
        self.audio_in = await asyncio.to_thread(
            self.pya.open,
            format=FORMAT, channels=CHANNELS, rate=SEND_RATE,
            input=True, input_device_index=mic_info.get("index"), frames_per_buffer=CHUNK
        )
        try:
            while not self._stop.is_set():
                data = await asyncio.to_thread(self.audio_in.read, CHUNK, exception_on_overflow=False)
                if data:
                    await self.to_send.put({"data": data, "mime_type": f"audio/pcm;rate={SEND_RATE}"})
                    self._last_user_audio_ts = time.time()
        except Exception:
            pass
        finally:
            if self.audio_in:
                self.audio_in.close()

    # Audio is pulled (PCM = audio chunks) from the set Queue
    async def _gemini_sender(self):
        while not self._stop.is_set():
            try:
                item = await self.to_send.get()
                # Seeing my second payload 
                # print(f"--> SENDING TO GEMINI: {item['mime_type']}, {len(item['data'])} bytes")
                await self.session.send(input=item)
            except asyncio.CancelledError:
                break
            except Exception:
                await asyncio.sleep(0.05)

    async def _gemini_receiver(self):
        while not self._stop.is_set():
            try:
                async for resp in self.session.receive():
                    # Seeing my third payload
                    # print(f"<-- RECEIVED FROM GEMINI: {resp}")
                    # print("\n" + "="*20 + " NEW OBJECT RECEIVED " + "="*20)
                    # print(f"Object Type: {type(resp)}")
                    # print("--- Full Object Content ---")
                    # print(resp)
                    # print("--- Available Attributes ---")
                    # print(dir(resp))
                    # print("="*60 + "\n")
                    sc = getattr(resp, "server_content", None)
                    if not sc:
                        continue

                    # ---- Transcriptions (cumulative strings from Gemini) ----
                    # Live user text 
                    input_trans = getattr(sc, "input_transcription", None)
                    if input_trans and getattr(input_trans, "text", None):
                        self.user_text = (input_trans.text or "").strip()
                        # broadcast in format
                        await self._broadcast({
                            "type": "transcript.message",
                            "role": "user",
                            "text": self.user_text,
                        })

                    # Gemini output transcript
                    output_trans = getattr(sc, "output_transcription", None)
                    if output_trans and getattr(output_trans, "text", None):
                        self.assistant_text = (output_trans.text or "").strip()
                        await self._broadcast({
                            "type": "transcript.message",
                            "role": "assistant",
                            "text": self.assistant_text,
                        })

                    # ---- Audio chunks from model ----
                    mt = getattr(sc, "model_turn", None)
                    if mt:
                        for part in getattr(mt, "parts", []):
                            blob = getattr(part, "inline_data", None) or getattr(part, "inlineData", None)
                            if not blob:
                                continue
                            data = getattr(blob, "data", None)
                            if not data:
                                continue
                            # Audio data
                            # Audio data is not played directly into the browser
                            # It breaks down into base64 code
                            audio = data if isinstance(data, bytes) else base64.b64decode(data)

                            if not self.bot_speaking:
                                self.bot_speaking = True
                                await self._broadcast_status("assistant", True)

                            # mark last TTS time
                            self._last_tts_audio_ts = time.time()
                            
                            # Output to the browser
                            if self.browser_mode:
                                # output through done using the socket
                                await self._emit_audio_to_clients(audio)
                            else:
                                await self.received.put(audio)

                await asyncio.sleep(0.02)
            except asyncio.CancelledError:
                break
            except Exception:
                await asyncio.sleep(0.1)

    async def play_audio(self):
        if self.browser_mode: 
        # If the app is in browser mode, this function does nothing and exits immediately.
            return
        stream = None
        try:
            while not self._stop.is_set():
                if not stream:
                    stream = await asyncio.to_thread(
                        # Plays out our audio in the browser
                        self.pya.open,
                        format=FORMAT, channels=CHANNELS, rate=RECV_RATE, output=True
                    )
                chunk = await self.received.get()
                if isinstance(chunk, bytes) and chunk:
                    await asyncio.to_thread(stream.write, chunk)
        except Exception:
            pass
        finally:
            if stream:
                stream.close()

    async def _status_heartbeat(self):
        """Drives speaking state transitions + commits to DB when utterances end."""
        while not self._stop.is_set():
            now = time.time()

            # ----- USER speaking end by silence -----
            if self.user_speaking and (now - self._last_user_audio_ts) * 1000 > USER_SILENCE_MS:
                self.user_speaking = False
                await self._broadcast_status("user", False)
                await self._commit_user_if_ready()

            # ----- ASSISTANT speaking end by silence -----
            if self.bot_speaking:
                if self.browser_mode:
                    quiet_ms = (now - (self._last_tts_audio_ts or now)) * 1000
                    if quiet_ms > ASSIST_SILENCE_MS:
                        self.bot_speaking = False
                        await self._broadcast_status("assistant", False)
                        await self._commit_assistant_if_ready()
                else:
                    # speaker mode: queue empty is a good signal
                    if self.received.empty():
                        self.bot_speaking = False
                        await self._broadcast_status("assistant", False)
                        await self._commit_assistant_if_ready()

            await asyncio.sleep(HEARTBEAT_PERIOD_S)

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

    # Feeds the browser chunks to the web-socket consumer
    async def push_client_audio(self, pcm_bytes: bytes, mime_type: str = f"audio/pcm;rate={SEND_RATE}"):
        if not pcm_bytes:
            return
        self._last_user_audio_ts = time.time()
        if not self.user_speaking:
            self.user_speaking = True
            await self._broadcast_status("user", True)
        await self.to_send.put({"data": pcm_bytes, "mime_type": mime_type})
        
    # This is the function that sends audio TO the browser
    async def _emit_audio_to_clients(self, pcm_bytes: bytes):
        # coalesce tiny chunks for smoother playback
        self._out_buf += pcm_bytes
        elapsed = time.time() - (self._last_emit or 0.0)
        should_emit = len(self._out_buf) >= 4800 or elapsed > 0.2
        if should_emit:
            b64 = base64.b64encode(self._out_buf).decode("ascii")
            # Send the data from model to browser using channels.
            await self._broadcast({
                "type": "audio.message",
                "mime": f"audio/pcm;rate={RECV_RATE}",
                "data": b64,
            })
            self._out_buf = bytearray()
            self._last_emit = time.time()

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
                # Enable input and output transcription
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
                    await self.session.send(input={
                        "text": "Start the conversation with a brief greeting and the first question."
                    })
                except Exception as e:
                    if self.stdout:
                        self.stdout.write(f"[init] failed to request first response: {e}\n")                
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
            # final commit of any buffered text
            try:
                await self._commit_user_if_ready()
                await self._commit_assistant_if_ready()
            except Exception:
                pass
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