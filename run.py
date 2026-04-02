#!/usr/bin/env python3
"""
Human Detect LED — Production Runner
Detection + LEDs with minimal CPU overhead. No camera stream by default.
Press the GPIO button (default pin 17) or POST /api/remote to enable remote UI.
"""
import json, os, cv2, threading, time, numpy as np, socket
import queue as _queue
from collections import deque
from flask import Flask, render_template, jsonify, request, Response

BASE_DIR    = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, 'config.json')

DEFAULT = {
    'brightness': 200, 'smoothing': 0.3, 'detection_threshold': 30,
    'led_count': 50, 'active_leds_count': 15, 'led_spread_mode': 'fixed',
    'active_color': '#ff6b6b', 'idle_color': '#1a1a2e',
    'confidence_threshold': 0.3, 'gpio_pin': 18, 'remote_btn_pin': 17,
    'strips': [{'gpio_pin': 18, 'led_count': 50, 'enabled': True,
                'label': 'Strip 1', 'color': '#092a13'}]
}

# ── config ────────────────────────────────────────────────────────────────────
_cfg_cache, _cfg_mtime = DEFAULT.copy(), 0

def load_cfg():
    global _cfg_cache, _cfg_mtime
    try:
        mt = os.path.getmtime(CONFIG_FILE)
        if mt != _cfg_mtime:
            _cfg_mtime = mt
            with open(CONFIG_FILE) as f:
                c = json.load(f)
            for k, v in DEFAULT.items():
                c.setdefault(k, v)
            _cfg_cache = c
    except Exception:
        pass
    return _cfg_cache

def save_cfg(c):
    global _cfg_cache, _cfg_mtime
    with open(CONFIG_FILE, 'w') as f:
        json.dump(c, f, indent=2)
    _cfg_cache = c.copy()
    try:
        _cfg_mtime = os.path.getmtime(CONFIG_FILE)
    except Exception:
        pass

def hex_to_rgb(h):
    h = h.lstrip('#')
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)

# ── LED hardware ──────────────────────────────────────────────────────────────
try:
    from rpi_ws281x import PixelStrip, Color
    LED_AVAILABLE = True
except ImportError:
    LED_AVAILABLE = False
    print("rpi_ws281x not available — LEDs disabled")

_led_strip = None

def init_led(gpio_pin=18, led_count=50, brightness=200):
    global _led_strip
    if not LED_AVAILABLE:
        return
    try:
        _led_strip = PixelStrip(led_count, gpio_pin, 800000, 10, False, brightness, 0)
        _led_strip.begin()
        for i in range(_led_strip.numPixels()):
            _led_strip.setPixelColor(i, Color(0, 0, 0))
        _led_strip.show()
        print(f"LED: {led_count} LEDs on GPIO {gpio_pin}")
    except Exception as e:
        print(f"LED init error: {e}")
        _led_strip = None

_led_q          = _queue.Queue(maxsize=1)
_led_brightness = 200
_led_intensities: list[float] = []
FADE_IN  = 0.25
FADE_OUT = 0.06
TICK_LED = 0.025

def led_worker():
    global _led_brightness, _led_intensities
    last = None
    while True:
        try:
            last = _led_q.get_nowait()
        except _queue.Empty:
            pass
        if not _led_strip or last is None:
            time.sleep(TICK_LED)
            continue
        active, n, col_h, idle_h, bri = last
        if len(_led_intensities) != n:
            _led_intensities = [0.0] * n
        try:
            if bri != _led_brightness:
                _led_strip.setBrightness(bri)
                _led_brightness = bri
            ar, ag, ab = hex_to_rgb(col_h)
            ir, ig, ib = hex_to_rgb(idle_h)
            changed = False
            for i in range(n):
                tgt = 1.0 if i in active else 0.0
                cur = _led_intensities[i]
                nxt = (min(1.0, cur + FADE_IN) if tgt > cur else
                       max(0.0, cur - FADE_OUT) if tgt < cur else cur)
                if nxt != cur:
                    _led_intensities[i] = nxt
                    changed = True
                    _led_strip.setPixelColor(i, Color(
                        int(ir + (ar - ir) * nxt),
                        int(ig + (ag - ig) * nxt),
                        int(ib + (ab - ib) * nxt),
                    ))
            if changed:
                _led_strip.show()
        except Exception as e:
            print(f"LED error: {e}")
        time.sleep(TICK_LED)

