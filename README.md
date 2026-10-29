# Spotify Mini Player


A small, always-on-top music player window for Windows. It shows the current song's title, artist, album cover and elapsed time, and lets you control playback with previous, play/pause and next buttons.

No Spotify API, Premium account or Client ID is required. The app connects to Windows' system media session, the same system the keyboard media keys use.

## Features

- HUD-style rounded horizontal window: album cover on the left, title and artist next to it, a progress bar with elapsed / remaining time below
- Click and hold on the progress bar and drag to any minute and second to seek
- Synced lyrics under the progress bar: they scroll line by line, the current line is highlighted and animated (powered by [LRCLIB](https://lrclib.net))
- Auto accent color from the album cover: the progress bar, equalizer and ripple effect smoothly change to the cover's color for every song
- Volume: a slider appears at the top when you hover; drag it or use the mouse wheel
- Compact mode: double-click an empty spot to shrink the player to a small strip with just the cover, the title and a thin progress line
- Animations: slide-in on launch, cover and text slide in when the song changes, animated equalizer bars while playing, smoothly moving progress bar
- Previous, play/pause and next buttons fade in on hover and follow your mouse; clicks trigger a ripple effect, and the content slides left/right when you skip tracks
- Settings via the gear icon in the top right: preset themes, full color customization with HEX / RGB / a color picker, window size (60%-200%), opacity, font picker, lyrics and compact mode toggles
- Ctrl + mouse wheel resizes the window quickly
- Theme, size, opacity, font, modes, window position and "always on top" are remembered (`%APPDATA%\SpotifyMiniPlayer\config.json`)
- Works with free Spotify accounts

## Usage

1. Download `SpotifyMiniPlayer.exe` from the [Releases](../../releases) page.
2. Double-click it to run. Start playing a song in Spotify and it appears in the window.

> Windows SmartScreen may show a warning because the exe is unsigned. Click "More info" → "Run anyway".

## Running from source

Python 3.12 is recommended.

```
pip install -r requirements.txt
python mini_player.py
```

## Building your own exe

```
pip install pyinstaller
pyinstaller --onefile --noconsole --collect-submodules winsdk --name SpotifyMiniPlayer mini_player.py
```

The output is created at `dist/SpotifyMiniPlayer.exe`. When a tag in the form `v*` (e.g. `v1.0.0`) is pushed to this repository, GitHub Actions builds the exe automatically and attaches it to the Release.

## Limitations

- The volume slider controls Spotify's own volume (or the system volume if Spotify isn't playing) and uses the `pycaw` package. If it isn't installed or there is no audio device, the slider is hidden and the rest of the app keeps working.
- Lyrics come from LRCLIB, an open lyrics service, not from Spotify. Not every song has synced lyrics, and the timing may differ from Spotify's own lyrics. An internet connection is needed to fetch them.
- Seeking depends on Spotify supporting position changes through Windows media controls. If it doesn't, the bar snaps back.
- Works only on Windows 10/11.
- The app does not control Spotify remotely. To pick a song or open a playlist, use Spotify's own window.
- It prefers the Spotify session if there is one. If Spotify is not running, it may show whatever media session Windows currently considers active (for example, music playing in a browser).

## License

MIT
