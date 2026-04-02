#!/usr/bin/env python3
"""
Human Detect LED — Desktop Controller
Lightweight local GUI. Talks to run.py via its API on localhost:5000.
"""
import tkinter as tk
import urllib.request, json, threading, socket, time

API = 'http://localhost:5000'

# ── colours ───────────────────────────────────────────────────────────────────
BG       = '#0d1117'
BG2      = '#161b22'
BG3      = '#21262d'
BORDER   = '#30363d'
TXT      = '#e6edf3'
TXT_DIM  = '#8b949e'
GREEN    = '#3fb950'
RED      = '#f85149'
BLUE     = '#58a6ff'
YELLOW   = '#d29922'
ORANGE   = '#ff6b35'

def api_get(path):
    with urllib.request.urlopen(f'{API}{path}', timeout=1.5) as r:
        return json.loads(r.read())

def api_post(path, data=None):
    body = json.dumps(data or {}).encode()
    req  = urllib.request.Request(
        f'{API}{path}', data=body,
        headers={'Content-Type': 'application/json'}, method='POST'
    )
    with urllib.request.urlopen(req, timeout=1.5) as r:
        return json.loads(r.read())

def local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(('8.8.8.8', 80)); ip = s.getsockname()[0]; s.close()
        return ip
    except Exception:
        return 'localhost'

