from pathlib import Path


# Default directory to save downloads. Users can override via CLI.
DEFAULT_DOWNLOAD_DIR: Path = Path.home() / "Downloads"

# Default formats
DEFAULT_VIDEO_FORMAT: str = "mp4"  # mp4 or webm
DEFAULT_AUDIO_FORMAT: str = "mp3"  # mp3 requires ffmpeg; fallback to m4a/webm

# Download behavior
MAX_RETRIES: int = 3
CHUNK_SIZE_BYTES: int = 1024 * 1024  # for any streamed ops outside pytube

# UI/CLI behavior
PROGRESS_BAR_MIN_INTERVAL: float = 0.1
PROGRESS_BAR_LEAVE: bool = True

# GUI preferences
DEFAULT_THEME: str = "light"  # "light" or "dark"


