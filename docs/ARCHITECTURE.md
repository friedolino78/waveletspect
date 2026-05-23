# WaveletSpect Architektur

## Übersicht

WaveletSpect besteht aus drei Hauptkomponenten, die über Thread-Grenzen hinweg
kommunizieren:

```
JACK Audio Thread  →  CWT Worker Thread  →  GTK Main Thread
     (Echtzeit)         (FFT-basiert)        (30 FPS Cairo)
```

## Komponenten

### 1. JackCapture (Thread)

- Öffnet JACK-Client mit `no_start_server=True`
- Registriert Eingangsport `input_1`
- Sammelt Audio-Blocks in einen Ringbuffer
- Triggered CWT-Berechnung alle `hop` Samples
- Level-Meter mit exponentiellem Glättungsfilter

Der JACK-Process-Callback (`_process_callback`) ist **Echtzeit-kritisch**:
- Keine Allokationen
- Keine Locks
- Nur Ringbuffer-Schreibung + Kopie für asynchrone CWT

### 2. CWTFFT (Engine)

FFT-basierte Continuous Wavelet Transform:

```
CWT(a)[n] = IFT{ X(f) * conj(Psi_a(f)) }
```

Dabei ist `Psi_a(f)` die analytische FT des skalierten Morlet-Wavelets:

```
Psi_a(f) = sqrt(a) * exp(-(f - f_center)² / (2 * sigma_f²))
```

mit:
- `f_center = omega0 / (2 * pi * a * dt)`
- `sigma_f = fb / (pi * a * dt)`

**Vektorisierung**: Alle Skalen gleichzeitig als Matrix-Operation:
```python
products = psi_ft_matrix * sig_fft[np.newaxis, :]
coeffs = np.fft.ifft(products, axis=1)
```

### 3. SpectrogramWidget (GTK DrawingArea)

- Custom GTK Widget mit Cairo-Rendering
- ~30 FPS Update-Rate via `GLib.timeout_add(33, ...)`
- Wasserfall als `cairo.ImageSurface` (Subsampling für Performance)
- Spektrum als Cairo-Path mit Fill
- Gitter + Achsenbeschriftung in logarithmischer Frequenzskala

## Thread-Kommunikation

```
JackCapture._process_cb()
    └── threading.Thread(target=_compute_cwt)
            └── queue.Queue.put(column)
                    └── SpectrogramWidget._on_timer()
                            └── queue.Queue.get()
                                    └── widget.queue_draw()
```

Die Queue hat max. 4 Einträge. Bei Overflow werden ältere Spalten fallengelassen
(Wiring: "neusten Daten priorisieren").

## Konfiguration

Alle Parameter werden beim Start per argparse gesetzt, zur Laufzeit über Tastatur:

| Parameter | CLI-Flag | Tastatur | Effekt |
|-----------|----------|----------|--------|
| dB-Bereich | `--ceiling` | `+`/`-` | Farbskalierung |
| Threshold | `--threshold` | `l`/`k` | Rausch-Unterdrückung |
| Farbschema | `--colormap` | `c` | Visuelles Erscheinungsbild |
| Reset | — | `r` | Alle auf Defaults |

## Abhängigkeiten

| Paket | Zweck |
|-------|-------|
| `numpy` | Array-Operationen, FFT |
| `PyWavelets` | `frequency2scale()` für Skalenberechnung |
| `JACK-Client` | Audio-Capture |
| `PyGObject` (gi) | GTK3 Widgets |
| `pycairo` | 2D-Rendering |
