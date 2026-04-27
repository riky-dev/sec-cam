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
    from PIL import Image, ImageOps
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
# Use an Event for pause/resume semantics so /stop takes effect immediately.
detection_event = threading.Event()
detection_event.set()  # detection enabled by default
last_event = 0
# Authorization: use CHAT_ID from env if set, otherwise will auto-authorize first messenger
AUTHORIZED_CHAT = str(CHAT_ID).strip() if CHAT_ID else None

VIDEO_WIDTH = int(os.getenv('VIDEO_WIDTH', '640'))
MAX_TELEGRAM_VIDEO_BYTES = int(os.getenv('MAX_TELEGRAM_VIDEO_BYTES', str(48 * 1024 * 1024)))
FFMPEG_TIMEOUT = int(os.getenv('FFMPEG_TIMEOUT', '30'))
FFMPEG_PRESET = os.getenv('FFMPEG_PRESET', 'ultrafast')
FFMPEG_CRF = int(os.getenv('FFMPEG_CRF', '36'))
VALIDATE_VIDEO = os.getenv('VALIDATE_VIDEO', '0').lower() in ('1', 'true', 'yes')
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


def dbg(*args, **kwargs):
    """Debug logging - only prints when DEBUG is true."""
    if not DEBUG:
        return
    log(*args, **kwargs)


def parse_command(text: str) -> str:
    """Extract the base command from Telegram message text.
    e.g. '/stop@MyBot extra' -> 'stop'
    Returns lowercase command without leading '/'. Empty string if not a command.
    """
    if not text:
        return ''
    token = text.split()[0]
    if not token.startswith('/'):
        return ''
    token = token.lstrip('/')
    token = token.split('@')[0]
    return token.lower().strip()


