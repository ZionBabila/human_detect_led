from flask import Flask, render_template, jsonify, request, Response
import json, os, cv2, threading, time, numpy as np, subprocess
from collections import deque
import queue as _queue

app = Flask(__name__)
CONFIG_FILE = 'config.json'

DEFAULT = {
    'brightness': 200,
    'smoothing': 0.3,
    'detection_threshold': 30,
    'led_count': 50,
    'active_leds_count': 15,
    'led_spread_mode': 'fixed',
    'active_color': '#ff6b6b',
    'idle_color': '#1a1a2e',
    'confidence_threshold': 0.3,
    'gpio_pin': 18,
    'strips': [
        {'gpio_pin': 18, 'led_count': 50, 'enabled': True, 'label': 'Strip 1', 'color': '#ff6b6b'}
    ],
    'cam1_index': -1,   # -1 = auto-detect
    'cam2_index': -1,
    'flip_h': True,
    'flip_v': False,
    'cam2_flip_h': False,
    'cam2_flip_v': False,
    'cam2_side':   'right',  # 'left' | 'right'
    'cam2_aspect': 'fit',    # 'fit' | 'native'
}

# ── LED HARDWARE (rpi_ws281x) ──────────────────────────────────────────────────
try:
    from rpi_ws281x import PixelStrip, Color
    LED_AVAILABLE = True
except ImportError:
    LED_AVAILABLE = False
    print("WARNING: rpi_ws281x not available, LEDs disabled")

_led_strip = None
_cam1_dev  = None   # currently active camera device index
_cam2_dev  = None
_cam2_frame      = None
_cam2_frame_lock = threading.Lock()

def init_led_strip(gpio_pin=18, led_count=50, brightness=200):
    global _led_strip
    if not LED_AVAILABLE:
        return False
    try:
        _led_strip = PixelStrip(led_count, gpio_pin, 800000, 10, False, brightness, 0)
        _led_strip.begin()
        for i in range(_led_strip.numPixels()):
            _led_strip.setPixelColor(i, Color(0, 0, 0))
        _led_strip.show()
        print(f"✅ LED strip initialized: {led_count} LEDs on GPIO {gpio_pin}")
        return True
    except Exception as e:
        print(f"❌ LED strip init failed: {e}")
        _led_strip = None
        return False

# ── LED thread — fading, dedicated, non-blocking ──────────────────────────────
_led_queue      = _queue.Queue(maxsize=1)
_led_brightness = 200

# Per-LED intensity: 0.0 = fully idle colour, 1.0 = fully active colour
_led_intensities: list[float] = []

FADE_IN_RATE  = 0.25   # intensity added per tick when LED should be ON  (~100ms to full)
FADE_OUT_RATE = 0.06   # intensity removed per tick when LED should be OFF (~400ms to off)
LED_TICK_S    = 0.025  # ~40 Hz update rate

def led_thread():
    global _led_brightness, _led_intensities
    last_cmd = None

    while True:
        # Pick up latest command if available, otherwise reuse last
        try:
            last_cmd = _led_queue.get_nowait()
        except _queue.Empty:
            pass

        if not _led_strip or last_cmd is None:
            time.sleep(LED_TICK_S)
            continue

        active_leds, led_count, color_hex, idle_hex, brightness = last_cmd

        # Grow/shrink intensity array when led_count changes
        if len(_led_intensities) != led_count:
            _led_intensities = [0.0] * led_count

        try:
            if brightness != _led_brightness:
                _led_strip.setBrightness(brightness)
                _led_brightness = brightness

            ar, ag, ab = hex_bgr(color_hex)   # active  colour (B,G,R → stored as B,G,R)
            ir, ig, ib = hex_bgr(idle_hex)     # idle colour

            changed = False
            for i in range(led_count):
                target = 1.0 if i in active_leds else 0.0
                cur    = _led_intensities[i]
                if target > cur:
                    cur = min(1.0, cur + FADE_IN_RATE)
                elif target < cur:
                    cur = max(0.0, cur - FADE_OUT_RATE)

                if cur != _led_intensities[i]:
                    _led_intensities[i] = cur
                    changed = True
                    # Blend idle → active by intensity
                    br = int(ir + (ar - ir) * cur)
                    bg = int(ig + (ag - ig) * cur)
                    bb = int(ib + (ab - ib) * cur)
                    # rpi_ws281x Color(r,g,b) — our hex_bgr returns (b,g,r)
                    _led_strip.setPixelColor(i, Color(bb, bg, br))

            if changed:
                _led_strip.show()

        except Exception as e:
            print(f"LED update error: {e}")

        time.sleep(LED_TICK_S)