def set_leds(active, n, col, idle, bri):
    try:
        _led_q.get_nowait()
    except _queue.Empty:
        pass
    _led_q.put_nowait((active, n, col, idle, bri))

def flash_signal(color_hex, n, bri, count=2):
    """Quick non-blocking LED flash to signal mode change."""
    def _do():
        idle = '#000000'
        for _ in range(count):
            set_leds(set(range(n)), n, color_hex, idle, bri)
            time.sleep(0.12)
            set_leds(set(), n, color_hex, idle, bri)
            time.sleep(0.12)
    threading.Thread(target=_do, daemon=True).start()

# ── remote mode ───────────────────────────────────────────────────────────────
_remote      = False
_remote_lock = threading.Lock()

def get_remote():
    with _remote_lock:
        return _remote

def set_remote(enabled: bool):
    global _remote
    with _remote_lock:
        if _remote == enabled:
            return
        _remote = enabled
    cfg = load_cfg()
    n   = int(cfg.get('led_count', 50))
    bri = int(cfg.get('brightness', 200))
    if enabled:
        ip = _get_ip()
        print(f"\n  Remote UI ON  →  http://{ip}:5000\n")
        flash_signal('#00ff88', n, bri, count=3)   # green flash = remote ON
    else:
        print("  Remote UI OFF")
        flash_signal('#ff4400', n, bri, count=2)   # red flash = remote OFF

def _get_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(('8.8.8.8', 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return 'localhost'

# ── GPIO button ───────────────────────────────────────────────────────────────
def init_button(pin: int):
    """Try gpiozero first, fall back to RPi.GPIO, skip gracefully if neither."""
    try:
        from gpiozero import Button
        btn = Button(pin, pull_up=True, bounce_time=0.3)
        btn.when_pressed = lambda: set_remote(not get_remote())
        print(f"Remote button: GPIO {pin}")
        return
    except Exception:
        pass
    try:
        import RPi.GPIO as GPIO
        GPIO.setmode(GPIO.BCM)
        GPIO.setup(pin, GPIO.IN, pull_up_down=GPIO.PUD_UP)
        _t = [0.0]
        def _cb(_ch):
            now = time.time()
            if now - _t[0] > 0.3:
                _t[0] = now
                set_remote(not get_remote())
        GPIO.add_event_detect(pin, GPIO.FALLING, callback=_cb, bouncetime=300)
        print(f"Remote button: GPIO {pin}")
        return
    except Exception:
        pass
    print(f"GPIO button not available (no gpiozero or RPi.GPIO) — use API to toggle remote")

# ── detection engine ──────────────────────────────────────────────────────────
USE_YOLO   = False
YOLO_MODEL = None
try:
    from ultralytics import YOLO
    YOLO_MODEL = YOLO('yolov8n.pt')
    YOLO_MODEL(np.zeros((480, 640, 3), dtype=np.uint8), verbose=False, classes=[0])
    USE_YOLO = True
    print("Detection: YOLOv8n")
except Exception as e:
    print(f"YOLOv8 not available ({e}) — using HOG+MOG2")

hog = cv2.HOGDescriptor()
hog.setSVMDetector(cv2.HOGDescriptor_getDefaultPeopleDetector())
mog = cv2.createBackgroundSubtractorMOG2(history=500, varThreshold=25, detectShadows=False)
_k_open   = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
_k_close  = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (15, 15))
_k_dilate = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (20, 20))

