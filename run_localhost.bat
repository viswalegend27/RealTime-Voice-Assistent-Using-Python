@echo off
cd /d "%~dp0"
call "ai-speech\Scripts\activate.bat"
cd voiceproject

REM Run with Daphne (ASGI) to support WebSockets
start "" cmd /k daphne -p 8000 voiceproject.asgi:application

REM Open browser
start http://127.0.0.1:8000/