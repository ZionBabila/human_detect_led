#!/usr/bin/env python3
"""
QA Test Suite — Human Detect Lit LED v4
Run on the Pi: python3 qa_test.py
Run remotely:  python3 qa_test.py --host 10.100.102.239
"""
import sys, json, time, socket, argparse, subprocess, urllib.request, urllib.error, os

parser = argparse.ArgumentParser()
parser.add_argument('--host', default='localhost')
parser.add_argument('--port', default=5000, type=int)
parser.add_argument('--timeout', default=6, type=int)
parser.add_argument('--no-shutdown-test', action='store_true', help='Skip shutdown test')
args = parser.parse_args()

BASE = f"http://{args.host}:{args.port}"
IS_LOCAL = args.host in ('localhost', '127.0.0.1')

G='\033[92m'; R='\033[91m'; Y='\033[93m'; C='\033[96m'; B='\033[1m'; X='\033[0m'
p=w=f=0; results=[]

def hdr(t): print(f"\n{C}{B}{'─'*52}\n  {t}\n{'─'*52}{X}")
def ok(n,d=''): global p; p+=1; print(f"  {G}✓{X} {n}" + (f"  {Y}({d}){X}" if d else '')); results.append(('PASS',n,d))
def fail(n,d=''): global f; f+=1; print(f"  {R}✗{X} {n}" + (f"  {R}({d}){X}" if d else '')); results.append(('FAIL',n,d))
def warn(n,d=''): global w; w+=1; print(f"  {Y}⚠{X} {n}" + (f"  {Y}({d}){X}" if d else '')); results.append(('WARN',n,d))

def get(path):
    try:
        with urllib.request.urlopen(BASE+path, timeout=args.timeout) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e: return e.code, {}
    except Exception as e: return None, str(e)

def post(path, data={}):
    body = json.dumps(data).encode()
    req = urllib.request.Request(BASE+path, data=body, headers={'Content-Type':'application/json'}, method='POST')
    try:
        with urllib.request.urlopen(req, timeout=args.timeout) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e: return e.code, {}
    except Exception as e: return None, str(e)

# ── 1. Port reachability ──────────────────────────────────────────────────────
hdr("1 · Network & Port Reachability")
try:
    s = socket.create_connection((args.host, args.port), timeout=3); s.close()
    ok(f"Flask port {args.port} reachable")
except Exception as e: fail(f"Flask port {args.port} unreachable", str(e))

if IS_LOCAL:
    try:
        s = socket.create_connection(('localhost', 7681), timeout=3); s.close()
        ok("ttyd port 7681 reachable")
    except Exception as e: fail("ttyd port 7681 unreachable", str(e))

# ── 2. Detection API ──────────────────────────────────────────────────────────
hdr("2 · Detection API  (/api/detection)")
st, det = get('/api/detection')
if st == 200: ok("HTTP 200")
else: fail("Endpoint error", f"status={st}"); det = {}

fps = det.get('fps', -1)
if fps > 0: ok("FPS > 0", f"fps={fps}")
elif fps == 0: fail("FPS is 0 — camera thread not running")
else: warn("FPS field missing", str(fps))

for field in ('detected','confidence','bbox','active_leds','hold_frames','person_w'):
    if field in det: ok(f"Field '{field}' present", str(det[field])[:30])
    else: warn(f"Field '{field}' missing")

# ── 3. Config API ─────────────────────────────────────────────────────────────
hdr("3 · Config API  (/api/config GET + POST)")
st, cfg = get('/api/config')
if st == 200: ok("GET returns 200")
else: fail("GET failed", str(st)); cfg = {}

for key in ('brightness','confidence_threshold','detection_threshold','led_count','gpio_pin','active_color','idle_color'):
    if key in cfg: ok(f"Key '{key}'", str(cfg[key]))
    else: warn(f"Key '{key}' missing")

# POST round-trip test (restore same value)
orig = cfg.get('brightness', 200)
st2, r2 = post('/api/config', {'brightness': orig})
if st2 == 200: ok("POST /api/config round-trip OK")
else: warn("POST /api/config failed", str(st2))

# ── 4. Thermal API ────────────────────────────────────────────────────────────
hdr("4 · Thermal API  (/api/thermal)")
st, therm = get('/api/thermal')
if st == 200: ok("HTTP 200")
else: fail("Failed", str(st)); therm = {}

temp = therm.get('temp_c', -1)
if temp > 0:
    ok(f"Temperature readable", f"{temp}°C")
    if temp < 70: ok("Temperature safe", f"{temp}°C < 70°C")
    elif temp < 80: warn("Temperature elevated", f"{temp}°C")
    else: fail("Temperature CRITICAL", f"{temp}°C ≥ 80°C")
else: fail("Temperature unreadable", str(temp))

