#!/usr/bin/env python3
"""
Human Detect LED — Full QA + Stress Test Suite
Covers: API correctness, config validation, detection quality,
        remote-mode, throughput, concurrency, memory, system health.
Output: console + qa_report.txt
"""
import sys, json, time, socket, os, threading, subprocess
import urllib.request, urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

# ── args ──────────────────────────────────────────────────────────────────────
import argparse
ap = argparse.ArgumentParser()
ap.add_argument('--host',    default='localhost')
ap.add_argument('--port',    default=5000, type=int)
ap.add_argument('--timeout', default=5,    type=int)
args = ap.parse_args()

BASE     = f"http://{args.host}:{args.port}"
IS_LOCAL = args.host in ('localhost', '127.0.0.1')
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_FILE = os.path.join(BASE_DIR, 'qa_report.txt')

# ── colours ───────────────────────────────────────────────────────────────────
G='\033[92m'; R='\033[91m'; Y='\033[93m'; C='\033[96m'; B='\033[1m'; X='\033[0m'

# ── state ─────────────────────────────────────────────────────────────────────
passed = failed = warned = 0
results = []
log_lines = []

def _log(line):
    log_lines.append(line)

def hdr(t):
    s = f"\n{'─'*56}\n  {t}\n{'─'*56}"
    print(C + B + s + X)
    _log(s)

def ok(name, detail=''):
    global passed
    passed += 1
    d = f'  ({detail})' if detail else ''
    line = f"  PASS  {name}{d}"
    print(f"  {G}✓{X} {name}" + (f"  {Y}({detail}){X}" if detail else ''))
    _log(line); results.append(('PASS', name, detail))

def fail(name, detail=''):
    global failed
    failed += 1
    d = f'  ({detail})' if detail else ''
    line = f"  FAIL  {name}{d}"
    print(f"  {R}✗{X} {name}" + (f"  {R}({detail}){X}" if detail else ''))
    _log(line); results.append(('FAIL', name, detail))

def warn(name, detail=''):
    global warned
    warned += 1
    d = f'  ({detail})' if detail else ''
    line = f"  WARN  {name}{d}"
    print(f"  {Y}⚠{X} {name}" + (f"  {Y}({detail}){X}" if detail else ''))
    _log(line); results.append(('WARN', name, detail))

def info(msg):
    print(f"    {C}{msg}{X}")
    _log(f"    {msg}")

# ── HTTP helpers ──────────────────────────────────────────────────────────────
def get(path, timeout=None):
    t = timeout or args.timeout
    try:
        with urllib.request.urlopen(BASE + path, timeout=t) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, {}
    except Exception as e:
        return None, str(e)

def post(path, data=None, timeout=None):
    t   = timeout or args.timeout
    body = json.dumps(data or {}).encode()
    req  = urllib.request.Request(
        BASE + path, data=body,
        headers={'Content-Type': 'application/json'}, method='POST'
    )
    try:
        with urllib.request.urlopen(req, timeout=t) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, {}
    except Exception as e:
        return None, str(e)

def timed_get(path):
    """Returns (status, data, elapsed_ms)."""
    t0 = time.perf_counter()
    st, d = get(path)
    return st, d, (time.perf_counter() - t0) * 1000

def get_pid():
    try:
        r = subprocess.run(['pgrep', '-f', 'run.py'], capture_output=True, text=True)
        pids = r.stdout.strip().split()
        if pids:
            return int(pids[0])
        r2 = subprocess.run(['pgrep', '-f', 'calibration_app.py'], capture_output=True, text=True)
        pids2 = r2.stdout.strip().split()
        return int(pids2[0]) if pids2 else None
    except Exception:
        return None

def get_rss_mb(pid):
    try:
        with open(f'/proc/{pid}/status') as f:
            for line in f:
                if line.startswith('VmRSS:'):
                    return int(line.split()[1]) / 1024
    except Exception:
        pass
    return None

