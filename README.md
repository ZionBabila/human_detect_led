# Human Detect LED

Raspberry Pi system that detects a person in front of a camera and lights up a WS281x LED strip in response. Controlled entirely from a browser — no app install needed.

![version](https://img.shields.io/badge/version-2.0.0-blue) ![platform](https://img.shields.io/badge/platform-Raspberry%20Pi-red) ![license](https://img.shields.io/badge/license-MIT-green)

---

## What it does

- Camera watches for a person using OpenCV (or YOLOv8 for higher accuracy)
- LED strip lights up when someone is detected, fades out when they leave
- Position tracking — LEDs follow where the person is standing
- Web UI to control everything live from your phone or laptop
- Supports multiple LED strips on different GPIO pins

## Hardware required

| Part | Notes |
|------|-------|
| Raspberry Pi 4 or 5 | Pi 3 works but expect higher CPU usage |
| USB or CSI camera | Any USB webcam works |
| WS281x LED strip | WS2812B, SK6812, etc. — 5V or 12V |
| 5V power supply | Dedicated supply for LEDs (not from the Pi USB port) |
| 470Ω resistor | On the data line between Pi GPIO and LED strip |
| Level shifter (optional) | Recommended for 12V strips |

**GPIO wiring:**
- LED data → GPIO 18 (default, configurable)
- Remote button → GPIO 17 (optional)
- LED ground → Pi ground (shared ground with Pi)

---

## Quick install

```bash
git clone https://github.com/ZionBabila/human_detect_led.git
cd human_detect_led
bash install.sh
sudo systemctl start human_detect_led
```

Then open `http://<your-pi-ip>:5000` in a browser.

> The installer sets up all dependencies and registers a systemd service that starts automatically on boot.

---

## First-time setup

1. Open the web UI at `http://<pi-ip>:5000`
2. The setup wizard will walk you through:
   - Configuring your LED strip (GPIO pin, LED count, color)
   - Setting a detection threshold for your room lighting
   - Testing the LEDs
3. Click **Save & Start**

---

## Web UI

| Feature | Description |
|---------|-------------|
| Live camera feed | Toggle on/off — off by default to save CPU |
| LED strip visualizer | Shows which LEDs are active in real time |
| Position bar | Shows where in the frame the person is detected |
| Brightness / Smoothing | Adjust live without restarting |
| Active / Idle colors | Pick any hex color for each state |
| Spread mode | Fixed (N LEDs always on) or Proportional (scales with person size) |
| Multi-strip | Configure up to N strips on different GPIO pins |

---

## Detection modes

| Mode | How to enable | CPU usage | Accuracy |
|------|--------------|-----------|----------|
| HOG (default) | Always available | Low (~50%) | Good |
| YOLOv8 | `pip install ultralytics` | Higher (~80%) | Excellent |

Switch modes in the web UI sidebar. YOLOv8 model weights download automatically on first use.

---

## Security

By default the UI is open on your local network. To require a password:

1. Edit the service file:
   ```bash
   sudo nano /etc/systemd/system/human_detect_led.service
   ```
2. Uncomment and set:
   ```
   Environment=LED_PASSWORD=your_password_here
   Environment=LED_SECRET_KEY=a_random_secret_key
   ```
3. Restart:
   ```bash
   sudo systemctl daemon-reload
   sudo systemctl restart human_detect_led
   ```

---

## Commands

```bash
make start        # Start the service
make stop         # Stop the service
make restart      # Restart after config changes
make logs         # Follow live logs
make status       # Check if running
make test         # Run QA tests
make test-full    # Run full QA suite (needs service running)
make uninstall    # Remove the service
```

---

## Files

```
run.py              — Main app (detection + LED control + web server)
calibration_app.py  — Setup wizard and calibration tool
desktop_app.py      — Optional desktop GUI
config.json         — Saved settings (auto-updated from web UI)
install.sh          — One-command installer
Makefile            — Common commands
qa_test.py          — Basic QA tests
qa_full.py          — Full QA suite
```

---

## Updating

```bash
cd human_detect_led
git pull
sudo systemctl restart human_detect_led
```

---

## Troubleshooting

**LEDs not lighting up**
- Check the service is running as root (`sudo systemctl status human_detect_led`)
- Verify GPIO pin matches your wiring in the web UI settings
- Run `make test` to check LED hardware availability

**Camera not detected**
- Run `ls /dev/video*` — camera should appear as `/dev/video0`
- Try `sudo apt install python3-opencv` if OpenCV errors appear

**High CPU usage**
- Switch from YOLOv8 to HOG mode in the sidebar
- Reduce camera resolution in the settings

**Can't reach the web UI**
- Check Pi IP: `hostname -I`
- Check firewall: `sudo ufw allow 5000/tcp`

---

## License

MIT — free to use, modify, and sell.
