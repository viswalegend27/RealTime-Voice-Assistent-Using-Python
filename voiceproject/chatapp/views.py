from django.shortcuts import render
from django.http import JsonResponse
import asyncio
from voiceapp.utils import AudioLoop
import pyaudio

def index(request):
    return render(request, 'chatapp/index.html')

def start_assistant(request):
    # This is a simplified example. In a real application, you would need to manage
    # the lifecycle of the assistant more carefully (e.g., using a background task queue).
    pya = pyaudio.PyAudio()
    loop = AudioLoop(pya, None)  # Passing None for stdout, as we'll use websockets
    asyncio.run(loop.run())
    return JsonResponse({'status': 'started'})

def stop_assistant(request):
    # This is a simplified example. You would need a way to signal the loop to stop.
    return JsonResponse({'status': 'stopped'})

def voice_assistant_view(request):
    return render(request, 'chatapp/index.html')  # Fixed template
