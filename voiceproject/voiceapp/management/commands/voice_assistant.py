import asyncio
import logging
import pyaudio
import base64
import time
import re
from django.conf import settings
from django.core.management.base import BaseCommand
from asgiref.sync import sync_to_async
from voiceapp.models import Conversation, Message
from google import genai
from .prompts import AGENT_PROMPT

# Silence the verbose logs
for logger in ["google.genai", "genai"]:
    logging.getLogger(logger).setLevel(logging.WARNING)

# Audio configuration or the standards in the audio is recieved
FORMAT, CHANNELS, CHUNK = pyaudio.paInt16, 1, 1024
SEND_RATE, RECV_RATE = 16000, 24000
MODEL = "models/gemini-2.0-flash-exp"

# Setting up the google client
client = genai.Client(
    api_key=getattr(settings, "GEMINI_API_KEY", None),
    http_options={"api_version": "v1alpha"}
)

# Function to normalize the transcript
def normalize_transcript(text):
    if not text:
        return text
    
    # Cleanup the empty spaces
    text = re.sub(r'\s+', ' ', text).strip()
    
    # Join single letters into words
    tokens = text.split()
    result = []
    i = 0
    # This code segment processes a list called tokens and merges consecutive single-letter tokens into full words, while leaving other tokens unchanged.
    while i < len(tokens):
        if len(tokens[i]) == 1 and tokens[i].isalpha():
            letters = [tokens[i]]
            i += 1
            while i < len(tokens) and len(tokens[i]) == 1 and tokens[i].isalpha():
                letters.append(tokens[i])
                i += 1
            result.append(''.join(letters) if len(letters) >= 2 else letters[0])
        else:
            result.append(tokens[i])
            i += 1
    
    # Fix punctuation spacing
    text = ' '.join(result)
    for pattern, replacement in [
        (r'\s+([,?.!;:])', r'\1'),
        (r"\s+'", "'"),
        (r"'\s+", "'"),
        (r'\s+"', '"'),
        (r'"\s+', '"'),
        (r'\s{2,}', ' ')
    ]:
        text = re.sub(pattern, replacement, text)
    
    return text.strip()

