"""
Spotify Mini Player (Windows) - HUD tarzı, animasyonlu, özelleştirilebilir

Windows'un sistem medya oturumuna (SMTC) bağlanır; Spotify API'si, Premium
hesap veya Client ID gerekmez.

Kullanım:
  - Pencereyi boş bir yerinden sol tıkla basılı tutarak sürükle.
  - Fareyi pencerenin üstüne getirince önceki / oynat-durdur / sonraki
    butonları belirir ve fareyi takip eder.
  - Sağ üstteki çark simgesi: tema ve renk ayarları (hazır temalar, HEX, RGB).
  - Sağ tık: "Her zaman üstte" seçeneği ve "Kapat".

Ayarlar %APPDATA%\\SpotifyMiniPlayer\\config.json dosyasına kaydedilir.
"""

import asyncio
import io
import json
import math
import os
import re
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
from datetime import datetime, timezone

from PIL import Image, ImageTk
from winsdk.windows.media.control import (
    GlobalSystemMediaTransportControlsSessionManager as MediaManager,
    GlobalSystemMediaTransportControlsSessionPlaybackStatus as PlaybackStatus,
)
from winsdk.windows.storage.streams import Buffer, DataReader, InputStreamOptions

# ----------------------------------------------------------------------------
# Sabitler
# ----------------------------------------------------------------------------
FONT_FAMILY = "Consolas"  # Piksel yazı tipi kuruluysa adını buraya yazabilirsin

W, H = 430, 110
KEY = "#010203"  # pencere köşelerini saydam yapmak için kullanılan anahtar renk

GREEN = "#1DB954"
LOGO_DARK = "#050505"

COVER = 68
COVER_X, COVER_Y = 12, 12
TX = 96                       # metinlerin sol kenarı
TITLE_Y, ARTIST_Y = 46, 68
TITLE_MAX_W = W - TX - 16
ARTIST_MAX_W = W - TX - 124   # sağdaki butonlara yer bırak
BAR_X1, BAR_X2, BAR_Y = 56, W - 56, 94

BTN_Y = 68
BTN_X = {"prev": W - 96, "toggle": W - 64, "next": W - 32}
GEAR_POS = (W - 62, 24)
LOGO_POS = (W - 28, 24)
NUDGE = 22  # ileri/geri tıklanınca içeriğin kayma miktarı (px)

# Spotify logosundaki üç yay: (sol uç, tepe noktası, sağ uç, kalınlık) - 24x24 birim
SPOTIFY_BARS = [
    ((4.8, 8.3), (12.2, 7.4), (19.6, 9.8), 2.5),
    ((5.5, 12.5), (12.0, 11.7), (18.4, 13.7), 2.1),
    ((6.1, 16.2), (11.8, 15.3), (17.0, 17.2), 1.7),
]

DEFAULT_THEME = {
    "bg": "#161616",      # arka plan
    "border": "#2c2c2c",  # çerçeve
    "title": "#ffffff",   # şarkı adı, ikonlar
    "text2": "#b3b3b3",   # sanatçı, süreler, küçük yazılar
    "accent": "#1DB954",  # ilerleme çubuğu, ekolayzır, dalga efekti
}

PRESETS = {
    "Siyah": DEFAULT_THEME,
    "Koyu gri": {"bg": "#2b2b2b", "border": "#404040", "title": "#ffffff",
                 "text2": "#bdbdbd", "accent": "#ffffff"},
    "Gece mavisi": {"bg": "#2a3541", "border": "#3b4a5a", "title": "#ffffff",
                    "text2": "#aab5c2", "accent": "#ffffff"},
    "Mor": {"bg": "#241a33", "border": "#3a2b52", "title": "#ffffff",
            "text2": "#b9a8d6", "accent": "#b57bff"},
    "Bordo": {"bg": "#2a1519", "border": "#46222a", "title": "#ffffff",
              "text2": "#d0a9b0", "accent": "#ff4d6d"},
    "Açık": {"bg": "#f2f2f2", "border": "#d0d0d0", "title": "#111111",
             "text2": "#555555", "accent": "#1DB954"},
}