thr = therm.get('throttle', -1)
if thr == 0: ok("Throttle level 0 — no throttling")
elif thr == 1: warn("Throttle level 1 — warm", f"{temp}°C")
elif thr >= 2: fail(f"Throttle level {thr} — performance degraded", f"{temp}°C")
else: warn("Throttle field missing")

# ── 5. Status API ─────────────────────────────────────────────────────────────
hdr("5 · Status API  (/api/status)")
st, stat = get('/api/status')
if st == 200: ok("HTTP 200")
else: fail("Failed", str(st))
if stat.get('camera_active'): ok("Camera active per status")
else: fail("Camera NOT active", str(stat))

# ── 6. Video Feed ─────────────────────────────────────────────────────────────
hdr("6 · MJPEG Video Feed  (/video_feed)")
try:
    with urllib.request.urlopen(BASE+'/video_feed', timeout=4) as r:
        ct = r.headers.get('Content-Type','')
        if 'multipart' in ct: ok("Streaming MJPEG", ct[:50])
        else: warn("Unexpected content-type", ct[:50])
        chunk = r.read(512)
        if chunk: ok("Delivering data", f"{len(chunk)} bytes")
        else: fail("Empty response")
except Exception as e: fail("Video feed error", str(e)[:60])

# ── 7. Shutdown endpoint ──────────────────────────────────────────────────────
hdr("7 · Shutdown API  (/api/shutdown)")
if args.no_shutdown_test:
    warn("Shutdown test skipped (--no-shutdown-test)")
else:
    st, resp = post('/api/shutdown')
    if st == 200 and resp.get('status') == 'shutting_down':
        ok("Endpoint responds correctly", "status=shutting_down")
        if IS_LOCAL:
            print(f"    {Y}⚡ Service restarting via systemd…{X}")
            time.sleep(7)
            st2, _ = get('/api/detection')
            if st2 == 200: ok("Service auto-restarted after shutdown ✓")
            else: warn("Service still restarting — check in a few seconds")
    else:
        fail("Shutdown endpoint missing or error", f"status={st}")

# ── 8. Local system checks ────────────────────────────────────────────────────
if IS_LOCAL:
    hdr("8 · System Checks  (Pi-local)")

    try:
        r = subprocess.run(['vcgencmd','measure_clock','arm'], capture_output=True, text=True, timeout=3)
        hz = int(r.stdout.strip().split('=')[1]) if '=' in r.stdout else 0
        mhz = hz/1_000_000
        if mhz >= 1800: ok(f"Overclock active", f"{mhz:.0f} MHz")
        else: warn("CPU below overclock target", f"{mhz:.0f} MHz")
    except Exception as e: warn("vcgencmd unavailable", str(e))

    for svc in ('human-led','ttyd'):
        r = subprocess.run(['systemctl','is-active',f'{svc}.service'], capture_output=True, text=True)
        s = r.stdout.strip()
        if s == 'active': ok(f"Service '{svc}' active")
        else: fail(f"Service '{svc}' not active", s)

    try:
        raw = int(open('/sys/class/thermal/thermal_zone0/temp').read().strip())
        ok("Thermal sensor file readable", f"{raw/1000:.1f}°C")
    except Exception as e: fail("Thermal sensor unreadable", str(e))

    for fpath in ('/home/babilnur/human_detect_led/calibration_app.py',
                  '/home/babilnur/human_detect_led/templates/index.html',
                  '/home/babilnur/human_detect_led/config.json'):
        if os.path.exists(fpath): ok(f"File exists: {os.path.basename(fpath)}", f"{os.path.getsize(fpath)} B")
        else: (warn if 'config' in fpath else fail)(f"File missing: {fpath}")

    # Check shutdown button in HTML
    html = open('/home/babilnur/human_detect_led/templates/index.html').read()
    if 'shutdown-btn' in html: ok("Shutdown button present in index.html")
    else: fail("Shutdown button missing from index.html")

    # Check check_thermal defined in app
    app_src = open('/home/babilnur/human_detect_led/calibration_app.py').read()
    if 'def check_thermal' in app_src: ok("check_thermal() defined in app")
    else: fail("check_thermal() missing from app")
    if '/api/shutdown' in app_src: ok("/api/shutdown route defined")
    else: fail("/api/shutdown route missing")

# ── Summary ───────────────────────────────────────────────────────────────────
total = p+f+w
hdr("QA SUMMARY")
print(f"  {G}PASS{X}  {p}")
print(f"  {R}FAIL{X}  {f}")
print(f"  {Y}WARN{X}  {w}")
print(f"  {'─'*20}")
print(f"  Total {total} checks\n")
if f == 0 and w == 0: print(f"  {G}{B}✓ All checks passed — system healthy{X}\n")
elif f == 0: print(f"  {Y}{B}⚠ Passed with warnings{X}\n")
else: print(f"  {R}{B}✗ {f} failure(s) — action required{X}\n")
sys.exit(0 if f == 0 else 1)
