#!/usr/bin/env python3
"""Human Detect LED — Production Runner"""
import json, os, cv2, threading, time, numpy as np, socket, functools, re, secrets
import queue as _queue, logging, logging.handlers
from collections import deque
from flask import (Flask, render_template, jsonify, request, Response,
                   session, redirect)

APP_VERSION = '2.1.0'
BASE_DIR    = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, 'config.json')
SETUP_FILE  = os.path.join(BASE_DIR, '.setup_complete')
PASSWD_FILE = os.path.join(BASE_DIR, '.password_hash')
_START_TIME = time.time()

# ── logging setup ─────────────────────────────────────────────────────────────
def _setup_logging():
    fmt = logging.Formatter('%(asctime)s %(levelname)s %(message)s',
                            datefmt='%H:%M:%S')
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    root.addHandler(sh)
    log_file = os.path.join(BASE_DIR, 'human_detect_led.log')
    fh = logging.handlers.RotatingFileHandler(
        log_file, maxBytes=1_000_000, backupCount=3)
    fh.setFormatter(fmt)
    root.addHandler(fh)

_setup_logging()
log = logging.getLogger('led')

DEFAULT = {
    'brightness': 200, 'smoothing': 0.3, 'detection_threshold': 30,
    'led_count': 50, 'active_leds_count': 15, 'led_spread_mode': 'fixed',
    'active_color': '#ff6b6b', 'idle_color': '#1a1a2e',
    'confidence_threshold': 0.3, 'gpio_pin': 18, 'remote_btn_pin': 17,
    'flip_h': True, 'flip_v': False,
    'cam1_index': -1,
    'cam2_index': -1, 'cam2_flip_h': False, 'cam2_flip_v': False,
    'cam2_side': 'right', 'cam2_aspect': 'fit',
    'strips': [{'gpio_pin': 18, 'led_count': 50, 'enabled': True,
                'label': 'Strip 1', 'color': '#ff6b6b'}]
}

# ── config validation ─────────────────────────────────────────────────────────
_HEX_RE     = re.compile(r'^#[0-9a-fA-F]{6}$')
_VALID_GPIO = frozenset({2,3,4,5,6,7,8,9,10,11,12,13,14,15,16,17,18,
                          19,20,21,22,23,24,25,26,27})

def _validate_cfg(updates: dict):
    """Validate config updates. Returns (clean_dict, error_list)."""
    clean = {}; errors = []
    numeric_rules = [
        ('brightness',           int,   0,    255),
        ('detection_threshold',  int,   5,    100),
        ('led_count',            int,   1,    300),
        ('active_leds_count',    int,   1,    300),
        ('smoothing',            float, 0.0,  0.98),
        ('confidence_threshold', float, 0.05, 1.0),
    ]
    for k, typ, lo, hi in numeric_rules:
        if k not in updates: continue
        try:
            v = typ(updates[k])
            if lo <= v <= hi: clean[k] = v
            else: errors.append(f'{k} must be between {lo} and {hi}')
        except (ValueError, TypeError):
            errors.append(f'{k}: invalid value')

    for k in ('active_color', 'idle_color'):
        if k not in updates: continue
        v = str(updates[k])
        if _HEX_RE.match(v): clean[k] = v
        else: errors.append(f'{k}: must be #rrggbb hex color')

    for k in ('flip_h', 'flip_v', 'cam2_flip_h', 'cam2_flip_v'):
        if k in updates:
            clean[k] = bool(updates[k])

    if 'cam2_side' in updates:
        v = str(updates['cam2_side'])
        if v in ('left', 'right'): clean['cam2_side'] = v
        else: errors.append("cam2_side must be 'left' or 'right'")

    if 'cam2_aspect' in updates:
        v = str(updates['cam2_aspect'])
        if v in ('fit', 'native'): clean['cam2_aspect'] = v
        else: errors.append("cam2_aspect must be 'fit' or 'native'")

    for k in ('cam1_index', 'cam2_index'):
        if k not in updates: continue
        try:
            v = int(updates[k])
            if -1 <= v <= 9: clean[k] = v
            else: errors.append(f'{k} must be -1 (auto/off) or 0-9')
        except (ValueError, TypeError):
            errors.append(f'{k}: must be integer')

    if 'led_spread_mode' in updates:
        v = str(updates['led_spread_mode'])
        if v in ('fixed', 'proportional'): clean['led_spread_mode'] = v
        else: errors.append("led_spread_mode must be 'fixed' or 'proportional'")

    for k in ('gpio_pin', 'remote_btn_pin'):
        if k not in updates: continue
        try:
            v = int(updates[k])
            if v in _VALID_GPIO: clean[k] = v
            else: errors.append(f'{k}: {v} is not a valid GPIO pin')
        except (ValueError, TypeError):
            errors.append(f'{k}: must be integer')

    if 'strips' in updates:
        raw = updates['strips']
        if not isinstance(raw, list):
            errors.append('strips: must be an array')
        else:
            clean_strips = []; strip_errors = []
            for i, s in enumerate(raw):
                if not isinstance(s, dict):
                    strip_errors.append(f'strips[{i}]: must be object'); continue
                cs = {}
                try:
                    gp = int(s.get('gpio_pin', 18))
                    if gp not in _VALID_GPIO:
                        strip_errors.append(f'strips[{i}].gpio_pin {gp} invalid'); continue
                    cs['gpio_pin'] = gp
                except Exception:
                    strip_errors.append(f'strips[{i}].gpio_pin invalid'); continue
                try:
                    lc = int(s.get('led_count', 50))
                    if not (1 <= lc <= 300):
                        strip_errors.append(f'strips[{i}].led_count must be 1-300'); continue
                    cs['led_count'] = lc
                except Exception:
                    strip_errors.append(f'strips[{i}].led_count invalid'); continue
                cs['enabled'] = bool(s.get('enabled', True))
                cs['label']   = str(s.get('label', f'Strip {i+1}'))[:32]
                col = str(s.get('color', '#ff6b6b'))
                cs['color']   = col if _HEX_RE.match(col) else '#ff6b6b'
                clean_strips.append(cs)
            errors.extend(strip_errors)
            if not strip_errors:
                clean['strips'] = clean_strips

    return clean, errors

