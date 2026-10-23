"""
Spotify Mini Player (Windows)

A small, always-on-top, HUD-style "now playing" window. It connects to the
Windows system media session (SMTC), so no Spotify API, Premium account or
Client ID is needed.

Usage:
  - Drag the window by any empty area.
  - Hover over it to reveal the previous / play-pause / next buttons.
  - Click or drag on the progress bar to seek.
  - Gear icon (top right): themes, colors (HEX / RGB), size, lyrics.
  - Ctrl + mouse wheel: resize the window.
  - Right-click: menu.

Synced lyrics are fetched from LRCLIB (https://lrclib.net), not from Spotify.
Settings are stored in %APPDATA%\\SpotifyMiniPlayer\\config.json
"""

import asyncio
import bisect
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

from PIL import Image, ImageTk
from winsdk.windows.media.control import (
    GlobalSystemMediaTransportControlsSessionManager as MediaManager,
    GlobalSystemMediaTransportControlsSessionPlaybackStatus as PlaybackStatus,
)
from winsdk.windows.storage.streams import Buffer, DataReader, InputStreamOptions

# ----------------------------------------------------------------------------
# Constants (all layout values are in "logical" pixels at 100% size)
# ----------------------------------------------------------------------------
FONT_FAMILY = "Consolas"  # you can put an installed pixel font name here

W0, H0 = 430, 110          # player part
LYR_TOP0, LYR_H0 = 108, 102  # lyrics panel (below the player part)
LYR_LH = 26                # lyric line spacing
LYR_SLOTS = 7
LYRIC_LEAD_MS = 300        # show lines slightly early so they are readable

SCALE_MIN, SCALE_MAX, SCALE_STEP = 0.6, 2.0, 0.05

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
    "accent": "#1DB954",  # progress bar, equalizer, ripple
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
              "text2": "#555555", "accent": "#1DB954"},
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
USER_AGENT = "SpotifyMiniPlayer/1.3 (https://github.com/Mc-Kaan/Spotify-Mini-Player)"


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

        head = ("Segoe UI", 10, "bold")

        # Presets
        tk.Label(win, text="Presets", bg=self.BG, fg=self.FG, font=head)\
            .pack(anchor="w", padx=14, pady=(12, 4))
        pf = tk.Frame(win, bg=self.BG)
        pf.pack(anchor="w", padx=11)
        for i, name in enumerate(PRESETS):
            tk.Button(pf, text=name, command=lambda n=name: self.apply_preset(n),
                      bg=self.BTN, fg=self.FG, activebackground="#444444",
                      activeforeground=self.FG, relief="flat", bd=0,
                      padx=10, pady=4, width=10)\
                .grid(row=i // 3, column=i % 3, padx=3, pady=3, sticky="ew")

        # Color targets
        tk.Label(win, text="Colors", bg=self.BG, fg=self.FG, font=head)\
            .pack(anchor="w", padx=14, pady=(12, 2))
        self.swatches = {}
        for k, label in THEME_LABELS:
            row = tk.Frame(win, bg=self.BG)
            row.pack(fill="x", padx=14, pady=1)
            tk.Radiobutton(row, text=label, variable=self.key, value=k,
                           command=self.load_editor, bg=self.BG, fg=self.FG,
                           selectcolor=self.FIELD, activebackground=self.BG,
                           activeforeground=self.FG, anchor="w", width=30)\
                .pack(side="left")
            sw = tk.Label(row, width=4, bg=player.theme[k], relief="solid", bd=1)
            sw.pack(side="right")
            self.swatches[k] = sw

        # Editor: preview, HEX, RGB
        ed = tk.Frame(win, bg=self.BG)
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
                  activebackground="#444444", activeforeground=self.FG,
                  relief="flat", bd=0, padx=8, pady=2)\
            .grid(row=0, column=3, padx=4)

        self.scales = {}
        sf = tk.Frame(win, bg=self.BG)
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

        # Window: size and lyrics
        tk.Label(win, text="Window", bg=self.BG, fg=self.FG, font=head)\
            .pack(anchor="w", padx=14, pady=(12, 2))
        zf = tk.Frame(win, bg=self.BG)
        zf.pack(fill="x", padx=14)
        tk.Label(zf, text="Size (%)", bg=self.BG, fg=self.FG, width=8, anchor="w")\
            .grid(row=0, column=0)
        self.size_scale = tk.Scale(zf, from_=int(SCALE_MIN * 100), to=int(SCALE_MAX * 100),
                                   resolution=5, orient="horizontal", length=280,
                                   command=self.on_size, bg=self.BG, fg=self.FG,
                                   troughcolor=self.FIELD, highlightthickness=0, bd=0,
                                   activebackground=GREEN)
        self.size_scale.grid(row=0, column=1, padx=4)
        tk.Checkbutton(win, text="Show synced lyrics (from LRCLIB)", variable=player.lyrics_var,
                       command=player.on_lyrics_toggle, bg=self.BG, fg=self.FG,
                       selectcolor=self.FIELD, activebackground=self.BG,
                       activeforeground=self.FG, anchor="w")\
            .pack(fill="x", padx=14, pady=(4, 0))
        tk.Label(win, text="Tip: Ctrl + mouse wheel over the player also resizes it.",
                 bg=self.BG, fg="#8a8a8a", font=("Segoe UI", 8), anchor="w")\
            .pack(fill="x", padx=16)

        # Bottom buttons
        bf = tk.Frame(win, bg=self.BG)
        bf.pack(fill="x", padx=14, pady=12)
        tk.Button(bf, text="Reset all", command=self.reset_all,
                  bg=self.BTN, fg=self.FG, activebackground="#444444",
                  activeforeground=self.FG, relief="flat", bd=0, padx=10, pady=4)\
            .pack(side="left")
        tk.Button(bf, text="Close", command=self.close, bg=GREEN, fg="#000000",
                  activebackground="#1ed760", activeforeground="#000000",
                  relief="flat", bd=0, padx=16, pady=4)\
            .pack(side="right")

        self.load_editor()
        self.sync_scale()
        self.place()

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

    # -- Editor ---------------------------------------------------------------
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

    # -- Window size ------------------------------------------------------------
    def on_size(self, value):
        if self.updating:
            return
        self.player.set_scale(float(value) / 100.0)

    def sync_scale(self):
        self.updating = True
        self.size_scale.set(int(round(self.player.scale * 100)))
        self.updating = False

    def reset_all(self):
        self.apply_preset("Black")
        self.player.set_scale(1.0)
        self.sync_scale()
        if not self.player.lyrics_var.get():
            self.player.lyrics_var.set(True)
            self.player.on_lyrics_toggle()

    def close(self):
        self.player.settings = None
        self.player.save_config()
        self.win.destroy()