def cpu_percent(pid, interval=1.0):
    """Rough CPU% for a pid over interval seconds."""
    try:
        def read_cpu(p):
            with open(f'/proc/{p}/stat') as f:
                vals = f.read().split()
            utime, stime = int(vals[13]), int(vals[14])
            with open('/proc/uptime') as f:
                uptime = float(f.read().split()[0])
            return utime + stime, uptime
        a, ua = read_cpu(pid)
        time.sleep(interval)
        b, ub = read_cpu(pid)
        hz = os.sysconf('SC_CLK_TCK')
        elapsed = (ub - ua) * hz
        if elapsed == 0:
            return 0.0
        return round((b - a) / elapsed * 100, 1)
    except Exception:
        return None

# ══════════════════════════════════════════════════════════════════════════════
#  1. CONNECTIVITY
# ══════════════════════════════════════════════════════════════════════════════
hdr("1 · Connectivity")
try:
    s = socket.create_connection((args.host, args.port), timeout=3); s.close()
    ok(f"Flask port {args.port} reachable")
except Exception as e:
    fail(f"Flask port {args.port} unreachable", str(e))
    print(f"\n{R}Cannot reach the service — aborting test.{X}\n")
    sys.exit(1)

# Check which app is running
_, stat0 = get('/api/status')
is_run_py = 'remote' in stat0
info(f"App: {'run.py' if is_run_py else 'calibration_app.py'}")

# ══════════════════════════════════════════════════════════════════════════════
#  2. CORE API CORRECTNESS
# ══════════════════════════════════════════════════════════════════════════════
hdr("2 · Core API Correctness")

# /api/detection
st, det = get('/api/detection')
if st == 200:
    ok("GET /api/detection → 200")
else:
    fail("GET /api/detection failed", f"status={st}")
    det = {}

required_fields = ['detected', 'confidence', 'fps', 'active_leds',
                   'cx', 'person_w', 'use_yolo', 'hold']
for f_ in required_fields:
    if f_ in det:
        ok(f"  field '{f_}' present", str(det[f_])[:25])
    else:
        fail(f"  field '{f_}' missing from /api/detection")

fps = det.get('fps', 0)
if fps > 5:
    ok(f"FPS healthy", f"{fps} fps")
elif fps > 0:
    warn(f"FPS low", f"{fps} fps — may be thermal throttled")
else:
    fail("FPS is 0 — camera or detect thread not running")

cx = det.get('cx', -1)
if 0.0 <= cx <= 1.0:
    ok("cx in valid range [0,1]", str(cx))
else:
    fail("cx out of range", str(cx))

pw = det.get('person_w', -1)
if 0.0 <= pw <= 1.0:
    ok("person_w in valid range [0,1]", str(pw))
else:
    fail("person_w out of range", str(pw))

# /api/config GET
st, cfg = get('/api/config')
if st == 200:
    ok("GET /api/config → 200")
else:
    fail("GET /api/config failed", f"status={st}")
    cfg = {}

config_keys = ['brightness', 'smoothing', 'led_count', 'active_leds_count',
               'active_color', 'idle_color', 'confidence_threshold',
               'led_spread_mode', 'strips']
for k in config_keys:
    if k in cfg:
        ok(f"  config key '{k}'", str(cfg[k])[:30])
    else:
        warn(f"  config key '{k}' missing")

# /api/status
st, stat = get('/api/status')
if st == 200:
    ok("GET /api/status → 200")
else:
    fail("GET /api/status failed", f"status={st}")

if stat.get('camera_active'):
    ok("Camera reported active")
else:
    fail("Camera NOT active", str(stat))

# /api/thermal
st, therm = get('/api/thermal')
if st == 200:
    ok("GET /api/thermal → 200")
else:
    fail("GET /api/thermal failed", f"status={st}")
    therm = {}