def call_termux_camera(path: Path) -> bool:
    """Capture a photo using termux-camera-photo
    Returns True if file exists afterwards
    """
    # Try with -c <id> first, then fallback to without -c if that fails.
    cmd1 = ["termux-camera-photo", "-c", str(CAMERA_ID), str(path)]
    cmd2 = ["termux-camera-photo", str(path)]
    for cmd in (cmd1, cmd2):
        try:
            dbg('Running camera command:', ' '.join(cmd))
            p = subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=15)
            dbg('camera stdout:', (p.stdout or '')[:200])
            dbg('camera stderr:', (p.stderr or '')[:200])
            if path.exists():
                dbg('camera captured file', path)
                return True
        except subprocess.CalledProcessError as e:
            dbg('camera call failed (CalledProcessError):', e.returncode, (e.stderr or '')[:300])
        except subprocess.TimeoutExpired:
            dbg('camera call timed out')
        except Exception as e:
            dbg('camera call exception:', e)
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
        # Downscale images before encoding to speed up ffmpeg and reduce CPU/IO
        try:
            im = Image.open(p)
            # fix orientation from EXIF if present
            try:
                im = ImageOps.exif_transpose(im)
            except Exception:
                pass
            # scale if wider than VIDEO_WIDTH
            if im.width > VIDEO_WIDTH:
                nh = int(im.height * (VIDEO_WIDTH / im.width))
                im = im.resize((VIDEO_WIDTH, nh), Image.BILINEAR)
            im.save(dest, format='JPEG', quality=60, optimize=True)
            dbg('wrote resized frame', dest)
        except Exception as e:
            dbg('failed to prepare frame', p, e)
            try:
                shutil.copy(str(p), str(dest))
            except Exception:
                pass
    try:
        fr = 1.0 / max(0.001, float(RECORD_FRAME_INTERVAL))
    except Exception:
        fr = 1.0
    framerate = max(1, int(round(fr)))

    # Quick sanity-check: can we open the first image with PIL?
    try:
        im0 = Image.open(img_paths[0])
        log('first image read ok, size=', im0.size, 'mode=', im0.mode)
    except Exception as e:
        log('first image cannot be opened by PIL:', e)
    # Try libx264 first, then fall back to mpeg4 if not available.
    def run_ffmpeg_stream(cmd):
        dbg('Running ffmpeg:', ' '.join(cmd))
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
                    dbg('ffmpeg stderr:', line)
            return p.returncode == 0
        except FileNotFoundError:
            log('ffmpeg not found')
            return False
        except Exception as e:
            log('ffmpeg run exception', e)
            return False


    def validate_video_file(p: Path, timeout=15) -> bool:
        """Use ffmpeg to validate that the produced video file is readable.
        Returns True if ffmpeg can read the file and exit code 0.
        """
        if not p.exists():
            return False
        cmd = ['ffmpeg', '-v', 'error', '-i', str(p), '-f', 'null', '-']
        dbg('validate_video_file running:', ' '.join(cmd))
        try:
            proc = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, timeout=timeout)
            if proc.returncode == 0:
                dbg('validate_video_file ok')
                return True
            else:
                log('validate_video_file failed, stderr:', (proc.stderr or '')[:500])
                return False
        except subprocess.TimeoutExpired:
            log('validate_video_file timed out')
            return False
        except FileNotFoundError:
            log('ffmpeg not found for validation')
            return False
        except Exception as e:
            log('validate_video_file exception', e)
            return False

    # Use libx264 with baseline profile and faststart to improve streaming/playback on Telegram
    # frames were pre-resized to VIDEO_WIDTH; avoid ffmpeg scaling to save CPU/time
    cmd1 = ['ffmpeg', '-y', '-framerate', str(framerate), '-i', str(tmpdir / 'img_%04d.jpg'), '-c:v', 'libx264', '-preset', FFMPEG_PRESET, '-crf', str(FFMPEG_CRF), '-profile:v', 'baseline', '-level', '3.0', '-pix_fmt', 'yuv420p', '-movflags', '+faststart', '-threads', '0', str(out_path)]
    ok = run_ffmpeg_stream(cmd1)
    if ok and out_path.exists():
        size = out_path.stat().st_size
        log('ffmpeg produced video (libx264), size=', size)
        if size <= 1024:
            log('produced file extremely small; treating as failure')
            ok = False
        else:
            if size > MAX_TELEGRAM_VIDEO_BYTES:
                log('video too large for Telegram, size bytes=', size)
                try:
                    shutil.rmtree(tmpdir)
                except Exception:
                    pass
                return None
            # Validate produced file with ffmpeg - if it's invalid, try re-encode from images
            if validate_video_file(out_path, timeout=FFMPEG_TIMEOUT):
                try:
                    shutil.rmtree(tmpdir)
                except Exception:
                    pass
                return out_path
            else:
                log('Produced video failed validation; attempting to re-encode from images')
                re_out = out_path.with_suffix('.re.mp4')
                cmd_re = ['ffmpeg', '-y', '-framerate', str(framerate), '-i', str(tmpdir / 'img_%04d.jpg'), '-c:v', 'libx264', '-preset', FFMPEG_PRESET, '-crf', str(FFMPEG_CRF), '-pix_fmt', 'yuv420p', '-movflags', '+faststart', '-threads', '0', str(re_out)]
                ok_re = run_ffmpeg_stream(cmd_re)
                if ok_re and re_out.exists() and validate_video_file(re_out, timeout=FFMPEG_TIMEOUT):
                    try:
                        shutil.move(str(re_out), str(out_path))
                        shutil.rmtree(tmpdir)
                        log('Re-encoded video succeeded')
                        return out_path
                    except Exception as e:
                        log('Re-encode move failed', e)

    # fallback to mpeg4
    log('ffmpeg libx264 failed or produced no output, trying mpeg4 fallback')
    cmd2 = ['ffmpeg', '-y', '-framerate', str(framerate), '-i', str(tmpdir / 'img_%04d.jpg'), '-vcodec', 'mpeg4', '-qscale:v', '5', '-movflags', '+faststart', '-threads', '0', str(out_path)]
    ok2 = run_ffmpeg_stream(cmd2)
    if ok2 and out_path.exists():
        size = out_path.stat().st_size
        log('ffmpeg produced video (mpeg4), size=', size)
        if size <= 1024:
            log('produced file extremely small; treating as failure')
            ok2 = False
        else:
            if size > MAX_TELEGRAM_VIDEO_BYTES:
                log('video too large for Telegram, size bytes=', size)
                try:
                    shutil.rmtree(tmpdir)
                except Exception:
                    pass
                return None
            # Validate mpeg4 file
            if validate_video_file(out_path, timeout=FFMPEG_TIMEOUT):
                try:
                    shutil.rmtree(tmpdir)
                except Exception:
                    pass
                return out_path
            else:
                log('mpeg4 produced but failed validation')

    log('ffmpeg fallback failed; attempting to produce GIF as fallback')
    # Attempt to create a GIF instead
    gif_path = out_path.with_suffix('.gif')
    try:
        okgif = make_gif(img_paths, gif_path)
        if okgif and gif_path.exists():
            log('GIF fallback produced', gif_path)
            # return the gif path (caller will decide how to send)
            try:
                shutil.rmtree(tmpdir)
            except Exception:
                pass
            return gif_path
    except Exception as e:
        log('GIF fallback exception', e)

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


# send_document removed — videos are sent via send_video (sendDocument workaround removed)



def send_message(text: str) -> bool:
    try:
        r = requests.post(f"{TG_API}/sendMessage", data={'chat_id': CHAT_ID, 'text': text}, timeout=10)
        if not r.ok:
            log('send_message failed', r.status_code, r.text[:500])
        return r.ok
    except Exception as e:
        log('send_message exception', e)
        return False


def send_animation(path: Path, caption: str = '') -> bool:
    url = f"{TG_API}/sendAnimation"
    try:
        with open(path, 'rb') as f:
            files = {'animation': f}
            data = {'chat_id': CHAT_ID, 'caption': caption}
            r = requests.post(url, data=data, files=files, timeout=120)
        if not r.ok:
            log('send_animation failed', r.status_code, r.text[:500])
        else:
            log('send_animation ok', path)
        return r.ok
    except Exception as e:
        log('send_animation exception', e)
        return False


