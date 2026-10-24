"""
Spotify Mini Player (Windows)

A small, always-on-top, HUD-style "now playing" window. It connects to the
Windows system media session (SMTC), so no Spotify API, Premium account or
Client ID is needed.

Usage:
  - Drag the window by any empty area.
  - Hover over it to reveal the previous / play-pause / next buttons and the
    volume slider.
  - Click or drag on the progress bar to seek.
  - Mouse wheel over the window: volume. Ctrl + mouse wheel: resize.
  - Double-click an empty area: switch between full and compact mode.
  - Gear icon (top right): themes, colors, size, opacity, font, lyrics.
  - Right-click: menu.

Synced lyrics are fetched from LRCLIB (https://lrclib.net), not from Spotify.
Volume uses the optional "pycaw" package (Spotify's volume, or the system volume).
Settings are stored in %APPDATA%\\SpotifyMiniPlayer\\config.json
"""

import asyncio
import bisect
import colorsys
import io
import json
import math
import os
import re
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from tkinter import ttk

from PIL import Image, ImageTk
from winsdk.windows.media.control import (
    GlobalSystemMediaTransportControlsSessionManager as MediaManager,
    GlobalSystemMediaTransportControlsSessionPlaybackStatus as PlaybackStatus,
)
from winsdk.windows.storage.streams import Buffer, DataReader, InputStreamOptions

# ----------------------------------------------------------------------------
# Constants (all layout values are in "logical" pixels at 100% size)
# ----------------------------------------------------------------------------
FONT_FAMILY = "Consolas"  # default font

W0, H0 = 430, 110            # full mode: player part
LYR_TOP0, LYR_H0 = 108, 102  # lyrics panel (below the player part)
LYR_LH = 26                  # lyric line spacing
LYR_SLOTS = 7
LYRIC_LEAD_MS = 300          # show lines slightly early so they are readable

CW0, CH0 = 260, 56           # compact mode

SCALE_MIN, SCALE_MAX, SCALE_STEP = 0.6, 2.0, 0.05
OPACITY_MIN, OPACITY_DEFAULT = 0.3, 0.95
VOLUME_STEP = 0.05

KEY = "#010203"  # color key used to make the window corners transparent
GREEN = "#1DB954"
LOGO_DARK = "#050505"

COVER = 68
COVER_X, COVER_Y = 12, 12
TX = 96                       # left edge of the text
TITLE_Y, ARTIST_Y = 46, 68
BAR_X1, BAR_X2, BAR_Y = 56, W0 - 56, 94

BTN_Y = 68
BTN_X = {"prev": W0 - 96, "toggle": W0 - 64, "next": W0 - 32}
GEAR_POS = (W0 - 62, 24)
LOGO_POS = (W0 - 28, 24)
NUDGE = 22  # how far the content slides when you skip a track

VOL_Y = 22                                   # volume slider row (top, revealed on hover)
VOL_ICON_X, VOL_X1, VOL_X2 = 192, 206, 306

# Compact mode layout
C_COVER, C_COVER_X, C_COVER_Y = 40, 8, 8
C_TX, C_TITLE_Y = 58, 24
C_BAR_Y = 44

# The three arcs of the Spotify logo: (left end, peak, right end, thickness) in a 24x24 box
SPOTIFY_BARS = [
    ((4.8, 8.3), (12.2, 7.4), (19.6, 9.8), 2.5),
    ((5.5, 12.5), (12.0, 11.7), (18.4, 13.7), 2.1),
    ((6.1, 16.2), (11.8, 15.3), (17.0, 17.2), 1.7),
]

DEFAULT_THEME = {
    "bg": "#161616",      # background
    "border": "#2c2c2c",  # border
    "title": "#ffffff",   # song title, icons, current lyric
    "text2": "#b3b3b3",   # artist, times, other lyrics
    "accent": "#1db954",  # progress bar, equalizer, ripple
}

PRESETS = {
    "Black": DEFAULT_THEME,
    "Dark gray": {"bg": "#2b2b2b", "border": "#404040", "title": "#ffffff",
                  "text2": "#bdbdbd", "accent": "#ffffff"},
    "Midnight": {"bg": "#2a3541", "border": "#3b4a5a", "title": "#ffffff",
                 "text2": "#aab5c2", "accent": "#ffffff"},
    "Purple": {"bg": "#241a33", "border": "#3a2b52", "title": "#ffffff",
               "text2": "#b9a8d6", "accent": "#b57bff"},
    "Burgundy": {"bg": "#2a1519", "border": "#46222a", "title": "#ffffff",
                 "text2": "#d0a9b0", "accent": "#ff4d6d"},
    "Light": {"bg": "#f2f2f2", "border": "#d0d0d0", "title": "#111111",
              "text2": "#555555", "accent": "#1db954"},
}

THEME_LABELS = [
    ("bg", "Background"),
    ("border", "Border"),
    ("title", "Title text, icons, current lyric"),
    ("text2", "Secondary text"),
    ("accent", "Accent (bar, equalizer)"),
]

CONFIG_DIR = os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"),
                          "SpotifyMiniPlayer")
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")

LRCLIB = "https://lrclib.net/api"
USER_AGENT = "SpotifyMiniPlayer/1.4 (https://github.com/Mc-Kaan/Spotify-Mini-Player)"


# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------
def fmt(ms):
    s = int(max(ms, 0) // 1000)
    return f"{s // 60}:{s % 60:02d}"


def normalize_hex(text, allow_short=True):
    """Turns '#abc' / 'AABBCC' into '#aabbcc'. Returns None if invalid."""
    s = (text or "").strip().lstrip("#")
    if allow_short and len(s) == 3:
        s = "".join(ch * 2 for ch in s)
    if len(s) == 6 and re.fullmatch(r"[0-9a-fA-F]{6}", s):
        return "#" + s.lower()
    return None


def hex_rgb(c):
    return tuple(int(c[i:i + 2], 16) for i in (1, 3, 5))


def mix(c1, c2, t):
    a, b = hex_rgb(c1), hex_rgb(c2)
    t = max(0.0, min(1.0, t))
    return "#%02x%02x%02x" % tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


def luminance(c):
    r, g, b = hex_rgb(c)
    return (0.2126 * r + 0.7152 * g + 0.0722 * b) / 255


def clamp(v, lo=0.0, hi=1.0):
    return max(lo, min(hi, v))


def ease_out(t):
    return 1 - (1 - t) ** 3


def rounded_rect(cv, x1, y1, x2, y2, r, **kw):
    pts = [x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y2 - r, x2, y2,
           x2 - r, y2, x1 + r, y2, x1, y2, x1, y2 - r, x1, y1 + r, x1, y1]
    return cv.create_polygon(pts, smooth=True, **kw)


def fit(font, text, max_w):
    if font.measure(text) <= max_w:
        return text
    while text and font.measure(text + "…") > max_w:
        text = text[:-1]
    return text + "…"


def bar_points(p0, pm, p2, cx, cy, s=1.0, n=14):
    """Converts one arc of the Spotify logo (quadratic Bezier) into a point list."""
    c = (2 * pm[0] - 0.5 * (p0[0] + p2[0]), 2 * pm[1] - 0.5 * (p0[1] + p2[1]))
    pts = []
    for i in range(n + 1):
        t = i / n
        x = (1 - t) ** 2 * p0[0] + 2 * (1 - t) * t * c[0] + t * t * p2[0]
        y = (1 - t) ** 2 * p0[1] + 2 * (1 - t) * t * c[1] + t * t * p2[1]
        pts += [cx + (x - 12) * s, cy + (y - 12) * s]
    return pts


def gear_points(cx, cy, angle, r_out, r_in, teeth=8):
    pts = []
    step = 360 / teeth
    for i in range(teeth):
        a = angle + i * step
        for da, r in ((-0.30 * step, r_in), (-0.17 * step, r_out),
                      (0.17 * step, r_out), (0.30 * step, r_in)):
            rad = math.radians(a + da)
            pts += [cx + r * math.cos(rad), cy + r * math.sin(rad)]
    return pts


def lyr_alpha(d):
    """How visible a lyric line is, depending on its distance from the current line."""
    d = abs(d)
    if d <= 1:
        return 1 - 0.55 * d
    if d < 2:
        return 0.45 * (2 - d)
    return 0.0


def dominant_color(data, bg):
    """Picks a vivid, readable accent color from the album cover (None for gray covers)."""
    try:
        img = Image.open(io.BytesIO(data)).convert("RGB").resize((40, 40))
        raw = img.tobytes()
    except Exception:
        return None
    bins = {}
    for i in range(0, len(raw) - 2, 3):
        r, g, b = raw[i], raw[i + 1], raw[i + 2]
        h, s, v = colorsys.rgb_to_hsv(r / 255, g / 255, b / 255)
        if s < 0.25 or v < 0.2:
            continue
        w = s * v
        e = bins.setdefault(int(h * 12) % 12, [0.0, 0.0, 0.0, 0.0])
        e[0] += w
        e[1] += r * w
        e[2] += g * w
        e[3] += b * w
    if not bins:
        return None
    w, rs, gs, bs = bins[max(bins, key=lambda k: bins[k][0])]
    h, s, v = colorsys.rgb_to_hsv(rs / w / 255, gs / w / 255, bs / w / 255)
    s = max(s, 0.55)
    # Keep it readable on the current background (bright on dark, deeper on light)
    v = clamp(v, 0.75, 1.0) if luminance(bg) < 0.5 else clamp(v, 0.35, 0.65)
    r, g, b = colorsys.hsv_to_rgb(h, s, v)
    return "#%02x%02x%02x" % (int(r * 255), int(g * 255), int(b * 255))


def load_config():
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def write_config(data):
    try:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception:
        pass


# ----------------------------------------------------------------------------
# Lyrics (LRCLIB)
# ----------------------------------------------------------------------------
_LRC_LINE = re.compile(r"^((?:\s*\[\d{1,3}:\d{1,2}(?:[.:]\d{1,3})?\])+)\s*(.*)$")
_LRC_STAMP = re.compile(r"\[(\d{1,3}):(\d{1,2}(?:[.:]\d{1,3})?)\]")


def parse_lrc(text):
    """Parses '[mm:ss.xx] line' text into a sorted list of (milliseconds, line)."""
    out = []
    for raw in (text or "").splitlines():
        m = _LRC_LINE.match(raw)
        if not m:
            continue
        body = m.group(2).strip() or "♪"
        for mins, secs in _LRC_STAMP.findall(m.group(1)):
            ms = int((int(mins) * 60 + float(secs.replace(":", "."))) * 1000)
            out.append((ms, body))
    out.sort(key=lambda x: x[0])
    return out


def clean_title(title):
    t = re.sub(r"\s*[\(\[][^\)\]]*[\)\]]", "", title or "")
    t = re.sub(r"\s+-\s+.*$", "", t)
    return t.strip() or (title or "")


def first_artist(artist):
    parts = re.split(r"\s*(?:,|&|;|/|\bfeat\b\.?|\bft\b\.?)\s*", artist or "", maxsplit=1, flags=re.I)
    return parts[0].strip() or (artist or "")


def http_json(url, timeout=8):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise


def pick_best(results, dur_s):
    if not isinstance(results, list):
        return None
    cands = [r for r in results
             if isinstance(r, dict) and (r.get("syncedLyrics") or r.get("instrumental"))]
    if not cands:
        return None
    if dur_s > 0:
        cands.sort(key=lambda r: abs((r.get("duration") or 0) - dur_s))
        if abs((cands[0].get("duration") or 0) - dur_s) > 6:
            return None  # probably a different version of the song; sync would be off
    return cands[0]


def fetch_lyrics(title, artist, album, dur_s):
    """Returns ('ok', lines) | ('instrumental', None) | ('none', None)."""

    def finish(rec):
        if not isinstance(rec, dict):
            return None
        lines = parse_lrc(rec.get("syncedLyrics") or "")
        if lines:
            return ("ok", lines)
        if rec.get("instrumental"):
            return ("instrumental", None)
        return None

    # 1) exact match by track signature (preferred by LRCLIB)
    if title and artist:
        q = {"track_name": title, "artist_name": artist}
        if album:
            q["album_name"] = album
        if dur_s > 0:
            q["duration"] = int(round(dur_s))
        res = finish(http_json(f"{LRCLIB}/get?{urllib.parse.urlencode(q)}"))
        if res:
            return res

    # 2) search fallbacks
    attempts = [
        {"track_name": title, "artist_name": artist},
        {"track_name": clean_title(title), "artist_name": first_artist(artist)},
        {"q": f"{first_artist(artist)} {clean_title(title)}"},
    ]
    seen = set()
    for q in attempts:
        key = tuple(sorted(q.items()))
        if key in seen:
            continue
        seen.add(key)
        res = finish(pick_best(http_json(f"{LRCLIB}/search?{urllib.parse.urlencode(q)}"), dur_s))
        if res:
            return res
    return ("none", None)


class LyricsStore:
    """Fetches lyrics in background threads and caches the results."""

    def __init__(self):
        self.cache = {}  # key -> (status, data, timestamp)
        self.lock = threading.Lock()

    def ensure(self, key, st):
        with self.lock:
            e = self.cache.get(key)
            if e is None or (e[0] == "error" and time.time() - e[2] > 20):
                if len(self.cache) > 60:
                    for k in list(self.cache)[:10]:
                        self.cache.pop(k, None)
                self.cache[key] = ("loading", None, time.time())
                threading.Thread(
                    target=self._work,
                    args=(key, st["title"], st["artist"], st.get("album", ""),
                          st["duration_ms"] / 1000.0),
                    daemon=True,
                ).start()
                e = self.cache[key]
            return e[0], e[1]

    def _work(self, key, title, artist, album, dur_s):
        try:
            status, data = fetch_lyrics(title, artist, album, dur_s)
        except Exception:
            status, data = "error", None
        with self.lock:
            self.cache[key] = (status, data, time.time())


# ----------------------------------------------------------------------------
# Volume (optional, needs the "pycaw" package)
# ----------------------------------------------------------------------------
class VolumeController:
    """Reads / sets the volume of Spotify's audio session (falls back to the master volume).

    All Windows audio (COM) calls run in one background thread. If pycaw isn't
    installed or there is no audio device, `available` stays False and the UI hides
    the volume slider.
    """

    def __init__(self):
        self.available = False
        self.level = None          # 0.0 - 1.0, last known value
        self._target = None
        self._wake = threading.Event()
        threading.Thread(target=self._run, daemon=True).start()

    def set(self, level):
        level = clamp(level)
        self.level = level
        self._target = level
        self._wake.set()

    def _interface(self, AudioUtilities):
        """Returns (getter, setter) for Spotify's session volume, else for the master volume."""
        try:
            for s in AudioUtilities.GetAllSessions():
                proc = getattr(s, "Process", None)
                if proc is not None and proc.name().lower() == "spotify.exe":
                    vol = s.SimpleAudioVolume
                    return vol.GetMasterVolume, lambda v: vol.SetMasterVolume(v, None)
        except Exception:
            pass
        spk = AudioUtilities.GetSpeakers()
        if hasattr(spk, "EndpointVolume"):
            ev = spk.EndpointVolume
        else:  # older pycaw versions
            from comtypes import CLSCTX_ALL, POINTER, cast
            from pycaw.pycaw import IAudioEndpointVolume
            ev = cast(spk.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None),
                      POINTER(IAudioEndpointVolume))
        return ev.GetMasterVolumeLevelScalar, lambda v: ev.SetMasterVolumeLevelScalar(v, None)

    def _run(self):
        try:
            import comtypes
            comtypes.CoInitialize()
            from pycaw.pycaw import AudioUtilities
        except Exception:
            return
        while True:
            try:
                getter, setter = self._interface(AudioUtilities)
                if self._target is not None:
                    target, self._target = self._target, None
                    setter(target)
                else:
                    self.level = float(getter())
                self.available = True
            except Exception:
                pass
            self._wake.wait(1.5)
            self._wake.clear()


# ----------------------------------------------------------------------------
# Windows media session (background thread)
# ----------------------------------------------------------------------------
class MediaBackend:
    """Watches and controls the Windows media session in a background thread."""

    def __init__(self):
        self.loop = asyncio.new_event_loop()
        self.session = None
        self._wake = None
        self.state = {
            "available": False,
            "title": "",
            "artist": "",
            "album": "",
            "app": "",
            "playing": False,
            "position_ms": 0.0,
            "duration_ms": 0.0,
            "updated_at": time.time(),
            "track_key": None,
            "cover": None,
        }
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        asyncio.set_event_loop(self.loop)
        self.loop.create_task(self._poll())
        self.loop.run_forever()

    async def _get_session(self):
        manager = await MediaManager.request_async()
        # Prefer the Spotify session; otherwise use whatever Windows considers active.
        for s in manager.get_sessions():
            if "spotify" in s.source_app_user_model_id.lower():
                return s
        return manager.get_current_session()

    async def _read_cover(self, thumbnail_ref):
        stream = await thumbnail_ref.open_read_async()
        size = stream.size
        buf = Buffer(size)
        await stream.read_async(buf, size, InputStreamOptions.READ_AHEAD)
        reader = DataReader.from_buffer(buf)
        data = bytearray(buf.length)
        reader.read_bytes(data)
        return bytes(data)

    async def _poll(self):
        self._wake = asyncio.Event()
        while True:
            try:
                session = await self._get_session()
                self.session = session
                if session is None:
                    self.state["available"] = False
                else:
                    props = await session.try_get_media_properties_async()
                    timeline = session.get_timeline_properties()
                    info = session.get_playback_info()

                    playing = info.playback_status == PlaybackStatus.PLAYING
                    position = timeline.position.total_seconds() * 1000
                    duration = (timeline.end_time - timeline.start_time).total_seconds() * 1000

                    # Windows reports the position at the time of the last update;
                    # if the song is playing, add the elapsed time.
                    if playing and timeline.last_updated_time:
                        age = (datetime.now(timezone.utc) - timeline.last_updated_time).total_seconds()
                        position += max(age, 0) * 1000
                    if duration > 0:
                        position = min(position, duration)

                    key = (props.title, props.artist)
                    if key != self.state["track_key"]:
                        cover = None
                        if props.thumbnail:
                            try:
                                cover = await self._read_cover(props.thumbnail)
                            except Exception:
                                cover = None
                        self.state["cover"] = cover
                        self.state["track_key"] = key

                    self.state.update(
                        available=True,
                        title=props.title or "Unknown song",
                        artist=props.artist or "",
                        album=props.album_title or "",
                        app=session.source_app_user_model_id or "",
                        playing=playing,
                        position_ms=position,
                        duration_ms=duration,
                        updated_at=time.time(),
                    )
            except Exception:
                self.state["available"] = False

            # Wait 1 s; a button press wakes this up earlier.
            try:
                await asyncio.wait_for(self._wake.wait(), 1)
            except asyncio.TimeoutError:
                pass
            self._wake.clear()

    def _run_later(self, coro_fn):
        asyncio.run_coroutine_threadsafe(coro_fn(), self.loop)

    def control(self, action):
        async def run():
            s = self.session
            if s is None:
                return
            if action == "next":
                await s.try_skip_next_async()
            elif action == "prev":
                await s.try_skip_previous_async()
            elif action == "toggle":
                await s.try_toggle_play_pause_async()
            await asyncio.sleep(0.3)
            if self._wake is not None:
                self._wake.set()

        self._run_later(run)

    def seek(self, position_ms):
        async def run():
            s = self.session
            if s is None:
                return
            # Windows expects the position in 100-nanosecond ticks.
            await s.try_change_playback_position_async(int(position_ms * 10000))
            await asyncio.sleep(0.3)
            if self._wake is not None:
                self._wake.set()

        self._run_later(run)


