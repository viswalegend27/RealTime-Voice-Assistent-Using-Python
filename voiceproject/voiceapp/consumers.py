# voiceapp/consumers.py
import json
from channels.generic.websocket import AsyncWebsocketConsumer

class TranscriptConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        self.group_name = "voice_transcripts"
        await self.channel_layer.group_add(self.group_name, self.channel_name)
        await self.accept()

    async def disconnect(self, code):
        await self.channel_layer.group_discard(self.group_name, self.channel_name)

    async def transcript_message(self, event):
        # event = {"type": "transcript.message", "role": "user|assistant", "text": "..."}
        await self.send(text_data=json.dumps({
            "role": event["role"],
            "text": event["text"],
        }))
