@echo off
cd /d "%~dp0"
set /p VIDEO_URL=Paste YouTube URL: 
python -m clipmaker "%VIDEO_URL%" %*
pause