# ----------------------------------------------------------------------------
# Settings window
# ----------------------------------------------------------------------------
class SettingsWindow:
    BG = "#1b1b1b"
    FG = "#e8e8e8"
    FIELD = "#2b2b2b"
    BTN = "#333333"
    HOVER = "#444444"

    def __init__(self, player):
        self.player = player
        self.updating = False
        self.win = win = tk.Toplevel(player.root)
        win.title("Mini Player Settings")
        win.configure(bg=self.BG)
        win.resizable(False, False)
        win.attributes("-topmost", True)
        win.protocol("WM_DELETE_WINDOW", self.close)

        self.key = tk.StringVar(value="bg")
        self.hex_var = tk.StringVar()
        self.font_var = tk.StringVar(value=player.font_family)

        # Tabs
        tabbar = tk.Frame(win, bg=self.BG)
        tabbar.pack(fill="x", padx=14, pady=(12, 0))
        self.tab_btns = {}
        for name in ("Theme", "Window"):
            b = tk.Button(tabbar, text=name, command=lambda n=name: self.show_tab(n),
                          relief="flat", bd=0, padx=16, pady=5, font=("Segoe UI", 10, "bold"))
            b.pack(side="left", padx=(0, 6))
            self.tab_btns[name] = b
        body = tk.Frame(win, bg=self.BG)
        body.pack(fill="x")
        self.tab_theme = tk.Frame(body, bg=self.BG)
        self.tab_window = tk.Frame(body, bg=self.BG)

        self.build_theme_tab(self.tab_theme)
        self.build_window_tab(self.tab_window)

        # Bottom buttons
        bf = tk.Frame(win, bg=self.BG)
        bf.pack(fill="x", padx=14, pady=12)
        tk.Button(bf, text="Reset all", command=self.reset_all,
                  bg=self.BTN, fg=self.FG, activebackground=self.HOVER,
                  activeforeground=self.FG, relief="flat", bd=0, padx=10, pady=4)\
            .pack(side="left")
        tk.Button(bf, text="Close", command=self.close, bg=GREEN, fg="#000000",
                  activebackground="#1ed760", activeforeground="#000000",
                  relief="flat", bd=0, padx=16, pady=4)\
            .pack(side="right")

        self.load_editor()
        self.sync_window_controls()
        self.show_tab("Theme")
        self.place()

    # -- Tabs ---------------------------------------------------------------------
    def show_tab(self, name):
        self.tab_theme.pack_forget()
        self.tab_window.pack_forget()
        (self.tab_theme if name == "Theme" else self.tab_window).pack(fill="x")
        for n, b in self.tab_btns.items():
            if n == name:
                b.config(bg=GREEN, fg="#000000", activebackground="#1ed760", activeforeground="#000000")
            else:
                b.config(bg=self.BTN, fg=self.FG, activebackground=self.HOVER, activeforeground=self.FG)

    def build_theme_tab(self, tab):
        p = self.player
        head = ("Segoe UI", 10, "bold")

        tk.Label(tab, text="Presets", bg=self.BG, fg=self.FG, font=head)\
            .pack(anchor="w", padx=14, pady=(12, 4))
        pf = tk.Frame(tab, bg=self.BG)
        pf.pack(anchor="w", padx=11)
        for i, name in enumerate(PRESETS):
            tk.Button(pf, text=name, command=lambda n=name: self.apply_preset(n),
                      bg=self.BTN, fg=self.FG, activebackground=self.HOVER,
                      activeforeground=self.FG, relief="flat", bd=0,
                      padx=10, pady=4, width=10)\
                .grid(row=i // 3, column=i % 3, padx=3, pady=3, sticky="ew")

        tk.Label(tab, text="Colors", bg=self.BG, fg=self.FG, font=head)\
            .pack(anchor="w", padx=14, pady=(12, 2))
        self.swatches = {}
        for k, label in THEME_LABELS:
            row = tk.Frame(tab, bg=self.BG)
            row.pack(fill="x", padx=14, pady=1)
            tk.Radiobutton(row, text=label, variable=self.key, value=k,
                           command=self.load_editor, bg=self.BG, fg=self.FG,
                           selectcolor=self.FIELD, activebackground=self.BG,
                           activeforeground=self.FG, anchor="w", width=30)\
                .pack(side="left")
            sw = tk.Label(row, width=4, bg=p.theme[k], relief="solid", bd=1)
            sw.pack(side="right")
            self.swatches[k] = sw

        tk.Checkbutton(tab, text="Auto accent color from the album cover",
                       variable=p.auto_var, command=p.on_auto_accent_toggle,
                       bg=self.BG, fg=self.FG, selectcolor=self.FIELD,
                       activebackground=self.BG, activeforeground=self.FG, anchor="w")\
            .pack(fill="x", padx=14, pady=(4, 0))

        ed = tk.Frame(tab, bg=self.BG)
        ed.pack(fill="x", padx=14, pady=(10, 2))
        self.preview = tk.Label(ed, width=9, height=3, relief="solid", bd=1)
        self.preview.grid(row=0, column=0, rowspan=2, padx=(0, 12), sticky="n")
        tk.Label(ed, text="HEX", bg=self.BG, fg=self.FG).grid(row=0, column=1, sticky="w")
        self.entry = tk.Entry(ed, textvariable=self.hex_var, width=10, bg=self.FIELD,
                              fg=self.FG, insertbackground=self.FG, relief="flat",
                              font=("Consolas", 12))
        self.entry.grid(row=0, column=2, sticky="w", padx=6)
        self.entry.bind("<KeyRelease>", self.on_hex)
        self.entry.bind("<Return>", self.on_hex)
        tk.Button(ed, text="Pick color…", command=self.pick, bg=self.BTN, fg=self.FG,
                  activebackground=self.HOVER, activeforeground=self.FG,
                  relief="flat", bd=0, padx=8, pady=2)\
            .grid(row=0, column=3, padx=4)

        self.scales = {}
        sf = tk.Frame(tab, bg=self.BG)
        sf.pack(fill="x", padx=14, pady=(6, 0))
        for i, (ch, col) in enumerate((("R", "#ff6b6b"), ("G", "#5fd068"), ("B", "#6aa8ff"))):
            tk.Label(sf, text=ch, bg=self.BG, fg=col, width=2,
                     font=("Consolas", 11, "bold")).grid(row=i, column=0)
            s = tk.Scale(sf, from_=0, to=255, orient="horizontal", length=280,
                         command=self.on_slider, bg=self.BG, fg=self.FG,
                         troughcolor=self.FIELD, highlightthickness=0, bd=0,
                         activebackground=col)
            s.grid(row=i, column=1, padx=4)
            self.scales[ch] = s

    def build_window_tab(self, tab):
        p = self.player
        head = ("Segoe UI", 10, "bold")

        def slider(parent, label, lo, hi, cmd):
            row = tk.Frame(parent, bg=self.BG)
            row.pack(fill="x", padx=14, pady=(2, 0))
            tk.Label(row, text=label, bg=self.BG, fg=self.FG, width=11, anchor="w").pack(side="left")
            s = tk.Scale(row, from_=lo, to=hi, resolution=5, orient="horizontal", length=250,
                         command=cmd, bg=self.BG, fg=self.FG, troughcolor=self.FIELD,
                         highlightthickness=0, bd=0, activebackground=GREEN)
            s.pack(side="left", padx=4)
            return s

        tk.Label(tab, text="Appearance", bg=self.BG, fg=self.FG, font=head)\
            .pack(anchor="w", padx=14, pady=(12, 2))
        self.size_scale = slider(tab, "Size (%)", int(SCALE_MIN * 100), int(SCALE_MAX * 100), self.on_size)
        self.opacity_scale = slider(tab, "Opacity (%)", int(OPACITY_MIN * 100), 100, self.on_opacity)

        tk.Label(tab, text="Font", bg=self.BG, fg=self.FG, font=head)\
            .pack(anchor="w", padx=14, pady=(12, 2))
        fr = tk.Frame(tab, bg=self.BG)
        fr.pack(fill="x", padx=14)
        try:
            style = ttk.Style(self.win)
            style.theme_use("clam")
            style.configure("TCombobox", fieldbackground=self.FIELD, background=self.BTN,
                            foreground=self.FG, arrowcolor=self.FG, bordercolor=self.BTN,
                            lightcolor=self.FIELD, darkcolor=self.FIELD)
            style.map("TCombobox", fieldbackground=[("readonly", self.FIELD)],
                      foreground=[("readonly", self.FG)])
            self.win.option_add("*TCombobox*Listbox.background", self.FIELD)
            self.win.option_add("*TCombobox*Listbox.foreground", self.FG)
        except Exception:
            pass
        fonts = sorted({f for f in tkfont.families(self.win) if not f.startswith("@")},
                       key=str.lower)
        self.font_box = ttk.Combobox(fr, textvariable=self.font_var, values=fonts, width=34)
        self.font_box.pack(side="left")
        self.font_box.bind("<<ComboboxSelected>>", self.on_font)
        self.font_box.bind("<Return>", self.on_font)
        tk.Label(tab, text="Pick any installed font, or type its name and press Enter.",
                 bg=self.BG, fg="#8a8a8a", font=("Segoe UI", 8), anchor="w")\
            .pack(fill="x", padx=16)

        tk.Label(tab, text="Modes", bg=self.BG, fg=self.FG, font=head)\
            .pack(anchor="w", padx=14, pady=(12, 2))
        for text, var, cmd in (
            ("Compact mode (double-click the player)", p.compact_var, p.on_compact_toggle),
            ("Show synced lyrics (from LRCLIB)", p.lyrics_var, p.on_lyrics_toggle),
            ("Always on top", p.topmost_var, p.apply_topmost),
        ):
            tk.Checkbutton(tab, text=text, variable=var, command=cmd, bg=self.BG, fg=self.FG,
                           selectcolor=self.FIELD, activebackground=self.BG,
                           activeforeground=self.FG, anchor="w")\
                .pack(fill="x", padx=14)
        tk.Label(tab, text="Tips: mouse wheel = volume, Ctrl + wheel = resize.",
                 bg=self.BG, fg="#8a8a8a", font=("Segoe UI", 8), anchor="w")\
            .pack(fill="x", padx=16, pady=(6, 0))

    def place(self):
        win, p = self.win, self.player
        win.update_idletasks()
        sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
        w, h = win.winfo_reqwidth(), win.winfo_reqheight()
        x = min(max(p.pos_x + p.W // 2 - w // 2, 10), max(10, sw - w - 10))
        y = p.pos_y + p.H + 12
        if y + h > sh - 40:
            y = max(10, p.pos_y - h - 12)
        win.geometry(f"+{x}+{y}")

    # -- Color editor ---------------------------------------------------------------
    def current(self):
        return self.player.theme[self.key.get()]

    def load_editor(self):
        c = self.current()
        r, g, b = hex_rgb(c)
        self.updating = True
        self.scales["R"].set(r)
        self.scales["G"].set(g)
        self.scales["B"].set(b)
        self.hex_var.set(c)
        self.preview.config(bg=c)
        self.updating = False

    def refresh_swatches(self):
        for k, sw in self.swatches.items():
            sw.config(bg=self.player.theme[k])

    def commit(self, c):
        self.preview.config(bg=c)
        self.player.set_theme_color(self.key.get(), c)
        self.swatches[self.key.get()].config(bg=c)

    def on_slider(self, _=None):
        if self.updating:
            return
        c = "#%02x%02x%02x" % tuple(int(float(self.scales[k].get())) for k in "RGB")
        self.updating = True
        self.hex_var.set(c)
        self.updating = False
        self.commit(c)

    def on_hex(self, event=None):
        if self.updating:
            return
        # While typing only full 6-digit codes are applied; short codes (#abc) on Enter.
        short_ok = getattr(event, "keysym", "") == "Return" or event is None
        c = normalize_hex(self.hex_var.get(), allow_short=short_ok)
        if not c:
            return
        r, g, b = hex_rgb(c)
        self.updating = True
        self.scales["R"].set(r)
        self.scales["G"].set(g)
        self.scales["B"].set(b)
        self.updating = False
        self.commit(c)

    def pick(self):
        from tkinter import colorchooser
        res = colorchooser.askcolor(color=self.current(), parent=self.win, title="Pick a color")
        if res and res[1]:
            c = normalize_hex(res[1])
            if c:
                self.hex_var.set(c)
                self.on_hex()

    def apply_preset(self, name):
        self.player.set_theme(PRESETS[name])
        self.refresh_swatches()
        self.load_editor()

    # -- Window tab ---------------------------------------------------------------
    def on_size(self, value):
        if not self.updating:
            self.player.set_scale(float(value) / 100.0)

    def on_opacity(self, value):
        if not self.updating:
            self.player.set_opacity(float(value) / 100.0)

    def on_font(self, _event=None):
        if not self.player.set_font(self.font_var.get()):
            self.font_var.set(self.player.font_family)  # invalid name: revert

    def sync_scale(self):
        self.updating = True
        self.size_scale.set(int(round(self.player.scale * 100)))
        self.updating = False

    def sync_window_controls(self):
        self.updating = True
        self.size_scale.set(int(round(self.player.scale * 100)))
        self.opacity_scale.set(int(round(self.player.opacity * 100)))
        self.font_var.set(self.player.font_family)
        self.updating = False

    def reset_all(self):
        p = self.player
        self.apply_preset("Black")
        p.set_scale(1.0)
        p.set_opacity(OPACITY_DEFAULT)
        p.set_font(FONT_FAMILY)
        p.auto_var.set(True)
        p.lyrics_var.set(True)
        p.compact_var.set(False)
        p.on_auto_accent_toggle()
        p.apply_modes()
        self.sync_window_controls()

    def close(self):
        self.player.settings = None
        self.player.save_config()
        self.win.destroy()


# ----------------------------------------------------------------------------
# Main UI
# ----------------------------------------------------------------------------
class MiniPlayer:
    def __init__(self, root, backend, volume=None):
        self.root = root
        self.backend = backend
        self.volume = volume if volume is not None else VolumeController()
        self.settings = None
        self.lyrics_store = LyricsStore()

        cfg = load_config()
        self.theme = dict(DEFAULT_THEME)
        saved = cfg.get("theme")
        if isinstance(saved, dict):
            for k in self.theme:
                c = normalize_hex(str(saved.get(k, "")))
                if c:
                    self.theme[k] = c
        try:
            sc = round(float(cfg.get("scale", 1.0)) / SCALE_STEP) * SCALE_STEP
            self.scale = round(clamp(sc, SCALE_MIN, SCALE_MAX), 2)
        except (TypeError, ValueError):
            self.scale = 1.0
        try:
            self.opacity = round(clamp(float(cfg.get("opacity", OPACITY_DEFAULT)), OPACITY_MIN, 1.0), 2)
        except (TypeError, ValueError):
            self.opacity = OPACITY_DEFAULT
        self.s = self.scale

        # Borderless, rounded corners, always on top
        root.overrideredirect(True)
        self.topmost_var = tk.BooleanVar(value=bool(cfg.get("topmost", True)))
        self.lyrics_var = tk.BooleanVar(value=bool(cfg.get("lyrics", True)))
        self.compact_var = tk.BooleanVar(value=bool(cfg.get("compact", False)))
        self.auto_var = tk.BooleanVar(value=bool(cfg.get("auto_accent", True)))
        self.lyrics_on = bool(self.lyrics_var.get())
        self.compact = bool(self.compact_var.get())
        self.auto_accent = bool(self.auto_var.get())
        root.attributes("-topmost", self.topmost_var.get())
        root.configure(bg=KEY)
        try:
            root.attributes("-transparentcolor", KEY)
        except tk.TclError:
            pass

        # Fonts (the family can be changed in the settings)
        self.font_family = FONT_FAMILY
        saved_font = cfg.get("font")
        if isinstance(saved_font, str):
            exact = {f.lower(): f for f in tkfont.families(root)}.get(saved_font.strip().lower())
            if exact:
                self.font_family = exact
        fam = self.font_family
        self.f_label = tkfont.Font(family=fam, size=-12)
        self.f_title = tkfont.Font(family=fam, size=-16, weight="bold")
        self.f_artist = tkfont.Font(family=fam, size=-13)
        self.f_time = tkfont.Font(family=fam, size=-12)
        self.f_cover_note = tkfont.Font(family=fam, size=-29)
        self.f_cover_note_c = tkfont.Font(family=fam, size=-17)
        self.f_logo_note = tkfont.Font(family=fam, size=-21)
        self.f_lyr = tkfont.Font(family=fam, size=-14)
        self.f_lyr_cur = tkfont.Font(family=fam, size=-16, weight="bold")
        self.f_lyr_msg = tkfont.Font(family=fam, size=-13)
        self.f_compact = tkfont.Font(family=fam, size=-14, weight="bold")
        self.font_specs = [(self.f_label, 12), (self.f_title, 16), (self.f_artist, 13),
                           (self.f_time, 12), (self.f_cover_note, 29), (self.f_cover_note_c, 17),
                           (self.f_logo_note, 21), (self.f_lyr, 14), (self.f_lyr_cur, 16),
                           (self.f_lyr_msg, 13), (self.f_compact, 14)]
        self.compute_layout()

        sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
        self.pos_x, self.pos_y = sw - self.W - 24, 24
        try:
            x, y = int(cfg["x"]), int(cfg["y"])
            if -self.W + 60 <= x <= sw - 60 and 0 <= y <= sh - 60:
                self.pos_x, self.pos_y = x, y
        except (KeyError, TypeError, ValueError):
            pass
        root.geometry(f"{self.W}x{self.H}+{self.pos_x}+{self.pos_y + int(self.S(30))}")
        try:
            root.attributes("-alpha", 0.0)
        except tk.TclError:
            pass

        self.cv = tk.Canvas(root, width=self.W, height=self.H, bg=KEY,
                            highlightthickness=0, bd=0)
        self.cv.pack()

        # State
        self.shown_key = object()      # forces an update on the first frame
        self.anim_start = 0.0
        self.slide_dir = 1
        self.pending_dir = 1
        self.pending_until = 0.0
        self.cover_src = object()
        self.cover_frames = []
        self.cover_idx = -1
        self.cover_anim = 0.0
        self.cov = (COVER_X, COVER_Y, COVER)
        self.disp_pos = 0.0
        self.eq_h = [3.0] * 4
        self.hover = False
        self.hover_t = 0.0
        self.mx, self.my = self.W / 2, self.H / 2
        self.btn_dx = 0.0
        self.placed_dx = None
        self.hl_x = self.S(BTN_X["toggle"])
        self.hl_a = 0.0
        self.gear_angle = 0.0
        self.gear_speed = 0.0
        self.gear_hot = 0.0
        self.nudge = 0.0
        self.ripples = []
        self.last_playing = None
        self.last_times = ("", "")
        self.texts_settled = False
        self.icon_dirty = True
        self.intro_done = False
        self.intro_start = time.time()
        self.drag_dx = self.drag_dy = 0
        self.press_target = None
        self.dragged = False
        self.save_job = None
        self.relayout_job = None
        # accent color (animated; can follow the album cover)
        self.accent_cur = self.theme["accent"]
        self.accent_target = self.theme["accent"]
        # seeking
        self.seeking = False
        self.seek_pos = 0.0
        self.seek_hold_until = 0.0
        self.seek_hold_pos = 0.0
        self.seek_hold_t0 = 0.0
        self.bar_hot = 0.0
        # volume
        self.vol_ui = None
        self.vol_drag = False
        self.vol_drag_level = 0.0
        self.vol_visible = False
        self.flash_until = 0.0
        self.flash_pct = 0
        self.flash_active = False
        # lyrics
        self.lyr_key = None
        self.lyr_lines = []
        self.lyr_times = []
        self.lyr_fit = []
        self.lyr_pos = -1.0
        self.slot_cache = []
        self.lyr_msg_cache = None

        self.build()
        self.apply_theme()
        self.bind_events()
        self.frame()

    # -- Layout / scaling -------------------------------------------------------
    def S(self, v):
        return v * self.s

    def compute_layout(self):
        self.s = self.scale
        if self.compact:
            w0, h0 = CW0, CH0
        else:
            w0, h0 = W0, H0 + (LYR_H0 if self.lyrics_on else 0)
        self.W = int(round(w0 * self.s))
        self.H = int(round(h0 * self.s))
        for font, px in self.font_specs:
            font.configure(family=self.font_family, size=-max(6, int(round(px * self.s))))

    def set_scale(self, value):
        value = round(clamp(round(value / SCALE_STEP) * SCALE_STEP, SCALE_MIN, SCALE_MAX), 2)
        if abs(value - self.scale) < 1e-6:
            return
        self.scale = value
        if self.settings is not None:
            try:
                self.settings.sync_scale()
            except tk.TclError:
                pass
        self.request_relayout()
        self.schedule_save()

    def request_relayout(self):
        if self.relayout_job is not None:
            self.root.after_cancel(self.relayout_job)
        self.relayout_job = self.root.after(120, self.do_relayout)

    def do_relayout(self):
        self.relayout_job = None
        self.relayout()

    def apply_modes(self):
        self.lyrics_on = bool(self.lyrics_var.get())
        self.compact = bool(self.compact_var.get())
        self.relayout()
        self.schedule_save()

    def on_lyrics_toggle(self):
        self.apply_modes()

    def on_compact_toggle(self):
        self.apply_modes()

    def on_auto_accent_toggle(self):
        self.auto_accent = bool(self.auto_var.get())
        self.refresh_accent_target(snap=False)
        self.schedule_save()

    def set_opacity(self, value):
        self.opacity = round(clamp(value, OPACITY_MIN, 1.0), 2)
        if self.intro_done:
            try:
                self.root.attributes("-alpha", self.opacity)
            except tk.TclError:
                pass
        self.schedule_save()

    def set_font(self, name):
        """Changes the font family. Returns False if the font isn't installed."""
        name = (name or "").strip()
        exact = {f.lower(): f for f in tkfont.families(self.root)}.get(name.lower())
        if not exact:
            return False
        if exact != self.font_family:
            self.font_family = exact
            self.request_relayout()
            self.schedule_save()
        return True

    def relayout(self):
        """Rebuilds the whole canvas for a new size / mode / font."""
        now = time.time()
        self.compute_layout()
        self.ripples = []
        self.cv.delete("all")
        self.cv.config(width=self.W, height=self.H)
        if not self.intro_done:
            self.intro_done = True
        try:
            self.root.attributes("-alpha", self.opacity)
        except tk.TclError:
            pass
        self.root.geometry(f"{self.W}x{self.H}+{self.pos_x}+{self.pos_y}")
        self.build()
        self.apply_theme()
        self.last_playing = None
        self.last_times = ("", "")
        self.icon_dirty = True
        self.vol_visible = False
        self.seeking = False
        self.vol_drag = False
        self.press_target = None
        self.hl_x = self.S(BTN_X["toggle"])
        data = self.cover_src if isinstance(self.cover_src, (bytes, bytearray)) else None
        self.set_cover(data, now - 10)
        self.apply_track_texts()
        self.anim_start = now - 10
        self.texts_settled = False
        self.flash_active = False
        self.prepare_lyrics()
        self.lyr_msg_cache = None

    # -- Drawing ----------------------------------------------------------------
    def reset_items(self):
        for name in ("bg_item", "cover_ph", "cover_note", "cover_item", "t_now", "t_title",
                     "t_artist", "bar_bg", "bar_fg", "knob", "t_left", "t_right", "logo_note",
                     "gear", "gear_hole", "hl", "ic_prev_bar", "ic_prev_tri", "play_icon",
                     "ic_next_bar", "ic_next_tri", "lyr_div", "t_lyr_msg", "vol_icon",
                     "vol_track", "vol_fill", "vol_knob"):
            setattr(self, name, None)
        self.logo = []
        self.eq = []
        self.pause_icons = []
        self.icon_items = []
        self.text_items = []
        self.lyr_slots = []
        self.vol_waves = []
        self.vol_items = []

    def build(self):
        self.reset_items()
        if self.compact:
            self.build_compact()
        else:
            self.build_full()
        self.slot_cache = ["hidden"] * len(self.lyr_slots)

    def build_cover(self):
        cv, S = self.cv, self.S
        x, y, size = self.cov
        cx, cy = S(x + size / 2), S(y + size / 2)
        self.cover_ph = cv.create_rectangle(S(x), S(y), S(x + size), S(y + size),
                                            fill="#000000", outline="")
        self.cover_note = cv.create_text(
            cx, cy, text="♪", font=self.f_cover_note_c if self.compact else self.f_cover_note)
        self.cover_item = cv.create_image(cx, cy, state="hidden")

    def build_compact(self):
        cv, S, W, H = self.cv, self.S, self.W, self.H
        self.bg_item = rounded_rect(cv, 1, 1, W - 1, H - 1, S(14),
                                    fill="#000000", outline="#000000", width=1)
        self.cov = (C_COVER_X, C_COVER_Y, C_COVER)
        self.build_cover()
        self.t_title = cv.create_text(S(C_TX), S(C_TITLE_Y), text="", anchor="w", font=self.f_compact)
        self.text_items = [(self.t_title, S(C_TX), S(C_TITLE_Y), "title", 0.0)]
        bw = max(2, int(round(S(2))))
        self.bar_bg = cv.create_line(S(C_TX), S(C_BAR_Y), S(CW0 - 12), S(C_BAR_Y),
                                     width=bw, capstyle="round")
        self.bar_fg = cv.create_line(S(C_TX), S(C_BAR_Y), S(C_TX), S(C_BAR_Y),
                                     width=bw, capstyle="round")
        self.eq = [cv.create_rectangle(0, 0, 0, 0, outline="") for _ in range(4)]

    def build_full(self):
        cv, S, W, H = self.cv, self.S, self.W, self.H
        self.bg_item = rounded_rect(cv, 1, 1, W - 1, H - 1, S(16),
                                    fill="#000000", outline="#000000", width=1)
        self.cov = (COVER_X, COVER_Y, COVER)
        self.build_cover()

        # Texts
        self.t_now = cv.create_text(S(TX), S(22), text="now playing", anchor="w", font=self.f_label)
        self.t_title = cv.create_text(S(TX), S(TITLE_Y), text="", anchor="w", font=self.f_title)
        self.t_artist = cv.create_text(S(TX), S(ARTIST_Y), text="", anchor="w", font=self.f_artist)
        # (item, x, y, color key, delay in s)
        self.text_items = [(self.t_title, S(TX), S(TITLE_Y), "title", 0.0),
                           (self.t_artist, S(TX), S(ARTIST_Y), "text2", 0.08)]

        # Progress bar and times
        bw = max(2, int(round(S(3))))
        self.bar_bg = cv.create_line(S(BAR_X1), S(BAR_Y), S(BAR_X2), S(BAR_Y),
                                     width=bw, capstyle="round")
        self.bar_fg = cv.create_line(S(BAR_X1), S(BAR_Y), S(BAR_X1), S(BAR_Y),
                                     width=bw, capstyle="round")
        self.knob = cv.create_oval(0, 0, 0, 0, outline="")
        self.t_left = cv.create_text(S(12), S(BAR_Y), text="0:00", anchor="w", font=self.f_time)
        self.t_right = cv.create_text(S(W0 - 12), S(BAR_Y), text="-0:00", anchor="e", font=self.f_time)

        # Top right: Spotify logo (or note), gear, equalizer
        lx, ly = S(LOGO_POS[0]), S(LOGO_POS[1])
        self.logo = [cv.create_oval(lx - S(12), ly - S(12), lx + S(12), ly + S(12),
                                    fill=GREEN, outline="")]
        for p0, pm, p2, wd in SPOTIFY_BARS:
            self.logo.append(cv.create_line(*bar_points(p0, pm, p2, lx, ly, self.s),
                                            fill=LOGO_DARK, width=max(1, S(wd)),
                                            capstyle="round", joinstyle="round"))
        self.logo_note = cv.create_text(lx, ly, text="♪", font=self.f_logo_note, state="hidden")

        gx, gy = S(GEAR_POS[0]), S(GEAR_POS[1])
        self.gear = cv.create_polygon(gear_points(gx, gy, 0, S(8.5), S(6.2)), outline="")
        self.gear_hole = cv.create_oval(gx - S(2.8), gy - S(2.8), gx + S(2.8), gy + S(2.8), outline="")

        self.eq = [cv.create_rectangle(0, 0, 0, 0, outline="") for _ in range(4)]

        # Volume slider (top row, revealed on hover)
        vy, ix = S(VOL_Y), S(VOL_ICON_X)
        self.vol_icon = cv.create_polygon(
            ix - S(6), vy - S(3), ix - S(3), vy - S(3), ix + S(1), vy - S(6),
            ix + S(1), vy + S(6), ix - S(3), vy + S(3), ix - S(6), vy + S(3),
            outline="", state="hidden")
        self.vol_waves = []
        for r in (4, 7):
            self.vol_waves.append(cv.create_arc(
                ix + S(1) - S(r), vy - S(r), ix + S(1) + S(r), vy + S(r),
                start=-40, extent=80, style="arc", width=max(1, S(1.5)), state="hidden"))
        vw = max(2, int(round(S(3))))
        self.vol_track = cv.create_line(S(VOL_X1), vy, S(VOL_X2), vy, width=vw,
                                        capstyle="round", state="hidden")
        self.vol_fill = cv.create_line(S(VOL_X1), vy, S(VOL_X1), vy, width=vw,
                                       capstyle="round", state="hidden")
        self.vol_knob = cv.create_oval(0, 0, 0, 0, outline="", state="hidden")
        self.vol_items = [self.vol_icon, *self.vol_waves, self.vol_track, self.vol_fill, self.vol_knob]

        # Highlight circle that follows the mouse (below the buttons)
        self.hl = cv.create_oval(0, 0, 0, 0, outline="")

        # Control buttons (revealed on hover)
        self.ic_prev_bar = cv.create_rectangle(0, 0, 0, 0, outline="")
        self.ic_prev_tri = cv.create_polygon(0, 0, 0, 0, 0, 0, outline="")
        self.play_icon = cv.create_polygon(0, 0, 0, 0, 0, 0, outline="")
        self.pause_icons = [cv.create_rectangle(0, 0, 0, 0, outline=""),
                            cv.create_rectangle(0, 0, 0, 0, outline="")]
        self.ic_next_bar = cv.create_rectangle(0, 0, 0, 0, outline="")
        self.ic_next_tri = cv.create_polygon(0, 0, 0, 0, 0, 0, outline="")
        self.icon_items = [self.ic_prev_bar, self.ic_prev_tri, self.play_icon,
                           *self.pause_icons, self.ic_next_bar, self.ic_next_tri]
        self.place_buttons(0.0)
        self.placed_dx = 0.0

        # Lyrics panel
        if self.lyrics_on:
            self.lyr_div = cv.create_line(S(16), S(LYR_TOP0 - 2), W - S(16), S(LYR_TOP0 - 2), width=1)
            self.lyr_slots = [cv.create_text(W / 2, 0, text="", font=self.f_lyr, state="hidden")
                              for _ in range(LYR_SLOTS)]
            self.t_lyr_msg = cv.create_text(W / 2, S(LYR_TOP0 + LYR_H0 / 2), text="",
                                            font=self.f_lyr_msg, state="hidden")

    def place_buttons(self, dx):
        S, c, by = self.S, self.cv.coords, self.S(BTN_Y)
        p, t, n = S(BTN_X["prev"]) + dx, S(BTN_X["toggle"]) + dx, S(BTN_X["next"]) + dx
        c(self.ic_prev_bar, p - S(6), by - S(5), p - S(4), by + S(5))
        c(self.ic_prev_tri, p + S(5), by - S(5), p + S(5), by + S(5), p - S(4), by)
        c(self.play_icon, t - S(4), by - S(6), t - S(4), by + S(6), t + S(6), by)
        c(self.pause_icons[0], t - S(5), by - S(6), t - S(2), by + S(6))
        c(self.pause_icons[1], t + S(2), by - S(6), t + S(5), by + S(6))
        c(self.ic_next_bar, n + S(4), by - S(5), n + S(6), by + S(5))
        c(self.ic_next_tri, n - S(5), by - S(5), n - S(5), by + S(5), n + S(4), by)

    def cfg(self, item, **kw):
        if item is not None:
            self.cv.itemconfig(item, **kw)

    def apply_theme(self):
        """Applies theme colors to static items (animated items read the theme each frame)."""
        t = self.theme
        bg = t["bg"]
        self.cfg(self.bg_item, fill=bg, outline=t["border"])
        self.cfg(self.cover_ph, fill=mix(bg, "#000000", 0.3))
        self.cfg(self.cover_note, fill=t["text2"])
        self.cfg(self.t_now, fill=t["text2"])
        self.cfg(self.bar_bg, fill=mix(bg, t["title"], 0.22))
        self.cfg(self.t_left, fill=t["text2"])
        self.cfg(self.t_right, fill=t["text2"])
        self.cfg(self.logo_note, fill=t["text2"])
        self.cfg(self.gear_hole, fill=bg)
        self.cfg(self.lyr_div, fill=mix(bg, t["text2"], 0.18))
        self.slot_cache = [None] * len(self.lyr_slots)
        self.lyr_msg_cache = None
        self.texts_settled = False
        self.icon_dirty = True
        self.refresh_accent_target(snap=True)

    # -- Accent color -------------------------------------------------------------
    def refresh_accent_target(self, snap):
        target = self.theme["accent"]
        if self.auto_accent and isinstance(self.cover_src, (bytes, bytearray)):
            c = dominant_color(self.cover_src, self.theme["bg"])
            if c:
                target = c
        self.accent_target = target.lower()
        if snap:
            self.accent_cur = self.accent_target
            self.apply_accent_items()

    def apply_accent_items(self):
        c = self.accent_cur
        self.cfg(self.bar_fg, fill=c)
        self.cfg(self.knob, fill=c)
        for bar in self.eq:
            self.cfg(bar, fill=c)

    def update_accent(self):
        if self.accent_cur == self.accent_target:
            return
        out = []
        for x, y in zip(hex_rgb(self.accent_cur), hex_rgb(self.accent_target)):
            d = y - x
            if d:
                step = d * 0.15
                if abs(step) < 1:
                    step = 1 if d > 0 else -1   # always make progress, never stall
                x = int(round(x + step))
                x = min(x, y) if d > 0 else max(x, y)
            out.append(x)
        self.accent_cur = "#%02x%02x%02x" % tuple(out)
        self.apply_accent_items()

    # -- Events -------------------------------------------------------------------
    def bind_events(self):
        cv = self.cv
        cv.bind("<Enter>", self.on_motion)
        cv.bind("<Motion>", self.on_motion)
        cv.bind("<Leave>", self.on_leave)
        cv.bind("<ButtonPress-1>", self.on_press)
        cv.bind("<B1-Motion>", self.on_drag)
        cv.bind("<ButtonRelease-1>", self.on_release)
        cv.bind("<Double-Button-1>", self.on_double)
        cv.bind("<MouseWheel>", self.on_scroll)
        cv.bind("<Control-MouseWheel>", self.on_zoom)

        self.menu = tk.Menu(self.root, tearoff=0)
        self.menu.add_checkbutton(label="Always on top", variable=self.topmost_var,
                                  command=self.apply_topmost)
        self.menu.add_checkbutton(label="Compact mode", variable=self.compact_var,
                                  command=self.on_compact_toggle)
        self.menu.add_checkbutton(label="Show lyrics", variable=self.lyrics_var,
                                  command=self.on_lyrics_toggle)
        self.menu.add_command(label="Settings…", command=self.open_settings)
        self.menu.add_separator()
        self.menu.add_command(label="Quit", command=self.quit_app)
        cv.bind("<Button-3>", self.show_menu)

    # -- Interaction ------------------------------------------------------------
    def can_seek(self):
        st = self.backend.state
        return bool(st["available"] and st["duration_ms"] > 0)

    def volume_ready(self):
        return bool(self.volume.available and self.volume.level is not None)

    def hit_test(self, x, y):
        if self.compact:
            return None
        S = self.S
        gx, gy = S(GEAR_POS[0]), S(GEAR_POS[1])
        if (x - gx) ** 2 + (y - gy) ** 2 <= S(13) ** 2:
            return "gear"
        if self.hover_t > 0.5:
            for name in ("prev", "toggle", "next"):
                if abs(x - (S(BTN_X[name]) + self.btn_dx)) <= S(15) and abs(y - S(BTN_Y)) <= S(14):
                    return name
            if self.volume_ready() and abs(y - S(VOL_Y)) <= S(10) \
                    and S(VOL_ICON_X) - S(8) <= x <= S(VOL_X2) + S(8):
                return "volume"
        if self.can_seek() and abs(y - S(BAR_Y)) <= S(10) and S(BAR_X1) - S(8) <= x <= S(BAR_X2) + S(8):
            return "seek"
        return None

    def on_motion(self, e):
        self.hover = True
        self.mx, self.my = e.x, e.y
        self.cv.config(cursor="hand2" if self.hit_test(e.x, e.y) else "")

    def on_leave(self, _e):
        self.hover = False
        self.cv.config(cursor="")

    def on_zoom(self, e):
        """Ctrl + mouse wheel: resize."""
        self.set_scale(self.scale + (SCALE_STEP if e.delta > 0 else -SCALE_STEP))

    def on_scroll(self, e):
        """Mouse wheel: volume."""
        if not self.volume_ready():
            return
        step = VOLUME_STEP if e.delta > 0 else -VOLUME_STEP
        level = clamp(self.volume.level + step)
        self.volume.set(level)
        self.flash_volume(level, time.time())

    def flash_volume(self, level, now):
        self.flash_pct = int(round(level * 100))
        self.flash_until = now + 1.2

    def on_double(self, e):
        """Double-click on an empty area: toggle compact mode."""
        if self.hit_test(e.x, e.y) is None:
            self.compact_var.set(not self.compact_var.get())
            self.on_compact_toggle()

    def update_seek(self, x):
        dur = self.backend.state["duration_ms"]
        ratio = clamp((x - self.S(BAR_X1)) / self.S(BAR_X2 - BAR_X1))
        self.seek_pos = ratio * dur

    def finish_seek(self):
        self.seeking = False
        st = self.backend.state
        if not st["available"] or st["duration_ms"] <= 0:
            return
        now = time.time()
        pos = self.seek_pos
        # Keep showing the new position until Windows reports it back.
        self.seek_hold_pos, self.seek_hold_t0, self.seek_hold_until = pos, now, now + 1.5
        st["position_ms"], st["updated_at"] = pos, now
        self.backend.seek(pos)

    def update_volume_from_x(self, x):
        level = clamp((x - self.S(VOL_X1)) / self.S(VOL_X2 - VOL_X1))
        self.vol_drag_level = level
        self.volume.set(level)
        self.flash_volume(level, time.time())

    def on_press(self, e):
        self.mx, self.my = e.x, e.y
        self.press_target = self.hit_test(e.x, e.y)
        self.dragged = False
        if self.press_target == "seek":
            self.seeking = True
            self.update_seek(e.x)
        elif self.press_target == "volume":
            self.vol_drag = True
            self.update_volume_from_x(e.x)
        elif self.press_target is None:
            self.drag_dx = e.x_root - self.root.winfo_x()
            self.drag_dy = e.y_root - self.root.winfo_y()

    def on_drag(self, e):
        self.mx, self.my = e.x, e.y
        if self.press_target == "seek":
            self.update_seek(e.x)
            return
        if self.press_target == "volume":
            self.update_volume_from_x(e.x)
            return
        if self.press_target is not None or not self.intro_done:
            return
        self.dragged = True
        self.pos_x = e.x_root - self.drag_dx
        self.pos_y = e.y_root - self.drag_dy
        self.root.geometry(f"+{self.pos_x}+{self.pos_y}")

    def on_release(self, e):
        if self.press_target == "seek":
            self.update_seek(e.x)
            self.finish_seek()
        elif self.press_target == "volume":
            self.update_volume_from_x(e.x)
            self.vol_drag = False
        else:
            target = self.hit_test(e.x, e.y)
            if self.press_target and target == self.press_target:
                self.activate(target)
            elif self.dragged:
                self.schedule_save()
        self.press_target = None
        self.dragged = False

    def activate(self, name):
        if name == "gear":
            self.open_settings()
        else:
            self.click(name)

    def click(self, action):
        now = time.time()
        self.add_ripple(self.S(BTN_X[action]) + self.btn_dx, now)
        if action == "next":
            self.nudge = -self.S(NUDGE)
            self.pending_dir, self.pending_until = 1, now + 3
        elif action == "prev":
            self.nudge = self.S(NUDGE)
            self.pending_dir, self.pending_until = -1, now + 3
        else:
            # Instant feedback: flip the icon right away, don't wait for Windows.
            st = self.backend.state
            if st["available"]:
                pos = st["position_ms"]
                if st["playing"]:
                    pos += (now - st["updated_at"]) * 1000
                st["position_ms"], st["updated_at"] = pos, now
                st["playing"] = not st["playing"]
        self.backend.control(action)

    def add_ripple(self, x, now):
        S = self.S
        item = self.cv.create_oval(x - S(6), S(BTN_Y) - S(6), x + S(6), S(BTN_Y) + S(6),
                                   outline=self.accent_cur, width=2)
        self.ripples.append((item, x, now))

    def show_menu(self, e):
        try:
            self.menu.tk_popup(e.x_root, e.y_root)
        finally:
            self.menu.grab_release()

    def apply_topmost(self):
        self.root.attributes("-topmost", self.topmost_var.get())
        self.schedule_save()

    def open_settings(self):
        if self.settings is not None:
            try:
                if self.settings.win.winfo_exists():
                    self.settings.win.lift()
                    return
            except tk.TclError:
                pass
        self.settings = SettingsWindow(self)

    def quit_app(self):
        self.save_config()
        self.root.destroy()

    # -- Theme / settings persistence ------------------------------------------
    def set_theme_color(self, key, color):
        self.theme[key] = color
        self.apply_theme()
        self.schedule_save()

    def set_theme(self, theme):
        for k in self.theme:
            c = normalize_hex(theme.get(k, ""))
            if c:
                self.theme[k] = c
        self.apply_theme()
        self.schedule_save()

    def schedule_save(self):
        if self.save_job is not None:
            self.root.after_cancel(self.save_job)
        self.save_job = self.root.after(600, self.save_config)

    def save_config(self):
        self.save_job = None
        write_config({"theme": self.theme, "scale": self.scale, "opacity": self.opacity,
                      "font": self.font_family, "lyrics": self.lyrics_on,
                      "compact": self.compact, "auto_accent": self.auto_accent,
                      "x": self.pos_x, "y": self.pos_y,
                      "topmost": bool(self.topmost_var.get())})

    # -- Frame loop (~30 FPS) ---------------------------------------------------
    def frame(self):
        try:
            now = time.time()
            st = self.backend.state
            self.intro(now)
            self.sync_track(st, now)
            self.update_accent()
            self.nudge *= 0.84
            if abs(self.nudge) < 0.2:
                self.nudge = 0.0
            self.update_vol_state()
            self.layout_texts(now)
            self.layout_cover(now)
            self.update_flash(now)
            if self.compact:
                self.update_progress_compact(st, now)
                self.update_eq(st, now)
            else:
                self.update_progress(st, now)
                self.update_lyrics(st, now)
                self.update_eq(st, now)
                self.update_buttons(st)
                self.update_volume(now)
                self.update_gear()
                self.update_ripples(now)
        finally:
            self.root.after(33, self.frame)

    def intro(self, now):
        if self.intro_done:
            return
        p = clamp((now - self.intro_start) / 0.6)
        e = ease_out(p)
        try:
            self.root.attributes("-alpha", self.opacity * e)
        except tk.TclError:
            pass
        self.root.geometry(f"+{self.pos_x}+{int(self.pos_y + (1 - e) * self.S(30))}")
        if p >= 1:
            self.intro_done = True

    def apply_track_texts(self, st=None):
        st = st or self.backend.state
        available = st["available"]
        if available:
            title, artist = st["title"], st["artist"]
        else:
            title, artist = "Nothing playing", "Start a song in Spotify"
        S = self.S
        if self.compact:
            self.cv.itemconfig(self.t_title, text=fit(self.f_compact, title, S(CW0 - C_TX - 44)))
            return
        self.cv.itemconfig(self.t_title, text=fit(self.f_title, title, S(W0 - TX - 16)))
        self.cv.itemconfig(self.t_artist, text=fit(self.f_artist, artist, S(W0 - TX - 124)))

        # Logo: Spotify logo for Spotify, a note icon for any other app
        is_spotify = (not available) or ("spotify" in st["app"].lower())
        for item in self.logo:
            self.cv.itemconfig(item, state="normal" if is_spotify else "hidden")
        self.cv.itemconfig(self.logo_note, state="hidden" if is_spotify else "normal")

    def sync_track(self, st, now):
        available = st["available"]
        key = (st["title"], st["artist"]) if available else None

        if key != self.shown_key:
            self.shown_key = key
            self.apply_track_texts(st)
            self.flash_active = False
            self.anim_start = now
            self.texts_settled = False
            # If a skip button was pressed, the new song slides in from that side.
            self.slide_dir = self.pending_dir if now < self.pending_until else 1
            self.pending_until = 0.0
            self.disp_pos = st["position_ms"] if available else 0.0

        cover = st["cover"] if available else None
        if cover is not self.cover_src:
            self.set_cover(cover, now)
            self.refresh_accent_target(snap=False)

    def set_cover(self, data, now):
        self.cover_src = data
        self.cover_frames = []
        self.cover_idx = -1
        self.cover_anim = now
        if data:
            try:
                img = Image.open(io.BytesIO(data)).convert("RGB")
                for f in (0.78, 0.86, 0.93, 0.98, 1.0):
                    size = max(int(round(self.S(self.cov[2]) * f)), 8)
                    frame = img.resize((size, size), Image.Resampling.LANCZOS)
                    self.cover_frames.append(ImageTk.PhotoImage(frame))
            except Exception:
                self.cover_frames = []
        has = bool(self.cover_frames)
        self.cv.itemconfig(self.cover_item, state="normal" if has else "hidden")
        self.cv.itemconfig(self.cover_ph, state="hidden" if has else "normal")
        self.cv.itemconfig(self.cover_note, state="hidden" if has else "normal")

    def layout_texts(self, now):
        S = self.S
        done = True
        for item, x, y, ckey, delay in self.text_items:
            color = self.theme[ckey]
            if self.texts_settled:
                off, fill = 0.0, color
            else:
                p = clamp((now - self.anim_start - delay) / 0.45)
                e = ease_out(p)
                off = self.slide_dir * (1 - e) * S(28)
                fill = mix(self.theme["bg"], color, e)
                if p < 1:
                    done = False
            self.cv.coords(item, x + off + self.nudge, y)
            self.cv.itemconfig(item, fill=fill)
        if not self.texts_settled:
            self.texts_settled = done

    def layout_cover(self, now):
        S = self.S
        x, y, size = self.cov
        off = self.nudge * 0.5
        cx, cy = S(x + size / 2) + off, S(y + size / 2)
        self.cv.coords(self.cover_ph, S(x) + off, S(y), S(x + size) + off, S(y + size))
        self.cv.coords(self.cover_note, cx, cy)
        self.cv.coords(self.cover_item, cx, cy)

        n = len(self.cover_frames)
        if n:
            p = clamp((now - self.cover_anim) / 0.35)
            idx = min(int(ease_out(p) * (n - 1) + 0.5), n - 1)
            if idx != self.cover_idx:
                self.cover_idx = idx
                self.cv.itemconfig(self.cover_item, image=self.cover_frames[idx])

    # -- Progress -----------------------------------------------------------------
    def current_position(self, st, now):
        """Returns (target position ms, duration ms) and advances the displayed position."""
        available = st["available"]
        dur = st["duration_ms"] if available else 0.0
        playing = bool(available and st["playing"])

        if self.seeking and dur > 0:
            target = self.seek_pos
        elif available:
            if now < self.seek_hold_until:
                target = self.seek_hold_pos + ((now - self.seek_hold_t0) * 1000 if playing else 0.0)
            else:
                target = st["position_ms"] + ((now - st["updated_at"]) * 1000 if playing else 0.0)
            if dur > 0:
                target = min(target, dur)
        else:
            target = 0.0

        if self.seeking:
            self.disp_pos = target
        else:
            diff = target - self.disp_pos
            if abs(diff) > 5000:
                self.disp_pos = target          # big jump: go straight there
            else:
                self.disp_pos += diff * 0.25    # small difference: glide
        return dur

    def update_progress(self, st, now):
        S = self.S
        dur = self.current_position(st, now)

        near = self.hover and abs(self.my - S(BAR_Y)) <= S(14) and dur > 0
        self.bar_hot += ((1.0 if (self.seeking or near) else 0.0) - self.bar_hot) * 0.25
        kr = S(3.5 + 2.5 * self.bar_hot)

        ratio = clamp(self.disp_pos / dur) if dur > 0 else 0.0
        x = S(BAR_X1) + S(BAR_X2 - BAR_X1) * ratio
        self.cv.coords(self.bar_fg, S(BAR_X1), S(BAR_Y), x, S(BAR_Y))
        self.cv.coords(self.knob, x - kr, S(BAR_Y) - kr, x + kr, S(BAR_Y) + kr)

        times = (fmt(self.disp_pos), "-" + fmt(dur - self.disp_pos))
        if times != self.last_times:
            self.last_times = times
            self.cv.itemconfig(self.t_left, text=times[0])
            self.cv.itemconfig(self.t_right, text=times[1])

    def update_progress_compact(self, st, now):
        S = self.S
        dur = self.current_position(st, now)
        ratio = clamp(self.disp_pos / dur) if dur > 0 else 0.0
        x = S(C_TX) + S(CW0 - 12 - C_TX) * ratio
        self.cv.coords(self.bar_fg, S(C_TX), S(C_BAR_Y), x, S(C_BAR_Y))

    # -- Volume -------------------------------------------------------------------
    def update_vol_state(self):
        """Eases the displayed volume toward the real one."""
        if not self.volume_ready():
            return
        target = self.vol_drag_level if self.vol_drag else self.volume.level
        if self.vol_ui is None:
            self.vol_ui = target
        else:
            self.vol_ui += (target - self.vol_ui) * 0.35

    def update_flash(self, now):
        """Briefly shows 'volume NN%' in place of the 'now playing' label (or the title)."""
        if now < self.flash_until:
            text = f"volume {self.flash_pct}%"
            if self.compact:
                self.cv.itemconfig(self.t_title, text=text)
            else:
                self.cv.itemconfig(self.t_now, text=text)
            self.flash_active = True
        elif self.flash_active:
            self.flash_active = False
            if self.compact:
                self.apply_track_texts()
            else:
                self.cv.itemconfig(self.t_now, text="now playing")

    def update_volume(self, now):
        S, t, cv = self.S, self.theme, self.cv
        if self.vol_ui is None or not self.volume_ready() or self.hover_t < 0.02:
            if self.vol_visible:
                for item in self.vol_items:
                    cv.itemconfig(item, state="hidden")
                self.vol_visible = False
            return
        self.vol_visible = True
        lvl = clamp(self.vol_ui)
        h = self.hover_t
        bg = t["bg"]
        vy = S(VOL_Y)
        x = S(VOL_X1) + S(VOL_X2 - VOL_X1) * lvl
        kr = S(4.5 if self.vol_drag else 3.5)

        cv.itemconfig(self.vol_icon, state="normal", fill=mix(bg, t["text2"], h))
        for i, wave in enumerate(self.vol_waves):
            shown = lvl > (0.02 if i == 0 else 0.5)
            cv.itemconfig(wave, state="normal" if shown else "hidden", outline=mix(bg, t["text2"], h))
        cv.itemconfig(self.vol_track, state="normal", fill=mix(bg, mix(bg, t["title"], 0.22), h))
        cv.coords(self.vol_fill, S(VOL_X1), vy, x, vy)
        cv.itemconfig(self.vol_fill, state="normal", fill=mix(bg, self.accent_cur, h))
        cv.coords(self.vol_knob, x - kr, vy - kr, x + kr, vy + kr)
        cv.itemconfig(self.vol_knob, state="normal", fill=mix(bg, t["title"], h))

    # -- Lyrics ---------------------------------------------------------------
    def prepare_lyrics(self):
        """Pre-computes the (ellipsized) strings of the current lyrics for the current size."""
        self.lyr_fit = []
        maxw = self.S(W0 - 36)
        for _, text in self.lyr_lines:
            self.lyr_fit.append((fit(self.f_lyr_cur, text, maxw), fit(self.f_lyr, text, maxw)))
        self.slot_cache = ["hidden"] * len(self.lyr_slots)

    def lyric_message(self, status, now):
        if status == "loading":
            n = int(now * 2.5) % 4
            return "Loading lyrics" + "." * n + " " * (3 - n)
        if status == "none":
            return "No synced lyrics for this song"
        if status == "instrumental":
            return "♪  Instrumental  ♪"
        if status == "error":
            return "Couldn't load lyrics"
        return ""

    def set_lyr_msg(self, text):
        if self.t_lyr_msg is None:
            return
        color = mix(self.theme["bg"], self.theme["text2"], 0.8)
        new = (text, color)
        if new == self.lyr_msg_cache:
            return
        self.lyr_msg_cache = new
        if text:
            self.cv.itemconfig(self.t_lyr_msg, text=text, fill=color, state="normal")
        else:
            self.cv.itemconfig(self.t_lyr_msg, state="hidden")

    def hide_lyric_slots(self):
        for k, item in enumerate(self.lyr_slots):
            if self.slot_cache[k] != "hidden":
                self.cv.itemconfig(item, state="hidden")
                self.slot_cache[k] = "hidden"

    def update_lyrics(self, st, now):
        if not self.lyrics_on or not self.lyr_slots:
            return
        key = self.shown_key if st["available"] else None
        status = "idle"
        data = None
        if isinstance(key, tuple):
            status, data = self.lyrics_store.ensure(key, st)

        if status == "ok":
            if self.lyr_key != key:
                self.lyr_key = key
                self.lyr_lines = data
                self.lyr_times = [ms for ms, _ in data]
                self.lyr_pos = -1.0
                self.prepare_lyrics()
            self.set_lyr_msg("")
            self.draw_lyric_lines()
        else:
            if self.lyr_key is not None:
                self.lyr_key = None
                self.lyr_lines = []
                self.lyr_times = []
                self.lyr_fit = []
            self.hide_lyric_slots()
            self.set_lyr_msg(self.lyric_message(status, now))

    def draw_lyric_lines(self):
        S, cv, t = self.S, self.cv, self.theme
        pos = self.disp_pos + LYRIC_LEAD_MS
        idx = bisect.bisect_right(self.lyr_times, pos) - 1   # -1 = before the first line
        target = float(idx)
        diff = target - self.lyr_pos
        # Smooth scroll when the line changes; snap on big jumps (seek / new song)
        self.lyr_pos = target if abs(diff) > 4 else self.lyr_pos + diff * 0.2

        base = math.floor(self.lyr_pos) - 3
        cy = S(LYR_TOP0 + LYR_H0 / 2)
        lh = S(LYR_LH)
        cx = self.W / 2
        n = len(self.lyr_lines)
        for k, item in enumerate(self.lyr_slots):
            li = base + k
            d = li - self.lyr_pos
            a = lyr_alpha(d)
            if 0 <= li < n and a > 0.02:
                cur = abs(d) < 0.5
                text = self.lyr_fit[li][0 if cur else 1]
                hl = clamp(1 - abs(d))
                color = mix(t["bg"], mix(t["text2"], t["title"], hl), a)
                new = (text, cur, color)
                cv.coords(item, cx, cy + d * lh)
                if self.slot_cache[k] != new:
                    cv.itemconfig(item, text=text, fill=color, state="normal",
                                  font=self.f_lyr_cur if cur else self.f_lyr)
                    self.slot_cache[k] = new
            elif self.slot_cache[k] != "hidden":
                cv.itemconfig(item, state="hidden")
                self.slot_cache[k] = "hidden"

    # -- Other animations ---------------------------------------------------------
    def update_eq(self, st, now):
        S = self.S
        playing = st["available"] and st["playing"]
        if self.compact:
            base_x, base_y = CW0 - 34, 34
        else:
            base_x, base_y = W0 - 100, 32
        for i, bar in enumerate(self.eq):
            if playing:
                target = 4 + 12 * (0.5 + 0.5 * math.sin(now * (5 + i * 1.7) + i * 1.3))
            else:
                target = 3
            self.eq_h[i] += (target - self.eq_h[i]) * 0.3
            x0 = S(base_x + i * 6)
            self.cv.coords(bar, x0, S(base_y) - S(self.eq_h[i]), x0 + S(4), S(base_y))

    def update_buttons(self, st):
        S, t, cv = self.S, self.theme, self.cv
        playing = bool(st["available"] and st["playing"])

        # Buttons fade in while the mouse is over the window
        target = 1.0 if self.hover else 0.0
        icon_changed = self.icon_dirty
        if abs(target - self.hover_t) > 0.001:
            step = 0.2
            self.hover_t = min(target, self.hover_t + step) if target > self.hover_t \
                else max(target, self.hover_t - step)
            icon_changed = True

        if playing != self.last_playing:
            self.last_playing = playing
            cv.itemconfig(self.play_icon, state="hidden" if playing else "normal")
            for item in self.pause_icons:
                cv.itemconfig(item, state="normal" if playing else "hidden")

        if icon_changed:
            self.icon_dirty = False
            color = mix(t["bg"], t["title"], self.hover_t)
            for item in self.icon_items:
                cv.itemconfig(item, fill=color)

        # Buttons follow the mouse horizontally (±8 px at 100%)
        dx_target = ((self.mx / self.W) - 0.5) * S(16) if self.hover else 0.0
        self.btn_dx += (dx_target - self.btn_dx) * 0.2
        if self.placed_dx is None or abs(self.btn_dx - self.placed_dx) > 0.05:
            self.placed_dx = self.btn_dx
            self.place_buttons(self.btn_dx)

        # Highlight circle glides to the button under the mouse
        over = None
        if self.hover and self.hover_t > 0.3 and abs(self.my - S(BTN_Y)) <= S(20) \
                and self.mx > self.W - S(124):
            over = min(BTN_X, key=lambda n: abs(self.mx - (S(BTN_X[n]) + self.btn_dx)))
        if over:
            tx = S(BTN_X[over]) + self.btn_dx
            if self.hl_a < 0.05:
                self.hl_x = tx
            else:
                self.hl_x += (tx - self.hl_x) * 0.35
        self.hl_a += ((1.0 if over else 0.0) - self.hl_a) * 0.3
        pressed = self.press_target in BTN_X and over == self.press_target
        r = S(11 if pressed else 13)
        by = S(BTN_Y)
        cv.coords(self.hl, self.hl_x - r, by - r, self.hl_x + r, by + r)
        cv.itemconfig(self.hl, fill=mix(t["bg"], t["title"], (0.26 if pressed else 0.14) * self.hl_a))

    def update_gear(self):
        S, t, cv = self.S, self.theme, self.cv
        over = self.hover and self.hit_test(self.mx, self.my) == "gear"
        self.gear_speed += ((220.0 if over else 0.0) - self.gear_speed) * 0.15
        self.gear_hot += ((1.0 if over else 0.0) - self.gear_hot) * 0.25
        if abs(self.gear_speed) > 0.3:
            self.gear_angle = (self.gear_angle + self.gear_speed * 0.033) % 360
            cv.coords(self.gear, *gear_points(S(GEAR_POS[0]), S(GEAR_POS[1]),
                                              self.gear_angle, S(8.5), S(6.2)))
        cv.itemconfig(self.gear, fill=mix(t["text2"], t["title"], self.gear_hot))

    def update_ripples(self, now):
        S = self.S
        alive = []
        for item, x, t0 in self.ripples:
            p = (now - t0) / 0.45
            if p >= 1:
                self.cv.delete(item)
                continue
            r = S(6 + 16 * ease_out(p))
            by = S(BTN_Y)
            self.cv.coords(item, x - r, by - r, x + r, by + r)
            self.cv.itemconfig(item, outline=mix(self.accent_cur, self.theme["bg"], p))
            alive.append((item, x, t0))
        self.ripples = alive


if __name__ == "__main__":
    root = tk.Tk()
    MiniPlayer(root, MediaBackend())
    root.mainloop()