# ── shared detection state ────────────────────────────────────────────────────
frame_lock = threading.Lock()
det_lock   = threading.Lock()
raw_lock   = threading.Lock()
_new_frame = threading.Event()
is_running = True
current_frame = None
latest_raw    = None

detection_state = {
    "detected": False, "bbox": None, "active_leds": [],
    "confidence": 0, "fps": 0, "hold": 0,
    "cx": 0.5, "person_w": 0.0, "use_yolo": USE_YOLO
}

smooth_x1 = smooth_y1 = smooth_x2 = smooth_y2 = 0.0
smooth_cx  = 0.5
_first_det = True
hold_ctr   = 0
HOLD_FRAMES    = 5
last_active    = []
fps_ring       = deque(maxlen=15)
t_fps          = time.time()
frame_ctr      = 0

_hog_lock = threading.Lock()
_hog_confirmed, _hog_bbox, _hog_conf, _hog_ts = False, None, 0.0, 0.0
HOG_EXPIRE = 0.6

_thermal_throttle, _thermal_ts = 0, 0.0

def check_thermal():
    global _thermal_throttle, _thermal_ts
    now = time.time()
    if now - _thermal_ts < 1.0:
        return _thermal_throttle
    _thermal_ts = now
    try:
        with open('/sys/class/thermal/thermal_zone0/temp') as f:
            t = int(f.read()) / 1000.0
        _thermal_throttle = 3 if t >= 85 else 2 if t >= 80 else 1 if t >= 75 else 0
    except Exception:
        _thermal_throttle = 0
    return _thermal_throttle

def _nms(boxes, weights, thr=0.5):
    if not len(boxes):
        return [], []
    b = np.array(boxes); w = np.array(weights).flatten()
    x1, y1 = b[:, 0], b[:, 1]
    x2, y2 = b[:, 0] + b[:, 2], b[:, 1] + b[:, 3]
    areas = b[:, 2] * b[:, 3]; order = w.argsort()[::-1]; keep = []
    while len(order):
        i = order[0]; keep.append(i)
        if len(order) == 1: break
        ix = np.maximum(x1[i], x1[order[1:]]); iy = np.maximum(y1[i], y1[order[1:]])
        ax = np.minimum(x2[i], x2[order[1:]]); ay = np.minimum(y2[i], y2[order[1:]])
        inter = np.maximum(0, ax - ix) * np.maximum(0, ay - iy)
        iou = inter / (areas[i] + areas[order[1:]] - inter + 1e-6)
        order = order[np.where(iou <= thr)[0] + 1]
    return [boxes[k] for k in keep], [w[k] for k in keep]

def _valid_box(x, y, bw, bh, fw, fh):
    asp = bh / max(bw, 1)
    return fh * 0.12 <= bh and fw * 0.04 <= bw <= fw * 0.85 and 0.8 <= asp <= 5.5