def update_led_strip(active_leds, led_count, color_hex='#ff6b6b', idle_hex='#1a1a2e', brightness=200):
    """Non-blocking: always keep only the latest LED command."""
    try:
        _led_queue.get_nowait()
    except _queue.Empty:
        pass
    _led_queue.put_nowait((active_leds, led_count, color_hex, idle_hex, brightness))

# ── shared state ───────────────────────────────────────────────────────────────
frame_lock    = threading.Lock()
det_lock      = threading.Lock()
raw_lock      = threading.Lock()
_new_frame_ev = threading.Event()   # fires when cam_thread writes a new frame

current_frame = None
latest_raw    = None
is_running    = True

_stream_remote_enabled = True

detection_state = {
    "detected": False, "bbox": None, "active_leds": [],
    "confidence": 0, "fps": 0, "hold_frames": 0
}

# ── config cache ───────────────────────────────────────────────────────────────
_cfg_cache = DEFAULT.copy()
_cfg_mtime = 0

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

def hex_bgr(h):
    h = h.lstrip('#')
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return (b, g, r)

# ── detection engine ───────────────────────────────────────────────────────────
USE_YOLO   = False
YOLO_MODEL = None
try:
    from ultralytics import YOLO
    YOLO_MODEL = YOLO('yolov8n.pt')
    dummy = np.zeros((480, 640, 3), dtype=np.uint8)
    YOLO_MODEL(dummy, verbose=False, classes=[0])
    USE_YOLO = True
    print("Detection engine: YOLOv8n ✓")
except Exception as e:
    print(f"YOLOv8 not available ({e}) — using HOG+MOG2")

hog = cv2.HOGDescriptor()
hog.setSVMDetector(cv2.HOGDescriptor_getDefaultPeopleDetector())

# detectShadows=False — we never use shadow pixels, saves ~15% MOG2 time
mog = cv2.createBackgroundSubtractorMOG2(
    history=500, varThreshold=25, detectShadows=False
)

_kernel_open   = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
_kernel_close  = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (15, 15))
_kernel_dilate = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (20, 20))

# ── smoothed bbox + hold state ─────────────────────────────────────────────────
smooth_x1 = smooth_y1 = smooth_x2 = smooth_y2 = 0.0
smooth_cx  = 0.5
_first_detection = True
HOLD_FRAMES  = 5
hold_counter = 0
last_active_leds = []

# HOG confirmation — written by hog_confirm_thread
_hog_confirmed   = False
_hog_bbox        = None     # (x1,y1,x2,y2) full-res
_hog_conf        = 0.0
_hog_confirm_ts  = 0.0      # timestamp of last HOG confirmation
HOG_EXPIRE_S     = 0.6      # expire HOG result after this many seconds
_hog_state_lock  = threading.Lock()

fps_ring      = deque(maxlen=15)
last_fps_time = time.time()
frame_ctr     = 0

# ── thermal — cached at 1 Hz, not per-frame ───────────────────────────────────
_thermal_throttle  = 0
_thermal_last_read = 0.0

def check_thermal():
    global _thermal_throttle, _thermal_last_read
    now = time.time()
    if now - _thermal_last_read < 1.0:
        return _thermal_throttle
    _thermal_last_read = now
    try:
        with open("/sys/class/thermal/thermal_zone0/temp") as tf:
            temp_c = int(tf.read().strip()) / 1000.0
        if temp_c >= 85:
            _thermal_throttle = 3
        elif temp_c >= 80:
            _thermal_throttle = 2
        elif temp_c >= 75:
            _thermal_throttle = 1
        else:
            _thermal_throttle = 0
    except Exception:
        _thermal_throttle = 0
    return _thermal_throttle


