#!/usr/bin/env python3
"""
camera_daemon.py

Minimal Python daemon for Termux-based security camera.

Features:
- Periodically capture low-res frames via termux-camera-photo for motion detection
- When motion is detected, capture a burst of images and assemble into a short video with ffmpeg
- Upload photo/video to Telegram using Bot API
- Respond to simple Telegram commands via long-polling getUpdates

This script is intentionally small and dependency-light so it runs well on Termux.
"""

import os
import sys
import time
import subprocess
import threading
import json
import shutil
from pathlib import Path

try:
    from PIL import Image
    import numpy as np
    import requests
    from dotenv import load_dotenv
except Exception as e:
    print("Missing dependencies. Run setup.sh to install requirements.")
    raise

# Load config
ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / '.env')

BOT_TOKEN = os.getenv('BOT_TOKEN')
CHAT_ID = os.getenv('CHAT_ID')
CAMERA_ID = os.getenv('CAMERA_ID', '0')

DETECTION_INTERVAL = float(os.getenv('DETECTION_INTERVAL', '1.0'))
DETECTION_WIDTH = int(os.getenv('DETECTION_WIDTH', '160'))
DETECTION_HEIGHT = int(os.getenv('DETECTION_HEIGHT', '120'))
DETECTION_ALPHA = float(os.getenv('DETECTION_ALPHA', '0.05'))
PIXEL_THRESHOLD = int(os.getenv('PIXEL_THRESHOLD', '25'))
MOTION_RATIO_THRESHOLD = float(os.getenv('MOTION_RATIO_THRESHOLD', '0.02'))
MIN_MOTION_FRAMES = int(os.getenv('MIN_MOTION_FRAMES', '3'))

RECORD_DURATION = float(os.getenv('RECORD_DURATION', '10'))
RECORD_FRAME_INTERVAL = float(os.getenv('RECORD_FRAME_INTERVAL', '0.5'))
COOLDOWN = float(os.getenv('COOLDOWN', '60'))

TMP_DIR = Path(os.getenv('TMP_DIR', str(ROOT / 'tmp')))
TMP_DIR.mkdir(parents=True, exist_ok=True)

TG_API = f"https://api.telegram.org/bot{BOT_TOKEN}"

running = True
detecting = True
last_event = 0


def log(*args, **kwargs):
    print(time.strftime('[%Y-%m-%d %H:%M:%S]'), *args, **kwargs)


def call_termux_camera(path: Path) -> bool:
    """Capture a photo using termux-camera-photo
    Returns True if file exists afterwards
    """
    cmd = ["termux-camera-photo", "-c", str(CAMERA_ID), str(path)]
    try:
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as e:
        log("Camera capture failed:", e)
        return False
    return path.exists()


def load_small_gray(path: Path, w=DETECTION_WIDTH, h=DETECTION_HEIGHT):
    im = Image.open(path).convert('L')
    im = im.resize((w, h), Image.BILINEAR)
    arr = np.asarray(im, dtype=np.float32)
    return arr


def assemble_video(img_paths, out_path):
    # Use ffmpeg to assemble images into mp4. Images must be named in order.
    # Create a temporary directory with symlinked sequential names if needed.
    tmpdir = TMP_DIR / f"ff_{int(time.time())}"
    tmpdir.mkdir(parents=True, exist_ok=True)
    for i, p in enumerate(img_paths):
        dest = tmpdir / f"img_{i:04d}.jpg"
        shutil.copy(str(p), str(dest))
    cmd = [
        'ffmpeg', '-y', '-framerate', str(int(1 / RECORD_FRAME_INTERVAL)) , '-i',
        str(tmpdir / 'img_%04d.jpg'), '-c:v', 'libx264', '-pix_fmt', 'yuv420p', str(out_path)
    ]
    try:
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        shutil.rmtree(tmpdir)
        return out_path.exists()
    except Exception as e:
        log('ffmpeg failed:', e)
        shutil.rmtree(tmpdir)
        return False


def send_photo(path: Path, caption: str = ''):
    url = f"{TG_API}/sendPhoto"
    with open(path, 'rb') as f:
        files = {'photo': f}
        data = {'chat_id': CHAT_ID, 'caption': caption}
        r = requests.post(url, data=data, files=files, timeout=60)
    return r.ok


def send_video(path: Path, caption: str = ''):
    url = f"{TG_API}/sendVideo"
    with open(path, 'rb') as f:
        files = {'video': f}
        data = {'chat_id': CHAT_ID, 'caption': caption}
        r = requests.post(url, data=data, files=files, timeout=120)
    return r.ok