def _leds_for(cx, n, count):
    c = round(cx * n); half = max(1, count // 2)
    return list(range(max(0, c - half), min(n, c + half + 1)))

# ── detection ─────────────────────────────────────────────────────────────────
def run_detection(frame, cfg):
    global smooth_x1, smooth_y1, smooth_x2, smooth_y2, smooth_cx
    global hold_ctr, last_active, fps_ring, t_fps, frame_ctr, _first_det

    h, w   = frame.shape[:2]
    n      = int(cfg.get('led_count', 50))
    acnt   = int(cfg.get('active_leds_count', 15))
    alpha  = 1.0 - float(cfg.get('smoothing', 0.3))
    cthr   = float(cfg.get('confidence_threshold', 0.3))
    bri    = int(cfg.get('brightness', 200))
    col    = cfg.get('active_color', '#ff6b6b')
    idle   = cfg.get('idle_color', '#1a1a2e')
    mode   = cfg.get('led_spread_mode', 'fixed')

    frame_ctr += 1
    now = time.time()
    if now - t_fps >= 1.0:
        fps_ring.append(frame_ctr / (now - t_fps))
        frame_ctr = 0; t_fps = now
    fps = round(sum(fps_ring) / len(fps_ring), 1) if fps_ring else 0

    detected = False; rx1 = ry1 = rx2 = ry2 = 0; conf = 0.0

    # ── YOLOv8 ──────────────────────────────────────────────────────────────
    if USE_YOLO:
        try:
            res = YOLO_MODEL(frame, verbose=False, classes=[0], imgsz=320, conf=cthr)
            bxs = res[0].boxes
            if bxs and len(bxs):
                cfs  = bxs.conf.cpu().numpy(); best = int(np.argmax(cfs))
                conf = float(cfs[best])
                rx1, ry1, rx2, ry2 = bxs.xyxy[best].cpu().numpy().astype(int)
                detected = True
        except Exception as e:
            print(f"YOLO error: {e}")

    # ── MOG2 + HOG ──────────────────────────────────────────────────────────
    else:
        sc = 0.5; sm = cv2.resize(frame, (int(w * sc), int(h * sc))); sh, sw = sm.shape[:2]
        fg = mog.apply(sm, learningRate=0.003)
        fg = cv2.threshold(fg, 200, 255, cv2.THRESH_BINARY)[1]
        fg = cv2.morphologyEx(fg, cv2.MORPH_OPEN,  _k_open)
        fg = cv2.morphologyEx(fg, cv2.MORPH_CLOSE, _k_close)
        fg = cv2.dilate(fg, _k_dilate, iterations=1)
        mpct = cv2.countNonZero(fg) / (sh * sw) * 100

        with _hog_lock:
            hog_ok  = _hog_confirmed and (now - _hog_ts) < HOG_EXPIRE
            hog_box = _hog_bbox if hog_ok else None
            hog_c   = _hog_conf

        if mpct > 0.5:
            cnts, _ = cv2.findContours(fg, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
            blobs   = sorted(
                [cv2.boundingRect(c) for c in cnts if cv2.contourArea(c) > sh * sw * 0.005],
                key=lambda b: b[2] * b[3], reverse=True
            )
            for bx, by, bw_, bh_ in blobs:
                if _valid_box(bx, by, bw_, bh_, sw, sh):
                    if hog_ok and hog_box:
                        rx1, ry1, rx2, ry2 = hog_box; conf = hog_c
                    else:
                        rx1, ry1 = int(bx / sc), int(by / sc)
                        rx2, ry2 = int((bx + bw_) / sc), int((by + bh_) / sc)
                        conf = 0.4
                    detected = True; break
        if not detected and hog_ok and hog_box:
            rx1, ry1, rx2, ry2 = hog_box; conf = hog_c; detected = True

    # ── bbox smooth + hold ───────────────────────────────────────────────────
    if detected:
        hold_ctr = HOLD_FRAMES
        if _first_det:
            smooth_x1, smooth_y1 = float(rx1), float(ry1)
            smooth_x2, smooth_y2 = float(rx2), float(ry2)
            smooth_cx = ((rx1 + rx2) / 2) / w; _first_det = False
        else:
            smooth_x1 = smooth_x1 * (1 - alpha) + rx1 * alpha
            smooth_y1 = smooth_y1 * (1 - alpha) + ry1 * alpha
            smooth_x2 = smooth_x2 * (1 - alpha) + rx2 * alpha
            smooth_y2 = smooth_y2 * (1 - alpha) + ry2 * alpha
            smooth_cx = (smooth_x1 + smooth_x2) / 2 / w
        if mode == 'proportional':
            acnt = max(1, round((smooth_x2 - smooth_x1) / w * n))
        last_active = _leds_for(smooth_cx, n, acnt)
        set_leds(set(last_active), n, col, idle, bri)

    elif hold_ctr > 0:
        hold_ctr -= 1; detected = hold_ctr > 0
        if hold_ctr == 0:
            _first_det = True
            set_leds(set(), n, col, idle, bri)

    pw = (smooth_x2 - smooth_x1) / w if w > 0 else 0.0
    with det_lock:
        detection_state.update({
            "detected": detected,
            "bbox": [int(smooth_x1), int(smooth_y1), int(smooth_x2), int(smooth_y2)] if detected else None,
            "active_leds": last_active if detected else [],
            "confidence": round(conf, 3), "fps": fps,
            "use_yolo": USE_YOLO, "hold": hold_ctr,
            "cx": round(smooth_cx, 4), "person_w": round(pw, 4),
        })

# ── HOG background thread ─────────────────────────────────────────────────────
def hog_worker():
    global _hog_confirmed, _hog_bbox, _hog_conf, _hog_ts
    while is_running:
        with raw_lock: frame = latest_raw
        if frame is None: time.sleep(0.1); continue
        h, w = frame.shape[:2]; sc = 0.5
        sm = cv2.resize(frame, (int(w * sc), int(h * sc)))
        bxs, wts = hog.detectMultiScale(
            sm, winStride=(8, 8), padding=(8, 8),
            scale=1.1, hitThreshold=0.6, useMeanshiftGrouping=False
        )
        best = None; bc = 0.0
        if len(bxs) > 0 and len(wts) > 0:
            bn, wn = _nms(bxs.tolist(), wts.tolist(), 0.4)
            for i, (bx, by, bw_, bh_) in enumerate(bn):
                rx, ry, rw, rh = int(bx/sc), int(by/sc), int(bw_/sc), int(bh_/sc)
                if _valid_box(rx, ry, rw, rh, w, h):
                    nc = min(1.0, max(0.0, (float(wn[i]) - 0.3) / 1.5))
                    if nc > bc: bc = nc; best = (rx, ry, rx + rw, ry + rh)
        with _hog_lock:
            _hog_confirmed = best is not None; _hog_bbox = best; _hog_conf = bc
            if best: _hog_ts = time.time()
        time.sleep(0.25)

# ── camera thread ─────────────────────────────────────────────────────────────
def cam_worker():
    global latest_raw, is_running
    cam = None
    for idx in ['/dev/video0', '/dev/video1', 0, 4, 2, 1]:
        cam = cv2.VideoCapture(idx, cv2.CAP_V4L2)
        if cam.isOpened(): print(f"Camera: {idx}"); break
    cam.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
    cam.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
    cam.set(cv2.CAP_PROP_BUFFERSIZE, 1)
    cam.set(cv2.CAP_PROP_FPS, 30)
    while is_running:
        if check_thermal() == 3: time.sleep(1); continue
        ret, frame = cam.read()
        if not ret: time.sleep(0.03); continue
        frame = cv2.flip(frame, 1)
        with raw_lock: latest_raw = frame
        _new_frame.set()
    cam.release()

# ── detect thread ─────────────────────────────────────────────────────────────
def detect_worker():
    global current_frame, is_running
    while is_running:
        if not _new_frame.wait(timeout=0.5): continue
        _new_frame.clear()
        with raw_lock: frame = latest_raw
        if frame is None: continue
        try:
            cfg = load_cfg()
            run_detection(frame.copy(), cfg)
            # Only keep an annotated frame in memory when remote UI is on
            if get_remote():
                out = frame.copy()
                with det_lock: st = detection_state.copy()
                if st['detected'] and st['bbox']:
                    x1, y1, x2, y2 = st['bbox']
                    pulse = int(abs(np.sin(time.time() * 3)) * 40)
                    cv2.rectangle(out, (x1, y1), (x2, y2), (0, 200 + pulse, 50 + pulse), 2)
                with frame_lock: current_frame = out
        except Exception as e:
            print(f"Detection error: {e}")

# ── Flask ─────────────────────────────────────────────────────────────────────
app = Flask(__name__, template_folder=os.path.join(BASE_DIR, 'templates'),
            static_folder=os.path.join(BASE_DIR, 'static'))

@app.route('/')
def index():
    return render_template('index.html', config=load_cfg())

@app.route('/api/config', methods=['GET'])
def get_cfg_route():
    return jsonify(load_cfg())

@app.route('/api/config', methods=['POST'])
def upd_cfg_route():
    d = request.get_json(); c = load_cfg()
    for k, v in d.items():
        if k in DEFAULT: c[k] = v
    save_cfg(c)
    return jsonify({'status': 'ok', 'config': c})

@app.route('/api/detection')
def get_det():
    with det_lock: return jsonify(detection_state)

@app.route('/api/status')
def get_status():
    return jsonify({
        'camera_active':  current_frame is not None,
        'use_yolo':       USE_YOLO,
        'stream_enabled': get_remote(),
        'remote':         get_remote(),
    })

@app.route('/api/remote', methods=['POST'])
def toggle_remote_route():
    d = request.get_json() or {}
    val = bool(d['enabled']) if 'enabled' in d else not get_remote()
    set_remote(val)
    return jsonify({'remote': get_remote()})

# compatibility with calibration_app UI stream button
@app.route('/api/toggle-stream', methods=['POST'])
def toggle_stream_route():
    set_remote(not get_remote())
    return jsonify({'stream_enabled': get_remote()})

@app.route('/api/test-led', methods=['POST'])
def test_led_route():
    cfg = load_cfg()
    n   = int(cfg.get('led_count', 50))
    col = cfg.get('active_color', '#ff6b6b')
    idle = cfg.get('idle_color', '#1a1a2e')
    bri  = int(cfg.get('brightness', 200))
    def _flash():
        for _ in range(3):
            set_leds(set(range(n)), n, col, idle, bri); time.sleep(0.2)
            set_leds(set(), n, col, idle, bri);          time.sleep(0.2)
    threading.Thread(target=_flash, daemon=True).start()
    return jsonify({'status': 'ok', 'led_available': LED_AVAILABLE})

@app.route('/api/thermal')
def get_thermal():
    try:
        with open('/sys/class/thermal/thermal_zone0/temp') as f:
            t = int(f.read()) / 1000.0
        return jsonify({'temp_c': round(t, 1), 'throttle': _thermal_throttle})
    except Exception:
        return jsonify({'temp_c': -1, 'throttle': 0})

def _gen_frames():
    while True:
        if not get_remote():
            time.sleep(0.5); continue
        with frame_lock:
            if current_frame is None: time.sleep(0.05); continue
            frame = current_frame.copy()
        _, buf = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, 80])
        yield b'--frame\r\nContent-Type: image/jpeg\r\n\r\n' + buf.tobytes() + b'\r\n'
        time.sleep(0.025)

@app.route('/video_feed')
def video_feed():
    if not get_remote():
        return Response(status=204)
    return Response(_gen_frames(), mimetype='multipart/x-mixed-replace; boundary=frame')

# ── main ──────────────────────────────────────────────────────────────────────
if __name__ == '__main__':
    cfg0   = load_cfg()
    strips = cfg0.get('strips', [])
    s0     = strips[0] if strips else {}
    init_led(
        gpio_pin  = s0.get('gpio_pin', 18),
        led_count = s0.get('led_count', 50),
        brightness = int(cfg0.get('brightness', 200))
    )

    btn_pin = int(cfg0.get('remote_btn_pin', 17))
    init_button(btn_pin)

    threading.Thread(target=led_worker,    daemon=True).start()
    threading.Thread(target=cam_worker,    daemon=True).start()
    threading.Thread(target=detect_worker, daemon=True).start()
    threading.Thread(target=hog_worker,    daemon=True).start()

    print(f"\nRunning headless on GPIO {btn_pin} button or:")
    print(f"  Enable remote:  curl -X POST http://localhost:5000/api/remote")
    print(f"  Disable remote: curl -X POST http://localhost:5000/api/remote\n")

    app.run(host='0.0.0.0', port=5000, debug=False, threaded=True)