# ----------------------------------------------------------------------------
# Main UI
# ----------------------------------------------------------------------------
class MiniPlayer:
    def __init__(self, root, backend):
        self.root = root
        self.backend = backend
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
        self.s = self.scale

        # Borderless, rounded corners, always on top
        root.overrideredirect(True)
        self.topmost_var = tk.BooleanVar(value=bool(cfg.get("topmost", True)))
        self.lyrics_var = tk.BooleanVar(value=bool(cfg.get("lyrics", True)))
        self.lyrics_on = bool(self.lyrics_var.get())
        root.attributes("-topmost", self.topmost_var.get())
        root.configure(bg=KEY)
        try:
            root.attributes("-transparentcolor", KEY)
        except tk.TclError:
            pass

        fam = FONT_FAMILY
        self.f_label = tkfont.Font(family=fam, size=-12)
        self.f_title = tkfont.Font(family=fam, size=-16, weight="bold")
        self.f_artist = tkfont.Font(family=fam, size=-13)
        self.f_time = tkfont.Font(family=fam, size=-12)
        self.f_cover_note = tkfont.Font(family=fam, size=-29)
        self.f_logo_note = tkfont.Font(family=fam, size=-21)
        self.f_lyr = tkfont.Font(family=fam, size=-14)
        self.f_lyr_cur = tkfont.Font(family=fam, size=-16, weight="bold")
        self.f_lyr_msg = tkfont.Font(family=fam, size=-13)
        self.font_specs = [(self.f_label, 12), (self.f_title, 16), (self.f_artist, 13),
                           (self.f_time, 12), (self.f_cover_note, 29), (self.f_logo_note, 21),
                           (self.f_lyr, 14), (self.f_lyr_cur, 16), (self.f_lyr_msg, 13)]
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
        # seeking
        self.seeking = False
        self.seek_pos = 0.0
        self.seek_hold_until = 0.0
        self.seek_hold_pos = 0.0
        self.seek_hold_t0 = 0.0
        self.bar_hot = 0.0
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
        self.W = int(round(W0 * self.s))
        self.H = int(round((H0 + (LYR_H0 if self.lyrics_on else 0)) * self.s))
        for font, px in self.font_specs:
            font.configure(size=-max(6, int(round(px * self.s))))

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
        if self.relayout_job is not None:
            self.root.after_cancel(self.relayout_job)
        self.relayout_job = self.root.after(120, self.do_relayout)
        self.schedule_save()

    def do_relayout(self):
        self.relayout_job = None
        self.relayout()

    def on_lyrics_toggle(self):
        self.lyrics_on = bool(self.lyrics_var.get())
        self.relayout()
        self.schedule_save()

    def relayout(self):
        """Rebuilds the whole canvas for a new size / lyrics setting."""
        now = time.time()
        self.compute_layout()
        self.ripples = []
        self.cv.delete("all")
        self.cv.config(width=self.W, height=self.H)
        if not self.intro_done:
            self.intro_done = True
            try:
                self.root.attributes("-alpha", 0.97)
            except tk.TclError:
                pass
        self.root.geometry(f"{self.W}x{self.H}+{self.pos_x}+{self.pos_y}")
        self.build()
        self.apply_theme()
        self.last_playing = None
        self.last_times = ("", "")
        self.icon_dirty = True
        self.hl_x = self.S(BTN_X["toggle"])
        data = self.cover_src if isinstance(self.cover_src, (bytes, bytearray)) else None
        self.set_cover(data, now - 10)
        self.apply_track_texts()
        self.anim_start = now - 10
        self.texts_settled = False
        self.prepare_lyrics()
        self.lyr_msg_cache = None

    # -- Drawing ----------------------------------------------------------------
    def build(self):
        cv, S, W, H = self.cv, self.S, self.W, self.H
        self.bg_item = rounded_rect(cv, 1, 1, W - 1, H - 1, S(16),
                                    fill="#000000", outline="#000000", width=1)

        # Cover (placeholder + real cover)
        cx, cy = S(COVER_X + COVER / 2), S(COVER_Y + COVER / 2)
        self.cover_ph = cv.create_rectangle(S(COVER_X), S(COVER_Y), S(COVER_X + COVER),
                                            S(COVER_Y + COVER), fill="#000000", outline="")
        self.cover_note = cv.create_text(cx, cy, text="♪", font=self.f_cover_note)
        self.cover_item = cv.create_image(cx, cy, state="hidden")

        # Texts
        self.t_now = cv.create_text(S(TX), S(22), text="now playing", anchor="w", font=self.f_label)
        self.t_title = cv.create_text(S(TX), S(TITLE_Y), text="", anchor="w", font=self.f_title)
        self.t_artist = cv.create_text(S(TX), S(ARTIST_Y), text="", anchor="w", font=self.f_artist)
        # (item, y, color key, delay in s)
        self.text_items = [(self.t_title, S(TITLE_Y), "title", 0.0),
                           (self.t_artist, S(ARTIST_Y), "text2", 0.08)]

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
        self.lyr_slots = []
        self.lyr_div = None
        self.t_lyr_msg = None
        if self.lyrics_on:
            self.lyr_div = cv.create_line(S(16), S(LYR_TOP0 - 2), W - S(16), S(LYR_TOP0 - 2), width=1)
            self.lyr_slots = [cv.create_text(W / 2, 0, text="", font=self.f_lyr, state="hidden")
                              for _ in range(LYR_SLOTS)]
            self.t_lyr_msg = cv.create_text(W / 2, S(LYR_TOP0 + LYR_H0 / 2), text="",
                                            font=self.f_lyr_msg, state="hidden")
        self.slot_cache = ["hidden"] * len(self.lyr_slots)

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

    def apply_theme(self):
        """Applies theme colors to static items (animated items read the theme each frame)."""
        t, cv = self.theme, self.cv
        bg = t["bg"]
        cv.itemconfig(self.bg_item, fill=bg, outline=t["border"])
        cv.itemconfig(self.cover_ph, fill=mix(bg, "#000000", 0.3))
        cv.itemconfig(self.cover_note, fill=t["text2"])
        cv.itemconfig(self.t_now, fill=t["text2"])
        cv.itemconfig(self.bar_bg, fill=mix(bg, t["title"], 0.22))
        cv.itemconfig(self.bar_fg, fill=t["accent"])
        cv.itemconfig(self.knob, fill=t["accent"])
        cv.itemconfig(self.t_left, fill=t["text2"])
        cv.itemconfig(self.t_right, fill=t["text2"])
        cv.itemconfig(self.logo_note, fill=t["text2"])
        cv.itemconfig(self.gear_hole, fill=bg)
        for bar in self.eq:
            cv.itemconfig(bar, fill=t["accent"])
        if self.lyr_div is not None:
            cv.itemconfig(self.lyr_div, fill=mix(bg, t["text2"], 0.18))
        self.slot_cache = [None] * len(self.lyr_slots)
        self.lyr_msg_cache = None
        self.texts_settled = False
        self.icon_dirty = True

    def bind_events(self):
        cv = self.cv
        cv.bind("<Enter>", self.on_motion)
        cv.bind("<Motion>", self.on_motion)
        cv.bind("<Leave>", self.on_leave)
        cv.bind("<ButtonPress-1>", self.on_press)
        cv.bind("<B1-Motion>", self.on_drag)
        cv.bind("<ButtonRelease-1>", self.on_release)
        cv.bind("<Control-MouseWheel>", self.on_wheel)

        self.menu = tk.Menu(self.root, tearoff=0)
        self.menu.add_checkbutton(label="Always on top", variable=self.topmost_var,
                                  command=self.apply_topmost)
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

    def hit_test(self, x, y):
        S = self.S
        gx, gy = S(GEAR_POS[0]), S(GEAR_POS[1])
        if (x - gx) ** 2 + (y - gy) ** 2 <= S(13) ** 2:
            return "gear"
        if self.hover_t > 0.5:
            for name in ("prev", "toggle", "next"):
                if abs(x - (S(BTN_X[name]) + self.btn_dx)) <= S(15) and abs(y - S(BTN_Y)) <= S(14):
                    return name
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

    def on_wheel(self, e):
        self.set_scale(self.scale + (SCALE_STEP if e.delta > 0 else -SCALE_STEP))

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

    def on_press(self, e):
        self.mx, self.my = e.x, e.y
        self.press_target = self.hit_test(e.x, e.y)
        self.dragged = False
        if self.press_target == "seek":
            self.seeking = True
            self.update_seek(e.x)
        elif self.press_target is None:
            self.drag_dx = e.x_root - self.root.winfo_x()
            self.drag_dy = e.y_root - self.root.winfo_y()

    def on_drag(self, e):
        self.mx, self.my = e.x, e.y
        if self.press_target == "seek":
            self.update_seek(e.x)
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
                                   outline=self.theme["accent"], width=2)
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
        write_config({"theme": self.theme, "scale": self.scale, "lyrics": self.lyrics_on,
                      "x": self.pos_x, "y": self.pos_y,
                      "topmost": bool(self.topmost_var.get())})

    # -- Frame loop (~30 FPS) ---------------------------------------------------
    def frame(self):
        try:
            now = time.time()
            st = self.backend.state
            self.intro(now)
            self.sync_track(st, now)
            self.nudge *= 0.84
            if abs(self.nudge) < 0.2:
                self.nudge = 0.0
            self.layout_texts(now)
            self.layout_cover(now)
            self.update_progress(st, now)
            self.update_lyrics(st, now)
            self.update_eq(st, now)
            self.update_buttons(st)
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
            self.root.attributes("-alpha", 0.97 * e)
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
            self.anim_start = now
            self.texts_settled = False
            # If a skip button was pressed, the new song slides in from that side.
            self.slide_dir = self.pending_dir if now < self.pending_until else 1
            self.pending_until = 0.0
            self.disp_pos = st["position_ms"] if available else 0.0

        cover = st["cover"] if available else None
        if cover is not self.cover_src:
            self.set_cover(cover, now)

    def set_cover(self, data, now):
        self.cover_src = data
        self.cover_frames = []
        self.cover_idx = -1
        self.cover_anim = now
        if data:
            try:
                img = Image.open(io.BytesIO(data)).convert("RGB")
                for f in (0.78, 0.86, 0.93, 0.98, 1.0):
                    size = max(int(round(self.S(COVER) * f)), 8)
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
        for item, y, ckey, delay in self.text_items:
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
            self.cv.coords(item, S(TX) + off + self.nudge, y)
            self.cv.itemconfig(item, fill=fill)
        if not self.texts_settled:
            self.texts_settled = done

    def layout_cover(self, now):
        S = self.S
        off = self.nudge * 0.5
        cx, cy = S(COVER_X + COVER / 2) + off, S(COVER_Y + COVER / 2)
        self.cv.coords(self.cover_ph, S(COVER_X) + off, S(COVER_Y),
                       S(COVER_X + COVER) + off, S(COVER_Y + COVER))
        self.cv.coords(self.cover_note, cx, cy)
        self.cv.coords(self.cover_item, cx, cy)

        n = len(self.cover_frames)
        if n:
            p = clamp((now - self.cover_anim) / 0.35)
            idx = min(int(ease_out(p) * (n - 1) + 0.5), n - 1)
            if idx != self.cover_idx:
                self.cover_idx = idx
                self.cv.itemconfig(self.cover_item, image=self.cover_frames[idx])

    def update_progress(self, st, now):
        S = self.S
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
        for i, bar in enumerate(self.eq):
            if playing:
                target = 4 + 12 * (0.5 + 0.5 * math.sin(now * (5 + i * 1.7) + i * 1.3))
            else:
                target = 3
            self.eq_h[i] += (target - self.eq_h[i]) * 0.3
            x0 = S(W0 - 100 + i * 6)
            self.cv.coords(bar, x0, S(32) - S(self.eq_h[i]), x0 + S(4), S(32))

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
            self.cv.itemconfig(item, outline=mix(self.theme["accent"], self.theme["bg"], p))
            alive.append((item, x, t0))
        self.ripples = alive


if __name__ == "__main__":
    root = tk.Tk()
    MiniPlayer(root, MediaBackend())
    root.mainloop()