def nms_boxes(boxes, weights, overlap_thresh=0.5):
    if len(boxes) == 0:
        return [], []
    boxes_arr = np.array(boxes)
    w_arr = np.array(weights).flatten()
    x1    = boxes_arr[:, 0]
    y1    = boxes_arr[:, 1]
    x2    = boxes_arr[:, 0] + boxes_arr[:, 2]
    y2    = boxes_arr[:, 1] + boxes_arr[:, 3]
    areas = boxes_arr[:, 2] * boxes_arr[:, 3]
    order = w_arr.argsort()[::-1]
    keep  = []
    while len(order) > 0:
        i = order[0]
        keep.append(i)
        if len(order) == 1:
            break
        xx1 = np.maximum(x1[i], x1[order[1:]])
        yy1 = np.maximum(y1[i], y1[order[1:]])
        xx2 = np.minimum(x2[i], x2[order[1:]])
        yy2 = np.minimum(y2[i], y2[order[1:]])
        inter = np.maximum(0, xx2 - xx1) * np.maximum(0, yy2 - yy1)
        iou   = inter / (areas[i] + areas[order[1:]] - inter + 1e-6)
        order = order[np.where(iou <= overlap_thresh)[0] + 1]
    return [boxes[k] for k in keep], [w_arr[k] for k in keep]


def validate_box(x, y, bw, bh, frame_w, frame_h):
    min_h  = frame_h * 0.12
    max_w  = frame_w * 0.85
    min_w  = frame_w * 0.04
    aspect = bh / max(bw, 1)
    if bh < min_h or bw < min_w or bw > max_w:
        return False
    if aspect < 0.8 or aspect > 5.5:
        return False
    return True


