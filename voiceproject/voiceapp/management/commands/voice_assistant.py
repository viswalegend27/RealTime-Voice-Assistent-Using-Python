# voice_assistant.py
import asyncio
import traceback
import logging
import pyaudio
import base64
import time
from django.conf import settings
from django.core.management.base import BaseCommand
from google import genai

# Reduce logging noise
logging.getLogger("google.genai").setLevel(logging.WARNING)
logging.getLogger("genai").setLevel(logging.WARNING)

# Audio constants
FORMAT = pyaudio.paInt16
CHANNELS = 1
SEND_RATE = 16000
RECV_RATE = 24000
CHUNK = 1024

MODEL = "models/gemini-2.0-flash-exp"
CONFIG = {
    "generation_config": {"response_modalities": ["AUDIO"]},
    "speech_config": {"voice_config": {"prebuilt_voice_config": {"voice_name": "Puck"}}},
    "input_audio_transcription": {},
    "output_audio_transcription": {},
}

client = genai.Client(
    api_key=getattr(settings, "GEMINI_API_KEY", None),
    http_options={"api_version": "v1alpha"}
)

class AudioLoop:
    def __init__(self, pya_instance, stdout):
        self.pya = pya_instance
        self.stdout = stdout
        self.to_send = asyncio.Queue(maxsize=10)
        self.received = asyncio.Queue()
        self.audio_stream = None
        self.session = None
        self._stop = asyncio.Event()
        self.tasks = []

        # Transcription buffering
        self.user_buffer = ""
        self.gemini_buffer = ""
        self.last_user_time = 0
        self.last_gemini_time = 0
        self.timeout = 1.5

    async def listen_audio(self):
        mic_info = self.pya.get_default_input_device_info()
        self.audio_stream = await asyncio.to_thread(
            self.pya.open, format=FORMAT, channels=CHANNELS, rate=SEND_RATE,
            input=True, input_device_index=mic_info.get("index"), frames_per_buffer=CHUNK
        )
        self.stdout.write("Microphone opened. Listening...\n")

        while not self._stop.is_set():
            try:
                data = await asyncio.to_thread(
                    self.audio_stream.read, CHUNK, exception_on_overflow=False
                )
                if data:
                    await self.to_send.put({
                        "data": data, 
                        "mime_type": f"audio/pcm;rate={SEND_RATE}"
                    })
            except Exception as e:
                self.stdout.write(f"Mic error: {e}\n")
                await asyncio.sleep(0.1)

    async def send_audio(self):
        while not self._stop.is_set():
            try:
                msg = await self.to_send.get()
                await self.session.send(input=msg)
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.stdout.write(f"Send error: {e}\n")
                await asyncio.sleep(0.1)

    def should_print(self, text, last_time):
        current_time = time.time()
        return (len(text.strip()) > 20 or 
                current_time - last_time > self.timeout or
                text.endswith(('.', '?', '!')))

    async def flush_buffers(self):
        while not self._stop.is_set():
            await asyncio.sleep(self.timeout)
            current_time = time.time()

            if (self.user_buffer.strip() and 
                current_time - self.last_user_time > self.timeout):
                self.stdout.write(f"You: {self.user_buffer.strip()}\n")
                self.user_buffer = ""

            if (self.gemini_buffer.strip() and 
                current_time - self.last_gemini_time > self.timeout):
                self.stdout.write(f"Gemini: {self.gemini_buffer.strip()}\n")
                self.gemini_buffer = ""

    async def receive_audio(self):
        while not self._stop.is_set():
            try:
                async for response in self.session.receive():
                    server_content = getattr(response, "server_content", None)
                    if not server_content:
                        continue

                    # Extract audio data
                    model_turn = getattr(server_content, "model_turn", None)
                    if model_turn:
                        parts = getattr(model_turn, "parts", None) or []
                        for part in parts:
                            inline_blob = (getattr(part, "inline_data", None) or 
                                         getattr(part, "inlineData", None))
                            if inline_blob:
                                blob_data = getattr(inline_blob, "data", None)
                                if blob_data:
                                    if isinstance(blob_data, (bytes, bytearray)):
                                        await self.received.put(bytes(blob_data))
                                    else:
                                        try:
                                            await self.received.put(base64.b64decode(blob_data))
                                        except:
                                            pass

                    # Handle user transcription
                    if hasattr(server_content, "input_transcription"):
                        input_trans = getattr(server_content, "input_transcription", None)
                        if input_trans:
                            txt = getattr(input_trans, "text", None)
                            if txt:
                                self.user_buffer = txt
                                self.last_user_time = time.time()
                                if self.should_print(txt, self.last_user_time):
                                    self.stdout.write(f"You: {txt.strip()}\n")
                                    self.user_buffer = ""

                    # Handle Gemini transcription
                    if hasattr(server_content, "output_transcription"):
                        output_trans = getattr(server_content, "output_transcription", None)
                        if output_trans:
                            txt = getattr(output_trans, "text", None)
                            if txt:
                                self.gemini_buffer = txt
                                self.last_gemini_time = time.time()
                                if self.should_print(txt, self.last_gemini_time):
                                    self.stdout.write(f"Gemini: {txt.strip()}\n")
                                    self.gemini_buffer = ""

                await asyncio.sleep(0.05)
            except asyncio.CancelledError:
                return
            except Exception as e:
                self.stdout.write(f"Receive error: {e}\n")
                await asyncio.sleep(0.5)

    async def play_audio(self):
        stream = None
        while not self._stop.is_set():
            try:
                if stream is None:
                    stream = await asyncio.to_thread(
                        self.pya.open, format=FORMAT, channels=CHANNELS, 
                        rate=RECV_RATE, output=True
                    )
                    self.stdout.write("Playback stream opened.\n")

                chunk = await self.received.get()
                if isinstance(chunk, bytes) and chunk:
                    await asyncio.to_thread(stream.write, chunk)
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.stdout.write(f"Playback error: {e}\n")
                await asyncio.sleep(0.1)

        if stream:
            try:
                stream.close()
            except:
                pass

    async def run(self):
        try:
            async with client.aio.live.connect(model=MODEL, config=CONFIG) as session:
                self.session = session
                self.stdout.write("Voice chat started — press Ctrl+C to stop.\n")

                self.tasks = [
                    asyncio.create_task(self.listen_audio()),
                    asyncio.create_task(self.send_audio()),
                    asyncio.create_task(self.receive_audio()),
                    asyncio.create_task(self.play_audio()),
                    asyncio.create_task(self.flush_buffers()),
                ]

                await self._stop.wait()

                for task in self.tasks:
                    task.cancel()
                await asyncio.gather(*self.tasks, return_exceptions=True)

        except Exception as e:
            self.stdout.write(f"Run error: {e}\n")
        finally:
            if self.audio_stream:
                try:
                    self.audio_stream.close()
                except:
                    pass
            self.stdout.write("Voice chat session ended.\n")

    async def stop(self):
        self._stop.set()

class Command(BaseCommand):
    help = "Starts a real-time voice chat with Gemini AI."

    def handle(self, *args, **options):
        pya = pyaudio.PyAudio()
        try:
            audio = AudioLoop(pya, self.stdout)
            asyncio.run(audio.run())
        except KeyboardInterrupt:
            self.stdout.write(self.style.SUCCESS("\nChat terminated by user."))
        except Exception:
            traceback.print_exc()
        finally:
            try:
                pya.terminate()
            except:
                pass
            self.stdout.write(self.style.SUCCESS("Audio resources released."))
