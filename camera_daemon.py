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

# Debug and logging
DEBUG = os.getenv('DEBUG', '0').lower() in ('1', 'true', 'yes', 'on')
LOG_FILE = TMP_DIR / 'sec_cam.log'
FALLBACK_LOG = ROOT / 'sec_cam.log'

TG_API = f"https://api.telegram.org/bot{BOT_TOKEN}"

running = True
detecting = True
last_event = 0
# Authorization: use CHAT_ID from env if set, otherwise will auto-authorize first messenger
AUTHORIZED_CHAT = str(CHAT_ID).strip() if CHAT_ID else None

VIDEO_WIDTH = int(os.getenv('VIDEO_WIDTH', '640'))
MAX_TELEGRAM_VIDEO_BYTES = int(os.getenv('MAX_TELEGRAM_VIDEO_BYTES', str(48 * 1024 * 1024)))
FFMPEG_TIMEOUT = int(os.getenv('FFMPEG_TIMEOUT', '30'))


def log(*args, **kwargs):
    line = time.strftime('[%Y-%m-%d %H:%M:%S]') + ' ' + ' '.join(str(a) for a in args)
    # Always print to stdout for quick debugging and flush so user sees it when running
    try:
        print(line, **kwargs)
        sys.stdout.flush()
    except Exception:
        pass
    # Append to primary log file; on failure, write to fallback log in repo root
    try:
        with open(LOG_FILE, 'a') as f:
            f.write(line + '\n')
    except Exception as e:
        try:
            with open(FALLBACK_LOG, 'a') as f2:
                f2.write(line + '\n')
        except Exception:
            # if file logging fails, at least print an error
            try:
                print('LOG WRITE FAILED:', e, file=sys.stderr)
                sys.stderr.flush()
            except Exception:
                pass


def call_termux_camera(path: Path) -> bool:
    """Capture a photo using termux-camera-photo
    Returns True if file exists afterwards
    """
    # Try with -c <id> first, then fallback to without -c if that fails.
    cmd1 = ["termux-camera-photo", "-c", str(CAMERA_ID), str(path)]
    cmd2 = ["termux-camera-photo", str(path)]
    for cmd in (cmd1, cmd2):
        try:
            log('Running camera command:', ' '.join(cmd))
            p = subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=15)
            log('camera stdout:', (p.stdout or '')[:200])
            log('camera stderr:', (p.stderr or '')[:200])
            if path.exists():
                return True
        except subprocess.CalledProcessError as e:
            log('camera call failed (CalledProcessError):', e.returncode, (e.stderr or '')[:300])
        except subprocess.TimeoutExpired as e:
            log('camera call timed out')
        except Exception as e:
            log('camera call exception:', e)
    return path.exists()


def load_small_gray(path: Path, w=DETECTION_WIDTH, h=DETECTION_HEIGHT):
    im = Image.open(path).convert('L')
    im = im.resize((w, h), Image.BILINEAR)
    arr = np.asarray(im, dtype=np.float32)
    return arr