class AudioLoop:
    def __init__(self, pya_instance, stdout):
        # Declaring pyAudio instance
        self.pya = pya_instance
        # Creating output variable
        self.stdout = stdout
        # Queue for sending
        self.to_send = asyncio.Queue(maxsize=10)
        # Queue for recieving
        self.received = asyncio.Queue()
        self.audio_stream = None
        self.session = None
        self._stop = asyncio.Event()
        self.user_buffer = ""
        self.gemini_buffer = ""
        self.last_user_time = 0
        self.last_gemini_time = 0
        self.timeout = 1.5
        self.conversation_id = None
        
        # Database injections.
        self.db_ops = {
            'create_conversation': sync_to_async(lambda: str(Conversation.objects.create().id)),
            'save_message': sync_to_async(self._save_message),
            'get_history': sync_to_async(self._get_history),
            'get_latest': sync_to_async(
                lambda: str(Conversation.objects.order_by('-created_at').first().id 
                        if Conversation.objects.exists() 
                        else Conversation.objects.create().id)
            )
        }
    
    # Function helps to save messages into our ID
    def _save_message(self, conversation_id, role, content):
        if content and content.strip():
            try:
                Message.objects.create(
                    conversation=Conversation.objects.get(id=conversation_id),
                    role='user' if role == 'user' else 'assistant',
                    content=content.strip()
                )
            except Exception as e:
                self.stdout.write(f"⚠️ DB Save Error: {e}\n")
    
    def _get_history(self, conversation_id):
        try:
            messages = Message.objects.filter(
                conversation_id=conversation_id
            ).order_by('-timestamp')[:6] # Gets the last sex messages
            
            history = [f"{'User' if m.role == 'user' else 'Assistant'}: {m.content}" 
                    for m in reversed(messages)]
            
            return f"Previous conversation:\n{chr(10).join(history)}" if history else "No prior conversation."
        except Exception:
            return "No prior conversation."
    
    async def listen_audio(self):
        mic_info = self.pya.get_default_input_device_info()
        self.audio_stream = await asyncio.to_thread(
            self.pya.open,
            format=FORMAT, channels=CHANNELS, rate=SEND_RATE,
            input=True, input_device_index=mic_info.get("index"),
            frames_per_buffer=CHUNK
        )
        
        self.stdout.write("🎙️ Microphone ready. Listening...\n")
        
        while not self._stop.is_set():
            try:
                data = await asyncio.to_thread(
                    self.audio_stream.read, CHUNK, exception_on_overflow=False
                )
                if data:
                    await self.to_send.put({
                        "data": data, 
                        # Multipurpose internet media extensions or A media type format
                        "mime_type": f"audio/pcm;rate={SEND_RATE}"
                    })
            except Exception as e:
                self.stdout.write(f"⚠️ Mic error: {e}\n")
                await asyncio.sleep(0.1)
    
    async def process_gemini(self):
        """Handle all Gemini interactions"""
        # Send audio
        async def send():
            while not self._stop.is_set():
                try:
                    # Asynchornous operation to capture our information and send to gemini
                    await self.session.send(input=await self.to_send.get()) # data to be sent
                except Exception as e:
                    if not isinstance(e, asyncio.CancelledError):
                        self.stdout.write(f"📤 Send error: {e}\n")
                    await asyncio.sleep(0.1)
        
        # Receive and process responses
        async def receive():
            while not self._stop.is_set():
                try:
                    async for response in self.session.receive():
                        sc = getattr(response, "server_content", None)
                        if not sc:
                            continue
                        
                        # Process audio
                        if mt := getattr(sc, "model_turn", None):
                            for part in getattr(mt, "parts", []):
                                if blob := (getattr(part, "inline_data", None) or 
                                        getattr(part, "inlineData", None)):
                                    if data := getattr(blob, "data", None):
                                        audio = data if isinstance(data, bytes) else base64.b64decode(data)
                                        await self.received.put(audio)
                        
                        # Process transcriptions
                        # Setting the transcriptions
                        for attr, buffer_attr, time_attr in [
                            ("input_transcription", "user_buffer", "last_user_time"),
                            ("output_transcription", "gemini_buffer", "last_gemini_time")
                        ]:
                            if hasattr(sc, attr) and (txt := getattr(getattr(sc, attr), "text", "")):
                                setattr(self, buffer_attr, 
                                    getattr(self, buffer_attr) + " " + normalize_transcript(txt.strip()))
                                setattr(self, time_attr, time.time())
                        
                    await asyncio.sleep(0.05)
                except Exception as e:
                    if not isinstance(e, asyncio.CancelledError):
                        self.stdout.write(f"📥 Receive error: {e}\n")
                    await asyncio.sleep(0.5)
        
        await asyncio.gather(send(), receive(), return_exceptions=True)
    
    async def play_audio(self):
        """Play received audio"""
        stream = None
        try:
            while not self._stop.is_set():
                if not stream:
                    stream = await asyncio.to_thread(
                        self.pya.open,
                        format=FORMAT, channels=CHANNELS, 
                        rate=RECV_RATE, output=True
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
    
    async def flush_transcripts(self):
        """Print and save transcripts when ready"""
        while not self._stop.is_set():
            await asyncio.sleep(self.timeout)
            now = time.time()
            
            for buffer_attr, time_attr, role, emoji, label in [
                ("user_buffer", "last_user_time", "user", "🧑", "You"),
                ("gemini_buffer", "last_gemini_time", "assistant", "🤖", "Gemini")
            ]:
                buffer = getattr(self, buffer_attr).strip()
                last_time = getattr(self, time_attr)
                
                if buffer and now - last_time > self.timeout:
                    if len(buffer) > 3 and (now - last_time > self.timeout or 
                                        buffer.endswith((".", "?", "!"))):
                        self.stdout.write(f"{emoji} {label}: {normalize_transcript(buffer)}\n")
                        await self.db_ops['save_message'](self.conversation_id, role, buffer)
                        setattr(self, buffer_attr, "")
    
    async def run(self):
        """Main execution loop"""
        try:
            # Setup conversation
            self.conversation_id = await self.db_ops['get_latest']()
            self.stdout.write(f"📝 Session ID: {self.conversation_id}\n")
            
            # Get history and create config
            history = await self.db_ops['get_history'](self.conversation_id)
            config = {
                "generation_config": {"response_modalities": ["AUDIO"]},
                "speech_config": {"voice_config": {"prebuilt_voice_config": {"voice_name": "Puck"}}},
                "input_audio_transcription": {},
                "output_audio_transcription": {},
                "system_instruction": {
                    "parts": [{"text": f"{AGENT_PROMPT.strip()}\n\n{history}\n\nNow continue the conversation naturally."}]
                }
            }
            
            # Start session
            async with client.aio.live.connect(model=MODEL, config=config) as session:
                self.session = session
                self.stdout.write("💬 Voice chat started — press Ctrl+C to stop.\n")
                
                # Run all tasks
                tasks = await asyncio.gather(
                    asyncio.create_task(self.listen_audio()),
                    asyncio.create_task(self.process_gemini()),
                    asyncio.create_task(self.play_audio()),
                    asyncio.create_task(self.flush_transcripts()),
                    return_exceptions=True
                )
                
                await self._stop.wait()
                
                for task in tasks:
                    if hasattr(task, 'cancel'):
                        task.cancel()
                
        except Exception as e:
            self.stdout.write(f"💥 Run error: {e}\n")
        finally:
            if self.audio_stream:
                self.audio_stream.close()
            self.stdout.write("👋 Session ended.\n")
    
    async def stop(self):
        """Stop the audio loop"""
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