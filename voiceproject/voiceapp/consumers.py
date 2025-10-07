# consumers.py
import json
import uuid
import asyncio
import base64
from channels.generic.websocket import AsyncWebsocketConsumer
from .utils import AudioLoop

PCM_SEND_RATE = 16000   # browser -> server (mic)
PCM_RECV_RATE = 24000   # server -> browser (TTS)

class TranscriptConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        self.group_name = f"voice_{uuid.uuid4().hex}"
        await self.channel_layer.group_add(self.group_name, self.channel_name)
        await self.accept()

        # Start background audio session for this connection
        self._loop_task = None
        self._audio = None
        try:
            # PyAudio not needed in browser_mode=True
            self._audio = AudioLoop(
                pya_instance=None,
                stdout=None,
                browser_mode=True,
                group_name=self.group_name
            )
            self._loop_task = asyncio.create_task(self._audio.run())
        except Exception:
            # Close gracefully if loop can't start
            try:
                await self.close(code=1011)
            except Exception:
                pass

    async def disconnect(self, code):
        await self.channel_layer.group_discard(self.group_name, self.channel_name)
        # Stop AudioLoop first so it stops emitting to the group
        try:
            if getattr(self, "_audio", None):
                await self._audio.stop()
        except Exception:
            pass

        # Cancel and await the task (avoid leaks)
        if getattr(self, "_loop_task", None):
            self._loop_task.cancel()
            try:
                await asyncio.wait_for(self._loop_task, timeout=1.0)
            except Exception:
                pass

    # Receive user's audio from browser
    async def receive(self, text_data=None, bytes_data=None):
        if not getattr(self, "_audio", None):
            return

        # Fast path: raw binary PCM16 mono @ 16kHz
        if bytes_data:
            await self._audio.push_client_audio(bytes_data, f"audio/pcm;rate={PCM_SEND_RATE}")
            return

        if not text_data:
            return

        try:
            data = json.loads(text_data)
        except Exception:
            return

        # ignore keepalive pings
        if data.get("type") == "ping":
            await self._send_json({"type": "pong"})
            return

        if data.get("type") == "audio" and "data" in data:
            try:
                b64 = data["data"]
                if not isinstance(b64, str) or not b64:
                    return
                # strict base64; rejects invalid chars
                pcm = base64.b64decode(b64, validate=True)
                mime = data.get("mime") or f"audio/pcm;rate={PCM_SEND_RATE}"
                # basic guard: only accept pcm
                if not str(mime).startswith("audio/pcm"):
                    return
                await self._audio.push_client_audio(pcm, mime)
            except Exception:
                pass

    # Handle transcript events from AudioLoop
    async def transcript_message(self, event):
        # event = {"type": "transcript.message", "role": "user"|"assistant", "text": "..."}
        await self._send_json({
            "role": event.get("role"),
            "text": event.get("text"),
        })

    # function determined to display the state where the user speaks
    async def status_message(self, event):
        await self._send_json({
            "type": "status",
            "role": event.get("role"),
            "speaking": bool(event.get("speaking")),
        })

    # Send Gemini's audio to browser
    async def audio_message(self, event):
        await self._send_json({
            "type": "audio",
            "mime": event.get("mime") or f"audio/pcm;rate={PCM_RECV_RATE}",
            "data": event.get("data"),
            "rate": PCM_RECV_RATE,
        })

    # Small helper to keep sends consistent/compact
    async def _send_json(self, payload: dict):
        try:
            await self.send(text_data=json.dumps(payload, separators=(",", ":")))
        except Exception:
            pass