def assemble_video(img_paths, out_path):
    # Use ffmpeg to assemble images into mp4. Images must be named in order.
    tmpdir = TMP_DIR / f"ff_{int(time.time())}"
    tmpdir.mkdir(parents=True, exist_ok=True)
    for i, p in enumerate(img_paths):
        dest = tmpdir / f"img_{i:04d}.jpg"
        shutil.copy(str(p), str(dest))
    try:
        fr = 1.0 / max(0.001, float(RECORD_FRAME_INTERVAL))
    except Exception:
        fr = 1.0
    framerate = max(1, int(round(fr)))
    # Try libx264 first, then fall back to mpeg4 if not available.
    def run_ffmpeg_stream(cmd):
        log('Running ffmpeg:', ' '.join(cmd))
        try:
            p = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
            try:
                stderr, _ = p.communicate(timeout=FFMPEG_TIMEOUT)
            except subprocess.TimeoutExpired:
                log('ffmpeg timed out, killing')
                p.kill()
                try:
                    stderr, _ = p.communicate(timeout=5)
                except Exception:
                    stderr = ''
                return False
            # log last portion of stderr if present
            if stderr:
                for line in stderr.splitlines()[-20:]:
                    log('ffmpeg stderr:', line)
            return p.returncode == 0
        except FileNotFoundError:
            log('ffmpeg not found')
            return False
        except Exception as e:
            log('ffmpeg run exception', e)
            return False

    cmd1 = ['ffmpeg', '-y', '-framerate', str(framerate), '-i', str(tmpdir / 'img_%04d.jpg'), '-c:v', 'libx264', '-pix_fmt', 'yuv420p', str(out_path)]
    ok = run_ffmpeg_stream(cmd1)
    if ok and out_path.exists():
        size = out_path.stat().st_size
        log('ffmpeg produced video (libx264), size=', size)
        if size > MAX_TELEGRAM_VIDEO_BYTES:
            log('video too large for Telegram, size bytes=', size)
            try:
                shutil.rmtree(tmpdir)
            except Exception:
                pass
            return False
        shutil.rmtree(tmpdir)
        return True

    # fallback to mpeg4
    log('ffmpeg libx264 failed or produced no output, trying mpeg4 fallback')
    cmd2 = ['ffmpeg', '-y', '-framerate', str(framerate), '-i', str(tmpdir / 'img_%04d.jpg'), '-vcodec', 'mpeg4', '-qscale:v', '5', str(out_path)]
    ok2 = run_ffmpeg_stream(cmd2)
    if ok2 and out_path.exists():
        size = out_path.stat().st_size
        log('ffmpeg produced video (mpeg4), size=', size)
        if size > MAX_TELEGRAM_VIDEO_BYTES:
            log('video too large for Telegram, size bytes=', size)
            try:
                shutil.rmtree(tmpdir)
            except Exception:
                pass
            return False
        shutil.rmtree(tmpdir)
        return True

    log('ffmpeg fallback failed')
    try:
        shutil.rmtree(tmpdir)
    except Exception:
        pass
    return False


def send_photo(path: Path, caption: str = ''):
    url = f"{TG_API}/sendPhoto"
    try:
        with open(path, 'rb') as f:
            files = {'photo': f}
            data = {'chat_id': CHAT_ID, 'caption': caption}
            r = requests.post(url, data=data, files=files, timeout=60)
        if not r.ok:
            log('send_photo failed', r.status_code, r.text[:500])
        else:
            log('send_photo ok', path)
        return r.ok
    except Exception as e:
        log('send_photo exception', e)
        return False


def send_video(path: Path, caption: str = ''):
    url = f"{TG_API}/sendVideo"
    try:
        with open(path, 'rb') as f:
            files = {'video': f}
            data = {'chat_id': CHAT_ID, 'caption': caption}
            r = requests.post(url, data=data, files=files, timeout=120)
        if not r.ok:
            log('send_video failed', r.status_code, r.text[:500])
        else:
            log('send_video ok', path)
        return r.ok
    except Exception as e:
        log('send_video exception', e)
        return False


def send_message(text: str) -> bool:
    try:
        r = requests.post(f"{TG_API}/sendMessage", data={'chat_id': CHAT_ID, 'text': text}, timeout=10)
        if not r.ok:
            log('send_message failed', r.status_code, r.text[:500])
        return r.ok
    except Exception as e:
        log('send_message exception', e)
        return False


def check_telegram() -> bool:
    """Validate bot token and register commands. Returns True if OK."""
    try:
        r = requests.get(f"{TG_API}/getMe", timeout=10)
        if not r.ok:
            log('getMe failed', r.status_code, r.text[:500])
            return False
        me = r.json().get('result', {})
        log('Bot info:', me.get('username'), me.get('first_name'))
        cmds = [
            {"command": "snap", "description": "Take a snapshot"},
            {"command": "video", "description": "Record a short video"},
            {"command": "start", "description": "Resume motion detection"},
            {"command": "stop", "description": "Pause motion detection"},
            {"command": "status", "description": "Get current status"}
        ]
        r2 = requests.post(f"{TG_API}/setMyCommands", json={"commands": cmds}, timeout=10)
        if not r2.ok:
            log('setMyCommands failed', r2.status_code, r2.text[:500])
        else:
            log('setMyCommands ok')
        return True
    except Exception as e:
        log('check_telegram exception', e)
        return False


