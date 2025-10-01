# consumers.py
import json
from channels.generic.websocket import AsyncWebsocketConsumer
import asyncio
import pyaudio
from .management.commands.voice_assistant import AudioLoop
import uuid

class TranscriptConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        self.group_name = f"voice_{uuid.uuid4().hex}"
        await self.channel_layer.group_add(self.group_name, self.channel_name)
        await self.accept()
        # Start background audio session for this connection
        try:
            self._pya = pyaudio.PyAudio()
            self._audio = AudioLoop(self._pya, stdout=None, browser_mode=True, group_name=self.group_name)
            self._loop_task = asyncio.create_task(self._audio.run())
        except Exception:
            pass

    async def disconnect(self, code):
        await self.channel_layer.group_discard(self.group_name, self.channel_name)
        try:
            if getattr(self, "_audio", None):
                await self._audio.stop()
        except Exception:
            pass
        if getattr(self, "_loop_task", None):
            self._loop_task.cancel()
        try:
            if getattr(self, "_pya", None):
                self._pya.terminate()
        except Exception:
            pass

    # Receive user's audio from browser
    async def receive(self, text_data=None, bytes_data=None):
        if not text_data:
            return
        try:
            data = json.loads(text_data)
        except Exception:
            return
        if data.get("type") == "audio" and "data" in data:
            try:
                import base64
                pcm = base64.b64decode(data["data"])
                mime = data.get("mime") or f"audio/pcm;rate=16000"
                if getattr(self, "_audio", None):
                    await self._audio.push_client_audio(pcm, mime)
            except Exception:
                pass

    # Handle transcript events from AudioLoop
    async def transcript_message(self, event):
        # event = {"type": "transcript.message", "role": "user"|"assistant", "text": "..."}
        await self.send(text_data=json.dumps({
            "role": event["role"],
            "text": event["text"],
        }))

    async def status_message(self, event):
        await self.send(text_data=json.dumps({
            "type": "status",
            "role": event["role"],
            "speaking": event["speaking"],
        }))

    # Send Gemini's audio to browser
    async def audio_message(self, event):
        await self.send(text_data=json.dumps({
            "type": "audio",
            "mime": event.get("mime"),
            "data": event.get("data"),
        }))
