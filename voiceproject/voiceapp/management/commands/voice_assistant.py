# voice_assistant.py
import asyncio
import traceback
import logging
import pyaudio
from django.conf import settings
from django.core.management.base import BaseCommand
import base64
import warnings
import sys

# --- Quiet the repeated SDK inline_data warning (two-layer) ---
warnings.filterwarnings("ignore", message=r".*non-text parts in the response.*inline_data.*")
_orig_stderr_write = sys.stderr.write


def _stderr_write_filter(s):
    try:
        if "there are non-text parts in the response: ['inline_data']" in s:
            return
        if "non-text parts in the response" in s and "inline_data" in s:
            return
    except Exception:
        pass
    return _orig_stderr_write(s)


sys.stderr.write = _stderr_write_filter

# reduce SDK logging noise
logging.getLogger("google.genai").setLevel(logging.WARNING)
logging.getLogger("genai").setLevel(logging.WARNING)


# --- Fallback fake client if google.genai isn't installed (keeps file runnable) ---
try:
    from google import genai
except ImportError:
    class _FakeSession:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass
        async def send(self, input): pass
        def receive(self):
            class _Turn:
                def __aiter__(self): return self
                async def __anext__(self): raise StopAsyncIteration
            return _Turn()

    class FakeClient:
        @property
        def aio(self):
            class _AIO:
                def live(self):
                    class _Live:
                        @staticmethod
                        async def connect(*a, **k): return _FakeSession()
                    return _Live()
            return _AIO()
    genai = FakeClient()


# audio constants
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
}

client = genai.Client(api_key=getattr(settings, "GEMINI_API_KEY", None),
                      http_options={"api_version": "v1alpha"})


