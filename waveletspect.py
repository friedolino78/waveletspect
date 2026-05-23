#!/usr/bin/env python3
"""
WaveletSpect - Echtzeit Wavelet-Spektrogramm via JACK + GTK3/Cairo
====================================================================

Ähnlich Baudline, aber basierert auf FFT-basierter Continuous Wavelet Transform.
Hohe Frequenzen -> hohe zeitliche Auflösung, tiefe Frequenzen -> hohe
Frequenzauflösung (inhärente constant-Q Eigenschaft der CWT).

Performance: FFT-basierte CWT (~50x schneller als pywt.cwt).

Verwendung:
    ./waveletspect.py [OPTIONS]
    ./waveletspect.py --help

Tastatur (im Fenster):
    q, Escape  Beenden
    + / -      Bereich +/- 5 dB
    c          Farbschema wechseln
    l/k        Threshold hoch/runter
    r          Reset Bereich auf Defaults

Anforderungen (System):
    sudo apt install python3-gi python3-gi-cairo gir1.2-gtk-3.0
    pip install numpy scipy PyWavelets JACK-Client

(C) 2026 - MIT License
"""

import sys
import argparse
import threading
import queue
import time
import logging
import numpy as np

# ─── Logging ───────────────────────────────────────────────────────────────────

LOG_FMT = "%(asctime)s [%(levelname)-7s] %(name)s: %(message)s"
LOG_DATE = "%H:%M:%S"
log = logging.getLogger("waveletspect")


def setup_logging(level=logging.INFO, logfile=None):
    root = logging.getLogger()
    root.setLevel(level)
    ch = logging.StreamHandler()
    ch.setLevel(level)
    ch.setFormatter(logging.Formatter(LOG_FMT, datefmt=LOG_DATE))
    root.addHandler(ch)
    if logfile:
        fh = logging.FileHandler(logfile, mode="w")
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(logging.Formatter(LOG_FMT, datefmt=LOG_DATE))
        root.addHandler(fh)


# ─── Argument-Parsing ─────────────────────────────────────────────────────────

parser = argparse.ArgumentParser(
    description="WaveletSpect - Echtzeit CWT Spektrogramm",
    formatter_class=argparse.ArgumentDefaultsHelpFormatter,
)
parser.add_argument("--client", default="waveletspect", help="JACK Client-Name")
parser.add_argument("--width", type=int, default=1280)
parser.add_argument("--height", type=int, default=700)
parser.add_argument("--history", type=int, default=512, help="Wasserfall-Zeilen")
parser.add_argument("--hop", type=int, default=512, help="Samples pro CWT (auto bei --quality)")
parser.add_argument("--freq-min", type=float, default=50.0)
parser.add_argument("--freq-max", type=float, default=16000.0)
parser.add_argument("--bands", type=int, default=128, help="Frequenzbaender (auto bei --quality)")
parser.add_argument("--quality", type=int, default=5, choices=range(1, 11),
                    metavar="N", help="Qualitaet 1=sehr schnell .. 10=hochaufloesend")
parser.add_argument("--wavelet", default="gaus4")
parser.add_argument("--nfft", type=int, default=0, help="FFT-Laenge (0=auto)")
parser.add_argument("--threshold", type=float, default=-70.0)
parser.add_argument("--ceiling", type=float, default=5.0)
parser.add_argument("--colormap", default="viridis",
                    choices=["viridis", "inferno", "magma", "plasma", "hot", "cool"])
parser.add_argument("--connect", action="store_true", default=False)
parser.add_argument("--debug", action="store_true")
parser.add_argument("--logfile", default=None)
ARGS = parser.parse_args()