def detect_motion_contours(fg_mask, frame_h, frame_w):
    contours, _ = cv2.findContours(fg_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    min_area = frame_h * frame_w * 0.005
    return [cv2.boundingRect(c) for c in contours if cv2.contourArea(c) > min_area]


def leds_for_position(cx, led_count, active_count):
    centre_led = round(cx * led_count)
    half = max(1, active_count // 2)
    return list(range(max(0, centre_led - half),
                      min(led_count, centre_led + half + 1)))


def detect_person(frame, cfg):
    global smooth_x1, smooth_y1, smooth_x2, smooth_y2
    global smooth_cx, hold_counter, last_active_leds
    global fps_ring, last_fps_time, frame_ctr, _first_detection

    h, w = frame.shape[:2]
    led_count    = int(cfg.get('led_count', 50))
    active_count = int(cfg.get('active_leds_count', 15))
    smoothing    = float(cfg.get('smoothing', 0.3))
    conf_thr     = float(cfg.get('confidence_threshold', 0.3))
    act_col      = hex_bgr(cfg.get('active_color', '#ff6b6b'))
    brightness   = int(cfg.get('brightness', 200))

    # FPS
    frame_ctr += 1
    now = time.time()
    if now - last_fps_time >= 1.0:
        fps_ring.append(frame_ctr / (now - last_fps_time))
        frame_ctr = 0
        last_fps_time = now
    cur_fps = round(sum(fps_ring) / len(fps_ring), 1) if fps_ring else 0

    detected = False
    raw_x1 = raw_y1 = raw_x2 = raw_y2 = 0
    conf   = 0.0
    hog_ok = False

    # ── YOLOv8 path ───────────────────────────────────────────────────────────
    if USE_YOLO:
        try:
            results = YOLO_MODEL(frame, verbose=False, classes=[0],
                                 imgsz=320, conf=conf_thr)
            boxes = results[0].boxes
            if boxes and len(boxes) > 0:
                confs = boxes.conf.cpu().numpy()
                best  = int(np.argmax(confs))
                conf  = float(confs[best])
                x1, y1, x2, y2 = boxes.xyxy[best].cpu().numpy().astype(int)
                raw_x1, raw_y1, raw_x2, raw_y2 = x1, y1, x2, y2
                detected = True
        except Exception as e:
            print(f"YOLO error: {e}")

    # ── Fast MOG2+blob path (every frame, ~5 ms) ──────────────────────────────
    else:
        det_scale = 0.5
        small     = cv2.resize(frame, (int(w * det_scale), int(h * det_scale)))
        sh, sw    = small.shape[:2]

        fg_raw  = mog.apply(small, learningRate=0.003)
        fg_mask = cv2.threshold(fg_raw, 200, 255, cv2.THRESH_BINARY)[1]
        fg_mask = cv2.morphologyEx(fg_mask, cv2.MORPH_OPEN,  _kernel_open)
        fg_mask = cv2.morphologyEx(fg_mask, cv2.MORPH_CLOSE, _kernel_close)
        fg_mask = cv2.dilate(fg_mask, _kernel_dilate, iterations=1)

        motion_pixels = cv2.countNonZero(fg_mask)
        motion_pct    = motion_pixels / (sh * sw) * 100

        # Read HOG state (expires after HOG_EXPIRE_S seconds)
        with _hog_state_lock:
            hog_age = now - _hog_confirm_ts
            hog_ok  = _hog_confirmed and hog_age < HOG_EXPIRE_S
            hog_box = _hog_bbox if hog_ok else None
            hog_c   = _hog_conf

        if motion_pct > 0.5:
            blobs = detect_motion_contours(fg_mask, sh, sw)
            for blob in sorted(blobs, key=lambda b: b[2] * b[3], reverse=True):
                bx, by, bw_b, bh_b = blob
                if validate_box(bx, by, bw_b, bh_b, sw, sh):
                    if hog_ok and hog_box:
                        raw_x1, raw_y1, raw_x2, raw_y2 = hog_box
                        conf = hog_c
                    else:
                        raw_x1 = int(bx / det_scale)
                        raw_y1 = int(by / det_scale)
                        raw_x2 = int((bx + bw_b) / det_scale)
                        raw_y2 = int((by + bh_b) / det_scale)
                        conf = 0.4
                    detected = True
                    break

        # HOG confirmed but motion briefly dropped — use HOG box
        if not detected and hog_ok and hog_box:
            raw_x1, raw_y1, raw_x2, raw_y2 = hog_box
            conf = hog_c
            detected = True

    # ── bbox smoothing + hold ──────────────────────────────────────────────────
    alpha = 1.0 - smoothing

    if detected:
        hold_counter = HOLD_FRAMES

        if _first_detection:
            # Snap immediately — no lerp from 0,0 or from center
            smooth_x1, smooth_y1 = float(raw_x1), float(raw_y1)
            smooth_x2, smooth_y2 = float(raw_x2), float(raw_y2)
            smooth_cx = ((raw_x1 + raw_x2) / 2) / w
            _first_detection = False
        else:
            smooth_x1 = smooth_x1 * (1 - alpha) + raw_x1 * alpha
            smooth_y1 = smooth_y1 * (1 - alpha) + raw_y1 * alpha
            smooth_x2 = smooth_x2 * (1 - alpha) + raw_x2 * alpha
            smooth_y2 = smooth_y2 * (1 - alpha) + raw_y2 * alpha
            smooth_cx = (smooth_x1 + smooth_x2) / 2 / w

        spread_mode = cfg.get('led_spread_mode', 'fixed')
        if spread_mode == 'proportional':
            person_w = (smooth_x2 - smooth_x1) / w if w > 0 else 0.0
            active_count = max(1, round(person_w * led_count))

        last_active_leds = leds_for_position(smooth_cx, led_count, active_count)

        update_led_strip(
            set(last_active_leds), led_count,
            color_hex=cfg.get('active_color', '#ff6b6b'),
            idle_hex=cfg.get('idle_color', '#1a1a2e'),
            brightness=brightness
        )

    elif hold_counter > 0:
        hold_counter -= 1
        detected = (hold_counter > 0)
        if hold_counter == 0:
            _first_detection = True   # next detection snaps again
            update_led_strip(
                set(), led_count,
                color_hex=cfg.get('active_color', '#ff6b6b'),
                idle_hex=cfg.get('idle_color', '#1a1a2e'),
                brightness=brightness
            )

    # ── draw on frame ──────────────────────────────────────────────────────────
    bx1, by1 = int(smooth_x1), int(smooth_y1)
    bx2, by2 = int(smooth_x2), int(smooth_y2)

    if detected and bx2 > bx1 and by2 > by1:
        pulse      = int(abs(np.sin(now * 3)) * 40)
        border_col = (0, 200 + pulse, 50 + pulse)

        cv2.rectangle(frame, (bx1, by1), (bx2, by2), border_col, 2)
        cv2.rectangle(frame, (bx1, by1 - 24), (bx2, by1), border_col, -1)
        engine_lbl = "YOLO" if USE_YOLO else ("HOG" if hog_ok else "MOG2")
        cv2.putText(frame, f"{engine_lbl} {int(conf*100)}%",
                    (bx1 + 4, by1 - 7), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 0), 1)

        clen = 14
        for (cx2, cy2, dx, dy) in [(bx1,by1,1,1),(bx2,by1,-1,1),(bx1,by2,1,-1),(bx2,by2,-1,-1)]:
            cv2.line(frame, (cx2, cy2), (cx2 + dx*clen, cy2), (0, 255, 0), 3)
            cv2.line(frame, (cx2, cy2), (cx2, cy2 + dy*clen), (0, 255, 0), 3)

        bar_h       = 20
        alpha_bright = brightness / 255.0
        cv2.rectangle(frame, (0, h - bar_h), (w, h), (15, 15, 15), -1)
        col = tuple(int(c * alpha_bright) for c in act_col)
        for i in last_active_leds:
            lx  = int(i / led_count * w)
            lw2 = max(2, int(w / led_count) - 1)
            cv2.rectangle(frame, (lx, h - bar_h + 2), (lx + lw2, h - 3), col, -1)
    else:
        cv2.rectangle(frame, (0, h - 20), (w, h), (15, 15, 15), -1)

    _person_w = (smooth_x2 - smooth_x1) / w if w > 0 else 0.0

    with det_lock:
        detection_state.update({
            "detected":    detected,
            "bbox":        [bx1, by1, bx2, by2] if detected else None,
            "active_leds": last_active_leds if detected else [],
            "confidence":  round(conf, 3),
            "fps":         cur_fps,
            "use_yolo":    USE_YOLO,
            "hold":        hold_counter,
            "cx":          round(smooth_cx, 4),
            "person_w":    round(_person_w, 4),
        })
    return frame


# ── HOG background thread (4 Hz) ──────────────────────────────────────────────
def hog_confirm_thread():
    global _hog_confirmed, _hog_bbox, _hog_conf, _hog_confirm_ts
    while is_running:
        with raw_lock:
            frame = latest_raw
        if frame is None:
            time.sleep(0.1)
            continue

        h, w  = frame.shape[:2]
        scale = 0.5
        small = cv2.resize(frame, (int(w * scale), int(h * scale)))

        boxes_hog, weights = hog.detectMultiScale(
            small, winStride=(8, 8), padding=(8, 8),
            scale=1.1, hitThreshold=0.6, useMeanshiftGrouping=False
        )

        best_box  = None
        best_conf = 0.0
        if len(boxes_hog) > 0 and len(weights) > 0:
            boxes_nms, weights_nms = nms_boxes(
                boxes_hog.tolist(), weights.tolist(), overlap_thresh=0.4
            )
            for i, (bx, by, bw_h, bh_h) in enumerate(boxes_nms):
                rx, ry = int(bx / scale), int(by / scale)
                rw, rh = int(bw_h / scale), int(bh_h / scale)
                if validate_box(rx, ry, rw, rh, w, h):
                    nc = min(1.0, max(0.0, (float(weights_nms[i]) - 0.3) / 1.5))
                    if nc > best_conf:
                        best_conf = nc
                        best_box  = (rx, ry, rx + rw, ry + rh)

        with _hog_state_lock:
            _hog_confirmed  = best_box is not None
            _hog_bbox       = best_box
            _hog_conf       = best_conf
            if best_box:
                _hog_confirm_ts = time.time()

        time.sleep(0.25)   # 4 Hz


# ── camera thread ──────────────────────────────────────────────────────────────
def _open_camera(idx):
    """Try to open a camera by index; return configured VideoCapture or None."""
    try:
        c = cv2.VideoCapture(idx, cv2.CAP_V4L2)
        if c.isOpened():
            c.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
            c.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
            c.set(cv2.CAP_PROP_BUFFERSIZE, 1)
            c.set(cv2.CAP_PROP_FPS, 30)
            return c
        c.release()
    except Exception:
        pass
    return None

def cam_thread():
    global latest_raw, is_running, _cam1_dev
    cam = None

    # Initial open: use configured index, otherwise auto-detect
    cfg0 = load_cfg()
    configured = cfg0.get('cam1_index', -1)
    candidates = ([configured] if configured >= 0 else []) + [0, 1, 2, 4]
    for idx in candidates:
        cam = _open_camera(idx)
        if cam:
            _cam1_dev = idx
            print(f"Camera at /dev/video{idx}")
            break

    while is_running:
        if check_thermal() == 3:
            time.sleep(1)
            continue

        # Hot-swap: if cam1_index changed in config, switch to new camera
        cfg0 = load_cfg()
        wanted = cfg0.get('cam1_index', -1)
        if wanted >= 0 and wanted != _cam1_dev:
            new_cam = _open_camera(wanted)
            if new_cam:
                if cam:
                    cam.release()
                cam = new_cam
                _cam1_dev = wanted
                print(f"Switched to camera /dev/video{wanted}")

        if not cam or not cam.isOpened():
            time.sleep(0.5)
            continue

        ret, frame = cam.read()
        if not ret:
            time.sleep(0.03)
            continue

        if cfg0.get('flip_h', True):
            frame = cv2.flip(frame, 1)
        if cfg0.get('flip_v', False):
            frame = cv2.flip(frame, 0)

        with raw_lock:
            latest_raw = frame
        _new_frame_ev.set()   # wake detect_thread

    if cam:
        cam.release()


# ── cam2 thread ────────────────────────────────────────────────────────────────
def cam2_thread():
    global _cam2_frame, _cam2_dev, is_running
    cam      = None
    last_idx = -999   # sentinel so first loop always checks

    while is_running:
        cfg0   = load_cfg()
        wanted = cfg0.get('cam2_index', -1)

        # Switch camera if index changed
        if wanted != last_idx:
            if cam:
                cam.release()
                cam = None
            _cam2_dev = None
            last_idx  = wanted
            if wanted >= 0:
                cam = _open_camera(wanted)
                if cam:
                    _cam2_dev = wanted
                    print(f"Cam2 opened at /dev/video{wanted}")

        if not cam or not cam.isOpened() or wanted < 0:
            with _cam2_frame_lock:
                _cam2_frame = None
            time.sleep(0.5)
            continue

        ret, frame = cam.read()
        if not ret:
            time.sleep(0.05)
            continue

        cfg0 = load_cfg()
        if cfg0.get('cam2_flip_h', False):
            frame = cv2.flip(frame, 1)
        if cfg0.get('cam2_flip_v', False):
            frame = cv2.flip(frame, 0)

        with _cam2_frame_lock:
            _cam2_frame = frame

        time.sleep(0.033)   # ~30 fps

    if cam:
        cam.release()


# ── detection thread ───────────────────────────────────────────────────────────
def detect_thread():
    """Process each new frame exactly once — sleeps until cam_thread signals."""
    global current_frame, is_running
    while is_running:
        fired = _new_frame_ev.wait(timeout=0.5)
        if not fired:
            continue
        _new_frame_ev.clear()

        with raw_lock:
            frame = latest_raw
        if frame is None:
            continue
        try:
            cfg = load_cfg()
            out = detect_person(frame.copy(), cfg)
        except Exception as e:
            print(f"Detection error: {e}")
            continue
        with frame_lock:
            current_frame = out


# ── MJPEG stream ───────────────────────────────────────────────────────────────
def gen_frames(is_remote=False):
    while is_running:
        if not _stream_remote_enabled:
            time.sleep(0.5)
            continue
        with frame_lock:
            if current_frame is None:
                time.sleep(0.05)
                continue
            frame = current_frame.copy()
        _, buf = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, 82])
        yield (b'--frame\r\nContent-Type: image/jpeg\r\n\r\n' + buf.tobytes() + b'\r\n')
        time.sleep(0.025)


