# Hardware Guide

Everything you need to build and wire a Human Detect LED system.

---

## Bill of Materials

| # | Part | Recommended | Min Spec | Approx Cost |
|---|------|-------------|----------|-------------|
| 1 | Raspberry Pi | Pi 4 (2GB+) | Pi 3B+ | $35–55 |
| 2 | MicroSD card | 16GB Class 10 | 8GB | $5–10 |
| 3 | Pi power supply | Official 5V/3A USB-C | 5V/2.5A | $8–12 |
| 4 | USB camera | Logitech C270 or C920 | Any USB webcam | $15–40 |
| 5 | WS281x LED strip | WS2812B 60 LED/m (5V) | WS2812B 30 LED/m | $8–20 |
| 6 | LED power supply | 5V/10A (for up to 300 LEDs) | Matches strip voltage | $10–15 |
| 7 | 470Ω resistor | 1/4W through-hole | 300–500Ω | <$1 |
| 8 | 1000µF capacitor | 6.3V or higher electrolytic | 470µF | <$1 |
| 9 | Jumper wires | Dupont M-F | — | $3–5 |
| 10 | Breadboard (optional) | Mini 170-point | — | $2–4 |

**Total: ~$85–160 depending on what you already have.**

> Pi 5 also works. Pi Zero 2W works but expect ~80% CPU with HOG detection.

---

## LED strip types

| Type | Voltage | Works? | Notes |
|------|---------|--------|-------|
| WS2812B | 5V | ✓ Best | Most common, easiest to wire |
| SK6812 | 5V | ✓ | Similar to WS2812B, has RGBW variant |
| WS2811 | 12V | ✓ | Needs level shifter + 12V supply |
| WS2813 | 5V | ✓ | Has backup data line — more reliable |
| APA102 | 5V | ✗ | Uses SPI, not compatible |
| NeoPixel | 5V | ✓ | Adafruit brand WS2812B, same wiring |

---

## Wiring

### Overview

```
Raspberry Pi                  LED Strip
─────────────                 ─────────
GPIO 18 ──── 470Ω ──────────► DIN (data in)
GND ─────────────────────────► GND
                               +5V ◄──── External 5V supply (+)
Pi GND ──────────────────────────────── External 5V supply (-)
```

> **Important:** Pi GND and power supply GND must be connected together (shared ground).
> Never power LEDs from the Pi's 5V pin — it can only supply ~500mA, not enough for strips.

### Step by step

1. **Data line** — Pi GPIO 18 → 470Ω resistor → LED strip DIN pad
2. **Ground** — Pi GND → LED strip GND (also connect to power supply -)
3. **LED power** — External 5V supply + → LED strip +5V pad
4. **Capacitor** — 1000µF cap across +5V and GND at the start of the strip (protects against power spikes)

### GPIO pin diagram (Pi 40-pin header)

```
 3V3  [ 1][ 2] 5V
 SDA  [ 3][ 4] 5V
 SCL  [ 5][ 6] GND ◄── connect to LED GND
      [ 7][ 8]
 GND  [ 9][10]
      [11][12] GPIO18 ◄── data line (through 470Ω resistor)
      [13][14] GND
      [15][16]
 3V3  [17][18] GPIO18  ← same pin, physical pin 12
```

### Multiple strips

Each strip needs its own GPIO pin and its own data resistor. Power can be shared from the same supply if it's rated for the total current.

| Strip | GPIO | Config label |
|-------|------|-------------|
| Strip 1 | GPIO 18 | default |
| Strip 2 | GPIO 12 | add in web UI |
| Strip 3 | GPIO 13 | add in web UI |

---

## Power calculator

Each WS2812B LED draws up to **60mA** at full white brightness (all channels max).
In practice with colored lighting at medium brightness: ~**15–20mA per LED**.

| LEDs | Max draw | Recommended PSU |
|------|----------|----------------|
| 30 | 1.8A | 5V/3A |
| 60 | 3.6A | 5V/5A |
| 144 | 8.6A | 5V/10A |
| 300 | 18A | 5V/20A (or split into segments) |

> For strips longer than 1m, inject power at both ends to avoid voltage drop dimming the far end.

---

## Camera placement

- Mount the camera **facing the area** you want to detect (not side-on)
- Ideal distance: **1.5m – 4m** from where people will stand
- Avoid pointing directly at a window or bright light source (backlighting kills detection)
- USB extension cable up to **5m** works fine for most USB 2.0 cameras

### For best detection accuracy

- Keep camera at **chest to head height** (~1–1.5m)
- Ensure even lighting — detection struggles in very dark or very bright scenes
- If using YOLOv8 mode, accuracy is much better in varied lighting

---

## Physical mounting ideas

| Setup | Description |
|-------|-------------|
| Shelf/desk | Pi sits on a shelf, camera on top of monitor or shelf edge |
| Behind TV | LED strip behind TV, camera on top — ambient lighting effect |
| Doorway | LED strip around door frame, camera above the door |
| Under cabinet | LED strip under kitchen cabinet, camera on wall |
| Display cabinet | Strip inside cabinet, camera outside pointing at viewer |

---

## Case / enclosure

No official case — the Pi can be used bare or in any standard Pi case.
The LED strip is typically attached with the included 3M adhesive backing.

For weatherproofing (outdoor use): use IP65 or IP67 rated LED strip and a weatherproof Pi enclosure. Ensure ventilation — the Pi runs warm.

---

## Troubleshooting wiring

| Symptom | Likely cause | Fix |
|---------|-------------|-----|
| LEDs don't light at all | No data signal reaching strip | Check 470Ω resistor is on data line, not GND |
| LEDs flicker or show wrong colors | Shared ground missing | Connect Pi GND to power supply GND |
| Only first few LEDs work | Insufficient power | Use a bigger PSU or inject power mid-strip |
| LEDs at far end are dim/wrong color | Voltage drop | Inject power at both ends of strip |
| Random flickers at startup | Power spike | Add 1000µF cap across +5V/GND at strip start |
| Pi reboots when LEDs turn on | PSU too weak | Use a dedicated LED power supply, not the Pi's USB-C |
| Camera not detected | Driver issue | Run `ls /dev/video*` — should show `/dev/video0` |
