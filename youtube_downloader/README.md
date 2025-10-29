# YouTube Downloader (CLI + Optional GUI)

A Python app to download YouTube videos or audio with progress bars, playlist/batch support, and a minimal Tkinter GUI.

## Features
- Video download (highest or specific resolution)
- Audio-only download (MP3 if ffmpeg is available)
- Format selection: mp4/webm/mp3
- Progress bars (tqdm)
- Playlist and batch support
- Basic metadata display
- Optional Tkinter GUI

## Requirements
- Python 3.9+
- Packages: `pytube`, `tqdm`
- Optional: `ffmpeg` for MP3 conversion (must be on PATH)

## Install
```bash
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -r youtube_downloader/requirements.txt
```

## CLI Examples
```bash
python -m youtube_downloader.main "https://www.youtube.com/watch?v=VIDEO_ID" --quality 1080p --format mp4
python -m youtube_downloader.main "https://www.youtube.com/watch?v=VIDEO_ID" --audio --format mp3
python -m youtube_downloader.main --playlist "https://www.youtube.com/playlist?list=PL..." --quality 720p
python -m youtube_downloader.main --batch urls.txt --audio --format mp3
python -m youtube_downloader.main URL --metadata
```

## GUI
```bash
python -m youtube_downloader.main --gui
```

## Notes
- Without ffmpeg, audio saves as source format (e.g., m4a/webm). MP3 is skipped.
- Requested resolutions may be unavailable; the app falls back when possible.

## License
MIT