# ── Flask routes ───────────────────────────────────────────────────────────────
@app.route('/')
def index():
    return render_template('index.html', config=load_cfg())

@app.route('/api/config', methods=['GET'])
def get_cfg():
    return jsonify(load_cfg())

@app.route('/api/config', methods=['POST'])
def upd_cfg():
    d = request.get_json()
    c = load_cfg()
    allowed = set(DEFAULT.keys())
    for k, v in d.items():
        if k in allowed:
            c[k] = v
    save_cfg(c)
    return jsonify({'status': 'ok', 'config': c})

@app.route('/api/detection')
def get_det():
    with det_lock:
        return jsonify(detection_state)

@app.route('/api/test-led', methods=['POST'])
def test_led():
    d   = request.get_json() or {}
    cfg = load_cfg()
    led_count  = int(cfg.get('led_count', 50))
    color      = d.get('color', cfg.get('active_color', '#ff6b6b'))
    idle       = cfg.get('idle_color', '#1a1a2e')
    brightness = int(cfg.get('brightness', 200))

    def _flash():
        all_leds = set(range(led_count))
        for _ in range(3):
            update_led_strip(all_leds, led_count, color_hex=color, idle_hex=idle, brightness=brightness)
            time.sleep(0.2)
            update_led_strip(set(), led_count, color_hex=color, idle_hex=idle, brightness=brightness)
            time.sleep(0.2)

    threading.Thread(target=_flash, daemon=True).start()
    return jsonify({'status': 'ok', 'led_available': LED_AVAILABLE})

