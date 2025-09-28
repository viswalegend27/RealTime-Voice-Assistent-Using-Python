# FILE: voiceproject/voiceapp/management/commands/voice_assistant.py
import asyncio
import traceback
import logging
import pyaudio
import base64
import time
import re
from django.conf import settings
from django.core.management.base import BaseCommand
from google import genai

# Import custom agent prompt
from .prompts import AGENT_PROMPT

# Silence verbose logs
logging.getLogger("google.genai").setLevel(logging.WARNING)
logging.getLogger("genai").setLevel(logging.WARNING)

# Setting how the audio is recorded
FORMAT, CHANNELS = pyaudio.paInt16, 1
# How the audio is been sent, recived by the User using AI
SEND_RATE, RECV_RATE, CHUNK = 16000, 24000, 1024

#Configuring the model used in our code for responses 
MODEL = "models/gemini-2.0-flash-exp"
CONFIG = {
    # Would set how the output is been recieved, in thi case it is AUDIO
    "generation_config": {"response_modalities": ["AUDIO"]},
    "speech_config": {"voice_config": {"prebuilt_voice_config": {"voice_name": "Puck"}}},
    "input_audio_transcription": {},
    "output_audio_transcription": {},
    "system_instruction": {"parts": [{"text": AGENT_PROMPT.strip()}]},
}

# Setting up the client by locating our the .env variables
client = genai.Client(
    api_key=getattr(settings, "GEMINI_API_KEY", None),
    http_options={"api_version": "v1alpha"},
)

# Make the transript expressions to look normal
def normalize_transcript(s: str) -> str:
    if not s:
        return s
    s = re.sub(r"\s+", " ", s).strip()

    # Split and join single-letter runs into words
    tokens = s.split(" ")
    out = []
    i = 0
    while i < len(tokens):
        t = tokens[i]
        if len(t) == 1 and t.isalpha():
            # collect run
            letters = [t]
            i += 1
            while i < len(tokens) and len(tokens[i]) == 1 and tokens[i].isalpha():
                letters.append(tokens[i]); i += 1
            if len(letters) >= 2:
                out.append("".join(letters))
            else:
                out.append(letters[0])
        else:
            out.append(t); i += 1
    s = " ".join(out)

    # Fix spacing around punctuation and contractions
    # Actual regular expressions used to remove empty spaces
    s = re.sub(r"\s+([,?.!;:])", r"\1", s)          # remove space before punctuation
    s = re.sub(r"\s+'", "'", s)                     # space before apostrophe
    s = re.sub(r"'\s+", "'", s)                     # space after apostrophe
    s = re.sub(r"\s+\"", "\"", s)                   # space before quote
    s = re.sub(r"\" \s+", "\"", s)                  # space after quote (best-effort)
    s = re.sub(r"\s{2,}", " ", s)                   # collapse any leftover multi-space
    return s.strip()