temp = therm.get('temp_c', -1)
if temp > 0:
    ok("Temperature readable", f"{temp}°C")
    if temp < 70:
        ok("Temperature safe", f"< 70°C")
    elif temp < 80:
        warn("Temperature elevated", f"{temp}°C")
    else:
        fail("Temperature critical", f"{temp}°C ≥ 80°C")
else:
    fail("Temperature unreadable")

thr = therm.get('throttle', -1)
if thr == 0:
    ok("No CPU throttling")
elif thr == 1:
    warn("Throttle level 1", f"{temp}°C")
elif thr >= 2:
    fail(f"Throttle level {thr} — performance degraded", f"{temp}°C")

# ══════════════════════════════════════════════════════════════════════════════
#  3. CONFIG VALIDATION
# ══════════════════════════════════════════════════════════════════════════════
hdr("3 · Config Validation")

orig_brightness = cfg.get('brightness', 200)

# Valid round-trip
st, r = post('/api/config', {'brightness': 180})
if st == 200 and r.get('config', {}).get('brightness') == 180:
    ok("POST brightness=180 accepted and echoed back")
else:
    fail("POST config round-trip failed", f"st={st} got={r.get('config',{}).get('brightness')}")

# Restore
post('/api/config', {'brightness': orig_brightness})
st, r2 = get('/api/config')
if r2.get('brightness') == orig_brightness:
    ok("Config restored to original value")
else:
    warn("Config restore mismatch", f"expected {orig_brightness} got {r2.get('brightness')}")

# Unknown key should be silently ignored (not crash)
st, r3 = post('/api/config', {'__invalid_key__': 999})
if st == 200:
    ok("Unknown config key silently ignored")
    if '__invalid_key__' not in r3.get('config', {}):
        ok("Unknown key not persisted")
    else:
        fail("Unknown key was persisted — no validation")
else:
    fail("POST with unknown key crashed", f"st={st}")

# Color format
st, r4 = post('/api/config', {'active_color': '#00ff88'})
_, r4g = get('/api/config')
if r4g.get('active_color') == '#00ff88':
    ok("Color '#00ff88' stored correctly")
    post('/api/config', {'active_color': cfg.get('active_color', '#ff6b6b')})
else:
    warn("Color not persisted correctly", str(r4g.get('active_color')))

# LED count boundaries
for val, label in [(1, 'min=1'), (300, 'max=300')]:
    st, _ = post('/api/config', {'led_count': val})
    if st == 200:
        ok(f"LED count boundary accepted", label)
    else:
        warn(f"LED count {label} rejected", f"st={st}")
post('/api/config', {'led_count': cfg.get('led_count', 50)})

# ══════════════════════════════════════════════════════════════════════════════
#  4. REMOTE MODE  (run.py specific)
# ══════════════════════════════════════════════════════════════════════════════
hdr("4 · Remote Mode  (/api/remote, /api/toggle-stream)")