def do_record_and_send():
    # Capture a burst of images into TMP_DIR
    log('do_record_and_send: starting')
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
    log('Assembling video to', out_mp4)
    ok = assemble_video(img_paths, out_mp4)
    if not ok:
        log('Failed to assemble video; sending first photo instead')
        # Try to send a small set of photos as fallback (first, middle, last)
        try:
            n = len(img_paths)
            candidates = []
            if n >= 1:
                candidates.append(img_paths[0])
            if n >= 3:
                candidates.append(img_paths[n // 2])
            if n >= 2:
                candidates.append(img_paths[-1])
            sent_any = False
            for idx, p in enumerate(candidates):
                log('Fallback send photo', idx, p)
                try:
                    ok2 = send_photo(p, caption=f'motion (photo {idx+1}/{len(candidates)})')
                    if ok2:
                        sent_any = True
                except Exception as e:
                    log('fallback send_photo exception', e)
            if not sent_any:
                log('Failed to send any fallback photos')
        except Exception as e:
            log('fallback photo send exception', e)
    else:
        ok2 = send_video(out_mp4, caption='motion (video)')
        if not ok2:
            log('Failed to send video; attempting to send a photo instead')
            if img_paths:
                send_photo(img_paths[0], caption='motion (photo, send failed)')
    # cleanup burst
    try:
        shutil.rmtree(burst_dir)
    except Exception:
        pass
    log('do_record_and_send: finished')
    return True


def telegram_worker():
    """Simple long-polling loop to process commands sent to the bot."""
    log('Telegram worker started')
    # Ensure bot commands are registered so Telegram clients show them
    try:
        cmds = [
            {"command": "snap", "description": "Take a snapshot"},
            {"command": "video", "description": "Record a short video"},
            {"command": "start", "description": "Resume motion detection"},
            {"command": "stop", "description": "Pause motion detection"},
            {"command": "status", "description": "Get current status"}
        ]
        requests.post(f"{TG_API}/setMyCommands", json={"commands": cmds}, timeout=10)
    except Exception as e:
        log('Failed to set bot commands:', e)
    offset = None
    global running, detecting
    # Prime offset so we don't reprocess old messages: get the latest update id
    try:
        r = requests.get(f"{TG_API}/getUpdates", params={'limit': 1}, timeout=5)
        if r.ok:
            data = r.json()
            if data.get('result'):
                offset = data['result'][-1]['update_id'] + 1
    except Exception:
        pass
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
                        ok = send_photo(p, caption='snapshot')
                        if not ok:
                            log('failed to send snapshot')
                        try:
                            p.unlink()
                        except Exception:
                            pass
                elif text == '/video':
                    ok = do_record_and_send()
                    if not ok:
                        requests.post(f"{TG_API}/sendMessage", data={'chat_id': CHAT_ID, 'text': 'failed to produce/send video'})
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
            log('Starting record/send...')
            # If DEBUG is enabled, attempt an immediate snapshot send to validate Telegram upload path
            if DEBUG:
                try:
                    dbg_p = TMP_DIR / f"dbg_snap_{int(time.time())}.jpg"
                    log('DEBUG: taking immediate snapshot to', dbg_p)
                    if call_termux_camera(dbg_p):
                        ok_dbg = send_photo(dbg_p, caption='debug snapshot on motion')
                        log('DEBUG: send_photo returned', ok_dbg)
                        try:
                            dbg_p.unlink()
                        except Exception:
                            pass
                    else:
                        log('DEBUG: immediate snapshot failed')
                except Exception as e:
                    log('DEBUG: exception while doing immediate snapshot/send', e)
            try:
                ok = do_record_and_send()
                log('do_record_and_send returned', ok)
            except Exception as e:
                log('do_record_and_send exception', e)

        tmp_snap.unlink(missing_ok=True)
        # sleep until next interval
        dt = time.time() - t0
        to_sleep = max(0.01, DETECTION_INTERVAL - dt)
        time.sleep(to_sleep)


def main():
    if not BOT_TOKEN or not CHAT_ID:
        print('BOT_TOKEN and CHAT_ID must be set in .env')
        sys.exit(1)

    # validate telegram and register commands
    ok = check_telegram()
    if not ok:
        log('Telegram check failed - bot may not work')
    else:
        send_message('Sec-Cam bot started')

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
