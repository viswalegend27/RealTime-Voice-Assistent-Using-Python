# Gemini Live Voice Assistant

A real-time voice chat application built with Django that uses Google's Gemini Live API for natural voice conversations with AI. Features real-time speech-to-text transcription for both user input and AI responses.

## Features

- 🎤 **Real-time Voice Chat**: Natural conversation with Gemini AI using voice
- 📝 **Live Transcription**: See text of both your speech and Gemini's responses in real-time
- 🔊 **High-quality Audio**: 16kHz input, 24kHz output with smart buffering
- ⚡ **Low Latency**: Optimized for responsive real-time conversation
- 🛡️ **Error Recovery**: Robust error handling with automatic recovery
- 🧹 **Clean Output**: Smart transcription buffering prevents fragmented text

## Prerequisites

- Python 3.8+
- Django 4.0+
- Google Cloud account with Gemini API access
- Microphone and speakers/headphones
- PyAudio compatible system

## Installation

### 1. Clone the Repository
```bash
git clone https://github.com/yourusername/gemini-voice-assistant.git
cd gemini-voice-assistant
```

### 2. Create Virtual Environment
```bash
python -m venv ai-speech
source ai-speech/bin/activate  # On Windows: ai-speech\Scripts\activate
```

### 3. Install Dependencies
```bash
pip install django
pip install pyaudio
pip install google-genai

or 

Simple 
```pip install -r requirements.txt``` 
Inside your virtual environment 
```

### 4. Set up Django Project
```bash
django-admin startproject voiceproject
cd voiceproject
python manage.py startapp voice_assistant
```

### 5. Add the Voice Assistant Code
- Copy `voice_assistant_optimized.py` to `voiceproject/voice_assistant/management/commands/voice_assistant.py`
- Create the necessary directory structure:
```bash
mkdir -p voice_assistant/management/commands
touch voice_assistant/management/__init__.py
touch voice_assistant/management/commands/__init__.py
```

## Configuration

### 1. Get Gemini API Key
- Go to [Google AI Studio](https://aistudio.google.com/)
- Create a new API key
- Copy your API key

### 2. Configure Django Settings
Add to your `voiceproject/settings.py`:

```python
# Add your app to INSTALLED_APPS
INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'voice_assistant',  # Add this line to your settings.py
]

# Add your Gemini API key
GEMINI_API_KEY = "your-api-key-here" 
To your .env file
```

### 3. Environment Variables (Recommended)
For better security, use environment variables:

```bash
# Create .env file
echo "GEMINI_API_KEY=your-api-key-here" > .env
```

Update `settings.py`:
```python
import os
from dotenv import load_dotenv

load_dotenv()
GEMINI_API_KEY = os.getenv('GEMINI_API_KEY')
```

## Usage

### 1. Start the Voice Assistant
```
Inside your venv,
inside folder voiceproject
python manage.py voice_assistant
```

### 2. Expected Output
```
Voice chat started — press Ctrl+C to stop.
Microphone opened. Listening...
Playback stream opened.
```

### 3. Start Talking!
- Speak naturally into your microphone
- Gemini will respond with voice
- Both your speech and Gemini's responses will be transcribed to text

### 4. Example Conversation
```
You: Hello Gemini, how are you today?
Gemini: Hello! I'm doing great, thank you for asking. How can I help you today?

You: What's the weather like?
Gemini: I don't have access to real-time weather data, but I'd be happy to help you find weather information or discuss weather-related topics.
```

### 5. Stop the Assistant
Press `Ctrl+C` to stop the voice chat.

### 6. Common problem 
Since it is completely free the API has it's Limitations. 🤓 

## Configuration Options

### Voice Settings
Modify the `CONFIG` in `voice_assistant.py`:

```python
CONFIG = {
    "generation_config": {"response_modalities": ["AUDIO"]},
    "speech_config": {
        "voice_config": {
            "prebuilt_voice_config": {
                "voice_name": "Puck"  # Options: Puck, Charon, Kore, Fenrir
            }
        }
    },
    "input_audio_transcription": {},
    "output_audio_transcription": {},
}
```

### Audio Settings
Adjust audio parameters if needed:

```python
FORMAT = pyaudio.paInt16
CHANNELS = 1
SEND_RATE = 16000  # Input sample rate
RECV_RATE = 24000  # Output sample rate
CHUNK = 1024       # Buffer size
```

## Troubleshooting

### Common Issues

**1. "No module named 'google.genai'"**
```bash
pip install google-genai
```

**2. "ModuleNotFoundError: No module named 'pyaudio'"**
- Follow the PyAudio installation instructions above
- On some systems, you may need to install system audio libraries first

**3. "Invalid API key"**
- Double-check your API key in settings.py
- Ensure your Google Cloud account has Gemini API access
- Try regenerating the API key

**4. "No audio input/output device"**
- Check your microphone and speaker connections
- Test audio with other applications first
- On Linux, you may need to install ALSA development packages:
```bash
sudo apt-get install libasound2-dev
```

**5. "Permission denied" for microphone**
- Grant microphone permissions to your terminal/IDE
- On macOS: System Preferences → Privacy & Security → Microphone
- On Windows: Settings → Privacy → Microphone

**6. "If you deleted database"**
- Always do the migration
- If not done the database won't be read
```bash
python manage.py migrate
```
```bash
python manage.py shell -c "from django.db import connection; print('voiceapp_conversation' in connection.introspection.table_names())"
```
```bash
python manage.py voice_assistant
```
### Audio Issues

**Feedback/Echo:**
- Use headphones instead of speakers
- Adjust microphone sensitivity
- Move microphone away from speakers

**Poor Audio Quality:**
- Check microphone connection
- Reduce background noise
- Ensure stable internet connection

**Fragmented Transcriptions:**
- The code includes buffering to prevent this
- If it still occurs, increase `timeout` value in the code

## Technical Details

### Architecture
- **Django Management Command**: Easy integration with Django projects
- **Asyncio**: Handles concurrent audio streaming and processing
- **PyAudio**: Cross-platform audio I/O
- **Google Gemini Live API**: Real-time AI conversation
- **Smart Buffering**: Prevents fragmented transcription output

### Audio Flow
1. **Input**: Microphone → PyAudio → Base64 encoding → Gemini API
2. **Processing**: Gemini AI processes speech and generates response
3. **Output**: Gemini API → Base64 decoding → PyAudio → Speakers
4. **Transcription**: Both input and output are transcribed to text in real-time

### Performance
- **Latency**: ~200-500ms typical response time
- **Memory**: Low memory footprint with streaming
- **CPU**: Minimal CPU usage for audio processing


## Contributing

1. Fork the repository
2. Create a feature branch (`git checkout -b feature/amazing-feature`)
3. Commit your changes (`git commit -m 'Add amazing feature'`)
4. Push to the branch (`git push origin feature/amazing-feature`)
5. Open a Pull Request


## Acknowledgments

- Google Gemini team for the Live API
- PyAudio developers for cross-platform audio support
- Django team for the excellent web framework

## Support

If you encounter issues:
1. Check the troubleshooting section above
2. Search existing GitHub issues
3. Create a new issue with detailed error information

---

**Note**: This project requires a Google Cloud account and Gemini API access. API usage may incur charges based on your usage.