@app.route('/video_feed')
def video_feed():
    remote_addr = request.remote_addr or ''
    is_remote   = remote_addr not in ('127.0.0.1', '::1', 'localhost', '')
    return Response(gen_frames(is_remote=is_remote),
                    mimetype='multipart/x-mixed-replace; boundary=frame')

@app.route('/video_feed2')
def video_feed2():
    def gen2():
        while is_running:
            with _cam2_frame_lock:
                frame = _cam2_frame
            if frame is None:
                time.sleep(0.1)
                continue
            _, buf = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, 75])
            yield (b'--frame\r\nContent-Type: image/jpeg\r\n\r\n' + buf.tobytes() + b'\r\n')
            time.sleep(0.033)
    return Response(gen2(), mimetype='multipart/x-mixed-replace; boundary=frame')

@app.route('/api/thermal')
def get_thermal():
    try:
        with open("/sys/class/thermal/thermal_zone0/temp") as tf:
            temp_c = int(tf.read().strip()) / 1000.0
        return jsonify({"temp_c": round(temp_c, 1), "throttle": _thermal_throttle})
    except Exception:
        return jsonify({"temp_c": -1, "throttle": 1})

@app.route('/api/toggle-stream', methods=['POST'])
def toggle_stream():
    global _stream_remote_enabled
    _stream_remote_enabled = not _stream_remote_enabled
    print(f"Remote streaming: {'ON' if _stream_remote_enabled else 'OFF'}")
    return jsonify({'stream_enabled': _stream_remote_enabled})

