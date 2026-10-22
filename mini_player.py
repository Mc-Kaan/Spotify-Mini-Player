"""
Spotify Mini Player (Windows) - HUD tarzı, animasyonlu sürüm

Windows'un sistem medya oturumuna (SMTC) bağlanır; Spotify API'si, Premium
hesap veya Client ID gerekmez.

Kullanım:
  - Pencereyi sol tıkla basılı tutarak sürükle.
  - Fareyi pencerenin üstüne getirince önceki / oynat-durdur / sonraki
    butonları belirir.
  - Sağ tık: "Her zaman üstte" seçeneği ve "Kapat".
"""

import asyncio
import io
import math
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
# Görünüm ayarları (istediğin gibi değiştirebilirsin)
# ----------------------------------------------------------------------------
FONT_FAMILY = "Consolas"  # Piksel yazı tipi kuruluysa adını buraya yazabilirsin

W, H = 430, 110
KEY = "#010203"  # pencere köşelerini saydam yapmak için kullanılan anahtar renk
BG = "#2a3541"
BORDER = "#3b4a5a"
C_LABEL = "#8f9bab"
C_TITLE = "#ffffff"
C_ARTIST = "#aab5c2"
C_TIME = "#c9d1da"
C_BAR_BG = "#4b5a6b"
C_BAR_FG = "#ffffff"
C_ICON = "#ffffff"
GREEN = "#1DB954"
C_LOGO_DARK = "#0e1a13"

COVER = 68
COVER_X, COVER_Y = 12, 12
TX = 96  # metinlerin sol kenarı
BAR_X1, BAR_X2, BAR_Y = 56, W - 56, 94
TEXT_MAX_W = W - TX - 104  # sağdaki butonlara yer bırak