class AudioLoop:
    def __init__(self, pya_instance, stdout):
        # pyAudio instance and output stream is stored for printing
        self.pya, self.stdout = pya_instance, stdout
        # AudioChunks are been stored to send to Gemini 
        self.to_send = asyncio.Queue(maxsize=10)
        # Stored audio-chunks for Playback
        self.received = asyncio.Queue()
        # Setting audio_stream and session == None
        self.audio_stream = self.session = None
        # Setting a var to instantiate when loop ends
        self._stop = asyncio.Event()
        self.tasks = []
        # User transcripts stored here
        self.user_buffer = self.gemini_buffer = ""
        # Last user context
        self.last_user_time = self.last_gemini_time = 0
        # Actual time-period for the transcripts to get printed out
        self.timeout = 0.5
        self.prompt_injected = True  

    async def listen_audio(self):
        mic_info = self.pya.get_default_input_device_info()
        # Setting up the Audio-streams for the PyAudio 
        self.audio_stream = await asyncio.to_thread(
            self.pya.open,
            format=FORMAT,
            channels=CHANNELS,
            rate=SEND_RATE,
            input=True,
            input_device_index=mic_info.get("index"),
            frames_per_buffer=CHUNK,
        )
        self.stdout.write("🎙️ Microphone ready. Listening...\n")
        while not self._stop.is_set():
            try:
                # Initial audio is been read
                data = await asyncio.to_thread(self.audio_stream.read, CHUNK, exception_on_overflow=False)
                if data:
                    # Storing this to sending it to the AI or other process
                    await self.to_send.put({"data": data, "mime_type": f"audio/pcm;rate={SEND_RATE}"})
            except Exception as e:
                self.stdout.write(f"⚠️ Mic error: {e}\n")
                await asyncio.sleep(0.1)

    async def send_audio(self):
        # Indefinite loop is set unit ctrl + c
        while not self._stop.is_set():
            try:
                # Gets the audio chunk message and stores in msg
                msg = await self.to_send.get()
                # Session out to Gemini via input
                await self.session.send(input=msg)
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.stdout.write(f"📤 Send error: {e}\n")
                await asyncio.sleep(0.1)

    # The method controls when text should be printed
    # It avoids printing very short fragments 
    def should_print(self, text, last_time):
        now = time.time() # Setting the time period
        clean = text.strip()
        return len(clean) > 3 and (now - last_time > self.timeout or clean.endswith((".", "?", "!")))

    # This function ensures that both user and AI transcript texts are printed in a clean, paced manner.
    async def flush_buffers(self):
        while not self._stop.is_set():
            await asyncio.sleep(self.timeout)
            now = time.time() # Get the current time to calculate the last User Transcript
            # Logic for sufficient time passed in User Input
            if self.user_buffer.strip() and now - self.last_user_time > self.timeout:
                full = self.user_buffer.strip() # Removes trailing and leading spaces
                if self.should_print(full, self.last_user_time):
                    self.stdout.write(f"🧑 You: {normalize_transcript(full)}\n")
                self.user_buffer = ""
            if self.gemini_buffer.strip() and now - self.last_gemini_time > self.timeout:
                full = self.gemini_buffer.strip()
                if self.should_print(full, self.last_gemini_time):
                    self.stdout.write(f"🤖 Gemini: {normalize_transcript(full)}\n")
                self.gemini_buffer = ""

    async def receive_audio(self):
        while not self._stop.is_set():
            try:
                async for response in self.session.receive(): # Is there any response recieved??
                    sc = getattr(response, "server_content", None) # Extracts the transcript, audio and other AI based responses
                    if not sc:
                        continue

                    # Handle audio output (inline_data)
                    mt = getattr(sc, "model_turn", None)
                    if mt:
                        for part in (getattr(mt, "parts", []) or []):
                            blob = getattr(part, "inline_data", None) or getattr(part, "inlineData", None)
                            if blob and (data := getattr(blob, "data", None)): # Tries to get the Audio Data blob
                                audio = bytes(data) if isinstance(data, (bytes, bytearray)) else base64.b64decode(data)
                                await self.received.put(audio)

                    # Handle user transcription
                    # Checks if sc has an input_transcription attribute with a text field
                    if hasattr(sc, "input_transcription") and (txt := getattr(sc.input_transcription, "text", "")):
                        norm = normalize_transcript(txt.strip())
                        # Normalizes the transcript text and appends it to self.user_buffer.
                        self.user_buffer += " " + norm
                        # Updates the User's last time message
                        self.last_user_time = time.time()

                    # Handle model output transcription
                    if hasattr(sc, "output_transcription") and (txt := getattr(sc.output_transcription, "text", "")):
                        norm = normalize_transcript(txt.strip())
                        self.gemini_buffer += " " + norm
                        self.last_gemini_time = time.time()

                    await asyncio.sleep(0.05)
            except asyncio.CancelledError:
                return
            except Exception as e:
                self.stdout.write(f"📥 Receive error: {e}\n")
                await asyncio.sleep(0.5)

    async def play_audio(self):
        stream = None
        while not self._stop.is_set():
            try:
                if not stream:
                    # Opens an audio output stream (speaker) with PyAudio in a separate thread so as not to block async tasks.
                    stream = await asyncio.to_thread(self.pya.open, format=FORMAT, channels=CHANNELS, rate=RECV_RATE, output=True)
                    # Prints that audio services is available
                    self.stdout.write("🔊 Playback active.\n")
                # Waits for the other audio output to occur
                chunk = await self.received.get()
                if isinstance(chunk, bytes) and chunk: # Checks it is not empty
                    # Plays the audio ins separate thread
                    await asyncio.to_thread(stream.write, chunk)
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.stdout.write(f"🔈 Playback error: {e}\n")
                await asyncio.sleep(0.1)
        if stream:
            stream.close()

    async def run(self):
        try:
            async with client.aio.live.connect(model=MODEL, config=CONFIG) as session:
                self.session = session
                self.stdout.write("💬 Voice chat started — press Ctrl+C to stop.\n")

                # (unchanged) tasks
                self.tasks = [
                    asyncio.create_task(self.listen_audio()),
                    asyncio.create_task(self.send_audio()),
                    asyncio.create_task(self.receive_audio()),
                    asyncio.create_task(self.play_audio()),
                    asyncio.create_task(self.flush_buffers()),
                ]
                await self._stop.wait()
                for t in self.tasks:
                    t.cancel()
                await asyncio.gather(*self.tasks, return_exceptions=True)
        except Exception as e:
            self.stdout.write(f"💥 Run error: {e}\n")
        finally:
            if self.audio_stream:
                self.audio_stream.close()
            self.stdout.write("👋 Session ended.\n")

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
            self.stdout.write(self.style.SUCCESS("\n🛑 Chat terminated by user."))
        except Exception:
            traceback.print_exc()
        finally:
            pya.terminate()
            self.stdout.write(self.style.SUCCESS("✅ Audio resources released."))
