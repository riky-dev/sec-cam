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

Tuning & Debug Tips

- If video assembly is slow, reduce RECORD_DURATION and/or increase RECORD_FRAME_INTERVAL in .env to capture fewer frames.
- Reduce VIDEO_WIDTH (default 640) to lower encoding CPU and upload size.
- FFMPEG_PRESET and FFMPEG_CRF in .env control encode speed vs quality; use 'ultrafast' and a larger CRF (30-40) for faster encodes.
- To speed up event handling, set VALIDATE_VIDEO=0 in .env to skip post-encode ffmpeg validation. Validation helps catch corrupt mp4s but costs extra time.
- Use DEBUG=1 in .env to enable verbose logging (includes ffmpeg stderr and debug snapshots).

Event notifications

- When motion is detected the bot now immediately sends a short message "Motion detected — preparing video (id=...)". The assembled video/animation or fallback photos are sent with that event id in their captions, and if possible are sent as replies to the initial message so the messages are grouped in Telegram.

If something is not working

- Check that Termux:API is installed and the termux-camera-photo command works manually.
- Ensure BOT_TOKEN and CHAT_ID in .env are correct; use test_telegram.py to verify API access.
- Inspect logs in the tmp directory (TMP_DIR/sec_cam.log) for errors.