# ── config ────────────────────────────────────────────────────────────────────
_cfg_cache, _cfg_mtime = DEFAULT.copy(), 0
_cfg_lock = threading.Lock()

def load_cfg():
    global _cfg_cache, _cfg_mtime
    try:
        mt = os.path.getmtime(CONFIG_FILE)
        if mt != _cfg_mtime:
            with _cfg_lock:
                _cfg_mtime = mt
                with open(CONFIG_FILE) as f:
                    c = json.load(f)
                for k, v in DEFAULT.items():
                    c.setdefault(k, v)
                _cfg_cache = c
    except Exception:
        pass
    return _cfg_cache

def save_cfg(c: dict):
    """Atomic config save protected by lock to prevent concurrent-write race."""
    global _cfg_cache, _cfg_mtime
    with _cfg_lock:
        tmp = CONFIG_FILE + '.tmp'
        with open(tmp, 'w') as f:
            json.dump(c, f, indent=2)
        os.replace(tmp, CONFIG_FILE)
        _cfg_cache = c.copy()
        try: _cfg_mtime = os.path.getmtime(CONFIG_FILE)
        except Exception: pass

def hex_to_rgb(h: str):
    h = h.lstrip('#')
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)

# ── LED hardware ──────────────────────────────────────────────────────────────
try:
    from rpi_ws281x import PixelStrip, Color
    LED_AVAILABLE = True
except ImportError:
    LED_AVAILABLE = False
    log.warning("rpi_ws281x not available — LEDs disabled")

_led_strips:     list = []   # PixelStrip | None, one per enabled strip
_strip_cfgs:     list = []   # strip config dict, one per enabled strip
_strip_ints:     list = []   # [float] intensity array, one per enabled strip
_led_brightness: int  = 200
_strip_lock      = threading.Lock()   # guards all _led_strips / _strip_cfgs / _strip_ints

FADE_IN  = 0.25
FADE_OUT = 0.06
TICK_LED = 0.025

# ── test-LED override (set by /api/test-led) ──────────────────────────────────
_test_led_idx   = None   # int = light only this index; None = normal operation
_test_led_until = 0.0
_test_led_lock  = threading.Lock()

def _init_one_strip(gpio_pin: int, led_count: int, brightness: int):
    if not LED_AVAILABLE:
        return None
    try:
        s = PixelStrip(led_count, gpio_pin, 800000, 10, False, brightness, 0)
        s.begin()
        for i in range(s.numPixels()):
            s.setPixelColor(i, Color(0, 0, 0))
        s.show()
        log.info("LED strip: %d LEDs on GPIO %d", led_count, gpio_pin)
        return s
    except Exception as e:
        log.error("LED init error GPIO %d: %s", gpio_pin, e)
        return None

def init_strips(strips_cfg: list, brightness: int):
    global _led_strips, _strip_cfgs, _strip_ints
    new_strips = []; new_cfgs = []; new_ints = []
    for s in strips_cfg:
        if not s.get('enabled', True):
            continue
        hw = _init_one_strip(int(s['gpio_pin']), int(s['led_count']), brightness)
        new_strips.append(hw)
        new_cfgs.append(s)
        new_ints.append([0.0] * int(s['led_count']))
    if not new_cfgs:
        log.warning("No enabled LED strips configured")
    with _strip_lock:
        _led_strips.clear(); _led_strips.extend(new_strips)
        _strip_cfgs.clear(); _strip_cfgs.extend(new_cfgs)
        _strip_ints.clear(); _strip_ints.extend(new_ints)

_led_q = _queue.Queue(maxsize=1)