def do_record_and_send():
    # Capture a burst of images into TMP_DIR
    timestamp = int(time.time())
    burst_dir = TMP_DIR / f"burst_{timestamp}"
    burst_dir.mkdir(parents=True, exist_ok=True)
    n_frames = max(1, int(RECORD_DURATION / RECORD_FRAME_INTERVAL))
    img_paths = []
    for i in range(n_frames):
        p = burst_dir / f"shot_{i:04d}.jpg"
        ok = call_termux_camera(p)
        if not ok:
            log('failed to capture frame', i)
        else:
            img_paths.append(p)
        time.sleep(RECORD_FRAME_INTERVAL)
    if not img_paths:
        log('No frames captured for record')
        return False
    out_mp4 = TMP_DIR / f"event_{timestamp}.mp4"
    ok = assemble_video(img_paths, out_mp4)
    if not ok:
        log('Failed to assemble video; sending first photo instead')
        send_photo(img_paths[0], caption='motion (photo)')
    else:
        send_video(out_mp4, caption='motion (video)')
    # cleanup burst
    try:
        shutil.rmtree(burst_dir)
    except Exception:
        pass
    return True


def telegram_worker():
    """Simple long-polling loop to process commands sent to the bot."""
    log('Telegram worker started')
    offset = None
    global running, detecting
    while running:
        try:
            params = {'timeout': 30}
            if offset:
                params['offset'] = offset
            r = requests.get(f"{TG_API}/getUpdates", params=params, timeout=40)
            if not r.ok:
                time.sleep(1)
                continue
            data = r.json()
            for item in data.get('result', []):
                offset = item['update_id'] + 1
                msg = item.get('message') or item.get('edited_message')
                if not msg:
                    continue
                text = msg.get('text', '').strip()
                chat = msg.get('chat', {})
                from_id = chat.get('id')
                # Only accept commands from the configured chat
                if str(from_id) != str(CHAT_ID):
                    log('Ignoring message from unknown chat', from_id)
                    continue
                log('Received command:', text)
                if text == '/snap' or text == '/photo':
                    p = TMP_DIR / f"snap_{int(time.time())}.jpg"
                    if call_termux_camera(p):
                        send_photo(p, caption='snapshot')
                        try:
                            p.unlink()
                        except Exception:
                            pass
                elif text == '/video':
                    do_record_and_send()
                elif text == '/stop':
                    detecting = False
                    requests.post(f"{TG_API}/sendMessage", data={'chat_id': CHAT_ID, 'text': 'detection paused'})
                elif text == '/start':
                    detecting = True
                    requests.post(f"{TG_API}/sendMessage", data={'chat_id': CHAT_ID, 'text': 'detection resumed'})
                elif text == '/status':
                    status = 'running' if detecting else 'paused'
                    requests.post(f"{TG_API}/sendMessage", data={'chat_id': CHAT_ID, 'text': f'status: {status}'})
        except Exception as e:
            log('Telegram worker error', e)
            time.sleep(1)


def detection_loop():
    log('Detection loop starting')
    bg = None
    motion_count = 0
    global last_event
    while running:
        if not detecting:
            time.sleep(1)
            continue
        t0 = time.time()
        tmp_snap = TMP_DIR / f"detect_{int(t0)}.jpg"
        ok = call_termux_camera(tmp_snap)
        if not ok:
            log('failed to capture detection frame')
            time.sleep(DETECTION_INTERVAL)
            continue
        try:
            frame = load_small_gray(tmp_snap)
        except Exception as e:
            log('failed to load frame', e)
            tmp_snap.unlink(missing_ok=True)
            time.sleep(DETECTION_INTERVAL)
            continue

        if bg is None:
            bg = frame.copy()
            tmp_snap.unlink(missing_ok=True)
            time.sleep(DETECTION_INTERVAL)
            continue

        # compute running average background and diff
        diff = np.abs(frame - bg)
        # update background
        bg = (1 - DETECTION_ALPHA) * bg + DETECTION_ALPHA * frame
        # threshold
        changed = diff > PIXEL_THRESHOLD
        ratio = float(np.sum(changed)) / changed.size
        log(f'motion ratio={ratio:.4f}')
        if ratio > MOTION_RATIO_THRESHOLD:
            motion_count += 1
        else:
            motion_count = 0

        if motion_count >= MIN_MOTION_FRAMES and (time.time() - last_event) > COOLDOWN:
            log('Motion detected - recording')
            last_event = time.time()
            # record and send
            do_record_and_send()

        tmp_snap.unlink(missing_ok=True)
        # sleep until next interval
        dt = time.time() - t0
        to_sleep = max(0.01, DETECTION_INTERVAL - dt)
        time.sleep(to_sleep)


def main():
    if not BOT_TOKEN or not CHAT_ID:
        print('BOT_TOKEN and CHAT_ID must be set in .env')
        sys.exit(1)

    # start telegram worker
    tw = threading.Thread(target=telegram_worker, daemon=True)
    tw.start()

    try:
        detection_loop()
    except KeyboardInterrupt:
        log('Shutting down')
    finally:
        global running
        running = False


if __name__ == '__main__':
    main()