# ── app ───────────────────────────────────────────────────────────────────────
class App:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title('LED Controller')
        self.root.configure(bg=BG)
        self.root.resizable(False, False)
        self.root.attributes('-topmost', False)

        self._online  = False
        self._remote  = False
        self._det     = False
        self._fps     = 0.0
        self._conf    = 0.0
        self._leds    = 0
        self._temp    = -1
        self._busy    = False   # prevent double-click spam on toggle

        self._build()
        self._schedule_poll()
        self.root.mainloop()

    # ── UI build ──────────────────────────────────────────────────────────────
    def _build(self):
        root = self.root
        PAD = 14

        # ── header ────────────────────────────────────────────────────────────
        hdr = tk.Frame(root, bg=BG2, pady=8)
        hdr.pack(fill='x')
        tk.Label(hdr, text='  ⬤  Human Detect LED', bg=BG2,
                 fg=ORANGE, font=('Segoe UI', 11, 'bold')).pack(side='left')
        self.lbl_temp = tk.Label(hdr, text='', bg=BG2,
                                 fg=TXT_DIM, font=('monospace', 9))
        self.lbl_temp.pack(side='right', padx=10)

        # ── service status bar ────────────────────────────────────────────────
        svc = tk.Frame(root, bg=BG, pady=4)
        svc.pack(fill='x', padx=PAD)
        tk.Label(svc, text='Service', bg=BG, fg=TXT_DIM,
                 font=('Segoe UI', 9)).pack(side='left')
        self.lbl_svc = tk.Label(svc, text='offline', bg=BG,
                                fg=RED, font=('monospace', 9, 'bold'))
        self.lbl_svc.pack(side='left', padx=6)

        _sep(root)

        # ── detection stats ───────────────────────────────────────────────────
        stats = tk.Frame(root, bg=BG, pady=6)
        stats.pack(fill='x', padx=PAD)

        self.lbl_det = _stat_val(stats, '—', GREEN, big=True)
        self.lbl_det.pack(side='left')

        right = tk.Frame(stats, bg=BG)
        right.pack(side='right')
        self.lbl_fps  = _stat_row(right, 'FPS',        '—',    BLUE)
        self.lbl_conf = _stat_row(right, 'Confidence', '—',    TXT)
        self.lbl_leds = _stat_row(right, 'Active LEDs','0',    YELLOW)

        _sep(root)

        # ── remote toggle button ──────────────────────────────────────────────
        btn_frame = tk.Frame(root, bg=BG, pady=12)
        btn_frame.pack(fill='x', padx=PAD)

        self.btn_remote = tk.Button(
            btn_frame, text='ENABLE WEB UI',
            font=('Segoe UI', 12, 'bold'),
            bg=BG3, fg=TXT_DIM,
            activebackground=BG3, activeforeground=TXT,
            relief='flat', bd=0, padx=20, pady=12,
            cursor='hand2',
            command=self._toggle_remote
        )
        self.btn_remote.pack(fill='x')

        # ── IP label (shown when remote is on) ────────────────────────────────
        self.lbl_ip = tk.Label(
            root, text='', bg=BG, fg=BLUE,
            font=('monospace', 9), pady=2
        )
        self.lbl_ip.pack()

        # ── brightness quick-slider ───────────────────────────────────────────
        _sep(root)
        bri_row = tk.Frame(root, bg=BG, pady=8)
        bri_row.pack(fill='x', padx=PAD)
        tk.Label(bri_row, text='Brightness', bg=BG, fg=TXT_DIM,
                 font=('Segoe UI', 9)).pack(side='left')
        self.lbl_bri_val = tk.Label(bri_row, text='—', bg=BG, fg=TXT,
                                    font=('monospace', 9, 'bold'), width=4)
        self.lbl_bri_val.pack(side='right')
        self.bri_var = tk.IntVar(value=200)
        self.slider = tk.Scale(
            root, from_=0, to=255,
            orient='horizontal', variable=self.bri_var,
            bg=BG, fg=TXT, troughcolor=BG3,
            highlightthickness=0, bd=0, showvalue=False,
            command=self._on_brightness
        )
        self.slider.pack(fill='x', padx=PAD, pady=(0, 10))

        self._brightness_after = None   # debounce timer id

        # ── bottom padding ────────────────────────────────────────────────────
        tk.Frame(root, bg=BG, height=4).pack()

    # ── toggle remote ─────────────────────────────────────────────────────────
    def _toggle_remote(self):
        if self._busy or not self._online:
            return
        self._busy = True
        self.btn_remote.config(state='disabled', text='…')

        def _do():
            try:
                d = api_post('/api/remote')
                self.root.after(0, lambda: self._apply_remote(d.get('remote', False)))
            except Exception:
                self.root.after(0, lambda: self._apply_remote(self._remote))
            self._busy = False

        threading.Thread(target=_do, daemon=True).start()

    def _apply_remote(self, on: bool):
        self._remote = on
        self._refresh_remote_ui()

    def _refresh_remote_ui(self):
        on = self._remote
        if not self._online:
            self.btn_remote.config(
                text='SERVICE OFFLINE',
                bg=BG3, fg=TXT_DIM, state='disabled'
            )
            self.lbl_ip.config(text='')
            return
        self.btn_remote.config(state='normal')
        if on:
            self.btn_remote.config(
                text='DISABLE WEB UI',
                bg='#1a3a1a', fg=GREEN
            )
            ip = local_ip()
            self.lbl_ip.config(text=f'http://{ip}:5000')
        else:
            self.btn_remote.config(
                text='ENABLE WEB UI',
                bg=BG3, fg=TXT_DIM
            )
            self.lbl_ip.config(text='')

    # ── brightness slider ─────────────────────────────────────────────────────
    def _on_brightness(self, val):
        self.lbl_bri_val.config(text=str(val))
        if self._brightness_after:
            self.root.after_cancel(self._brightness_after)
        self._brightness_after = self.root.after(
            400, lambda: self._send_brightness(int(val))
        )

    def _send_brightness(self, val):
        def _do():
            try:
                api_post('/api/config', {'brightness': val})
            except Exception:
                pass
        threading.Thread(target=_do, daemon=True).start()

    # ── polling ───────────────────────────────────────────────────────────────
    def _schedule_poll(self):
        threading.Thread(target=self._fetch, daemon=True).start()
        self.root.after(600, self._schedule_poll)

    def _fetch(self):
        try:
            det  = api_get('/api/detection')
            stat = api_get('/api/status')
            cfg  = api_get('/api/config')
            try:
                therm = api_get('/api/thermal')
                temp  = therm.get('temp_c', -1)
            except Exception:
                temp = -1
            self.root.after(0, lambda: self._update(det, stat, cfg, temp))
        except Exception:
            self.root.after(0, self._show_offline)

    def _update(self, det, stat, cfg, temp):
        self._online = True
        self._remote = stat.get('remote', stat.get('stream_enabled', False))
        self._det    = det.get('detected', False)
        self._fps    = det.get('fps', 0)
        self._conf   = det.get('confidence', 0)
        self._leds   = len(det.get('active_leds', []))
        bri          = cfg.get('brightness', 200)

        # service badge
        self.lbl_svc.config(text='online', fg=GREEN)

        # detection
        if self._det:
            self.lbl_det.config(
                text=f'DETECTED', fg=GREEN
            )
        else:
            self.lbl_det.config(text='no person', fg=TXT_DIM)

        self.lbl_fps.config( text=f'{self._fps}')
        self.lbl_conf.config(text=f'{int(self._conf*100)}%' if self._det else '—')
        self.lbl_leds.config(text=str(self._leds))

        # brightness slider (only sync if user isn't dragging)
        if abs(self.bri_var.get() - bri) > 2:
            self.bri_var.set(bri)
        self.lbl_bri_val.config(text=str(bri))

        # temp
        self.lbl_temp.config(
            text=f'{temp}°C' if temp > 0 else '',
            fg=RED if temp >= 75 else TXT_DIM
        )

        if not self._busy:
            self._refresh_remote_ui()

    def _show_offline(self):
        self._online = False
        self.lbl_svc.config(text='offline', fg=RED)
        self.lbl_det.config(text='—', fg=TXT_DIM)
        self.lbl_fps.config(text='—')
        self.lbl_conf.config(text='—')
        self.lbl_leds.config(text='—')
        self.lbl_temp.config(text='')
        if not self._busy:
            self._refresh_remote_ui()

# ── helpers ───────────────────────────────────────────────────────────────────
def _sep(parent):
    tk.Frame(parent, bg=BORDER, height=1).pack(fill='x', padx=0)

def _stat_val(parent, text, color, big=False):
    return tk.Label(parent, text=text, bg=BG, fg=color,
                    font=('Segoe UI', 16 if big else 11, 'bold'))

def _stat_row(parent, label, value, color):
    row = tk.Frame(parent, bg=BG)
    row.pack(anchor='e', pady=1)
    tk.Label(row, text=label + '  ', bg=BG, fg=TXT_DIM,
             font=('Segoe UI', 9)).pack(side='left')
    lbl = tk.Label(row, text=value, bg=BG, fg=color,
                   font=('monospace', 9, 'bold'), width=8, anchor='e')
    lbl.pack(side='right')
    return lbl

if __name__ == '__main__':
    App()