def make_gif(img_paths, out_gif_path: Path, max_width=VIDEO_WIDTH, duration_ms=None):
    """Create a GIF from the list of image paths using Pillow.
    duration_ms: per-frame duration in milliseconds
    Returns True on success.
    """
    try:
        frames = []
        for p in img_paths:
            try:
                im = Image.open(p).convert('RGB')
            except Exception as e:
                log('make_gif: failed to open', p, e)
                continue
            # resize if wider than max_width
            if im.width > max_width:
                nh = int(im.height * (max_width / im.width))
                im = im.resize((max_width, nh), Image.BILINEAR)
            frames.append(im)
        if not frames:
            log('make_gif: no frames to make gif')
            return False
        if duration_ms is None:
            duration_ms = int(RECORD_FRAME_INTERVAL * 1000)
        frames[0].save(out_gif_path, save_all=True, append_images=frames[1:], duration=duration_ms, loop=0, optimize=True)
        log('make_gif: saved', out_gif_path)
        return True
    except Exception as e:
        log('make_gif exception', e)
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
            dbg('setMyCommands ok')
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
    result = assemble_video(img_paths, out_mp4)
    if not result:
        log('Failed to assemble video; sending a set of fallback photos')
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
        # assemble_video returned a Path to the resulting media (mp4 or gif)
        if isinstance(result, Path):
            media_path = result
            suffix = media_path.suffix.lower()
            if suffix in ('.mp4', '.mov'):
                ok2 = send_video(media_path, caption='motion (video)')
                if not ok2:
                    log('Failed to send video; attempting to send photos as fallback')
                    if img_paths:
                        for p in img_paths[:3]:
                            send_photo(p, caption='motion (photo, send failed)')
            elif suffix in ('.gif',):
                ok2 = send_animation(media_path, caption='motion (animation)')
                if not ok2:
                    log('Failed to send animation; attempting to send photos as fallback')
                    if img_paths:
                        for p in img_paths[:3]:
                            send_photo(p, caption='motion (photo, send failed)')
            else:
                # unknown suffix - try send_video then send_photo
                ok2 = send_video(media_path, caption='motion (video)')
                if not ok2 and img_paths:
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
    global running
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
                if AUTHORIZED_CHAT and str(from_id) != AUTHORIZED_CHAT:
                    dbg('Ignoring message from unknown chat', from_id)
                    continue
                dbg('Received raw command text:', text)
                cmd = parse_command(text)
                if not cmd:
                    continue
                log('Received command:', cmd)
                if cmd in ('snap', 'photo'):
                    # take snapshot and send
                    def snap_job():
                        p = TMP_DIR / f"snap_{int(time.time())}.jpg"
                        if call_termux_camera(p):
                            ok = send_photo(p, caption='snapshot')
                            if not ok:
                                log('failed to send snapshot')
                            try:
                                p.unlink()
                            except Exception:
                                pass
                    threading.Thread(target=snap_job, daemon=True).start()
                    send_message('snapshot requested')
                elif cmd == 'video':
                    # start record in background
                    threading.Thread(target=do_record_and_send, daemon=True).start()
                    send_message('video requested')
                elif cmd == 'stop':
                    detection_event.clear()
                    send_message('detection paused')
                elif cmd == 'start':
                    detection_event.set()
                    send_message('detection resumed')
                elif cmd == 'status':
                    status = 'running' if detection_event.is_set() else 'paused'
                    send_message(f'status: {status}')
        except Exception as e:
            log('Telegram worker error', e)
            time.sleep(1)


def detection_loop():
    log('Detection loop starting')
    bg = None
    motion_count = 0
    global last_event
    while running:
        if not detection_event.is_set():
            # paused
            time.sleep(0.5)
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
        dbg(f'motion ratio={ratio:.4f}')
        if ratio > MOTION_RATIO_THRESHOLD:
            motion_count += 1
        else:
            motion_count = 0

        if motion_count >= MIN_MOTION_FRAMES and (time.time() - last_event) > COOLDOWN:
            log('Motion detected - recording')
            last_event = time.time()
            # run recording in a separate thread so detection loop continues
            def record_thread():
                log('record_thread started')
                if DEBUG:
                    try:
                        dbg_p = TMP_DIR / f"dbg_snap_{int(time.time())}.jpg"
                        dbg('DEBUG: taking immediate snapshot to', dbg_p)
                        if call_termux_camera(dbg_p):
                            ok_dbg = send_photo(dbg_p, caption='debug snapshot on motion')
                            dbg('DEBUG: send_photo returned', ok_dbg)
                            try:
                                dbg_p.unlink()
                            except Exception:
                                pass
                        else:
                            dbg('DEBUG: immediate snapshot failed')
                    except Exception as e:
                        dbg('DEBUG: exception while doing immediate snapshot/send', e)
                try:
                    res = do_record_and_send()
                    log('record_thread finished, result=', bool(res))
                except Exception as e:
                    log('record_thread exception', e)

            t = threading.Thread(target=record_thread, daemon=True)
            t.start()

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