def _leds_for(cx: float, n: int, count: int) -> list:
    if count <= 0 or n <= 0:
        return []
    c    = round(cx * n)
    half = max(1, count // 2)
    return list(range(max(0, c - half), min(n, c + half + 1)))

def led_worker():
    global _led_brightness, _strip_ints
    last = None
    while True:
        try: last = _led_q.get_nowait()
        except _queue.Empty: pass
        with _strip_lock:
            has_strips = bool(_strip_cfgs)
        if not has_strips or last is None:
            time.sleep(TICK_LED); continue
        cx, acnt, col_h, idle_h, bri, mode, person_w = last
        try:
            with _strip_lock:
                if bri != _led_brightness:
                    for hw in _led_strips:
                        if hw: hw.setBrightness(bri)
                    _led_brightness = bri
            ir, ig, ib = hex_to_rgb(idle_h)
            with _strip_lock:
                strips_snapshot = list(zip(_led_strips, _strip_cfgs, _strip_ints))
            for hw, scfg, ints in strips_snapshot:
                n = int(scfg['led_count'])
                strip_col = scfg.get('color') or col_h
                sar, sag, sab = hex_to_rgb(strip_col)
                with _test_led_lock:
                    ti    = _test_led_idx
                    t_end = _test_led_until
                if ti is not None and time.time() < t_end:
                    active = {ti} if ti < n else set()
                elif cx is None or acnt <= 0:
                    active = set()
                elif mode == 'proportional':
                    active = set(_leds_for(cx, n, max(1, round(person_w * n))))
                else:
                    active = set(_leds_for(cx, n, acnt))
                changed = False
                for i in range(n):
                    tgt = 1.0 if i in active else 0.0
                    cur = ints[i]
                    nxt = (min(1.0, cur + FADE_IN) if tgt > cur else
                           max(0.0, cur - FADE_OUT) if tgt < cur else cur)
                    if nxt != cur:
                        ints[i] = nxt; changed = True
                        if hw:
                            hw.setPixelColor(i, Color(
                                int(ir + (sar - ir) * nxt),
                                int(ig + (sag - ig) * nxt),
                                int(ib + (sab - ib) * nxt),
                            ))
                if changed and hw: hw.show()
        except Exception as e:
            log.error("LED worker error: %s", e)
        time.sleep(TICK_LED)

def set_leds(cx, acnt: int, col: str, idle: str, bri: int,
             mode: str = 'fixed', person_w: float = 0.0):
    """Queue LED update. cx=None or acnt<=0 → all off."""
    try: _led_q.get_nowait()
    except _queue.Empty: pass
    _led_q.put_nowait((cx, acnt, col, idle, bri, mode, person_w))

def flash_signal(color_hex: str, bri: int, count: int = 2):
    def _do():
        for _ in range(count):
            set_leds(0.5, 9999, color_hex, '#000000', bri)
            time.sleep(0.12)
            set_leds(None, 0, color_hex, '#000000', bri)
            time.sleep(0.12)
    threading.Thread(target=_do, daemon=True).start()

# ── remote mode ───────────────────────────────────────────────────────────────
_remote      = False
_remote_lock = threading.Lock()

def get_remote():
    with _remote_lock: return _remote

def set_remote(enabled: bool):
    global _remote
    with _remote_lock:
        if _remote == enabled: return
        _remote = enabled
    cfg = load_cfg()
    bri = int(cfg.get('brightness', 200))
    if enabled:
        log.info("Remote UI ON  →  http://%s:5000", _get_ip())
        flash_signal('#00ff88', bri, count=3)
    else:
        log.info("Remote UI OFF")
        flash_signal('#ff4400', bri, count=2)

def _get_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(('8.8.8.8', 80)); ip = s.getsockname()[0]; s.close(); return ip
    except Exception: return 'localhost'

# ── GPIO button ───────────────────────────────────────────────────────────────
def init_button(pin: int):
    try:
        from gpiozero import Button
        btn = Button(pin, pull_up=True, bounce_time=0.3)
        btn.when_pressed = lambda: set_remote(not get_remote())
        log.info("Remote button: GPIO %d (gpiozero)", pin); return
    except Exception: pass
    try:
        import RPi.GPIO as GPIO
        GPIO.setmode(GPIO.BCM); GPIO.setup(pin, GPIO.IN, pull_up_down=GPIO.PUD_UP)
        _t = [0.0]
        def _cb(_ch):
            now = time.time()
            if now - _t[0] > 0.3: _t[0] = now; set_remote(not get_remote())
        GPIO.add_event_detect(pin, GPIO.FALLING, callback=_cb, bouncetime=300)
        log.info("Remote button: GPIO %d (RPi.GPIO)", pin); return
    except Exception: pass
    log.warning("GPIO button not available — use API or web UI to toggle remote")

# ── detection engine ──────────────────────────────────────────────────────────
USE_YOLO   = False
YOLO_MODEL = None
try:
    from ultralytics import YOLO
    YOLO_MODEL = YOLO('yolov8n.pt')
    YOLO_MODEL(np.zeros((480, 640, 3), dtype=np.uint8), verbose=False, classes=[0])
    USE_YOLO = True
    log.info("Detection: YOLOv8n")
except Exception as e:
    log.info("YOLOv8 not available (%s) — using HOG+MOG2", e)

hog = cv2.HOGDescriptor()
hog.setSVMDetector(cv2.HOGDescriptor_getDefaultPeopleDetector())
mog = cv2.createBackgroundSubtractorMOG2(history=30, varThreshold=25, detectShadows=False)
_k_open   = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5,  5))
_k_close  = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (15, 15))
_k_dilate = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (20, 20))

# ── shared detection state ────────────────────────────────────────────────────
frame_lock  = threading.Lock()
det_lock    = threading.Lock()
raw_lock    = threading.Lock()
raw2_lock   = threading.Lock()
_new_frame  = threading.Event()
is_running  = True
current_frame = None
latest_raw    = None
latest_raw2      = None   # second camera; None = not active
_cam2_active     = False  # True when cam2_worker is capturing frames
_cam1_dev        = None   # device index/path cam_worker successfully opened
_cam2_last_dev   = None   # most-recently-released cam2 device index
_cam2_released_at = 0.0   # timestamp of that release

detection_state = {
    "detected": False, "bbox": None, "active_leds": [],
    "confidence": 0, "fps": 0, "hold": 0,
    "cx": 0.5, "person_w": 0.0, "use_yolo": USE_YOLO
}

smooth_x1 = smooth_y1 = smooth_x2 = smooth_y2 = 0.0
smooth_cx  = 0.5
_first_det = True
hold_ctr   = 0
HOLD_FRAMES = 5
last_active = []
fps_ring    = deque(maxlen=15)
t_fps       = time.time()
frame_ctr   = 0

_hog_lock = threading.Lock()
_hog_confirmed, _hog_bbox, _hog_conf, _hog_ts = False, None, 0.0, 0.0
HOG_EXPIRE = 0.6

_thermal_throttle, _thermal_ts = 0, 0.0

def check_thermal():
    global _thermal_throttle, _thermal_ts
    now = time.time()
    if now - _thermal_ts < 1.0: return _thermal_throttle
    _thermal_ts = now
    try:
        with open('/sys/class/thermal/thermal_zone0/temp') as f:
            t = int(f.read()) / 1000.0
        _thermal_throttle = 3 if t >= 85 else 2 if t >= 80 else 1 if t >= 75 else 0
    except Exception:
        _thermal_throttle = 0
    return _thermal_throttle

def _nms(boxes, weights, thr=0.5):
    if not len(boxes): return [], []
    b = np.array(boxes); w = np.array(weights).flatten()
    x1, y1 = b[:,0], b[:,1]; x2, y2 = b[:,0]+b[:,2], b[:,1]+b[:,3]
    areas = b[:,2]*b[:,3]; order = w.argsort()[::-1]; keep = []
    while len(order):
        i = order[0]; keep.append(i)
        if len(order) == 1: break
        ix = np.maximum(x1[i],x1[order[1:]]); iy = np.maximum(y1[i],y1[order[1:]])
        ax = np.minimum(x2[i],x2[order[1:]]); ay = np.minimum(y2[i],y2[order[1:]])
        inter = np.maximum(0,ax-ix)*np.maximum(0,ay-iy)
        iou   = inter/(areas[i]+areas[order[1:]]-inter+1e-6)
        order = order[np.where(iou<=thr)[0]+1]
    return [boxes[k] for k in keep], [w[k] for k in keep]

