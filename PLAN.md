Sec-Cam (Termux) - Plan 1 (Minimal, Python + Termux:API)

Goal
Turn a rooted Android device running Termux into a small motion-detecting security camera that:

- Responds to remote Telegram commands (snapshot, video, start/stop/status).
- Automatically records a short video clip and sends it to a Telegram chat when motion is detected.

What this repo provides (scaffold)

- setup.sh -- single script you run on the phone to install packages and prepare the repo (Termux).
- camera_daemon.py -- Python daemon implementing: periodic capture, motion detection, burst capture -> ffmpeg -> send to Telegram, and a small Telegram command listener.
- requirements.txt -- Python dependencies.
- .env.example -- example configuration file (BOT_TOKEN, CHAT_ID, tuning values).
- README.md -- quick usage notes.

High level architecture

- Termux:API (termux-camera-photo) to capture images.
- Python process (daemon) reads low-resolution frames for motion detection (frame-diff + running average) and on trigger captures a short burst of images and assembles them into a video with ffmpeg.
- Telegram Bot API (requests) for commands and media delivery.
- Optional: Termux:Boot + termux-wake-lock to keep the daemon running after boot.

Key defaults (tunable in .env)

- Detection interval: 1.0s
- Detection resolution: 160x120
- Background update (running average alpha): 0.05
- Pixel threshold: 25
- Motion ratio threshold: 0.02 (2% of pixels)
- Minimum motion frames: 3 (consecutive frames over threshold before triggering)
- Record duration: 10s (assembled video)
- Record frame interval: 0.5s (2 fps by default)
- Cooldown after event: 60s

Prerequisites (on the phone)

- Termux (up to date)
- Termux:API app installed and permissions granted
- Termux:Boot (optional, for auto-start on boot)
- A Telegram bot token (BotFather) and the chat_id you want notifications sent to

Security

- Keep BOT_TOKEN private (don't commit .env). The setup script creates .env.example only.

Next steps after cloning

1. Edit .env (copy .env.example -> .env) and fill BOT_TOKEN and CHAT_ID.
   . venv/bin/activate
   python camera_daemon.py
2. From your Telegram account send /snap, /video, /status, /stop, /start to the bot.

Notes

- The setup script will optionally create a Termux:Boot starter script so the daemon can start automatically on boot.
- Video recording is done by capturing a burst of images then assembling them with ffmpeg. This is more portable than trying to access a /dev/video device and works reliably without special camera device nodes.

Performance and validation notes

- The daemon can validate assembled mp4 files using ffmpeg to ensure they are readable before sending. This is enabled by the VALIDATE_VIDEO setting (default: 0 for speed). Validation can catch corrupt outputs but adds extra CPU/time.
- Tuning knobs: RECORD_DURATION, RECORD_FRAME_INTERVAL, VIDEO_WIDTH, FFMPEG_PRESET, FFMPEG_CRF, VALIDATE_VIDEO. Lower durations, lower resolution and faster presets reduce CPU and upload time.

User notifications

- On motion detection the daemon sends an immediate Telegram message announcing the event and an event id (e.g. evt_1616161616). The assembled video or fallback photos include that id in their caption and are sent as replies to the initial message when possible. This groups related messages in Telegram and gives you a quick in-chat reference id for each event.
