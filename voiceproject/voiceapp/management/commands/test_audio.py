import time
import random
import pyttsx3
import speech_recognition as sr

# Django-specific imports
from django.core.management.base import BaseCommand
from django.conf import settings

# Google GenAI imports
from google import genai
from google.genai import errors as genai_errors

# Local app imports
from prompts import INSTRUCTIONS
import tools

class Command(BaseCommand):
    help = 'Starts the voice assistant to interact with Gemini.'

    def handle(self, *args, **options):
        """The main entry point for the Django management command."""
        self._setup()
        self.run_assistant()

    def _setup(self):
        """Initializes API clients, TTS engine, and other resources."""
        self.stdout.write(self.style.SUCCESS("🚀 Initializing assistant..."))

        # Configure the Gemini client from Django settings
        try:
            self.client = genai.Client(api_key=settings.GEMINI_API_KEY)
            self.client = genai.Client()
            self.model_name = "gemini-2.0-flash-exp" 
        except Exception as e:
            self.stdout.write(self.style.ERROR(f"Failed to configure Gemini client: {e}"))
            raise

        # Initialize Text-to-Speech (TTS) engine
        try:
            import win32com.client
            self.engine = win32com.client.Dispatch("SAPI.SpVoice")
            self._use_sapi = True
        except ImportError:
            self.engine = pyttsx3.init()
            self._use_sapi = False
        
        self.recognizer = sr.Recognizer()

    def speak(self, text):
        """Converts text into speech."""
        self.stdout.write(self.style.SUCCESS(f"🤖 Assistant: {text}"))
        if self._use_sapi:
            self.engine.Speak(text)
            time.sleep(0.03)
        else:
            self.engine.say(text)
            self.engine.runAndWait()

    def listen(self):
        """Listens for a command from the user."""
        with sr.Microphone() as source:
            self.stdout.write(self.style.HTTP_INFO("\nListening..."))
            self.recognizer.adjust_for_ambient_noise(source, duration=0.5)
            audio = self.recognizer.listen(source)
        time.sleep(0.05) # Release mic before TTS

        try:
            command = self.recognizer.recognize_google(audio)
            self.stdout.write(f"👤 You said: {command}")
            return command.lower()
        except sr.UnknownValueError:
            self.speak("Sorry, I didn't catch that.")
            return ""
        except sr.RequestError:
            self.speak("My speech service is currently unavailable.")
            return ""

    def gen_backoff(self, contents, max_retries=5, base=1, max_delay=30):
        """Calls the Gemini API with exponential backoff for rate limit errors."""
        for i in range(max_retries):
            try:
                return self.client.models.generate_content(model=self.model_name, contents=contents)
            except genai_errors.ClientError as e:
                if getattr(e, "status_code", None) == 429 or "RESOURCE_EXHAUSTED" in str(e).upper():
                    t = min(max_delay, base * (2 ** i)) + random.random()
                    self.stdout.write(self.style.WARNING(f"Rate limited. Retrying in {t:.1f}s..."))
                    time.sleep(t)
                    continue
                raise
        raise RuntimeError("Exhausted retries due to rate limiting.")

    def generate_response(self, action_description, user_command):
        """Generates a dynamic, natural-sounding response after performing an action."""
        prompt = f'{INSTRUCTIONS}\n\nContext: The user said "{user_command}".\nAs the AI assistant, you have just performed the following action: {action_description}.\n\nNow, generate a brief, natural response to inform the user that you\'ve completed their request.'
        try:
            response = self.gen_backoff(prompt)
            return getattr(response, "text", "Got it, Boss.")
        except Exception as e:
            self.stdout.write(self.style.ERROR(f"Error generating dynamic response: {e}"))
            return "Done, Boss."

    def _clean_for_speech(self, text):
        """Removes markdown characters that shouldn't be spoken."""
        return ' '.join(text.replace('*', '').replace('#', '').split())

    def handle_query(self, command):
        """Handles general queries by sending them to Gemini."""
        self.speak("Let me check on that for you, Boss.")
        try:
            response = self.gen_backoff(command)
            answer = self._clean_for_speech(getattr(response, "text", "I'm not sure how to respond to that."))
            self.speak(answer)
        except Exception as e:
            self.stdout.write(self.style.ERROR(f"Gemini Error: {e}"))
            self.speak("I couldn't get a response from Gemini right now.")
    
    def run_assistant(self):
        """The main loop of the voice assistant."""
        commands = {
            "time": tools.handle_time,
            "open youtube": tools.handle_open_youtube,
            "open google": tools.handle_open_google,
            "stop": tools.handle_exit,
            "exit": tools.handle_exit
        }

        self.speak("Hello Boss, I'm online and ready to assist.")
        running = True
        while running:
            command = self.listen()
            if not command:
                continue

            triggered_command = None
            for key in commands.keys():
                if key in command:
                    triggered_command = key
                    break
            
            if triggered_command:
                handler_function = commands[triggered_command]
                # Pass the class methods as callbacks
                if handler_function(command, self.speak, self.generate_response) is False:
                    running = False
            else:
                self.handle_query(command)