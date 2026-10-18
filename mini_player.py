"""
Spotify Mini Player (Windows)

Windows'un sistem medya oturumuna (SMTC) bağlanır; Spotify API'si, Premium
hesap veya Client ID gerekmez. Çalan şarkının adını, sanatçısını, kapağını ve
süresini gösterir; önceki / oynat-durdur / sonraki kontrollerini sağlar.
"""

import asyncio
import io
import threading
import time
import tkinter as tk
from datetime import datetime, timezone

from PIL import Image, ImageTk
from winsdk.windows.media.control import (
    GlobalSystemMediaTransportControlsSessionManager as MediaManager,
    GlobalSystemMediaTransportControlsSessionPlaybackStatus as PlaybackStatus,
)
from winsdk.windows.storage.streams import Buffer, DataReader, InputStreamOptions

BG = "#121212"
FG = "#FFFFFF"
FG_DIM = "#B3B3B3"
ACCENT = "#1DB954"
COVER_SIZE = 180


def fmt(ms):
    s = int(max(ms, 0) // 1000)
    return f"{s // 60}:{s % 60:02d}"


class MediaBackend:
    """Windows medya oturumunu ayrı bir thread'de izler ve kontrol eder."""

    def __init__(self):
        self.loop = asyncio.new_event_loop()
        self.session = None
        self.state = {
            "available": False,
            "title": "",
            "artist": "",
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


class MiniPlayer:
    def __init__(self, root, backend):
        self.root = root
        self.backend = backend
        self.cover_key = None
        self.cover_img = None

        root.title("Mini Player")
        root.geometry("260x380")
        root.resizable(False, False)
        root.configure(bg=BG)
        root.attributes("-topmost", True)

        # Üst: şarkı adı ve sanatçı
        self.title_lbl = tk.Label(root, text="Çalan şarkı yok", bg=BG, fg=FG,
                                  font=("Segoe UI", 12, "bold"), wraplength=240)
        self.title_lbl.pack(pady=(12, 0))
        self.artist_lbl = tk.Label(root, text="", bg=BG, fg=FG_DIM, font=("Segoe UI", 9))
        self.artist_lbl.pack()

        # Alt: kapak
        self.cover_lbl = tk.Label(root, bg=BG)
        self.cover_lbl.pack(pady=10)

        # İlerleme çubuğu ve süre
        self.bar = tk.Canvas(root, width=220, height=4, bg="#404040", highlightthickness=0)
        self.bar.pack()
        self.bar_fill = self.bar.create_rectangle(0, 0, 0, 4, fill=ACCENT, width=0)
        self.time_lbl = tk.Label(root, text="0:00 / 0:00", bg=BG, fg=FG_DIM, font=("Segoe UI", 9))
        self.time_lbl.pack(pady=(4, 0))

        # Kontroller
        btns = tk.Frame(root, bg=BG)
        btns.pack(pady=10)
        for col, (text, action) in enumerate([("⏮", "prev"), ("⏯", "toggle"), ("⏭", "next")]):
            tk.Button(btns, text=text, width=4, bg="#282828", fg=FG, relief="flat",
                      activebackground="#3E3E3E", activeforeground=FG,
                      command=lambda a=action: backend.control(a)).grid(row=0, column=col, padx=4)

        self.update_ui()

    def update_ui(self):
        st = self.backend.state

        if not st["available"]:
            self.title_lbl.config(text="Çalan şarkı yok")
            self.artist_lbl.config(text="")
        else:
            self.title_lbl.config(text=st["title"])
            self.artist_lbl.config(text=st["artist"])

            if st["track_key"] != self.cover_key:
                self.cover_key = st["track_key"]
                self.set_cover(st["cover"])

            position = st["position_ms"]
            if st["playing"]:
                position += (time.time() - st["updated_at"]) * 1000
            duration = st["duration_ms"]
            if duration > 0:
                position = min(position, duration)

            self.time_lbl.config(text=f"{fmt(position)} / {fmt(duration)}")
            ratio = position / duration if duration > 0 else 0
            self.bar.coords(self.bar_fill, 0, 0, 220 * ratio, 4)

        self.root.after(250, self.update_ui)

    def set_cover(self, data):
        if not data:
            self.cover_lbl.config(image="")
            self.cover_img = None
            return
        try:
            img = Image.open(io.BytesIO(data)).convert("RGB").resize((COVER_SIZE, COVER_SIZE))
            self.cover_img = ImageTk.PhotoImage(img)
            self.cover_lbl.config(image=self.cover_img)
        except Exception:
            self.cover_lbl.config(image="")
            self.cover_img = None


if __name__ == "__main__":
    root = tk.Tk()
    MiniPlayer(root, MediaBackend())
    root.mainloop()