THEME_LABELS = [
    ("bg", "Arka plan"),
    ("border", "Çerçeve"),
    ("title", "Başlık yazısı ve ikonlar"),
    ("text2", "İkincil yazılar"),
    ("accent", "Vurgu (çubuk, ekolayzır)"),
]

CONFIG_DIR = os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"),
                          "SpotifyMiniPlayer")
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")


# ----------------------------------------------------------------------------
# Yardımcı fonksiyonlar
# ----------------------------------------------------------------------------
def fmt(ms):
    s = int(max(ms, 0) // 1000)
    return f"{s // 60}:{s % 60:02d}"


def normalize_hex(text, allow_short=True):
    """'#abc', 'AABBCC' gibi girdileri '#aabbcc' biçimine çevirir; geçersizse None."""
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


def bar_points(p0, pm, p2, cx, cy, n=14):
    """Spotify logosundaki bir yayı (karesel Bezier) nokta listesine çevirir."""
    c = (2 * pm[0] - 0.5 * (p0[0] + p2[0]), 2 * pm[1] - 0.5 * (p0[1] + p2[1]))
    pts = []
    for i in range(n + 1):
        t = i / n
        x = (1 - t) ** 2 * p0[0] + 2 * (1 - t) * t * c[0] + t * t * p2[0]
        y = (1 - t) ** 2 * p0[1] + 2 * (1 - t) * t * c[1] + t * t * p2[1]
        pts += [cx + (x - 12), cy + (y - 12)]
    return pts


def gear_points(cx, cy, angle, r_out=8.5, r_in=6.2, teeth=8):
    pts = []
    step = 360 / teeth
    for i in range(teeth):
        a = angle + i * step
        for da, r in ((-0.30 * step, r_in), (-0.17 * step, r_out),
                      (0.17 * step, r_out), (0.30 * step, r_in)):
            rad = math.radians(a + da)
            pts += [cx + r * math.cos(rad), cy + r * math.sin(rad)]
    return pts


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
# Windows medya oturumu (arka plan)
# ----------------------------------------------------------------------------
class MediaBackend:
    """Windows medya oturumunu ayrı bir thread'de izler ve kontrol eder."""

    def __init__(self):
        self.loop = asyncio.new_event_loop()
        self.session = None
        self._wake = None
        self.state = {
            "available": False,
            "title": "",
            "artist": "",
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
        # Varsa Spotify oturumunu tercih et, yoksa sistemin aktif oturumunu al.
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

                    # Windows konumu son güncelleme anındaki değer olarak verir;
                    # çalıyorsa geçen süreyi ekle.
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
                        title=props.title or "Bilinmeyen şarkı",
                        artist=props.artist or "",
                        app=session.source_app_user_model_id or "",
                        playing=playing,
                        position_ms=position,
                        duration_ms=duration,
                        updated_at=time.time(),
                    )
            except Exception:
                self.state["available"] = False

            # 1 sn bekle; butona basılınca daha erken uyandır.
            try:
                await asyncio.wait_for(self._wake.wait(), 1)
            except asyncio.TimeoutError:
                pass
            self._wake.clear()

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

        asyncio.run_coroutine_threadsafe(run(), self.loop)


# ----------------------------------------------------------------------------
# Ayarlar penceresi
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
        win.title("Mini Player Ayarları")
        win.configure(bg=self.BG)
        win.resizable(False, False)
        win.attributes("-topmost", True)
        win.protocol("WM_DELETE_WINDOW", self.close)

        self.key = tk.StringVar(value="bg")
        self.hex_var = tk.StringVar()

        head = ("Segoe UI", 10, "bold")

        # Hazır temalar
        tk.Label(win, text="Hazır temalar", bg=self.BG, fg=self.FG, font=head)\
            .pack(anchor="w", padx=14, pady=(12, 4))
        pf = tk.Frame(win, bg=self.BG)
        pf.pack(anchor="w", padx=11)
        for i, name in enumerate(PRESETS):
            tk.Button(pf, text=name, command=lambda n=name: self.apply_preset(n),
                      bg=self.BTN, fg=self.FG, activebackground="#444444",
                      activeforeground=self.FG, relief="flat", bd=0,
                      padx=10, pady=4, width=10)\
                .grid(row=i // 3, column=i % 3, padx=3, pady=3, sticky="ew")

        # Renk öğeleri
        tk.Label(win, text="Renk ayarla", bg=self.BG, fg=self.FG, font=head)\
            .pack(anchor="w", padx=14, pady=(12, 2))
        self.swatches = {}
        for k, label in THEME_LABELS:
            row = tk.Frame(win, bg=self.BG)
            row.pack(fill="x", padx=14, pady=1)
            tk.Radiobutton(row, text=label, variable=self.key, value=k,
                           command=self.load_editor, bg=self.BG, fg=self.FG,
                           selectcolor=self.FIELD, activebackground=self.BG,
                           activeforeground=self.FG, anchor="w", width=26)\
                .pack(side="left")
            sw = tk.Label(row, width=4, bg=player.theme[k], relief="solid", bd=1)
            sw.pack(side="right")
            self.swatches[k] = sw

        # Düzenleyici: önizleme, HEX, RGB
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
        tk.Button(ed, text="Renk seç…", command=self.pick, bg=self.BTN, fg=self.FG,
                  activebackground="#444444", activeforeground=self.FG,
                  relief="flat", bd=0, padx=8, pady=2)\
            .grid(row=0, column=3, padx=4)

        self.scales = {}
        sf = tk.Frame(win, bg=self.BG)
        sf.pack(fill="x", padx=14, pady=(6, 0))
        for i, (ch, col) in enumerate((("R", "#ff6b6b"), ("G", "#5fd068"), ("B", "#6aa8ff"))):
            tk.Label(sf, text=ch, bg=self.BG, fg=col, width=2,
                     font=("Consolas", 11, "bold")).grid(row=i, column=0)
            s = tk.Scale(sf, from_=0, to=255, orient="horizontal", length=250,
                         command=self.on_slider, bg=self.BG, fg=self.FG,
                         troughcolor=self.FIELD, highlightthickness=0, bd=0,
                         activebackground=col)
            s.grid(row=i, column=1, padx=4)
            self.scales[ch] = s

        # Alt butonlar
        bf = tk.Frame(win, bg=self.BG)
        bf.pack(fill="x", padx=14, pady=12)
        tk.Button(bf, text="Varsayılana dön", command=lambda: self.apply_preset("Siyah"),
                  bg=self.BTN, fg=self.FG, activebackground="#444444",
                  activeforeground=self.FG, relief="flat", bd=0, padx=10, pady=4)\
            .pack(side="left")
        tk.Button(bf, text="Kapat", command=self.close, bg=GREEN, fg="#000000",
                  activebackground="#1ed760", activeforeground="#000000",
                  relief="flat", bd=0, padx=16, pady=4)\
            .pack(side="right")

        self.load_editor()
        self.place()

    def place(self):
        win, p = self.win, self.player
        win.update_idletasks()
        sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
        w, h = win.winfo_reqwidth(), win.winfo_reqheight()
        x = min(max(p.pos_x + W // 2 - w // 2, 10), max(10, sw - w - 10))
        y = p.pos_y + H + 12
        if y + h > sh - 40:
            y = max(10, p.pos_y - h - 12)
        win.geometry(f"+{x}+{y}")

    # -- Düzenleyici -------------------------------------------------------
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
        # Yazarken sadece 6 haneli geçerli kodlar uygulanır; 3 haneli kısa kod Enter ile.
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
        res = colorchooser.askcolor(color=self.current(), parent=self.win, title="Renk seç")
        if res and res[1]:
            c = normalize_hex(res[1])
            if c:
                self.hex_var.set(c)
                self.on_hex()

    def apply_preset(self, name):
        self.player.set_theme(PRESETS[name])
        self.refresh_swatches()
        self.load_editor()

    def close(self):
        self.player.settings = None
        self.player.save_config()
        self.win.destroy()


# ----------------------------------------------------------------------------
# Ana arayüz
# ----------------------------------------------------------------------------
class MiniPlayer:
    def __init__(self, root, backend):
        self.root = root
        self.backend = backend
        self.settings = None

        cfg = load_config()
        self.theme = dict(DEFAULT_THEME)
        saved = cfg.get("theme")
        if isinstance(saved, dict):
            for k in self.theme:
                c = normalize_hex(str(saved.get(k, "")))
                if c:
                    self.theme[k] = c

        # Çerçevesiz, köşeleri yuvarlak, her zaman üstte
        root.overrideredirect(True)
        self.topmost_var = tk.BooleanVar(value=bool(cfg.get("topmost", True)))
        root.attributes("-topmost", self.topmost_var.get())
        root.configure(bg=KEY)
        try:
            root.attributes("-transparentcolor", KEY)
        except tk.TclError:
            pass

        sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
        self.pos_x, self.pos_y = sw - W - 24, 24
        try:
            x, y = int(cfg["x"]), int(cfg["y"])
            if -W + 60 <= x <= sw - 60 and 0 <= y <= sh - 60:
                self.pos_x, self.pos_y = x, y
        except (KeyError, TypeError, ValueError):
            pass
        root.geometry(f"{W}x{H}+{self.pos_x}+{self.pos_y + 30}")
        try:
            root.attributes("-alpha", 0.0)
        except tk.TclError:
            pass

        self.cv = tk.Canvas(root, width=W, height=H, bg=KEY, highlightthickness=0, bd=0)
        self.cv.pack()

        self.f_label = tkfont.Font(family=FONT_FAMILY, size=9)
        self.f_title = tkfont.Font(family=FONT_FAMILY, size=12, weight="bold")
        self.f_artist = tkfont.Font(family=FONT_FAMILY, size=10)
        self.f_time = tkfont.Font(family=FONT_FAMILY, size=9)

        # Durum
        self.shown_key = object()      # ilk karede mutlaka güncellensin
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
        self.mx, self.my = W / 2, H / 2
        self.btn_dx = 0.0
        self.placed_dx = None
        self.hl_x = BTN_X["toggle"]
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

        self.build()
        self.apply_theme()
        self.bind_events()
        self.frame()

    # -- Çizim -------------------------------------------------------------
    def build(self):
        cv = self.cv
        self.bg_item = rounded_rect(cv, 1, 1, W - 1, H - 1, 16, fill="#000000", outline="#000000", width=1)

        # Kapak (yer tutucu + gerçek kapak)
        cx, cy = COVER_X + COVER // 2, COVER_Y + COVER // 2
        self.cover_ph = cv.create_rectangle(COVER_X, COVER_Y, COVER_X + COVER, COVER_Y + COVER,
                                            fill="#000000", outline="")
        self.cover_note = cv.create_text(cx, cy, text="♪", font=(FONT_FAMILY, 22))
        self.cover_item = cv.create_image(cx, cy, state="hidden")

        # Metinler
        self.t_now = cv.create_text(TX, 22, text="now playing", anchor="w", font=self.f_label)
        self.t_title = cv.create_text(TX, TITLE_Y, text="", anchor="w", font=self.f_title)
        self.t_artist = cv.create_text(TX, ARTIST_Y, text="", anchor="w", font=self.f_artist)
        # (öğe, y, renk anahtarı, gecikme sn)
        self.text_items = [(self.t_title, TITLE_Y, "title", 0.0),
                           (self.t_artist, ARTIST_Y, "text2", 0.08)]

        # İlerleme çubuğu ve süreler
        self.bar_bg = cv.create_line(BAR_X1, BAR_Y, BAR_X2, BAR_Y, width=3, capstyle="round")
        self.bar_fg = cv.create_line(BAR_X1, BAR_Y, BAR_X1, BAR_Y, width=3, capstyle="round")
        self.knob = cv.create_oval(0, 0, 0, 0, outline="")
        self.t_left = cv.create_text(12, BAR_Y, text="0:00", anchor="w", font=self.f_time)
        self.t_right = cv.create_text(W - 12, BAR_Y, text="-0:00", anchor="e", font=self.f_time)

        # Sağ üst: Spotify logosu (veya nota), çark, ekolayzır
        lx, ly = LOGO_POS
        self.logo = [cv.create_oval(lx - 12, ly - 12, lx + 12, ly + 12, fill=GREEN, outline="")]
        for p0, pm, p2, wd in SPOTIFY_BARS:
            self.logo.append(cv.create_line(*bar_points(p0, pm, p2, lx, ly), fill=LOGO_DARK,
                                            width=wd, capstyle="round", joinstyle="round"))
        self.logo_note = cv.create_text(lx, ly, text="♪", font=(FONT_FAMILY, 16), state="hidden")

        gx, gy = GEAR_POS
        self.gear = cv.create_polygon(gear_points(gx, gy, 0), outline="")
        self.gear_hole = cv.create_oval(gx - 2.8, gy - 2.8, gx + 2.8, gy + 2.8, outline="")

        self.eq = [cv.create_rectangle(0, 0, 0, 0, outline="") for _ in range(4)]

        # Fareyi takip eden vurgu dairesi (butonların altında)
        self.hl = cv.create_oval(0, 0, 0, 0, outline="")

        # Kontrol butonları (fare üstüne gelince belirir)
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

    def place_buttons(self, dx):
        c, by = self.cv.coords, BTN_Y
        p, t, n = BTN_X["prev"] + dx, BTN_X["toggle"] + dx, BTN_X["next"] + dx
        c(self.ic_prev_bar, p - 6, by - 5, p - 4, by + 5)
        c(self.ic_prev_tri, p + 5, by - 5, p + 5, by + 5, p - 4, by)
        c(self.play_icon, t - 4, by - 6, t - 4, by + 6, t + 6, by)
        c(self.pause_icons[0], t - 5, by - 6, t - 2, by + 6)
        c(self.pause_icons[1], t + 2, by - 6, t + 5, by + 6)
        c(self.ic_next_bar, n + 4, by - 5, n + 6, by + 5)
        c(self.ic_next_tri, n - 5, by - 5, n - 5, by + 5, n + 4, by)

    def apply_theme(self):
        """Tema renklerini sabit öğelere uygular (animasyonlu öğeler kendi karesinde alır)."""
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

        self.menu = tk.Menu(self.root, tearoff=0)
        self.menu.add_checkbutton(label="Her zaman üstte", variable=self.topmost_var,
                                  command=self.apply_topmost)
        self.menu.add_command(label="Ayarlar", command=self.open_settings)
        self.menu.add_separator()
        self.menu.add_command(label="Kapat", command=self.quit_app)
        cv.bind("<Button-3>", self.show_menu)

    # -- Etkileşim ---------------------------------------------------------
    def hit_test(self, x, y):
        gx, gy = GEAR_POS
        if (x - gx) ** 2 + (y - gy) ** 2 <= 13 ** 2:
            return "gear"
        if self.hover_t > 0.5:
            for name in ("prev", "toggle", "next"):
                if abs(x - (BTN_X[name] + self.btn_dx)) <= 15 and abs(y - BTN_Y) <= 14:
                    return name
        return None

    def on_motion(self, e):
        self.hover = True
        self.mx, self.my = e.x, e.y
        self.cv.config(cursor="hand2" if self.hit_test(e.x, e.y) else "")

    def on_leave(self, _e):
        self.hover = False
        self.cv.config(cursor="")

    def on_press(self, e):
        self.mx, self.my = e.x, e.y
        self.press_target = self.hit_test(e.x, e.y)
        self.dragged = False
        if self.press_target is None:
            self.drag_dx = e.x_root - self.root.winfo_x()
            self.drag_dy = e.y_root - self.root.winfo_y()

    def on_drag(self, e):
        self.mx, self.my = e.x, e.y
        if self.press_target is not None or not self.intro_done:
            return
        self.dragged = True
        self.pos_x = e.x_root - self.drag_dx
        self.pos_y = e.y_root - self.drag_dy
        self.root.geometry(f"+{self.pos_x}+{self.pos_y}")

    def on_release(self, e):
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
        self.add_ripple(BTN_X[action] + self.btn_dx, now)
        if action == "next":
            self.nudge = -NUDGE
            self.pending_dir, self.pending_until = 1, now + 3
        elif action == "prev":
            self.nudge = NUDGE
            self.pending_dir, self.pending_until = -1, now + 3
        else:
            # Anında geri bildirim: Windows yanıt verene kadar simgeyi hemen çevir.
            st = self.backend.state
            if st["available"]:
                pos = st["position_ms"]
                if st["playing"]:
                    pos += (now - st["updated_at"]) * 1000
                st["position_ms"], st["updated_at"] = pos, now
                st["playing"] = not st["playing"]
        self.backend.control(action)

    def add_ripple(self, x, now):
        item = self.cv.create_oval(x - 6, BTN_Y - 6, x + 6, BTN_Y + 6,
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

    # -- Tema / ayar kaydı ---------------------------------------------------
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
        write_config({"theme": self.theme, "x": self.pos_x, "y": self.pos_y,
                      "topmost": bool(self.topmost_var.get())})

    # -- Kare döngüsü (~30 FPS) -------------------------------------------
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
        self.root.geometry(f"+{self.pos_x}+{int(self.pos_y + (1 - e) * 30)}")
        if p >= 1:
            self.intro_done = True

    def sync_track(self, st, now):
        available = st["available"]
        key = (st["title"], st["artist"]) if available else None

        if key != self.shown_key:
            self.shown_key = key
            if available:
                title, artist = st["title"], st["artist"]
            else:
                title, artist = "Çalan şarkı yok", "Spotify'da bir şarkı başlat"
            self.cv.itemconfig(self.t_title, text=fit(self.f_title, title, TITLE_MAX_W))
            self.cv.itemconfig(self.t_artist, text=fit(self.f_artist, artist, ARTIST_MAX_W))
            self.anim_start = now
            self.texts_settled = False
            # Butona basıldıysa yeni şarkı o yönden kayarak gelir.
            self.slide_dir = self.pending_dir if now < self.pending_until else 1
            self.pending_until = 0.0
            self.disp_pos = st["position_ms"] if available else 0.0

            # Logo: Spotify ise logo, başka uygulama ise nota simgesi
            is_spotify = (not available) or ("spotify" in st["app"].lower())
            for item in self.logo:
                self.cv.itemconfig(item, state="normal" if is_spotify else "hidden")
            self.cv.itemconfig(self.logo_note, state="hidden" if is_spotify else "normal")

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
                for s in (0.78, 0.86, 0.93, 0.98, 1.0):
                    size = max(int(COVER * s), 8)
                    frame = img.resize((size, size), Image.Resampling.LANCZOS)
                    self.cover_frames.append(ImageTk.PhotoImage(frame))
            except Exception:
                self.cover_frames = []
        has = bool(self.cover_frames)
        self.cv.itemconfig(self.cover_item, state="normal" if has else "hidden")
        self.cv.itemconfig(self.cover_ph, state="hidden" if has else "normal")
        self.cv.itemconfig(self.cover_note, state="hidden" if has else "normal")

    def layout_texts(self, now):
        done = True
        for item, y, ckey, delay in self.text_items:
            color = self.theme[ckey]
            if self.texts_settled:
                off, fill = 0.0, color
            else:
                p = clamp((now - self.anim_start - delay) / 0.45)
                e = ease_out(p)
                off = self.slide_dir * (1 - e) * 28
                fill = mix(self.theme["bg"], color, e)
                if p < 1:
                    done = False
            self.cv.coords(item, TX + off + self.nudge, y)
            self.cv.itemconfig(item, fill=fill)
        if not self.texts_settled:
            self.texts_settled = done

    def layout_cover(self, now):
        off = self.nudge * 0.5
        cx, cy = COVER_X + COVER // 2 + off, COVER_Y + COVER // 2
        self.cv.coords(self.cover_ph, COVER_X + off, COVER_Y, COVER_X + COVER + off, COVER_Y + COVER)
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
        if st["available"]:
            dur = st["duration_ms"]
            target = st["position_ms"]
            if st["playing"]:
                target += (now - st["updated_at"]) * 1000
            if dur > 0:
                target = min(target, dur)
        else:
            dur, target = 0.0, 0.0

        diff = target - self.disp_pos
        if abs(diff) > 5000:
            self.disp_pos = target          # büyük sıçrama: direkt git
        else:
            self.disp_pos += diff * 0.25    # küçük fark: yumuşak kay

        ratio = clamp(self.disp_pos / dur) if dur > 0 else 0.0
        x = BAR_X1 + (BAR_X2 - BAR_X1) * ratio
        self.cv.coords(self.bar_fg, BAR_X1, BAR_Y, x, BAR_Y)
        self.cv.coords(self.knob, x - 4, BAR_Y - 4, x + 4, BAR_Y + 4)

        times = (fmt(self.disp_pos), "-" + fmt(dur - self.disp_pos))
        if times != self.last_times:
            self.last_times = times
            self.cv.itemconfig(self.t_left, text=times[0])
            self.cv.itemconfig(self.t_right, text=times[1])

    def update_eq(self, st, now):
        playing = st["available"] and st["playing"]
        for i, bar in enumerate(self.eq):
            if playing:
                target = 4 + 12 * (0.5 + 0.5 * math.sin(now * (5 + i * 1.7) + i * 1.3))
            else:
                target = 3
            self.eq_h[i] += (target - self.eq_h[i]) * 0.3
            x0 = W - 100 + i * 6
            self.cv.coords(bar, x0, 32 - self.eq_h[i], x0 + 4, 32)

    def update_buttons(self, st):
        t, cv = self.theme, self.cv
        playing = bool(st["available"] and st["playing"])

        # Fare pencerenin içindeyken butonlar belirir
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

        # Butonlar fareyi yatayda takip eder (±8 px)
        dx_target = ((self.mx / W) - 0.5) * 16 if self.hover else 0.0
        self.btn_dx += (dx_target - self.btn_dx) * 0.2
        if abs(self.btn_dx - self.placed_dx) > 0.05:
            self.placed_dx = self.btn_dx
            self.place_buttons(self.btn_dx)

        # Fareyi takip eden vurgu dairesi: en yakın butona kayar
        over = None
        if self.hover and self.hover_t > 0.3 and abs(self.my - BTN_Y) <= 20 and self.mx > W - 124:
            over = min(BTN_X, key=lambda n: abs(self.mx - (BTN_X[n] + self.btn_dx)))
        if over:
            tx = BTN_X[over] + self.btn_dx
            if self.hl_a < 0.05:
                self.hl_x = tx
            else:
                self.hl_x += (tx - self.hl_x) * 0.35
        self.hl_a += ((1.0 if over else 0.0) - self.hl_a) * 0.3
        pressed = self.press_target in BTN_X and over == self.press_target
        r = 11 if pressed else 13
        cv.coords(self.hl, self.hl_x - r, BTN_Y - r, self.hl_x + r, BTN_Y + r)
        cv.itemconfig(self.hl, fill=mix(t["bg"], t["title"], (0.26 if pressed else 0.14) * self.hl_a))

    def update_gear(self):
        t, cv = self.theme, self.cv
        over = self.hover and self.hit_test(self.mx, self.my) == "gear"
        self.gear_speed += ((220.0 if over else 0.0) - self.gear_speed) * 0.15
        self.gear_hot += ((1.0 if over else 0.0) - self.gear_hot) * 0.25
        if abs(self.gear_speed) > 0.3:
            self.gear_angle = (self.gear_angle + self.gear_speed * 0.033) % 360
            cv.coords(self.gear, *gear_points(GEAR_POS[0], GEAR_POS[1], self.gear_angle))
        cv.itemconfig(self.gear, fill=mix(t["text2"], t["title"], self.gear_hot))

    def update_ripples(self, now):
        alive = []
        for item, x, t0 in self.ripples:
            p = (now - t0) / 0.45
            if p >= 1:
                self.cv.delete(item)
                continue
            r = 6 + 16 * ease_out(p)
            self.cv.coords(item, x - r, BTN_Y - r, x + r, BTN_Y + r)
            self.cv.itemconfig(item, outline=mix(self.theme["accent"], self.theme["bg"], p))
            alive.append((item, x, t0))
        self.ripples = alive


if __name__ == "__main__":
    root = tk.Tk()
    MiniPlayer(root, MediaBackend())
    root.mainloop()