# ----------------------------------------------------------------------------
# Yardımcı fonksiyonlar
# ----------------------------------------------------------------------------
def fmt(ms):
    s = int(max(ms, 0) // 1000)
    return f"{s // 60}:{s % 60:02d}"


def hex_rgb(c):
    return tuple(int(c[i:i + 2], 16) for i in (1, 3, 5))


def mix(c1, c2, t):
    a, b = hex_rgb(c1), hex_rgb(c2)
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


# ----------------------------------------------------------------------------
# Windows medya oturumu (arka plan)
# ----------------------------------------------------------------------------
class MediaBackend:
    """Windows medya oturumunu ayrı bir thread'de izler ve kontrol eder."""

    def __init__(self):
        self.loop = asyncio.new_event_loop()
        self.session = None
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
            await asyncio.sleep(1)

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

        asyncio.run_coroutine_threadsafe(run(), self.loop)


# ----------------------------------------------------------------------------
# Arayüz
# ----------------------------------------------------------------------------
class MiniPlayer:
    def __init__(self, root, backend):
        self.root = root
        self.backend = backend

        # Çerçevesiz, köşeleri yuvarlak, her zaman üstte
        root.overrideredirect(True)
        root.attributes("-topmost", True)
        root.configure(bg=KEY)
        try:
            root.attributes("-transparentcolor", KEY)
        except tk.TclError:
            pass

        self.pos_x = root.winfo_screenwidth() - W - 24
        self.pos_y = 24
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
        self.cover_src = object()
        self.cover_frames = []
        self.cover_idx = -1
        self.cover_anim = 0.0
        self.disp_pos = 0.0
        self.eq_h = [3.0] * 4
        self.hover = False
        self.hover_t = 0.0
        self.last_playing = None
        self.last_times = ("", "")
        self.texts_settled = False
        self.intro_done = False
        self.intro_start = time.time()
        self.drag_dx = self.drag_dy = 0

        self.build()
        self.bind_events()
        self.frame()

    # -- Çizim -------------------------------------------------------------
    def build(self):
        cv = self.cv
        rounded_rect(cv, 1, 1, W - 1, H - 1, 16, fill=BG, outline=BORDER, width=1)

        # Kapak (yer tutucu + gerçek kapak)
        cx, cy = COVER_X + COVER // 2, COVER_Y + COVER // 2
        self.cover_ph = cv.create_rectangle(COVER_X, COVER_Y, COVER_X + COVER, COVER_Y + COVER,
                                            fill="#1f2a35", outline="")
        self.cover_note = cv.create_text(cx, cy, text="♪", fill=C_LABEL, font=(FONT_FAMILY, 22))
        self.cover_item = cv.create_image(cx, cy, state="hidden")

        # Metinler
        self.t_now = cv.create_text(TX, 22, text="now playing", anchor="w",
                                    font=self.f_label, fill=C_LABEL)
        self.t_title = cv.create_text(TX, 44, text="", anchor="w",
                                      font=self.f_title, fill=C_TITLE)
        self.t_artist = cv.create_text(TX, 66, text="", anchor="w",
                                       font=self.f_artist, fill=C_ARTIST)
        # (öğe, y, son renk, gecikme sn)
        self.text_items = [(self.t_title, 44, C_TITLE, 0.0), (self.t_artist, 66, C_ARTIST, 0.08)]

        # İlerleme çubuğu ve süreler
        cv.create_line(BAR_X1, BAR_Y, BAR_X2, BAR_Y, fill=C_BAR_BG, width=3, capstyle="round")
        self.bar_fg = cv.create_line(BAR_X1, BAR_Y, BAR_X1, BAR_Y, fill=C_BAR_FG,
                                     width=3, capstyle="round")
        self.knob = cv.create_oval(0, 0, 0, 0, fill=C_BAR_FG, outline="")
        self.t_left = cv.create_text(12, BAR_Y, text="0:00", anchor="w",
                                     font=self.f_time, fill=C_TIME)
        self.t_right = cv.create_text(W - 12, BAR_Y, text="-0:00", anchor="e",
                                      font=self.f_time, fill=C_TIME)

        # Sağ üst: Spotify logosu (veya nota) ve ekolayzır çubukları
        lx, ly = W - 28, 24
        self.logo = [cv.create_oval(lx - 11, ly - 11, lx + 11, ly + 11, fill=GREEN, outline="")]
        for r, wd in ((9, 2.6), (7, 2.2), (5, 1.9)):
            self.logo.append(cv.create_arc(lx - r, ly + 5 - r, lx + r, ly + 5 + r,
                                           start=48, extent=84, style="arc",
                                           outline=C_LOGO_DARK, width=wd))
        self.logo_note = cv.create_text(lx, ly, text="♪", fill=C_LABEL,
                                        font=(FONT_FAMILY, 16), state="hidden")
        self.eq = [cv.create_rectangle(0, 0, 0, 0, fill=GREEN, outline="") for _ in range(4)]

        # Kontrol butonları (fare üstüne gelince belirir)
        self.icon_items = []
        by = 68
        # önceki
        cx_prev, cx_play, cx_next = W - 88, W - 58, W - 28
        cv.create_rectangle(cx_prev - 14, by - 12, cx_prev + 14, by + 12,
                            fill=BG, outline="", tags=("prev",))
        self.icon_items += [
            cv.create_rectangle(cx_prev - 6, by - 5, cx_prev - 4, by + 5, outline="", fill=BG, tags=("prev",)),
            cv.create_polygon(cx_prev + 5, by - 5, cx_prev + 5, by + 5, cx_prev - 4, by,
                              outline="", fill=BG, tags=("prev",)),
        ]
        # oynat / durdur
        cv.create_rectangle(cx_play - 14, by - 12, cx_play + 14, by + 12,
                            fill=BG, outline="", tags=("toggle",))
        self.play_icon = cv.create_polygon(cx_play - 4, by - 6, cx_play - 4, by + 6, cx_play + 6, by,
                                           outline="", fill=BG, tags=("toggle",))
        self.pause_icons = [
            cv.create_rectangle(cx_play - 5, by - 6, cx_play - 2, by + 6, outline="", fill=BG, tags=("toggle",)),
            cv.create_rectangle(cx_play + 2, by - 6, cx_play + 5, by + 6, outline="", fill=BG, tags=("toggle",)),
        ]
        self.icon_items += [self.play_icon] + self.pause_icons
        # sonraki
        cv.create_rectangle(cx_next - 14, by - 12, cx_next + 14, by + 12,
                            fill=BG, outline="", tags=("next",))
        self.icon_items += [
            cv.create_rectangle(cx_next + 4, by - 5, cx_next + 6, by + 5, outline="", fill=BG, tags=("next",)),
            cv.create_polygon(cx_next - 5, by - 5, cx_next - 5, by + 5, cx_next + 4, by,
                              outline="", fill=BG, tags=("next",)),
        ]

    def bind_events(self):
        cv = self.cv
        for tag in ("prev", "toggle", "next"):
            cv.tag_bind(tag, "<Button-1>", lambda e, a=tag: self.click(a))
        cv.bind("<Enter>", lambda e: setattr(self, "hover", True))
        cv.bind("<Leave>", lambda e: setattr(self, "hover", False))
        cv.bind("<ButtonPress-1>", self.drag_start)
        cv.bind("<B1-Motion>", self.drag_move)

        self.topmost_var = tk.BooleanVar(value=True)
        self.menu = tk.Menu(self.root, tearoff=0)
        self.menu.add_checkbutton(label="Her zaman üstte", variable=self.topmost_var,
                                  command=lambda: self.root.attributes("-topmost", self.topmost_var.get()))
        self.menu.add_separator()
        self.menu.add_command(label="Kapat", command=self.root.destroy)
        cv.bind("<Button-3>", self.show_menu)

    # -- Etkileşim ---------------------------------------------------------
    def click(self, action):
        if self.hover_t > 0.5:
            self.backend.control(action)

    def drag_start(self, e):
        self.drag_dx = e.x_root - self.root.winfo_x()
        self.drag_dy = e.y_root - self.root.winfo_y()

    def drag_move(self, e):
        self.pos_x = e.x_root - self.drag_dx
        self.pos_y = e.y_root - self.drag_dy
        self.root.geometry(f"+{self.pos_x}+{self.pos_y}")

    def show_menu(self, e):
        try:
            self.menu.tk_popup(e.x_root, e.y_root)
        finally:
            self.menu.grab_release()

    # -- Kare döngüsü (~30 FPS) -------------------------------------------
    def frame(self):
        try:
            now = time.time()
            st = self.backend.state
            self.intro(now)
            self.sync_track(st, now)
            self.anim_texts(now)
            self.anim_cover(now)
            self.update_progress(st, now)
            self.update_eq(st, now)
            self.update_hover(st)
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
            self.cv.itemconfig(self.t_title, text=fit(self.f_title, title, TEXT_MAX_W))
            self.cv.itemconfig(self.t_artist, text=fit(self.f_artist, artist, TEXT_MAX_W))
            self.anim_start = now
            self.texts_settled = False
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

    def anim_texts(self, now):
        if self.texts_settled:
            return
        done = True
        for item, y, color, delay in self.text_items:
            p = clamp((now - self.anim_start - delay) / 0.45)
            e = ease_out(p)
            self.cv.coords(item, TX + (1 - e) * 28, y)
            self.cv.itemconfig(item, fill=mix(BG, color, e))
            if p < 1:
                done = False
        self.texts_settled = done

    def anim_cover(self, now):
        n = len(self.cover_frames)
        if not n:
            return
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
            x0 = W - 78 + i * 6
            self.cv.coords(bar, x0, 32 - self.eq_h[i], x0 + 4, 32)

    def update_hover(self, st):
        playing = bool(st["available"] and st["playing"])
        changed = False

        target = 1.0 if self.hover else 0.0
        if abs(target - self.hover_t) > 0.001:
            step = 0.2
            if target > self.hover_t:
                self.hover_t = min(target, self.hover_t + step)
            else:
                self.hover_t = max(target, self.hover_t - step)
            changed = True

        if playing != self.last_playing:
            self.last_playing = playing
            self.cv.itemconfig(self.play_icon, state="hidden" if playing else "normal")
            for item in self.pause_icons:
                self.cv.itemconfig(item, state="normal" if playing else "hidden")

        if changed:
            color = mix(BG, C_ICON, self.hover_t)
            for item in self.icon_items:
                self.cv.itemconfig(item, fill=color)


if __name__ == "__main__":
    root = tk.Tk()
    MiniPlayer(root, MediaBackend())
    root.mainloop()
