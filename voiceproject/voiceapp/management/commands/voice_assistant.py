# voiceapp/management/commands/voice_assistant.py
from django.core.management.base import BaseCommand
from voiceapp.utils import AudioLoop  # re-export the real class

class Command(BaseCommand):
    help = "Browser-only mode: this command is a stub to keep compatibility."

    def handle(self, *args, **options):
        self.stdout.write(self.style.WARNING(
            "This project is configured for browser-only voice I/O. "
            "Run the WebSocket client in the browser instead of a CLI command."
        ))