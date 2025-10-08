@echo off
REM Change to the project folder
cd /d "%~dp0"

REM Activate the virtual environment
call "ai-speech\Scripts\activate.bat"

REM Move into project directory
cd voiceproject

REM Start the Django server
start "" cmd /k python manage.py runserver

REM Open the default web browser to localhost
start http://127.0.0.1:8000/
