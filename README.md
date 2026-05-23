# WaveletSpect

Echtzeit Wavelet-Spektrogramm via JACK + GTK3/Cairo.

Ähnlich [Baudline](https://baudline.com/), aber basierend auf FFT-basierter Continuous Wavelet Transform (CWT).
Hohe Frequenzen erhalten höhere zeitliche Auflösung als tiefe Frequenzen
(inhärente constant-Q Eigenschaft der Wavelets).

![WaveletSpect Screenshot](screenshots/waveletspect.png)

## Features

- **FFT-basierte CWT**: ~50x schneller als pywt.cwt direkt
- **Logarithmische Frequenzskala**: Constant-Q Auflösung (hoch = zeitlich genau, tief = frequenzgenau)
- **Wasserfall-Display**: Scrollendes Spektrogramm mit konfigurierbarer Historie
- **Live-Spektrum**: Momentan-Spektrum als Balkendiagramm
- **6 Farbschemas**: viridis, inferno, magma, plasma, hot, cool
- **Einstellbarer Bereich**: dB-Clipping tastaturgesteuert
- **JACK Audio**: Pro-Audio Low-Latency Audio-Anbindung

## Warum Wavelets?

**STFT (klassisch):**
- Feste Zeit-Frequenz-Auflösung (Heisenberg-Begrenzung)
- Gute Auflösung entweder in Zeit ODER Frequenz

**CWT (Wavelets):**
- Hohe Frequenzen → schmale Wavelets → hohe zeitliche Auflösung
- Tiefe Frequenzen → breite Wavelets → hohe Frequenzauflösung
- Entspricht menschlichem Hören und Baudline-Darstellung

## Installation

### System-Pakete (Debian/Ubuntu)

**Mit PipeWire (empfohlen):**
```bash
sudo apt install python3-gi python3-gi-cairo gir1.2-gtk-3.0 pipewire pipewire-jack
```

**Oder mit klassischem JACK:**
```bash
sudo apt install python3-gi python3-gi-cairo gir1.2-gtk-3.0 jackd2
```

PipeWire wird automatisch erkannt und bietet JACK-kompatible APIs. Kein `jackd` nötig — PipeWire übernimmt die Audio-Verbindung.

### Python-Pakete

```bash
pip install -r requirements.txt
```

### Oder via setup.py

```bash
pip install .
```

## Start

**Mit PipeWire (Standard auf modernen Linux-Distributionen):**
```bash
# PipeWire läuft bereits als Benutzer-Dienst
# Einfach starten:
python3 waveletspect.py --connect
```

**Oder mit klassischem JACK:**
```bash
jackd -d alsa &
python3 waveletspect.py --connect
```

## Optionen

| Flag | Default | Beschreibung |
|------|---------|-------------|
| `--client` | waveletspect | JACK Client-Name |
| `--width` | 1280 | Fensterbreite |
| `--height` | 700 | Fensterhöhe |
| `--freq-min` | 50 | Untere Frequenzgrenze (Hz) |
| `--freq-max` | 16000 | Obere Frequenzgrenze (Hz) |
| `--bands` | 160 | Anzahl Frequenzbänder |
| `--hop` | 256 | Samples zwischen CWT-Berechnungen |
| `--history` | 512 | Wasserfall-Zeitfenster (Zeilen) |
| `--wavelet` | cmor1.5-1.0 | Wavelet-Typ |
| `--threshold` | -70 | dB Clipping unten |
| `--ceiling` | 5 | dB Clipping oben |
| `--colormap` | viridis | Farbschema |
| `--connect` | false | Autoconnect zu system:capture |
| `--debug` | false | Debug-Logging |

## Tastatur

| Taste | Funktion |
|-------|---------|
| `q` / `Esc` | Beenden |
| `+` / `-` | dB-Bereich vergrößern/verkleinern |
| `c` | Farbschema wechseln |
| `l` / `k` | Threshold hoch/runter |
| `r` | Range-Reset |

## Wavelet-Auswahl

| Name | Geschwindigkeit | Eigenschaft |
|------|-----------------|-------------|
| `cmor1.5-1.0` | ★★★★ | Guter Kompromiss (Default) |
| `cmor0.5-0.5` | ★★★★ | Schmaler, weniger Überlapp |
| `gaus4` | ★★★★★ | Sehr schnell, klar |
| `gaus8` | ★★★★★ | Noch schneller |
| `morl` | ★★★★ | Klassisch |

## Architektur

```
┌─────────────┐     Ringbuffer     ┌──────────────┐     Queue     ┌────────────┐
│  JACK Input  │ ──────────────▶  │  CWT Thread  │ ──────────▶  │  GTK Main  │
│  (Realtime)  │   (lock-free)    │  (FFT-based)  │  (columns)   │  (30 FPS)  │
└─────────────┘                   └──────────────┘              └────────────┘
                                                                        │
                                                                        ▼
                                                                ┌────────────┐
                                                                │   Cairo    │
                                                                │  Rendering │
                                                                └────────────┘
```

## Performance

Typisch auf x86_64 (48 kHz, Hop 256):

| Bänder | Wavelet | nfft | Zeit/Spalte | RT-Faktor |
|--------|---------|------|-------------|-----------|
| 96 | gaus4 | 4096 | ~3.5ms | ~1.5x |
| 128 | gaus4 | 4096 | ~4.5ms | ~1.2x |
| 96 | cmor1.5-1.0 | 4096 | ~18ms | ~0.3x |
| 160 | cmor1.5-1.0 | 8192 | ~25ms | ~0.2x |

RT-Faktor > 1.0 = Echtzeitfähig. Hop-Intervall = 5.3ms bei 48kHz/256.

**Optimierungen:**
- rfft statt fft (2x schneller)
- Parseval-Energie im Frequenzbereich (keine IFFT)
- Persistenter Worker-Thread (kein Thread-pro-Hop-Overhead)
- Vektorisierte Wavelet-FT (NumPy Broadcasting)

**Numba/Cython?** Nicht nötig — NumPy's FFT ist bereits in C kompiliert.
Der Python-Overhead ist < 5% der Gesamtzeit.

Für volle Echtzeit mit Hop=256 empfiehlt sich `gaus4` oder weniger Bänder.

## Lizenz

MIT — siehe [LICENSE](LICENSE).