# ─── Quality Preset ───────────────────────────────────────────────────────────
# Qualität 1-10: Trade-off zwischen Zeit- und Frequenzauflösung
#  1 = sehr schnell (wenig Bänder, kleiner Hop, kleine FFT)
#  5 = ausgewogen (Standard)
# 10 = hochauflösend (viele Bänder, großer Hop, große FFT)
if ARGS.quality != 5 or ARGS.nfft == 0:
    q = ARGS.quality
    if ARGS.bands == 128:  # nur überschreiben wenn Default
        ARGS.bands = int(np.interp(q, [1, 10], [48, 320]))
    if ARGS.hop == 512:  # nur überschreiben wenn Default
        ARGS.hop = int(np.interp(q, [1, 10], [128, 2048]))
    if ARGS.nfft == 0:
        ARGS.nfft = int(np.interp(q, [1, 10], [2048, 16384]))
    # Block-Size für CWT anpassen
    ARGS._cwt_block = max(16, ARGS.bands // 4)

setup_logging(
    level=logging.DEBUG if ARGS.debug else logging.INFO,
    logfile=ARGS.logfile,
)

# ─── Imports ──────────────────────────────────────────────────────────────────

try:
    import pywt
except ImportError:
    log.critical("PyWavelets fehlt: pip install PyWavelets")
    sys.exit(1)

try:
    import jack
except ImportError:
    log.critical("JACK-Client fehlt: pip install JACK-Client")
    sys.exit(1)

import gi
gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gtk, Gdk, GLib
import cairo

log.debug("Alle Imports erfolgreich")


# ═══════════════════════════════════════════════════════════════════════════════
# FFT-BASED CWT ENGINE
# ═══════════════════════════════════════════════════════════════════════════════

class CWTFFT:
    """
    FFT-basierte Continuous Wavelet Transform fuer komplexe Morlet-Wavelets.

    Die CWT wird im Frequenzbereich berechnet:
        CWT(a) = IFT[ X(f) * conj(Psi_a(f)) ]

    wobei Psi_a(f) die FT des skalierten Morlet-Wavelets ist:
        Psi_a(f) = sqrt(a) * exp(-(2*pi*a*(f-fc))^2 / (2*fb))

    Dies ist ~50x schneller als pywt.cwt fuer typische Parameter.

    Parameters
    ----------
    sr : float
        Samplerate in Hz
    freq_min, freq_max : float
        Frequenzbereich in Hz
    num_bands : int
        Anzahl Frequenzbaender (logarithmisch verteilt)
    wavelet_name : str
        Wavelet-Typ: 'cmorB-FC' (B=Bandwidth, FC=Center-Freq)
    """

    def __init__(self, sr, freq_min, freq_max, num_bands, wavelet_name):
        self.sr = sr
        self.dt = 1.0 / sr
        self.num_bands = num_bands
        self.wavelet_name = wavelet_name
        self.PI = np.pi

        # Logarithmisch verteilte Frequenzen
        self.freqs = np.logspace(np.log10(freq_min), np.log10(freq_max), num_bands)

        # Skalen aus Frequenzen
        self.scales = pywt.frequency2scale(wavelet_name, self.freqs * self.dt)

        # FFT-Laenge (next power of 2)
        min_len = ARGS.nfft if hasattr(ARGS, 'nfft') else 4096
        self._nfft = 1 << (max(min_len, 2048) - 1).bit_length()

        # rfft-Laenge (nur positive Frequenzen)
        self._nrfft = self._nfft // 2 + 1

        # FFT-Frequenz-Achse (nur positiv fuer rfft)
        self._fft_freqs = np.fft.rfftfreq(self._nfft, d=self.dt)

        # Wavelet-Parameter aus Name parsen
        self._parse_wavelet_params(wavelet_name)

        # Vorberechnete Wavelet-FTs (nur positive Frequenzen)
        self._psi_ft = self._build_wavelet_spectrum()

        # Block-Size für Cache-freundliches Processing
        self._block_size = getattr(ARGS, '_cwt_block', max(16, num_bands // 4))

        log.info("CWT-FFT init: nfft=%d, bands=%d, block=%d, wavelet=%s",
                 self._nfft, num_bands, self._block_size, wavelet_name)

    def _parse_wavelet_params(self, name):
        """Parse Wavelet-Parameter aus dem Namen."""
        if name.startswith("cmor"):
            parts = name.replace("cmor", "").split("-")
            self._fb = float(parts[0]) if len(parts) > 0 else 1.0
            self._fc = float(parts[1]) if len(parts) > 1 else 1.0
            self._omega0 = 2 * self.PI * self._fc  # Morlet-Parameter
        elif name.startswith("gaus"):
            order = int(name.replace("g", "").replace("aus", ""))
            self._fb = float(order)
            self._fc = 0.0
            self._omega0 = 2 * self.PI * 0.25  # Gauss-Wavelet
        elif name == "morl":
            self._fb = 1.0
            self._fc = 0.8125  # Standard Morlet
            self._omega0 = 2 * self.PI * self._fc
        else:
            self._fb = 1.0
            self._fc = 1.0
            self._omega0 = 2 * self.PI

    def _build_wavelet_spectrum(self):
        """
        Berechnet die Fourier-Transformierte fuer jede skalierte Wavelet.

        Fuer Morlet: Psi_a(f) = sqrt(a) * exp(-(f - f_center)^2 / (2*sigma_f^2))
        mit f_center = omega0 / (2*pi*a*dt) und sigma_f = fb / (pi*a*dt)

        Fuer Gaussian (gaus4): Psi_a(f) = sqrt(a) * exp(-2 * (pi*f*dt*a)^2) * (2*pi*i*f*dt*a)^4
        """
        nfft = self._nfft
        nrfft = self._nrfft
        psi = np.zeros((self.num_bands, nrfft), dtype=np.complex128)
        fft_freqs = self._fft_freqs  # nur positive Frequenzen

        if self.wavelet_name.startswith("gaus"):
            # Gaussian Wavelet: FT = sqrt(a) * exp(-2 * (2*pi*f*dt*a)^2) * (i*omega)^n
            n = int(self._fb)  # Ordnung
            f = fft_freqs[:, np.newaxis]  # (nrfft, 1)
            a = self.scales * self.dt  # (num_bands,)
            omega = 2.0 * np.pi * f * a  # (nrfft, num_bands)
            gauss = np.exp(-2.0 * omega ** 2)
            poly = (1j * omega) ** n
            psi = (np.sqrt(self.scales)[np.newaxis, :] * gauss * poly).T  # (num_bands, nrfft)
        else:
            for i, a in enumerate(self.scales):
                f_center = self._omega0 / (2 * self.PI * a * self.dt)
                sigma_f = self._fb / (self.PI * a * self.dt)
                psi[i] = np.exp(-0.5 * ((fft_freqs - f_center) / max(sigma_f, 1e-10)) ** 2)
                psi[i] *= np.sqrt(a)

        return psi

    def process(self, signal):
        """
        Berechnet eine CWT-Spalte fuer das gegebene Signal.
        Block-Processing fuer grosse Band-Zahlen (Cache-freundlich).
        """
        nfft = self._nfft

        # Buffer zuschneiden/padden
        if len(signal) >= nfft:
            buf = signal[-nfft:]
        else:
            buf = np.zeros(nfft)
            buf[:len(signal)] = signal

        # rfft (einmal)
        sig_ft = np.fft.rfft(buf)

        # Block-Processing: vermeidet Cache-Misses bei vielen Baendern
        block = self._block_size
        energy = np.empty(self.num_bands, dtype=np.float64)
        for start in range(0, self.num_bands, block):
            end = min(start + block, self.num_bands)
            products = self._psi_ft[start:end] * sig_ft[np.newaxis, :]
            energy[start:end] = np.sqrt(
                (products.real ** 2 + products.imag ** 2).sum(axis=1) / nfft
            )

        # dB-Normalisierung
        max_e = np.max(energy)
        if max_e > 0:
            energy_db = 20.0 * np.log10(energy / max_e + 1e-30)
        else:
            energy_db = np.full(self.num_bands, -120.0)

        return energy_db


# ═══════════════════════════════════════════════════════════════════════════════
# JACK CAPTURE THREAD
# ═══════════════════════════════════════════════════════════════════════════════

MAX_QUEUE = 4


class JackCapture(threading.Thread):
    """JACK-Client: sammelt Audio, berechnet CWT asynchron."""

    def __init__(self, client_name):
        super().__init__(daemon=True)
        self._q = queue.Queue(maxsize=MAX_QUEUE)
        self._work_q = queue.Queue(maxsize=8)
        self._running = threading.Event()
        self._running.set()
        self._level = 0.0
        self._columns_computed = 0
        self._overruns = 0
        self.client = None
        self.cwt = None

        # JACK
        try:
            self.client = jack.Client(client_name, no_start_server=True)
        except jack.JackError as e:
            log.critical("JACK-Server nicht erreichbar: %s", e)
            log.info("Starte mit: jackd -d alsa &")
            sys.exit(1)

        self.sr = float(self.client.samplerate)
        self.blocksize = self.client.blocksize

        # CWT-Engine
        self.cwt = CWTFFT(
            self.sr, ARGS.freq_min, ARGS.freq_max,
            ARGS.bands, ARGS.wavelet,
        )

        # Ringbuffer
        self._buf_len = self.cwt._nfft
        self._ring = np.zeros(self._buf_len, dtype=np.float64)
        self._ring_pos = 0
        self._hop_counter = 0

        # Port
        self.port = self.client.inports.register("input_1")
        self.client.set_process_callback(self._process_cb)

        log.info("JACK: sr=%.0f Hz, blocksize=%d", self.sr, self.blocksize)

    def _process_cb(self, nframes):
        """JACK Process Callback - muss Echtzeit-sicher sein."""
        try:
            data = np.asarray(self.port.get_array(), dtype=np.float64)
        except Exception:
            return None

        # Level (exponentiell gleitend)
        inst_level = np.sqrt(np.mean(data ** 2))
        self._level = 0.9 * self._level + 0.1 * inst_level

        # Bei Pause keine Daten verarbeiten
        if not self._running.is_set():
            return None

        # Ringbuffer
        n = len(data)
        if n >= self._buf_len:
            self._ring = data[-self._buf_len:].copy()
            self._ring_pos = 0
        else:
            end = self._ring_pos + n
            if end <= self._buf_len:
                self._ring[self._ring_pos:end] = data
            else:
                first = self._buf_len - self._ring_pos
                self._ring[self._ring_pos:] = data[:first]
                self._ring[:n - first] = data[first:]
            self._ring_pos = end % self._buf_len

        self._hop_counter += n
        if self._hop_counter >= ARGS.hop:
            self._hop_counter = 0
            try:
                self._work_q.put_nowait(self._ring.copy())
            except queue.Full:
                self._overruns += 1

        return None

    def _worker(self):
        """Persistenter CWT Worker-Thread."""
        while self._running.is_set():
            try:
                buf = self._work_q.get(timeout=0.1)
            except queue.Empty:
                continue
            try:
                t0 = time.monotonic()
                col = self.cwt.process(buf)
                dt_ms = (time.monotonic() - t0) * 1000.0
                hop_interval_ms = ARGS.hop / self.sr * 1000
                if dt_ms > hop_interval_ms * 0.5:
                    log.warning("CWT langsam: %.1fms (hop: %.1fms)", dt_ms, hop_interval_ms)

                try:
                    self._q.put_nowait(col)
                    self._columns_computed += 1
                except queue.Full:
                    self._overruns += 1
            except Exception as e:
                log.error("CWT-Fehler: %s", e)

    def start(self):
        self.client.activate()
        if ARGS.connect:
            self._autoconnect()
        # Worker-Thread fuer CWT starten
        self._worker_thread = threading.Thread(target=self._worker, daemon=True)
        self._worker_thread.start()
        super().start()
        log.info("JACK Capture aktiv")

    def _autoconnect(self):
        try:
            sources = self.client.get_ports(is_physical=True, is_output=True, is_audio=True)
            if sources:
                self.client.connect(sources[0], self.port)
                log.info("Verbunden: %s -> %s", sources[0].name, self.port.name)
            else:
                log.warning("Keine JACK-Quellen fuer Autoconnect")
        except Exception as e:
            log.warning("Autoconnect: %s", e)

    def stop(self):
        self._running.clear()
        if self.client:
            try:
                self.client.deactivate()
                self.client.close()
            except Exception:
                pass
        while not self._q.empty():
            try:
                self._q.get_nowait()
            except queue.Empty:
                break
        log.info("Capture gestoppt (Spalten: %d, Overruns: %d)",
                 self._columns_computed, self._overruns)

    def get_column(self):
        try:
            return self._q.get_nowait()
        except queue.Empty:
            return None

    @property
    def level(self):
        return self._level

    def run(self):
        self._running.wait(1e9)


# ═══════════════════════════════════════════════════════════════════════════════
# COLORMAP
# ═══════════════════════════════════════════════════════════════════════════════

def build_colormap(name="viridis", n=256):
    """Erzeugt Colormap als (n, 4) uint8 BGRA-Array fuer Cairo."""
    maps = {
        "viridis": [
            (0.267, 0.004, 0.329), (0.282, 0.140, 0.458),
            (0.254, 0.265, 0.529), (0.207, 0.372, 0.553),
            (0.163, 0.471, 0.558), (0.128, 0.567, 0.551),
            (0.135, 0.659, 0.518), (0.267, 0.749, 0.441),
            (0.478, 0.821, 0.318), (0.741, 0.873, 0.154),
            (0.993, 0.906, 0.144),
        ],
        "inferno": [
            (0.001, 0.000, 0.014), (0.067, 0.013, 0.105),
            (0.189, 0.055, 0.167), (0.341, 0.089, 0.153),
            (0.500, 0.138, 0.118), (0.655, 0.199, 0.080),
            (0.796, 0.287, 0.043), (0.902, 0.416, 0.016),
            (0.976, 0.576, 0.078), (0.988, 0.765, 0.255),
            (0.988, 0.998, 0.645),
        ],
        "magma": [
            (0.001, 0.000, 0.014), (0.067, 0.008, 0.100),
            (0.184, 0.051, 0.195), (0.337, 0.076, 0.227),
            (0.500, 0.118, 0.227), (0.663, 0.173, 0.212),
            (0.804, 0.251, 0.188), (0.918, 0.365, 0.184),
            (0.984, 0.522, 0.267), (0.996, 0.712, 0.420),
            (0.987, 0.991, 0.750),
        ],
        "plasma": [
            (0.050, 0.030, 0.528), (0.200, 0.020, 0.650),
            (0.350, 0.050, 0.640), (0.500, 0.110, 0.590),
            (0.650, 0.180, 0.510), (0.770, 0.270, 0.410),
            (0.870, 0.390, 0.300), (0.940, 0.530, 0.200),
            (0.980, 0.690, 0.130), (0.990, 0.850, 0.110),
            (0.940, 0.980, 0.130),
        ],
        "hot": [
            (0.0, 0.0, 0.0), (0.3, 0.0, 0.0), (0.6, 0.0, 0.0),
            (1.0, 0.0, 0.0), (1.0, 0.3, 0.0), (1.0, 0.6, 0.0),
            (1.0, 1.0, 0.0), (1.0, 1.0, 0.5), (1.0, 1.0, 1.0),
        ],
        "cool": [
            (0.0, 0.0, 0.0), (0.0, 0.1, 0.3), (0.0, 0.2, 0.5),
            (0.0, 0.4, 0.7), (0.0, 0.6, 0.9), (0.1, 0.8, 1.0),
            (0.3, 0.9, 0.8), (0.5, 1.0, 0.5), (0.8, 1.0, 0.2),
            (1.0, 0.8, 0.0), (1.0, 0.4, 0.0),
        ],
    }

    colors = maps.get(name, maps["viridis"])
    positions = np.linspace(0, 1, len(colors))
    result = np.zeros((n, 4), dtype=np.uint8)

    for i in range(n):
        t = i / max(n - 1, 1)
        idx = np.searchsorted(positions, t)
        if idx == 0:
            c = colors[0]
        elif idx >= len(colors):
            c = colors[-1]
        else:
            frac = (t - positions[idx - 1]) / max(positions[idx] - positions[idx - 1], 1e-10)
            c = tuple(colors[idx - 1][j] * (1 - frac) + colors[idx][j] * frac for j in range(3))
        # Cairo BGRA
        result[i] = [int(c[2] * 255), int(c[1] * 255), int(c[0] * 255), 255]

    return result


COLORMAPS = ["viridis", "inferno", "magma", "plasma", "hot", "cool"]


# ═══════════════════════════════════════════════════════════════════════════════
# GTK SPECTROGRAM WIDGET
# ═══════════════════════════════════════════════════════════════════════════════

class SpectrogramWidget(Gtk.DrawingArea):
    """Custom GTK Widget: Wasserfall-Spektrogramm + Momentan-Spektrum."""

    def __init__(self, capture):
        super().__init__()
        self.capture = capture
        self.history = ARGS.history
        self.num_bands = ARGS.bands
        self.threshold = ARGS.threshold
        self.ceiling = ARGS.ceiling
        self._cmap_idx = COLORMAPS.index(ARGS.colormap) if ARGS.colormap in COLORMAPS else 0
        self.cmap = build_colormap(COLORMAPS[self._cmap_idx])

        # Wasserfall-Puffer: (bands, history)
        self.waterfall = np.full(
            (self.num_bands, self.history),
            self.threshold - 20.0,
            dtype=np.float64,
        )
        self._col_write = 0
        self._spectrum = np.full(self.num_bands, self.threshold - 20.0)

        # Frequenz-Achse
        self.freqs = np.logspace(
            np.log10(ARGS.freq_min), np.log10(ARGS.freq_max), self.num_bands,
        )

        # FPS
        self._fps_count = 0
        self._fps_time = time.monotonic()
        self._fps = 0

        # Timer
        self.set_size_request(ARGS.width, ARGS.height)
        self.connect("draw", self._on_draw)
        GLib.timeout_add(33, self._on_timer)

    def _on_timer(self):
        updated = False
        col = self.capture.get_column()
        while col is not None:
            self.waterfall[:, self._col_write] = col
            self._col_write = (self._col_write + 1) % self.history
            self._spectrum = col.copy()
            updated = True
            col = self.capture.get_column()

        if updated:
            self._fps_count += 1
            now = time.monotonic()
            if now - self._fps_time >= 1.0:
                self._fps = self._fps_count / (now - self._fps_time)
                self._fps_count = 0
                self._fps_time = now
            self.queue_draw()

        return True

    def _on_draw(self, widget, cr):
        w = self.get_allocated_width()
        h = self.get_allocated_height()

        # Layout
        h_wf = int(h * 0.68)
        h_sp = h - h_wf
        ml, mr, mt, mb = 55, 10, 20, 25
        plot_w = w - ml - mr
        plot_h_wf = h_wf - mt - mb
        plot_h_sp = h_sp - 10

        # Hintergrund
        cr.set_source_rgb(0.04, 0.04, 0.06)
        cr.paint()

        if plot_w > 0 and plot_h_wf > 0:
            self._draw_waterfall(cr, ml, mt, plot_w, plot_h_wf)

        if plot_w > 0 and plot_h_sp > 0:
            self._draw_spectrum(cr, ml, h_wf + 5, plot_w, plot_h_sp)

        self._draw_info(cr, ml, h - 2, w - ml - mr)
        return False

    def _draw_waterfall(self, cr, x0, y0, w, h):
        """
        Wasserfall: Zeit von unten (neu) nach oben (alt),
        Frequenz von links (tief) nach rechts (hoch).
        Vektorisiert: keine Python-for-Schleifen über Pixel.
        """
        bands = self.num_bands
        cols = self.history

        x_step = max(1, bands // w)
        y_step = max(1, cols // h)
        draw_rows = min(bands // x_step, w)
        draw_cols = min(cols // y_step, h)

        if draw_rows < 1 or draw_cols < 1:
            return

        rng = self.ceiling - self.threshold

        # Zeit-Indizes: unten=neu (col_write-1), oben=alt
        t_idx = np.arange(draw_cols)
        data_cols = (self._col_write - 1 - (draw_cols - 1 - t_idx) * y_step) % cols
        data_cols[data_cols < 0] += cols  # (draw_cols,)

        # Frequenz-Indizes: links=tief (0), rechts=hoch (bands-1)
        f_idx = np.arange(draw_rows)
        data_rows = f_idx * x_step  # (draw_rows,)
        data_rows[data_rows >= bands] = bands - 1

        # Daten extrahieren: waterfall[data_rows, data_cols]
        # data_rows als Spalten-Indizes, data_cols als Zeilen-Indizes
        vals = self.waterfall[data_rows[:, np.newaxis], data_cols[np.newaxis, :]]  # (draw_rows, draw_cols)
        vals = vals.T  # (draw_cols, draw_rows) = (zeit, freq)

        # Normalisieren und Colormap (vektorisiert)
        norm = (vals - self.threshold) / rng
        np.clip(norm, 0.0, 1.0, out=norm)
        ci = (norm * 255).astype(np.intp)

        # Colormap anwenden
        buf = self.cmap[ci]  # (draw_cols, draw_rows, 4)

        try:
            surface = cairo.ImageSurface.create_for_data(
                bytearray(buf.tobytes()), cairo.FORMAT_ARGB32,
                draw_rows, draw_cols, draw_rows * 4,
            )
            cr.save()
            cr.translate(x0, y0)
            cr.scale(w / draw_rows, h / draw_cols)
            cr.set_source_surface(surface, 0, 0)
            cr.paint()
            cr.restore()
        except Exception as e:
            log.error("Cairo-Surface: %s", e)

        self._draw_wf_axes(cr, x0, y0, w, h)

    def _draw_wf_axes(self, cr, x0, y0, w, h):
        """Achsen: Frequenz unten (log), Zeit links."""
        cr.set_font_size(9)
        cr.select_font_face("monospace", 0, 0)

        # Frequenz-Achse (unten, horizontal)
        freq_ticks = [50, 63, 80, 100, 125, 160, 200, 250, 315, 400, 500,
                      630, 800, 1000, 1250, 1600, 2000, 2500, 3150, 4000,
                      5000, 6300, 8000, 10000, 12500, 16000, 20000]
        freq_ticks = [f for f in freq_ticks if ARGS.freq_min <= f <= ARGS.freq_max]
        log_fmin = np.log10(ARGS.freq_min)
        log_fmax = np.log10(ARGS.freq_max)

        for f in freq_ticks:
            log_pos = (np.log10(f) - log_fmin) / (log_fmax - log_fmin)
            x = x0 + w * log_pos
            cr.set_source_rgba(0.25, 0.25, 0.25, 0.5)
            cr.set_line_width(0.5)
            cr.move_to(x, y0)
            cr.line_to(x, y0 + h)
            cr.stroke()
            label = str(f) if f < 1000 else str(f // 1000) + "k"
            cr.set_source_rgb(0.55, 0.55, 0.55)
            cr.move_to(x + 2, y0 + h - 3)
            cr.show_text(label)

        # Zeit-Achse (links, vertikal): unten = neu, oben = alt
        n_ticks = 8
        for i in range(n_ticks + 1):
            y = y0 + h * (1.0 - i / n_ticks)  # unten=i=0, oben=i=n_ticks
            cr.set_source_rgba(0.25, 0.25, 0.25, 0.3)
            cr.set_line_width(0.5)
            cr.move_to(x0, y)
            cr.line_to(x0 + w, y)
            cr.stroke()
            # Zeitlabel: unten = 0 (neu), oben = max (alt)
            secs_back = i * ARGS.hop * self.history / self.capture.sr / n_ticks
            if secs_back >= 1:
                label = "-%.1fs" % secs_back
            else:
                label = "-%.0fms" % (secs_back * 1000)
            cr.set_source_rgb(0.45, 0.45, 0.45)
            cr.set_font_size(8)
            cr.move_to(x0 + 3, y - 3)
            cr.show_text(label)

        cr.set_source_rgb(0.35, 0.35, 0.35)
        cr.set_line_width(1)
        cr.rectangle(x0, y0, w, h)
        cr.stroke()

    def _draw_spectrum(self, cr, x0, y0, w, h):
        if w <= 0 or h <= 0:
            return

        cr.set_source_rgb(0.06, 0.06, 0.10)
        cr.rectangle(x0, y0, w, h)
        cr.fill()

        cr.set_line_width(0.5)
        for db in range(int(self.threshold), int(self.ceiling) + 1, 10):
            y = y0 + h * (1.0 - (db - self.threshold) / (self.ceiling - self.threshold))
            cr.set_source_rgba(0.2, 0.2, 0.2, 0.5)
            cr.move_to(x0, y)
            cr.line_to(x0 + w, y)
            cr.stroke()
            cr.set_source_rgb(0.4, 0.4, 0.4)
            cr.set_font_size(7)
            cr.move_to(x0 + 2, y - 2)
            cr.show_text(str(db))

        log_fmin = np.log10(ARGS.freq_min)
        log_fmax = np.log10(ARGS.freq_max)
        rng = self.ceiling - self.threshold

        cr.set_source_rgb(0.0, 0.85, 1.0)
        cr.set_line_width(1.5)

        started = False
        for i in range(self.num_bands):
            log_f = np.log10(self.freqs[i])
            x = x0 + w * (log_f - log_fmin) / (log_fmax - log_fmin)
            val = self._spectrum[i]
            y_norm = max(0.0, min(1.0, (val - self.threshold) / rng))
            y = y0 + h * (1.0 - y_norm)
            if not started:
                cr.move_to(x, y)
                started = True
            else:
                cr.line_to(x, y)
        cr.stroke()

        cr.set_source_rgba(0.0, 0.6, 0.8, 0.15)
        cr.line_to(x0 + w, y0 + h)
        cr.line_to(x0, y0 + h)
        cr.close_path()
        cr.fill()

        cr.set_source_rgb(0.3, 0.3, 0.3)
        cr.set_line_width(1)
        cr.rectangle(x0, y0, w, h)
        cr.stroke()

    def _draw_info(self, cr, x0, y0, w):
        cr.set_font_size(10)
        cr.select_font_face("monospace", 0, 0)

        level_db = 20 * np.log10(self.capture.level + 1e-30)
        cmap_name = COLORMAPS[self._cmap_idx]

        info = (
            "Wavelet: %s | Freq: %.0f-%.0f Hz | Bands: %d | Hop: %d | "
            "Range: %.0f..%.0f dB | CM: %s | FPS: %.0f | Level: %.1f dB"
            % (ARGS.wavelet, ARGS.freq_min, ARGS.freq_max, ARGS.bands, ARGS.hop,
               self.threshold, self.ceiling, cmap_name, self._fps, level_db)
        )

        cr.set_source_rgb(0.6, 0.6, 0.6)
        cr.move_to(x0, y0 - 3)
        cr.show_text(info)


# ═══════════════════════════════════════════════════════════════════════════════
# HAUPTFENSTER
# ═══════════════════════════════════════════════════════════════════════════════

class MainWindow(Gtk.Window):
    def __init__(self, capture):
        super().__init__(title="WaveletSpect")
        self.capture = capture

        self.set_default_size(ARGS.width, ARGS.height)
        self.set_position(Gtk.WindowPosition.CENTER)

        header = Gtk.HeaderBar()
        header.set_show_close_button(True)
        header.set_title("WaveletSpect")
        header.set_subtitle(
            "CWT | %s | %.0f-%.0f Hz" % (ARGS.wavelet, ARGS.freq_min, ARGS.freq_max)
        )
        self.set_titlebar(header)

        self.spectro = SpectrogramWidget(capture)
        self.add(self.spectro)

        self.connect("key-press-event", self._on_key)
        self.connect("destroy", self._on_destroy)

        log.info("Fenster: %dx%d", ARGS.width, ARGS.height)

    def _on_key(self, widget, event):
        key = Gdk.keyval_name(event.keyval)

        if key in ("q", "Escape"):
            self._on_destroy(self)
        elif key in ("space", "Pause"):
            self._paused = not getattr(self, '_paused', False)
            self.capture._running.clear() if self._paused else self.capture._running.set()
            log.info("Pause: %s", self._paused)
        elif key in ("plus", "equal"):
            self.spectro.ceiling = min(self.spectro.ceiling + 5, 20)
            log.debug("Ceiling: %.0f dB", self.spectro.ceiling)
        elif key == "minus":
            self.spectro.ceiling = max(self.spectro.ceiling - 5, self.spectro.threshold + 10)
            log.debug("Ceiling: %.0f dB", self.spectro.ceiling)
        elif key == "c":
            self.spectro._cmap_idx = (self.spectro._cmap_idx + 1) % len(COLORMAPS)
            self.spectro.cmap = build_colormap(COLORMAPS[self.spectro._cmap_idx])
            log.info("Colormap: %s", COLORMAPS[self.spectro._cmap_idx])
        elif key == "l":
            self.spectro.threshold = min(self.spectro.threshold + 5, self.spectro.ceiling - 10)
            log.debug("Threshold: %.0f dB", self.spectro.threshold)
        elif key == "k":
            self.spectro.threshold = max(self.spectro.threshold - 5, -120)
            log.debug("Threshold: %.0f dB", self.spectro.threshold)
        elif key == "r":
            self.spectro.threshold = ARGS.threshold
            self.spectro.ceiling = ARGS.ceiling
            log.info("Range reset: %.0f..%.0f dB", self.spectro.threshold, self.spectro.ceiling)
        elif key in "123456789":
            q = int(key)
            self._restart_quality(q)

        return True

    def _restart_quality(self, q):
        """Qualität zur Laufzeit ändern (neue CWT-Engine)."""
        import numpy as np
        ARGS.bands = int(np.interp(q, [1, 10], [48, 320]))
        ARGS.hop = int(np.interp(q, [1, 10], [128, 2048]))
        ARGS.nfft = int(np.interp(q, [1, 10], [2048, 16384]))
        ARGS._cwt_block = max(16, ARGS.bands // 4)
        log.info("Qualitaet %d: bands=%d, hop=%d, nfft=%d", q, ARGS.bands, ARGS.hop, ARGS.nfft)
        # CWT-Engine neu initialisieren
        self.capture.cwt = CWTFFT(
            self.capture.sr, ARGS.freq_min, ARGS.freq_max,
            ARGS.bands, ARGS.wavelet,
        )
        # Wasserfall und Frequenz-Achse anpassen
        self.spectro.num_bands = ARGS.bands
        self.spectro.waterfall = np.full(
            (ARGS.bands, self.spectro.history), ARGS.threshold - 20.0, dtype=np.float64,
        )
        self.spectro.freqs = np.logspace(
            np.log10(ARGS.freq_min), np.log10(ARGS.freq_max), ARGS.bands,
        )
        self.spectro._spectrum = np.full(ARGS.bands, ARGS.threshold - 20.0)

    def _on_destroy(self, win):
        log.info("Beende...")
        self.capture.stop()
        Gtk.main_quit()


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    log.info("=" * 60)
    log.info("  WaveletSpect - FFT-basiertes CWT Spektrogramm")
    log.info("=" * 60)
    log.info("  Client:    %s", ARGS.client)
    log.info("  Freq:      %.0f - %.0f Hz", ARGS.freq_min, ARGS.freq_max)
    log.info("  Bander:    %d", ARGS.bands)
    log.info("  Historie:  %d", ARGS.history)
    log.info("  Hop:       %d", ARGS.hop)
    log.info("  Wavelet:   %s", ARGS.wavelet)
    log.info("  Range:     %.0f - %.0f dB", ARGS.threshold, ARGS.ceiling)
    log.info("  Colormap:  %s", ARGS.colormap)
    log.info("  Tastatur:  q=Beenden, +/- =Range, c=Farbe, l/k=Threshold, r=Reset")
    log.info("=" * 60)

    capture = JackCapture(ARGS.client)
    capture.start()

    win = MainWindow(capture)
    win.show_all()

    try:
        Gtk.main()
    except KeyboardInterrupt:
        log.info("SIGINT")
    finally:
        capture.stop()
        capture.join(timeout=3)
        log.info("Beendet.")


if __name__ == "__main__":
    main()