class AudioLoop:
    def __init__(self, pya_instance, stdout):
        self.pya = pya_instance
        self.stdout = stdout
        self.to_send = None
        self.received = None
        self.audio_stream = None
        self.session = None
        self._stop = asyncio.Event()
        self.tasks = []

    async def listen_audio(self):
        # open mic once and stream
        mic_info = self.pya.get_default_input_device_info()
        self.audio_stream = await asyncio.to_thread(
            self.pya.open,
            format=FORMAT, channels=CHANNELS, rate=SEND_RATE,
            input=True, input_device_index=mic_info.get("index"),
            frames_per_buffer=CHUNK,
        )
        self.stdout.write("Microphone opened. Listening...\n")
        kwargs = {"exception_on_overflow": False}
        while not self._stop.is_set():
            try:
                data = await asyncio.to_thread(self.audio_stream.read, CHUNK, **kwargs)
                if data:
                    await self.to_send.put({"data": data, "mime_type": f"audio/pcm;rate={SEND_RATE}"})
            except Exception as e:
                self.stdout.write(f"Microphone error (continuing): {e}\n")
                await asyncio.sleep(0.1)

    async def send_realtime(self):
        while not self._stop.is_set():
            try:
                msg = await self.to_send.get()
                try:
                    await self.session.send(input=msg)
                except Exception as e:
                    self.stdout.write(f"Send error (retrying): {e}\n")
                    await asyncio.sleep(0.1)
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.stdout.write(f"Send loop error: {e}\n")
                await asyncio.sleep(0.1)

    async def _enqueue_audio_bytes(self, data):
        if isinstance(data, (bytes, bytearray)):
            await self.received.put(bytes(data))
        else:
            # assume base64 string
            try:
                await self.received.put(base64.b64decode(data))
            except Exception:
                pass

    async def receive_audio(self):
        while not self._stop.is_set():
            try:
                async for response in self.session.receive():
                    server_content = getattr(response, "server_content", None)
                    model_turn = getattr(server_content, "model_turn", None) if server_content else None

                    # 1) consume model_turn.parts inline_data bytes (Blob.data)
                    parts = getattr(model_turn, "parts", None) or []
                    if parts:
                        for part in parts:
                            inline_blob = getattr(part, "inline_data", None) or getattr(part, "inlineData", None)
                            if inline_blob is not None:
                                blob_data = getattr(inline_blob, "data", None)
                                if blob_data:
                                    # raw bytes or base64 string
                                    if isinstance(blob_data, (bytes, bytearray)):
                                        await self._enqueue_audio_bytes(blob_data)
                                    else:
                                        try:
                                            await self._enqueue_audio_bytes(base64.b64decode(blob_data))
                                        except Exception:
                                            pass
                                    try:
                                        setattr(inline_blob, "data", None)
                                    except Exception:
                                        pass

                    # 2) top-level inline_data fallback (list/dict)
                    inline = getattr(response, "inline_data", None)
                    if inline:
                        parts_list = inline if isinstance(inline, (list, tuple)) else getattr(inline, "parts", None) or []
                        for p in parts_list:
                            if isinstance(p, dict):
                                b = p.get("data") or p.get("b64") or p.get("base64")
                            else:
                                b = getattr(p, "data", None) or getattr(p, "b64", None) or getattr(p, "base64", None)
                            if b:
                                if isinstance(b, (bytes, bytearray)):
                                    await self._enqueue_audio_bytes(b)
                                else:
                                    try:
                                        await self._enqueue_audio_bytes(base64.b64decode(b))
                                    except Exception:
                                        pass
                        try:
                            setattr(response, "inline_data", None)
                        except Exception:
                            pass

                    # 3) print user transcription if present
                    if server_content and hasattr(server_content, "input_transcription"):
                        txt = getattr(server_content.input_transcription, "text", None)
                        if txt:
                            self.stdout.write(f"You: {txt}\n")

                    # 4) print model text parts
                    if parts:
                        for part in parts:
                            if hasattr(part, "text") and part.text:
                                self.stdout.write(f"Gemini: {part.text}\n")

                    # 5) response.text fallback
                    if hasattr(response, "text") and response.text:
                        self.stdout.write(f"Gemini: {response.text}\n")

                    # 6) response.data / output_audio fallbacks
                    if hasattr(response, "data") and response.data:
                        data = response.data
                        if isinstance(data, (bytes, bytearray)):
                            await self._enqueue_audio_bytes(data)
                        else:
                            try:
                                await self._enqueue_audio_bytes(base64.b64decode(data))
                            except Exception:
                                pass
                        try:
                            setattr(response, "data", None)
                        except Exception:
                            pass

                    if hasattr(response, "output_audio") and response.output_audio:
                        out = response.output_audio
                        if isinstance(out, (bytes, bytearray)):
                            await self._enqueue_audio_bytes(out)
                        else:
                            try:
                                await self._enqueue_audio_bytes(base64.b64decode(out))
                            except Exception:
                                pass
                        try:
                            setattr(response, "output_audio", None)
                        except Exception:
                            pass

                # if receive loop exited normally, sleep then retry
                await asyncio.sleep(0.05)
            except asyncio.CancelledError:
                return
            except Exception as e:
                traceback.print_exc()
                self.stdout.write(f"Receive loop error (recovering): {e}\n")
                await asyncio.sleep(0.5)

    async def play_audio(self):
        stream = None
        while not self._stop.is_set():
            try:
                if stream is None:
                    stream = await asyncio.to_thread(
                        self.pya.open, format=FORMAT, channels=CHANNELS, rate=RECV_RATE, output=True
                    )
                    self.stdout.write("Playback stream opened.\n")
                chunk = await self.received.get()
                if isinstance(chunk, bytes) and chunk:
                    await asyncio.to_thread(stream.write, chunk)
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.stdout.write(f"Playback error (recovering): {e}\n")
                await asyncio.sleep(0.1)
        if stream:
            try:
                stream.close()
            except Exception:
                pass

    async def run(self):
        try:
            async with client.aio.live.connect(model=MODEL, config=CONFIG) as session:
                self.session = session
                self.to_send = asyncio.Queue(maxsize=10)
                self.received = asyncio.Queue()

                self.stdout.write("Voice chat started — press Ctrl+C to stop.\nUse headphones to avoid feedback.\n")

                # start tasks (create_task so one failure doesn't cancel others)
                self.tasks = [
                    asyncio.create_task(self.listen_audio()),
                    asyncio.create_task(self.send_realtime()),
                    asyncio.create_task(self.receive_audio()),
                    asyncio.create_task(self.play_audio()),
                ]
                # wait until stop event is set
                await self._stop.wait()

                for t in self.tasks:
                    t.cancel()
                await asyncio.gather(*self.tasks, return_exceptions=True)
        except Exception as e:
            traceback.print_exception(e)
            self.stdout.write(f"Run error: {e}\n")
        finally:
            if self.audio_stream:
                try:
                    self.audio_stream.close()
                except Exception:
                    pass
            self.stdout.write("Voice chat session ended.\n")

    async def stop(self):
        self._stop.set()


class Command(BaseCommand):
    help = "Starts a real-time voice chat with Gemini AI."

    def handle(self, *args, **options):
        logging.captureWarnings(True)
        logging.getLogger("py.warnings").setLevel(logging.ERROR)

        pya = pyaudio.PyAudio()
        loop = None
        try:
            audio = AudioLoop(pya, self.stdout)
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            loop.run_until_complete(audio.run())
        except KeyboardInterrupt:
            if loop and not loop.is_closed():
                try:
                    loop.run_until_complete(audio.stop())
                    loop.run_until_complete(asyncio.sleep(0.05))
                except Exception:
                    pass
            self.stdout.write(self.style.SUCCESS("\nChat terminated by user."))
        except Exception:
            traceback.print_exc()
        finally:
            try:
                pya.terminate()
            except Exception:
                pass
            self.stdout.write(self.style.SUCCESS("Audio resources released."))