@app.route('/api/cmd', methods=['POST'])
def exec_cmd():
    try:
        cmd = request.json.get('cmd', '')
        if not cmd:
            return {'error': 'No command provided'}, 400
        allowed = ['vcgencmd', 'df', 'free', 'ps', 'systemctl', 'uptime', 'cat /proc/cpuinfo']
        if not any(cmd.startswith(a) for a in allowed):
            return {'error': 'Command not allowed'}, 403
        result = subprocess.check_output(cmd, shell=True, text=True, timeout=10)
        return {'result': result.strip(), 'status': 'ok'}
    except subprocess.TimeoutExpired:
        return {'error': 'Command timeout'}, 500
    except Exception as e:
        return {'error': str(e)}, 500

@app.route('/api/shutdown', methods=['POST'])
def shutdown():
    def _stop():
        time.sleep(0.4)
        os.kill(os.getpid(), __import__('signal').SIGTERM)
    threading.Thread(target=_stop, daemon=True).start()
    return jsonify({'status': 'shutting_down'})

@app.route('/api/status')
def status():
    cfg0 = load_cfg()
    return jsonify({
        'camera_active':  current_frame is not None,
        'use_yolo':       USE_YOLO,
        'stream_enabled': _stream_remote_enabled,
        'cam1_dev':       _cam1_dev,
        'cam2_active':    _cam2_dev is not None,
        'cam2_index':     cfg0.get('cam2_index', -1),
    })