if is_run_py:
    # Check initial remote state
    _, s0 = get('/api/status')
    initial_remote = s0.get('remote', False)
    info(f"Remote currently: {initial_remote}")

    # Toggle on
    st, r = post('/api/remote', {'enabled': True})
    if st == 200:
        ok("POST /api/remote {enabled:true} → 200")
        if r.get('remote') is True:
            ok("Remote state confirmed ON in response")
        else:
            fail("Response did not confirm remote=true", str(r))
    else:
        fail("POST /api/remote failed", f"st={st}")

    time.sleep(0.5)

    # Video feed should now stream (not 204)
    try:
        with urllib.request.urlopen(BASE + '/video_feed', timeout=4) as rv:
            ct = rv.headers.get('Content-Type', '')
            chunk = rv.read(256)
            if 'multipart' in ct and chunk:
                ok("Video feed streams when remote=ON")
            else:
                warn("Video feed odd response when remote=ON", ct)
    except Exception as e:
        fail("Video feed error when remote=ON", str(e)[:50])

    # Toggle off
    st, r = post('/api/remote', {'enabled': False})
    if st == 200 and r.get('remote') is False:
        ok("POST /api/remote {enabled:false} → remote OFF confirmed")
    else:
        fail("Could not disable remote mode", str(r))

    time.sleep(0.5)

    # Video feed should now return 204
    try:
        with urllib.request.urlopen(BASE + '/video_feed', timeout=3) as rv:
            if rv.status == 204:
                ok("Video feed returns 204 when remote=OFF (no encoding)")
            else:
                warn("Expected 204 when remote=OFF", f"got {rv.status}")
    except urllib.error.HTTPError as e:
        if e.code == 204:
            ok("Video feed returns 204 when remote=OFF (no encoding)")
        else:
            warn("Unexpected HTTP error from video feed", f"{e.code}")
    except Exception as e:
        warn("Video feed check inconclusive", str(e)[:50])

    # Restore original state
    post('/api/remote', {'enabled': initial_remote})

    # /api/toggle-stream compatibility
    _, s1 = get('/api/status')
    st, _ = post('/api/toggle-stream')
    _, s2 = get('/api/status')
    if st == 200 and s2.get('stream_enabled') != s1.get('stream_enabled'):
        ok("/api/toggle-stream toggles remote state")
        post('/api/toggle-stream')  # restore
    else:
        warn("/api/toggle-stream may not work", f"st={st}")

else:
    warn("run.py not detected — remote mode tests skipped")
    # Test calibration_app stream toggle
    st, _ = post('/api/toggle-stream')
    if st == 200:
        ok("/api/toggle-stream responds 200")
        post('/api/toggle-stream')  # restore
    else:
        warn("/api/toggle-stream failed", f"st={st}")

# ══════════════════════════════════════════════════════════════════════════════
#  5. TEST-LED ENDPOINT
# ══════════════════════════════════════════════════════════════════════════════
hdr("5 · LED Test Endpoint  (/api/test-led)")
st, r = post('/api/test-led', {})
if st == 200:
    ok("POST /api/test-led → 200")
    if 'led_available' in r:
        ok(f"led_available field present", str(r['led_available']))
    else:
        warn("led_available field missing from response")
else:
    fail("POST /api/test-led failed", f"st={st}")

# ══════════════════════════════════════════════════════════════════════════════
#  6. RESPONSE LATENCY BENCHMARK
# ══════════════════════════════════════════════════════════════════════════════
hdr("6 · API Response Latency  (50 sequential calls)")

N = 50
latencies = []
errors    = 0

for _ in range(N):
    st, _, ms = timed_get('/api/detection')
    if st == 200:
        latencies.append(ms)
    else:
        errors += 1

if latencies:
    latencies.sort()
    avg = sum(latencies) / len(latencies)
    p50 = latencies[int(len(latencies) * 0.50)]
    p95 = latencies[int(len(latencies) * 0.95)]
    p99 = latencies[min(int(len(latencies) * 0.99), len(latencies)-1)]
    mx  = latencies[-1]

    info(f"avg={avg:.1f}ms  p50={p50:.1f}ms  p95={p95:.1f}ms  p99={p99:.1f}ms  max={mx:.1f}ms  errors={errors}")

    if avg < 30:
        ok("Average latency excellent", f"{avg:.1f}ms < 30ms")
    elif avg < 80:
        ok("Average latency acceptable", f"{avg:.1f}ms < 80ms")
    else:
        warn("Average latency high", f"{avg:.1f}ms")

    if p95 < 100:
        ok("p95 latency good", f"{p95:.1f}ms < 100ms")
    elif p95 < 300:
        warn("p95 latency elevated", f"{p95:.1f}ms")
    else:
        fail("p95 latency too high", f"{p95:.1f}ms ≥ 300ms")

    if errors == 0:
        ok("Zero errors in 50 sequential calls")
    else:
        fail(f"{errors} errors in {N} sequential calls")