def _valid_box(x, y, bw, bh, fw, fh):
    asp = bh / max(bw, 1)
    return fh*0.12 <= bh and fw*0.04 <= bw <= fw*0.85 and 0.8 <= asp <= 5.5

# ── detection ─────────────────────────────────────────────────────────────────
def run_detection(frame, cfg):
    global smooth_x1, smooth_y1, smooth_x2, smooth_y2, smooth_cx
    global hold_ctr, last_active, fps_ring, t_fps, frame_ctr, _first_det

    h, w  = frame.shape[:2]
    n     = int(cfg.get('led_count', 50))
    acnt  = int(cfg.get('active_leds_count', 15))
    alpha = 1.0 - float(cfg.get('smoothing', 0.3))
    cthr  = float(cfg.get('confidence_threshold', 0.3))
    bri   = int(cfg.get('brightness', 200))
    col   = cfg.get('active_color', '#ff6b6b')
    idle  = cfg.get('idle_color', '#1a1a2e')
    mode  = cfg.get('led_spread_mode', 'fixed')

    frame_ctr += 1
    now = time.time()
    if now - t_fps >= 1.0:
        fps_ring.append(frame_ctr / (now - t_fps))
        frame_ctr = 0; t_fps = now
    fps = round(sum(fps_ring) / len(fps_ring), 1) if fps_ring else 0

    detected = False; rx1 = ry1 = rx2 = ry2 = 0; conf = 0.0

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
            log.error("YOLO error: %s", e)
    else:
        sc = 0.5; sm = cv2.resize(frame,(int(w*sc),int(h*sc))); sh,sw = sm.shape[:2]
        fg = mog.apply(sm, learningRate=0.003)
        fg = cv2.threshold(fg, 200, 255, cv2.THRESH_BINARY)[1]
        fg = cv2.morphologyEx(fg, cv2.MORPH_OPEN,  _k_open)
        fg = cv2.morphologyEx(fg, cv2.MORPH_CLOSE, _k_close)
        fg = cv2.dilate(fg, _k_dilate, iterations=1)
        mpct = cv2.countNonZero(fg) / (sh*sw) * 100
        with _hog_lock:
            hog_ok  = _hog_confirmed and (now - _hog_ts) < HOG_EXPIRE
            hog_box = _hog_bbox if hog_ok else None; hog_c = _hog_conf
        if mpct > 0.5:
            cnts, _ = cv2.findContours(fg, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
            blobs = sorted(
                [cv2.boundingRect(c) for c in cnts if cv2.contourArea(c) > sh*sw*0.005],
                key=lambda b: b[2]*b[3], reverse=True
            )
            for bx, by, bw_, bh_ in blobs:
                if _valid_box(bx, by, bw_, bh_, sw, sh):
                    if hog_ok and hog_box:
                        rx1,ry1,rx2,ry2 = hog_box; conf = hog_c
                    else:
                        rx1,ry1 = int(bx/sc),int(by/sc)
                        rx2,ry2 = int((bx+bw_)/sc),int((by+bh_)/sc); conf = 0.4
                    detected = True; break
        if not detected and hog_ok and hog_box:
            rx1,ry1,rx2,ry2 = hog_box; conf = hog_c; detected = True

    if detected:
        hold_ctr = HOLD_FRAMES
        if _first_det:
            smooth_x1,smooth_y1 = float(rx1),float(ry1)
            smooth_x2,smooth_y2 = float(rx2),float(ry2)
            smooth_cx = ((rx1+rx2)/2)/w; _first_det = False
        else:
            smooth_x1 = smooth_x1*(1-alpha) + rx1*alpha
            smooth_y1 = smooth_y1*(1-alpha) + ry1*alpha
            smooth_x2 = smooth_x2*(1-alpha) + rx2*alpha
            smooth_y2 = smooth_y2*(1-alpha) + ry2*alpha
            smooth_cx  = (smooth_x1+smooth_x2)/2/w
        pw = (smooth_x2-smooth_x1)/w if w > 0 else 0.0
        if mode == 'proportional':
            acnt = max(1, round(pw * n))
        last_active = _leds_for(smooth_cx, n, acnt)
        set_leds(smooth_cx, acnt, col, idle, bri, mode, pw)
    elif hold_ctr > 0:
        hold_ctr -= 1; detected = hold_ctr > 0
        if hold_ctr == 0:
            _first_det = True
            set_leds(None, 0, col, idle, bri)

    pw = (smooth_x2-smooth_x1)/w if w > 0 else 0.0
    with det_lock:
        detection_state.update({
            "detected":    detected,
            "bbox":        [int(smooth_x1),int(smooth_y1),int(smooth_x2),int(smooth_y2)] if detected else None,
            "active_leds": last_active if detected else [],
            "confidence":  round(conf,3), "fps": fps,
            "use_yolo":    USE_YOLO, "hold": hold_ctr,
            "cx":          round(smooth_cx,4), "person_w": round(pw,4),
        })

# ── HOG background thread ─────────────────────────────────────────────────────
def hog_worker():
    global _hog_confirmed, _hog_bbox, _hog_conf, _hog_ts
    while is_running:
        with raw_lock: frame = latest_raw
        if frame is None: time.sleep(0.1); continue
        h, w = frame.shape[:2]; sc = 0.5
        sm = cv2.resize(frame, (int(w*sc), int(h*sc)))
        bxs, wts = hog.detectMultiScale(
            sm, winStride=(8,8), padding=(8,8),
            scale=1.1, hitThreshold=0.6, useMeanshiftGrouping=False
        )
        best = None; bc = 0.0
        if len(bxs) > 0 and len(wts) > 0:
            bn, wn = _nms(bxs.tolist(), wts.tolist(), 0.4)
            for i, (bx, by, bw_, bh_) in enumerate(bn):
                rx,ry,rw,rh = int(bx/sc),int(by/sc),int(bw_/sc),int(bh_/sc)
                if _valid_box(rx,ry,rw,rh,w,h):
                    nc = min(1.0, max(0.0, (float(wn[i])-0.3)/1.5))
                    if nc > bc: bc = nc; best = (rx,ry,rx+rw,ry+rh)
        with _hog_lock:
            _hog_confirmed = best is not None; _hog_bbox = best; _hog_conf = bc
            if best: _hog_ts = time.time()
        time.sleep(0.25)

# ── camera helpers ────────────────────────────────────────────────────────────
def _cam_device_name(index: int) -> str:
    """Return human-readable name for /dev/videoN, or empty string."""
    try:
        with open(f'/sys/class/video4linux/video{index}/name') as f:
            return f.read().strip()
    except Exception:
        return ''

def _open_verified_camera(path_or_int, max_wait_s: float = 0.0) -> 'cv2.VideoCapture | None':
    """Open a camera and verify it can deliver a real frame. Returns cap or None.

    max_wait_s: extra time budget (seconds) for cameras that need to warm up after
    being just released (e.g. scan after remove).  0 = fast path (5 reads, no delay).
    """
    try:
        c = cv2.VideoCapture(path_or_int, cv2.CAP_V4L2)
        if not c.isOpened():
            c.release(); return None
        # Fast check: 5 reads with no sleep — metadata-only nodes fail here
        for _ in range(5):
            ret, _ = c.read()
            if ret:
                return c
        # Slow check: give recently-released cameras up to max_wait_s to restart streaming
        deadline = time.time() + max_wait_s
        while time.time() < deadline:
            time.sleep(0.1)
            ret, _ = c.read()
            if ret:
                return c
        c.release()
        return None
    except Exception:
        return None

# ── camera thread with reconnect ──────────────────────────────────────────────
# Only even-numbered video nodes are capture nodes on most USB cameras
_CAM_PATHS = [f'/dev/video{i}' for i in range(0, 10, 2)]

def cam_worker():
    global latest_raw, is_running, _cam1_dev
    backoff = 1
    while is_running:
        _cfg0 = load_cfg()
        _c2i  = int(_cfg0.get('cam2_index', -1))
        _c1i  = int(_cfg0.get('cam1_index', -1))
        # Exclude cam2's device path and index
        _skip = {_c2i, f'/dev/video{_c2i}'} if _c2i >= 0 else set()
        cam = None
        if _c1i >= 0:
            # User pinned cam1 — use that device directly
            path = f'/dev/video{_c1i}'
            cam = _open_verified_camera(path)
            if cam: _cam1_dev = _c1i; log.info("Camera1: pinned to %s", path)
            else: log.warning("Camera1: pinned index %d not available, retrying in %ds", _c1i, backoff)
        else:
            # Auto-detect: try capture nodes, skip whichever cam2 is using
            for path in _CAM_PATHS:
                idx = int(path.replace('/dev/video', ''))
                if idx in _skip or path in _skip: continue
                cam = _open_verified_camera(path)
                if cam: _cam1_dev = idx; log.info("Camera1: auto-detected %s (%s)", path, _cam_device_name(idx)); break
        if cam is None:
            log.warning("Camera1: no device found, retrying in %ds", backoff)
            time.sleep(backoff); backoff = min(backoff * 2, 30); continue
        backoff = 1
        cam.set(cv2.CAP_PROP_FRAME_WIDTH,  640)
        cam.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
        cam.set(cv2.CAP_PROP_BUFFERSIZE,   1)
        cam.set(cv2.CAP_PROP_FPS,          30)
        fails = 0
        while is_running:
            if check_thermal() == 3: time.sleep(1); continue
            ret, frame = cam.read()
            if not ret:
                fails += 1
                if fails > 30: log.warning("Camera: too many read failures, reconnecting"); break
                time.sleep(0.03); continue
            fails = 0
            _fc = load_cfg()
            fh, fv = _fc.get('flip_h', True), _fc.get('flip_v', False)
            if fh and fv:  frame = cv2.flip(frame, -1)
            elif fh:       frame = cv2.flip(frame,  1)
            elif fv:       frame = cv2.flip(frame,  0)
            with raw_lock: latest_raw = frame
            _new_frame.set()
        cam.release()
        if is_running: time.sleep(2)

# ── second camera thread ──────────────────────────────────────────────────────
def cam2_worker():
    global latest_raw2, is_running, _cam2_active, _cam2_last_dev, _cam2_released_at
    while is_running:
        _c2 = load_cfg()
        idx = int(_c2.get('cam2_index', -1))
        if idx < 0:
            with raw2_lock: latest_raw2 = None
            _cam2_active = False
            time.sleep(2); continue
        path = f'/dev/video{idx}'
        cam = _open_verified_camera(path)
        if cam is None:
            log.warning("Camera2: %s not a working capture device, retrying in 5s", path)
            with raw2_lock: latest_raw2 = None
            _cam2_active = False
            time.sleep(5); continue
        log.info("Camera2: opened %s (%s)", path, _cam_device_name(idx))
        cam.set(cv2.CAP_PROP_FRAME_WIDTH,  640)
        cam.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
        cam.set(cv2.CAP_PROP_BUFFERSIZE,   1)
        cam.set(cv2.CAP_PROP_FPS,          30)
        fails = 0; last_idx = idx; cfg_tick = 0
        fh = False; fv = False
        _last_frame_t = 0.0
        _frame_gap = 1.0 / 15  # cap cam2 at 15fps to reduce CPU/swap load
        while is_running:
            # Re-read config only every 30 frames instead of every frame
            if cfg_tick % 30 == 0:
                _cc = load_cfg()
                if int(_cc.get('cam2_index', -1)) != last_idx: break
                fh = _cc.get('cam2_flip_h', False)
                fv = _cc.get('cam2_flip_v', False)
            cfg_tick += 1
            if check_thermal() == 3: time.sleep(1); continue
            ret, frame = cam.read()
            if not ret:
                fails += 1
                if fails > 30: log.warning("Camera2: too many failures, reconnecting"); break
                time.sleep(0.03); continue
            fails = 0
            # Throttle cam2 to 15fps to keep CPU/swap load manageable
            now = time.time()
            if now - _last_frame_t < _frame_gap:
                continue
            _last_frame_t = now
            if fh and fv:  frame = cv2.flip(frame, -1)
            elif fh:       frame = cv2.flip(frame,  1)
            elif fv:       frame = cv2.flip(frame,  0)
            with raw2_lock: latest_raw2 = frame
            _cam2_active = True
        _cam2_last_dev    = idx
        _cam2_released_at = time.time()
        cam.release()
        log.info("Camera2: released /dev/video%d", idx)
        with raw2_lock: latest_raw2 = None
        _cam2_active = False
        if is_running: time.sleep(2)

# ── detect thread ─────────────────────────────────────────────────────────────
def detect_worker():
    global current_frame, is_running
    _cfg_cache_d = None; _cfg_tick_d = 0
    while is_running:
        if not _new_frame.wait(timeout=0.5): continue
        _new_frame.clear()
        with raw_lock: frame = latest_raw
        if frame is None: continue
        try:
            _cfg_tick_d += 1
            if _cfg_cache_d is None or _cfg_tick_d % 30 == 0:
                _cfg_cache_d = load_cfg()
            cfg = _cfg_cache_d
            with raw2_lock: frame2 = latest_raw2
            if frame2 is not None:
                h, w = frame.shape[:2]
                half_w = w // 2
                aspect = cfg.get('cam2_aspect', 'fit')
                if aspect == 'native':
                    # Maintain each camera's aspect ratio, scale to half_w wide
                    h1n = int(half_w * frame.shape[0]  / frame.shape[1])
                    h2n = int(half_w * frame2.shape[0] / frame2.shape[1])
                    tgt_h = max(h1n, h2n)
                    f1 = cv2.resize(frame,  (half_w, h1n))
                    f2 = cv2.resize(frame2, (half_w, h2n))
                    # Pad shorter one to same height
                    if h1n < tgt_h: f1 = cv2.copyMakeBorder(f1, 0, tgt_h-h1n, 0, 0, cv2.BORDER_CONSTANT)
                    if h2n < tgt_h: f2 = cv2.copyMakeBorder(f2, 0, tgt_h-h2n, 0, 0, cv2.BORDER_CONSTANT)
                else:
                    # Fit: fill the frame, slight distortion
                    f1 = cv2.resize(frame,  (half_w, h))
                    f2 = cv2.resize(frame2, (half_w, h))
                stitched = np.hstack([f2, f1] if cfg.get('cam2_side', 'right') == 'left' else [f1, f2])
                # Downscale for detection to keep same CPU load as single camera
                detect_frame = cv2.resize(stitched, (w, h)) if stitched.shape[:2] != (h, w) else stitched
                frame = stitched
            else:
                detect_frame = frame
            run_detection(detect_frame, cfg)
            out = frame.copy()
            with det_lock: st = detection_state.copy()
            if st['detected'] and st['bbox']:
                x1,y1,x2,y2 = st['bbox']
                pulse = int(abs(np.sin(time.time()*3))*40)
                cv2.rectangle(out,(x1,y1),(x2,y2),(0,200+pulse,50+pulse),2)
            with frame_lock: current_frame = out
        except Exception as e:
            log.error("Detection error: %s", e)

# ── password helpers ──────────────────────────────────────────────────────────
import hashlib as _hashlib

def _hash_password(pw: str) -> str:
    salt = os.urandom(32)
    key  = _hashlib.pbkdf2_hmac('sha256', pw.encode(), salt, 260_000)
    return salt.hex() + ':' + key.hex()

def _check_password(pw: str, stored: str) -> bool:
    try:
        salt_hex, key_hex = stored.split(':')
        salt = bytes.fromhex(salt_hex)
        key  = _hashlib.pbkdf2_hmac('sha256', pw.encode(), salt, 260_000)
        return key.hex() == key_hex
    except Exception:
        return False

def _passwd_set() -> bool:
    """True if a password has been configured (file or env var)."""
    return bool(os.environ.get('LED_PASSWORD')) or os.path.exists(PASSWD_FILE)

def _check_login(pw: str) -> bool:
    env_pw = os.environ.get('LED_PASSWORD', '')
    if env_pw:
        return pw == env_pw
    if os.path.exists(PASSWD_FILE):
        try:
            return _check_password(pw, open(PASSWD_FILE).read().strip())
        except Exception:
            return False
    return False

# ── Flask + auth ──────────────────────────────────────────────────────────────
def _load_secret_key():
    env = os.environ.get('LED_SECRET_KEY')
    if env:
        return env
    key_file = os.path.join(BASE_DIR, '.secret_key')
    try:
        with open(key_file) as f:
            k = f.read().strip()
        if k:
            return k
    except FileNotFoundError:
        pass
    k = secrets.token_hex(32)
    with open(key_file, 'w') as f:
        f.write(k)
    return k

_SECRET_KEY = _load_secret_key()

app = Flask(__name__,
            template_folder=os.path.join(BASE_DIR, 'templates'),
            static_folder=os.path.join(BASE_DIR, 'static'))
app.secret_key = _SECRET_KEY

def _is_authed():
    return session.get('authenticated', False)

def require_login(f):
    @functools.wraps(f)
    def decorated(*args, **kwargs):
        if not _is_authed():
            if request.path.startswith('/api/') or request.path == '/video_feed':
                return jsonify({'error': 'unauthorized'}), 401
            return redirect('/login')
        return f(*args, **kwargs)
    return decorated

@app.route('/login', methods=['GET', 'POST'])
def login():
    # If setup not done yet, go there first
    if not _setup_complete():
        return redirect('/setup')
    err = ''
    if request.method == 'POST':
        if _check_login(request.form.get('password', '')):
            session['authenticated'] = True
            return redirect('/')
        err = 'Incorrect password'
    return render_template('login.html', error=err)

@app.route('/logout', methods=['POST'])
def logout():
    session.clear()
    return redirect('/login')

# ── setup wizard ──────────────────────────────────────────────────────────────
def _setup_complete():
    """Setup is complete only when both the flag file and a password exist."""
    return os.path.exists(SETUP_FILE) and _passwd_set()

@app.route('/setup', methods=['GET', 'POST'])
def setup_wizard():
    # After setup is done, require login to revisit setup
    if _setup_complete() and not _is_authed():
        return redirect('/login')
    if request.method == 'POST':
        d = request.get_json() or {}
        # Step 0: set password
        if d.get('action') == 'set_password':
            pw = d.get('password', '')
            if len(pw) < 8:
                return jsonify({'status': 'error',
                                'errors': ['Password must be at least 8 characters']}), 400
            with open(PASSWD_FILE, 'w') as f:
                f.write(_hash_password(pw))
            session['authenticated'] = True  # auto-login after setting password
            return jsonify({'status': 'ok'})
        # Final step: mark setup complete
        if d.get('complete'):
            open(SETUP_FILE, 'w').close()
            return jsonify({'status': 'ok'})
        # Config updates (strips, detection settings)
        cfg = load_cfg()
        updates, errors = _validate_cfg(d)
        if errors:
            return jsonify({'status': 'error', 'errors': errors}), 400
        cfg.update(updates)
        save_cfg(cfg)
        return jsonify({'status': 'ok', 'config': cfg})
    return render_template('setup.html', config=load_cfg())

# ── main routes ───────────────────────────────────────────────────────────────
@app.route('/')
@require_login
def index():
    if not _setup_complete():
        return redirect('/setup')
    return render_template('index.html', config=load_cfg())

@app.route('/api/config', methods=['GET'])
@require_login
def get_cfg_route():
    return jsonify(load_cfg())

@app.route('/api/config', methods=['POST'])
@require_login
def upd_cfg_route():
    data = request.get_json()
    if not data:
        return jsonify({'status': 'error', 'errors': ['No JSON body']}), 400
    current = load_cfg()
    updates, errors = _validate_cfg(data)
    if errors:
        return jsonify({'status': 'error', 'errors': errors}), 400
    current.update(updates)
    save_cfg(current)
    if 'strips' in updates or 'brightness' in updates:
        threading.Thread(
            target=init_strips,
            args=(current.get('strips', []), int(current.get('brightness', 200))),
            daemon=True
        ).start()
        log.info("LED strips reinit triggered by config change")
    return jsonify({'status': 'ok', 'config': current})

@app.route('/api/detection')
@require_login
def get_det():
    with det_lock: return jsonify(detection_state)

@app.route('/api/status')
@require_login
def get_status():
    uptime = int(time.time() - _START_TIME)
    with _strip_lock:
        strips_status = [
            {'label': sc.get('label', f'Strip {i+1}'),
             'gpio_pin': sc['gpio_pin'],
             'led_count': sc['led_count'],
             'hw_ok': hw is not None}
            for i, (hw, sc) in enumerate(zip(_led_strips, _strip_cfgs))
        ]
    try:
        with open('/sys/class/thermal/thermal_zone0/temp') as f:
            temp_c = round(int(f.read()) / 1000.0, 1)
    except Exception:
        temp_c = -1
    return jsonify({
        'version':        APP_VERSION,
        'uptime_s':       uptime,
        'camera_active':  current_frame is not None,
        'cam1_dev':       _cam1_dev,
        'cam2_active':    _cam2_active,
        'use_yolo':       USE_YOLO,
        'led_available':  LED_AVAILABLE,
        'strips':         strips_status,
        'remote':         get_remote(),
        'stream_enabled': get_remote(),
        'temp_c':         temp_c,
        'auth_enabled':   _passwd_set(),
    })

@app.route('/api/remote', methods=['POST'])
@require_login
def toggle_remote_route():
    d   = request.get_json() or {}
    val = bool(d['enabled']) if 'enabled' in d else not get_remote()
    set_remote(val)
    return jsonify({'remote': get_remote()})

@app.route('/api/toggle-stream', methods=['POST'])
@require_login
def toggle_stream_route():
    set_remote(not get_remote())
    return jsonify({'stream_enabled': get_remote()})

@app.route('/api/test-led', methods=['POST'])
@require_login
def test_led_route():
    global _test_led_idx, _test_led_until
    cfg    = load_cfg()
    data   = request.get_json() or {}
    target = data.get('target', 'all')

    if target in ('first', 'last'):
        with _strip_lock:
            cfgs = list(_strip_cfgs)
        n   = int(cfgs[0]['led_count']) if cfgs else int(cfg.get('led_count', 50))
        idx = 0 if target == 'first' else n - 1
        with _test_led_lock:
            _test_led_idx   = idx
            _test_led_until = time.time() + 2.0
        log.info("Test LED: %s → index %d of %d", target, idx, n)
        return jsonify({'status': 'ok', 'led_available': LED_AVAILABLE, 'led_index': idx})

    # target == 'all': flash all strips
    col  = cfg.get('active_color', '#ff6b6b')
    idle = cfg.get('idle_color', '#1a1a2e')
    bri  = int(cfg.get('brightness', 200))
    def _flash():
        for _ in range(3):
            set_leds(0.5, 9999, col, idle, bri); time.sleep(0.2)
            set_leds(None, 0,   col, idle, bri); time.sleep(0.2)
    threading.Thread(target=_flash, daemon=True).start()
    return jsonify({'status': 'ok', 'led_available': LED_AVAILABLE})

@app.route('/api/scan-cameras')
@require_login
def scan_cameras():
    """Scan /dev/video0..9 (even nodes = capture). Verify each can deliver a frame."""
    # Resolve active cam1 index
    cam1_int = None
    if _cam1_dev is not None:
        cam1_int = _cam1_dev if isinstance(_cam1_dev, int) else None
        if cam1_int is None:
            m = re.match(r'/dev/video(\d+)', str(_cam1_dev))
            if m: cam1_int = int(m.group(1))

    found = []
    cfg0 = load_cfg()
    cam2_int = int(cfg0.get('cam2_index', -1))
    recently_released = (
        _cam2_last_dev is not None and
        time.time() - _cam2_released_at < 8.0
    )

    for i in range(0, 10, 2):  # even nodes only — odd are metadata on USB cams
        name = _cam_device_name(i) or f'Camera {i}'

        # Already held open by cam_thread — include without re-opening
        if i == cam1_int:
            found.append({'index': i, 'name': name, 'active': True}); continue

        # Already held open by cam2_worker — include without re-opening
        if i == cam2_int and _cam2_active:
            found.append({'index': i, 'name': name, 'active': True}); continue

        # Recently released by cam2_worker — include directly; the device may still
        # be locked at kernel level and _open_verified_camera would fail the fast path
        if i == _cam2_last_dev and recently_released:
            found.append({'index': i, 'name': name, 'active': False}); continue

        # Verify in a background thread so a hung open can't block the HTTP response
        result = [None]   # will hold the opened cap or None
        def _check(idx=i, r=result):
            # Give recently-released cameras up to 1.5 s to restart streaming
            wait = 1.5 if (idx == _cam2_last_dev and recently_released) else 0.0
            r[0] = _open_verified_camera(f'/dev/video{idx}', max_wait_s=wait)
        t = threading.Thread(target=_check, daemon=True)
        t.start(); t.join(timeout=3.0)
        cap = result[0]
        if cap is not None:
            try: cap.release()
            except Exception: pass
            found.append({'index': i, 'name': name, 'active': False})

    return jsonify({'cameras': found, 'cam1': cam1_int})

@app.route('/api/cameras/debug')
@require_login
def cameras_debug():
    """Real-time camera state — open in browser to diagnose issues."""
    return jsonify({
        'cam1_dev':               _cam1_dev,
        'cam2_active':            _cam2_active,
        'cam2_last_dev':          _cam2_last_dev,
        'cam2_released_secs_ago': round(time.time() - _cam2_released_at, 1) if _cam2_released_at else None,
        'cam2_frame_ready':       latest_raw2 is not None,
        'cam1_frame_ready':       current_frame is not None,
        'config_cam1_index':      load_cfg().get('cam1_index', -1),
        'config_cam2_index':      load_cfg().get('cam2_index', -1),
    })

@app.route('/video_feed2')
@require_login
def video_feed2():
    """MJPEG stream for cam2 PiP preview."""
    def _gen2():
        while True:
            with raw2_lock:
                frame = latest_raw2
            if frame is None:
                time.sleep(0.1); continue
            _, buf = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, 70])
            yield b'--frame\r\nContent-Type: image/jpeg\r\n\r\n' + buf.tobytes() + b'\r\n'
            time.sleep(0.066)   # ~15 fps — matches cam2_worker throttle
    return Response(_gen2(), mimetype='multipart/x-mixed-replace; boundary=frame')