@app.route('/api/scan-cameras')
def scan_cameras():
    import glob as _glob
    cfg0 = load_cfg()
    cameras = []

    # Words in sysfs device name that mean it is NOT a real camera
    # (RPi has many /dev/video* nodes for codecs, ISP, etc.)
    _SKIP = {'codec', 'isp', 'unicam', 'bcm2835', 'rpivid',
             'hevc', 'h264', 'mpeg', 'jpeg', 'still', 'image', 'capture',
             'stateless', 'output', 'mem2mem'}

    for dev in sorted(_glob.glob('/dev/video*')):
        num = dev.replace('/dev/video', '')
        if not num.isdigit():
            continue
        idx = int(num)

        # Read device name from sysfs — instant, no camera open needed
        try:
            sysname = open(f'/sys/class/video4linux/video{idx}/name').read().strip()
        except Exception:
            sysname = f'Camera {idx}'

        # Skip non-camera devices by name
        if any(w in sysname.lower() for w in _SKIP):
            continue

        # Already held open by cam_thread or cam2_thread — include directly, never re-open
        if idx == _cam1_dev or idx == _cam2_dev:
            cameras.append({'index': idx, 'dev': dev, 'name': sysname, 'active': True})
            continue

        # Try to open with a hard 2-second timeout (runs in a worker thread)
        result = [False]
        def _try(i=idx, r=result):
            try:
                cap = cv2.VideoCapture(i, cv2.CAP_V4L2)
                r[0] = cap.isOpened()
                cap.release()
            except Exception:
                pass
        t = threading.Thread(target=_try, daemon=True)
        t.start()
        t.join(timeout=2.0)

        if result[0]:
            cameras.append({'index': idx, 'dev': dev, 'name': sysname, 'active': False})

    # ── Pi power/throttle status ──────────────────────────────────────────────
    throttled = None
    try:
        out = subprocess.check_output(['vcgencmd', 'get_throttled'], text=True, timeout=2)
        throttled = out.strip().split('=')[-1]   # e.g. '0x0' or '0x50005'
    except Exception:
        pass

    # ── Pi core voltage ───────────────────────────────────────────────────────
    volts = None
    try:
        out = subprocess.check_output(['vcgencmd', 'measure_volts', 'core'], text=True, timeout=2)
        volts = out.strip().split('=')[-1]       # e.g. '1.2063V'
    except Exception:
        pass

    cam1 = cfg0.get('cam1_index', _cam1_dev if _cam1_dev is not None else -1)
    cam2 = cfg0.get('cam2_index', -1)
    return jsonify({
        'cameras':  cameras,
        'cam1':     cam1,
        'cam2':     cam2,
        'throttled': throttled,
        'volts':    volts,
    })

if __name__ == '__main__':
    cfg0   = load_cfg()
    strips = cfg0.get('strips', [])
    if strips:
        s0 = strips[0]
        init_led_strip(
            gpio_pin=s0.get('gpio_pin', 18),
            led_count=s0.get('led_count', 50),
            brightness=int(cfg0.get('brightness', 200))
        )
    else:
        init_led_strip()

    threading.Thread(target=led_thread,         daemon=True).start()
    threading.Thread(target=cam_thread,         daemon=True).start()
    threading.Thread(target=cam2_thread,        daemon=True).start()
    threading.Thread(target=detect_thread,      daemon=True).start()
    threading.Thread(target=hog_confirm_thread, daemon=True).start()
    print("Starting on http://0.0.0.0:5000")
    app.run(host='0.0.0.0', port=5000, debug=False, threaded=True)