else:
    fail("All latency calls failed")

# ══════════════════════════════════════════════════════════════════════════════
#  7. CONCURRENT REQUESTS STRESS TEST
# ══════════════════════════════════════════════════════════════════════════════
hdr("7 · Concurrent Requests Stress  (20 threads × 5 calls)")

THREADS    = 20
CALLS_EACH = 5
c_errors   = 0
c_times    = []
c_lock     = threading.Lock()

def _worker(_):
    times = []; errs = 0
    for _ in range(CALLS_EACH):
        st, _, ms = timed_get('/api/detection')
        if st == 200:
            times.append(ms)
        else:
            errs += 1
    with c_lock:
        c_times.extend(times)
        global c_errors
        c_errors += errs

with ThreadPoolExecutor(max_workers=THREADS) as ex:
    list(ex.map(_worker, range(THREADS)))

total_calls = THREADS * CALLS_EACH
ok_calls    = len(c_times)
if c_times:
    c_avg = sum(c_times) / len(c_times)
    c_max = max(c_times)
    info(f"{ok_calls}/{total_calls} succeeded  avg={c_avg:.1f}ms  max={c_max:.1f}ms  errors={c_errors}")

    if c_errors == 0:
        ok(f"No errors under {THREADS}-thread concurrency")
    elif c_errors <= total_calls * 0.05:
        warn(f"<5% error rate under concurrency", f"{c_errors} errors")
    else:
        fail(f"High error rate under concurrency", f"{c_errors}/{total_calls}")

    if c_avg < 200:
        ok("Avg latency under concurrency acceptable", f"{c_avg:.1f}ms")
    else:
        warn("Avg latency high under concurrency", f"{c_avg:.1f}ms")
else:
    fail("All concurrent calls failed")

# ══════════════════════════════════════════════════════════════════════════════
#  8. RAPID CONFIG SPAM
# ══════════════════════════════════════════════════════════════════════════════
hdr("8 · Rapid Config Write Stress  (30 POSTs in quick succession)")

spam_errors = 0
values      = list(range(100, 230, 4))[:30]   # 30 different brightness values

for v in values:
    st, _ = post('/api/config', {'brightness': v}, timeout=3)
    if st != 200:
        spam_errors += 1

if spam_errors == 0:
    ok("30 rapid config POSTs — all accepted")
else:
    warn(f"{spam_errors}/30 config POSTs failed under spam")

# Verify final value and file integrity
time.sleep(0.3)
st, final_cfg = get('/api/config')
if st == 200:
    ok("Config readable after spam test")
else:
    fail("Config unreadable after spam test")

try:
    with open(os.path.join(BASE_DIR, 'config.json')) as fj:
        json.load(fj)
    ok("config.json is valid JSON after spam test")
except Exception as e:
    fail("config.json corrupted after spam test", str(e))

# Restore brightness
post('/api/config', {'brightness': orig_brightness})

# ══════════════════════════════════════════════════════════════════════════════
#  9. DETECTION CONSISTENCY
# ══════════════════════════════════════════════════════════════════════════════
hdr("9 · Detection State Consistency  (20 polls × 200ms)")

cx_values   = []
fps_values  = []
type_errors = []

for i in range(20):
    st, d = get('/api/detection')
    if st != 200:
        type_errors.append(f"poll {i}: st={st}")
        continue
    # type checks
    if not isinstance(d.get('detected'), bool):
        type_errors.append(f"poll {i}: 'detected' not bool")
    if not isinstance(d.get('fps'), (int, float)):
        type_errors.append(f"poll {i}: 'fps' not numeric")
    if not isinstance(d.get('active_leds'), list):
        type_errors.append(f"poll {i}: 'active_leds' not list")
    if d.get('detected') and d.get('active_leds'):
        led_count = final_cfg.get('led_count', 50)
        bad = [l for l in d['active_leds'] if not (0 <= l < led_count)]
        if bad:
            type_errors.append(f"poll {i}: out-of-range LEDs {bad}")
    cx_values.append(d.get('cx', 0))
    fps_values.append(d.get('fps', 0))
    time.sleep(0.2)

