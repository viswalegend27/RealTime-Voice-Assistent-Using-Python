# voiceapp/consumers.py
import json
from channels.generic.websocket import AsyncWebsocketConsumer
import asyncio
import pyaudio
from .management.commands.voice_assistant import AudioLoop

class TranscriptConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        self.group_name = "voice_transcripts"
        await self.channel_layer.group_add(self.group_name, self.channel_name)
        await self.accept()
        # Start background audio session if not already running for this connection
        self._loop_task = None
        try:
            self._pya = pyaudio.PyAudio()
            self._audio = AudioLoop(self._pya, stdout=None)
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

    async def transcript_message(self, event):
        # event = {"type": "transcript.message", "role": "user|assistant", "text": "..."}
        await self.send(text_data=json.dumps({
            "role": event["role"],
            "text": event["text"],
        }))

    async def status_message(self, event):
        # event = {"type": "status.message", "role": "user|assistant", "speaking": bool}
        await self.send(text_data=json.dumps({
            "type": "status",
            "role": event["role"],
            "speaking": event["speaking"],
        }))