@app.route('/api/thermal')
@require_login
def get_thermal():
    try:
        with open('/sys/class/thermal/thermal_zone0/temp') as f:
            t = int(f.read()) / 1000.0
        return jsonify({'temp_c': round(t,1), 'throttle': _thermal_throttle})
    except Exception:
        return jsonify({'temp_c': -1, 'throttle': 0})

# ── MJPEG stream with adaptive quality ───────────────────────────────────────
_mjpeg_quality = 80

def _gen_frames():
    global _mjpeg_quality
    while True:
        if not get_remote(): time.sleep(0.5); continue
        with frame_lock:
            if current_frame is None: time.sleep(0.05); continue
            frame = current_frame.copy()
        t0 = time.time()
        _, buf = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, _mjpeg_quality])
        yield b'--frame\r\nContent-Type: image/jpeg\r\n\r\n' + buf.tobytes() + b'\r\n'
        elapsed = time.time() - t0
        if elapsed > 0.10 and _mjpeg_quality > 40:
            _mjpeg_quality = max(40, _mjpeg_quality - 5)
        elif elapsed < 0.03 and _mjpeg_quality < 85:
            _mjpeg_quality = min(85, _mjpeg_quality + 2)
        time.sleep(0.025)

@app.route('/video_feed')
@require_login
def video_feed():
    if not get_remote():
        return Response(status=204)
    return Response(_gen_frames(), mimetype='multipart/x-mixed-replace; boundary=frame')

# ── main ──────────────────────────────────────────────────────────────────────
if __name__ == '__main__':
    cfg0   = load_cfg()
    strips = cfg0.get('strips', [])
    if not strips:
        strips = [{'gpio_pin': cfg0.get('gpio_pin', 18),
                   'led_count': cfg0.get('led_count', 50),
                   'enabled': True, 'label': 'Strip 1',
                   'color': cfg0.get('active_color', '#ff6b6b')}]
    init_strips(strips, int(cfg0.get('brightness', 200)))
    init_button(int(cfg0.get('remote_btn_pin', 17)))

    for target in [led_worker, cam_worker, cam2_worker, detect_worker, hog_worker]:
        threading.Thread(target=target, daemon=True).start()

    log.info("Human Detect LED v%s starting", APP_VERSION)
    if os.environ.get('LED_PASSWORD'):
        log.info("Auth: password from LED_PASSWORD env var")
    elif os.path.exists(PASSWD_FILE):
        log.info("Auth: password from %s", PASSWD_FILE)
    if not _setup_complete():
        log.info("First run — open http://%s:5000/setup to configure", _get_ip())
    else:
        log.info("Running — http://%s:5000", _get_ip())

    app.run(host='0.0.0.0', port=5000, debug=False, threaded=True)