if not type_errors:
    ok("All 20 detection polls returned valid types")
else:
    for e in type_errors[:5]:
        fail("Type error in detection response", e)

if cx_values:
    if all(0.0 <= c <= 1.0 for c in cx_values):
        ok("cx always in [0,1] across 20 polls")
    else:
        fail("cx went out of range", str([c for c in cx_values if not 0<=c<=1]))

if fps_values:
    avg_fps = sum(fps_values) / len(fps_values)
    if avg_fps > 5:
        ok("FPS stable across 20 polls", f"avg={avg_fps:.1f}")
    else:
        warn("FPS consistently low", f"avg={avg_fps:.1f}")

# ══════════════════════════════════════════════════════════════════════════════
#  10. MEMORY + CPU UNDER LOAD
# ══════════════════════════════════════════════════════════════════════════════
hdr("10 · Memory & CPU Under Load")
pid = get_pid()
if pid and IS_LOCAL:
    rss_before = get_rss_mb(pid)
    info(f"Process PID: {pid}  |  RSS before: {rss_before:.1f} MB")

    # Generate load: 100 detection requests
    for _ in range(100):
        get('/api/detection')

    time.sleep(1)
    rss_after = get_rss_mb(pid)
    growth = rss_after - rss_before if rss_before and rss_after else None

    info(f"RSS after 100 requests: {rss_after:.1f} MB  (growth: {growth:+.1f} MB)")

    if growth is not None:
        if growth < 5:
            ok("Memory growth acceptable under load", f"{growth:+.1f} MB")
        elif growth < 20:
            warn("Memory grew noticeably under load", f"{growth:+.1f} MB")
        else:
            fail("Possible memory leak", f"grew {growth:.1f} MB")

    # CPU reading
    info("Measuring CPU usage over 2 seconds...")
    cpu = cpu_percent(pid, interval=2.0)
    if cpu is not None:
        info(f"CPU usage (idle): {cpu}%")
        if cpu < 30:
            ok("CPU usage healthy at idle", f"{cpu}%")
        elif cpu < 60:
            warn("CPU usage moderate", f"{cpu}%")
        else:
            fail("CPU usage high at idle", f"{cpu}%")
    else:
        warn("Could not measure CPU usage")
else:
    warn("Skipping memory/CPU test", "not local or PID not found")

# ══════════════════════════════════════════════════════════════════════════════
#  11. SYSTEM HEALTH  (local only)
# ══════════════════════════════════════════════════════════════════════════════
if IS_LOCAL:
    hdr("11 · System Health  (Pi-local)")

    # systemd service
    r = subprocess.run(['systemctl', 'is-active', 'human-led.service'],
                       capture_output=True, text=True)
    if r.stdout.strip() == 'active':
        ok("human-led.service is active")
    else:
        fail("human-led.service is not active", r.stdout.strip())

    # SD card free space
    try:
        st2 = os.statvfs('/')
        free_gb = st2.f_bavail * st2.f_frsize / 1e9
        if free_gb > 1:
            ok("Disk free space OK", f"{free_gb:.1f} GB free")
        elif free_gb > 0.2:
            warn("Disk space low", f"{free_gb:.1f} GB free")
        else:
            fail("Disk almost full", f"{free_gb:.2f} GB free")
    except Exception as e:
        warn("Disk check failed", str(e))

    # CPU clock
    try:
        r2 = subprocess.run(['vcgencmd', 'measure_clock', 'arm'],
                            capture_output=True, text=True, timeout=3)
        hz  = int(r2.stdout.strip().split('=')[1]) if '=' in r2.stdout else 0
        mhz = hz / 1_000_000
        if mhz >= 1500:
            ok("CPU clock healthy", f"{mhz:.0f} MHz")
        else:
            warn("CPU clock low (throttled?)", f"{mhz:.0f} MHz")
    except Exception:
        warn("vcgencmd not available")

    # RAM
    try:
        with open('/proc/meminfo') as fm:
            lines = fm.readlines()
        mem = {l.split(':')[0]: int(l.split()[1]) for l in lines if ':' in l}
        free_mb  = mem.get('MemAvailable', 0) / 1024
        total_mb = mem.get('MemTotal', 1) / 1024
        pct_used = 100 - (free_mb / total_mb * 100)
        info(f"RAM: {free_mb:.0f} MB free / {total_mb:.0f} MB total ({pct_used:.0f}% used)")
        if pct_used < 70:
            ok("RAM usage healthy", f"{pct_used:.0f}%")
        elif pct_used < 85:
            warn("RAM usage elevated", f"{pct_used:.0f}%")
        else:
            fail("RAM critically high", f"{pct_used:.0f}%")
    except Exception as e:
        warn("RAM check failed", str(e))

