# Sec-Cam (Termux)

Minimal motion-detecting security camera for rooted Android running Termux. Uses Termux:API to capture images, a Python daemon for detection, ffmpeg to assemble short videos, and the Telegram Bot API to send notifications and accept commands.

Quick start

1. On your phone open Termux, clone this repo and cd to it.
2. Copy .env.example to .env and set BOT_TOKEN and CHAT_ID.
3. Run: ./setup.sh
4. Activate the virtualenv and run the daemon:
   . venv/bin/activate
   python camera_daemon.py

Commands the bot accepts (via Telegram chat):

- /snap - send a snapshot now
- /video - record a short video now and send
- /start - resume motion detection
- /stop - pause motion detection
- /status - get current status