# ══════════════════════════════════════════════════════════════════════════════
#  12. FILE INTEGRITY
# ══════════════════════════════════════════════════════════════════════════════
hdr("12 · File Integrity")

files = {
    'run.py':            (True,  'production runner'),
    'calibration_app.py':(True,  'setup / calibration'),
    'desktop_app.py':    (True,  'desktop GUI'),
    'config.json':       (True,  'configuration'),
    'templates/index.html': (True, 'web UI template'),
    'static/manifest.json': (False,'PWA manifest'),
    'qa_test.py':        (False, 'original QA'),
}

for fname, (required, desc) in files.items():
    path = os.path.join(BASE_DIR, fname)
    if os.path.exists(path):
        size = os.path.getsize(path)
        ok(f"{fname}", f"{size} B — {desc}")
    elif required:
        fail(f"{fname} missing", desc)
    else:
        warn(f"{fname} missing (optional)", desc)

# Python syntax checks
for pyfile in ['run.py', 'calibration_app.py', 'desktop_app.py']:
    path = os.path.join(BASE_DIR, pyfile)
    if not os.path.exists(path):
        continue
    try:
        import ast
        with open(path) as f_:
            ast.parse(f_.read())
        ok(f"{pyfile} — valid Python syntax")
    except SyntaxError as e:
        fail(f"{pyfile} — syntax error", str(e))

# config.json validity
try:
    with open(os.path.join(BASE_DIR, 'config.json')) as fj:
        c = json.load(fj)
    ok("config.json valid JSON")
    required_cfg = ['brightness', 'led_count', 'active_color', 'idle_color', 'strips']
    for k in required_cfg:
        if k in c:
            ok(f"  config key '{k}'", str(c[k])[:30])
        else:
            fail(f"  config key '{k}' missing from config.json")
except Exception as e:
    fail("config.json invalid", str(e))

# Key feature checks in source
checks = [
    ('run.py',             'def set_remote',          'remote mode toggle'),
    ('run.py',             'def flash_signal',         'LED flash signal'),
    ('run.py',             'def init_button',          'GPIO button init'),
    ('run.py',             'hex_to_rgb',               'clean color conversion'),
    ('run.py',             'FADE_IN',                  'LED fade'),
    ('run.py',             'led_spread_mode',          'proportional spread mode'),
    ('calibration_app.py', 'def check_thermal',        'thermal throttle'),
    ('calibration_app.py', 'smooth_cx = (smooth_x1',  'fixed double-smooth'),
    ('calibration_app.py', 'centre_led = round',       'rounded LED centre'),
    ('desktop_app.py',     'class App',                'desktop app class'),
    ('desktop_app.py',     'toggle_remote',            'remote toggle button'),
    ('templates/index.html','toggleRunMode',           'run mode toggle JS'),
    ('templates/index.html','applyRunMode',            'run mode apply JS'),
]
for fname, token, desc in checks:
    path = os.path.join(BASE_DIR, fname)
    if not os.path.exists(path):
        continue
    found = token in open(path).read()
    if found:
        ok(f"  {fname}: {desc}")
    else:
        fail(f"  {fname}: '{token}' not found — {desc} may be broken")

# ══════════════════════════════════════════════════════════════════════════════
#  SUMMARY + CONCLUSIONS
# ══════════════════════════════════════════════════════════════════════════════
total = passed + failed + warned
hdr("SUMMARY")
print(f"  {G}PASS{X}  {passed}")
print(f"  {R}FAIL{X}  {failed}")
print(f"  {Y}WARN{X}  {warned}")
print(f"  {'─'*30}")
print(f"  Total  {total} checks\n")

_log(f"\nSUMMARY\nPASS {passed}  FAIL {failed}  WARN {warned}  TOTAL {total}")

# ── conclusions ───────────────────────────────────────────────────────────────
hdr("CONCLUSIONS & RECOMMENDATIONS")
conclusions = []

if failed == 0 and warned == 0:
    msg = "All checks passed — system fully healthy."
    print(f"  {G}{B}{msg}{X}")
    conclusions.append(msg)
elif failed == 0:
    msg = f"No failures, {warned} warning(s). System functional."
    print(f"  {Y}{B}{msg}{X}")
    conclusions.append(msg)
else:
    msg = f"{failed} failure(s) require attention."
    print(f"  {R}{B}{msg}{X}")
    conclusions.append(msg)

# Specific recommendations based on results
fail_names = [n for s, n, _ in results if s == 'FAIL']
warn_names = [n for s, n, _ in results if s == 'WARN']

if any('FPS' in n for n in fail_names + warn_names):
    r_ = "  → FPS issue: check camera connection and thermal throttle"
    print(Y + r_ + X); conclusions.append(r_)

if any('Temperature' in n or 'Throttle' in n for n in fail_names + warn_names):
    r_ = "  → Thermal: improve Pi cooling or reduce detection resolution"
    print(Y + r_ + X); conclusions.append(r_)

if any('memory' in n.lower() or 'leak' in n.lower() for n in fail_names):
    r_ = "  → Memory leak detected: restart service nightly via cron"
    print(Y + r_ + X); conclusions.append(r_)

if any('RAM' in n for n in fail_names + warn_names):
    r_ = "  → RAM high: close other processes or add swap"
    print(Y + r_ + X); conclusions.append(r_)

if any('latency' in n.lower() for n in fail_names + warn_names):
    r_ = "  → High API latency: Flask threaded mode may be contended — normal under load"
    print(Y + r_ + X); conclusions.append(r_)

if any('concurrent' in n.lower() for n in fail_names):
    r_ = "  → Concurrent request failures: expected on Pi — consider reducing poll rate"
    print(Y + r_ + X); conclusions.append(r_)

if any('config.json' in n for n in fail_names):
    r_ = "  → Config corruption risk: consider atomic write with temp file + rename"
    print(Y + r_ + X); conclusions.append(r_)

# ── write report ──────────────────────────────────────────────────────────────
ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
report_header = (
    f"Human Detect LED — QA Report\n"
    f"Generated: {ts}\n"
    f"Host: {args.host}:{args.port}\n"
    f"App: {'run.py' if is_run_py else 'calibration_app.py'}\n"
    f"{'='*56}\n"
)
with open(LOG_FILE, 'w') as lf:
    lf.write(report_header)
    for line in log_lines:
        # strip ANSI codes
        import re
        clean = re.sub(r'\033\[[0-9;]*m', '', line)
        lf.write(clean + '\n')
    lf.write('\nCONCLUSIONS\n')
    for c in conclusions:
        lf.write(c + '\n')

print(f"\n  Report saved → {LOG_FILE}\n")
sys.exit(0 if failed == 0 else 1)
