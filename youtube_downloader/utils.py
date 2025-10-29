import re
import shutil
import subprocess
import json
from datetime import datetime
from pathlib import Path
from typing import Optional, Tuple, Dict, Any
import urllib.parse as _urlparse


def sanitize_filename(name: str) -> str:
    """Return a filesystem-safe filename derived from the given name.

    Removes/normalizes characters that are not allowed across OSes.
    """
    name = name.strip()
    # Replace path separators and illegal characters
    name = re.sub(r"[\\/\n\r\t]+", " ", name)
    name = re.sub(r"[<>:\"|?*]", "", name)
    # Collapse whitespace
    name = re.sub(r"\s+", " ", name)
    # Trim length conservatively
    return name[:180].rstrip(" .") or "download"


def ensure_directory(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def is_ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


def convert_to_mp3(input_path: Path, output_path: Optional[Path] = None) -> Path:
    """Convert an audio file to MP3 using ffmpeg. Returns output path.

    Requires ffmpeg to be installed and available on PATH.
    """
    if not is_ffmpeg_available():
        raise RuntimeError("ffmpeg is not available on PATH")

    if output_path is None:
        output_path = input_path.with_suffix(".mp3")

    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        str(input_path),
        "-vn",
        "-codec:a",
        "libmp3lame",
        "-qscale:a",
        "2",
        str(output_path),
    ]
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return output_path


def parse_timecode(tc: Optional[str]) -> Optional[str]:
    """Validate and normalize timecode to HH:MM:SS format for ffmpeg.

    Accepts formats like SS, MM:SS, or HH:MM:SS. Returns normalized string or None.
    """
    if not tc:
        return None
    parts = [int(p) for p in tc.split(":")]
    if len(parts) == 1:
        h, m, s = 0, 0, parts[0]
    elif len(parts) == 2:
        h, m, s = 0, parts[0], parts[1]
    elif len(parts) == 3:
        h, m, s = parts
    else:
        raise ValueError("Invalid timecode format")
    if m > 59 or s > 59 or h < 0 or m < 0 or s < 0:
        raise ValueError("Invalid timecode values")
    return f"{h:02d}:{m:02d}:{s:02d}"


def ffmpeg_trim(input_path: Path, start: Optional[str], end: Optional[str], output_path: Optional[Path] = None) -> Path:
    """Trim a media file between start and end timecodes using ffmpeg.
    If end is provided, use -to; otherwise copy from start to end of file.
    Tries to stream copy when possible; falls back to re-encode if needed.
    """
    if not is_ffmpeg_available():
        raise RuntimeError("ffmpeg is not available on PATH")
    norm_start = parse_timecode(start)
    norm_end = parse_timecode(end)
    if output_path is None:
        suffix = input_path.suffix
        output_path = input_path.with_name(input_path.stem + ".trimmed" + suffix)
    cmd = ["ffmpeg", "-y"]
    if norm_start:
        cmd += ["-ss", norm_start]
    cmd += ["-i", str(input_path)]
    if norm_end:
        cmd += ["-to", norm_end]
    # Attempt stream copy
    cmd += ["-c", "copy", str(output_path)]
    result = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if result.returncode != 0:
        # Fallback to re-encode video/audio
        cmd = ["ffmpeg", "-y"]
        if norm_start:
            cmd += ["-ss", norm_start]
        cmd += ["-i", str(input_path)]
        if norm_end:
            cmd += ["-to", norm_end]
        cmd += ["-c:v", "libx264", "-c:a", "aac", str(output_path)]
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return output_path


def ffmpeg_convert(input_path: Path, target_ext: str, output_path: Optional[Path] = None) -> Path:
    """Convert media file to another container/codec using ffmpeg.
    target_ext should be like 'mp4', 'webm', 'avi', 'wav'.
    """
    if not is_ffmpeg_available():
        raise RuntimeError("ffmpeg is not available on PATH")
    if output_path is None:
        output_path = input_path.with_suffix("." + target_ext.lstrip("."))
    cmd = ["ffmpeg", "-y", "-i", str(input_path), str(output_path)]
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return output_path


def ensure_unique_path(path: Path) -> Path:
    """Return a non-colliding path by appending (n) if needed."""
    if not path.exists():
        return path
    parent = path.parent
    stem = path.stem
    suffix = path.suffix
    idx = 1
    while True:
        candidate = parent / f"{stem} ({idx}){suffix}"
        if not candidate.exists():
            return candidate
        idx += 1


def notify(title: str, message: str) -> None:
    """Send a desktop notification if possible."""
    try:
        from plyer import notification

        notification.notify(title=title, message=message, app_name="YouTube Downloader")
    except Exception:
        # Silently ignore if notifications are unavailable
        pass


def write_log_entry(log_file: Path, data: Dict[str, Any]) -> None:
    log_file.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps({"timestamp": datetime.utcnow().isoformat() + "Z", **data}, ensure_ascii=False)
    with log_file.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def suggest_best_format(network_speed_mbps: float) -> str:
    """Very simple heuristic: prefer mp4 for typical conditions, webm for lower bitrate.
    Returns 'mp4' or 'webm'.
    """
    if network_speed_mbps < 5.0:
        return "webm"
    return "mp4"


# Preferences persistence
def _settings_path() -> Path:
    base = Path.home() / ".config" / "youtube_downloader"
    base.mkdir(parents=True, exist_ok=True)
    return base / "settings.json"


def load_settings() -> Dict[str, Any]:
    path = _settings_path()
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_settings(data: Dict[str, Any]) -> None:
    path = _settings_path()
    try:
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


def normalize_youtube_url(url: str) -> str:
    """Normalize common YouTube URLs to canonical watch/playlist forms.
    - Expands youtu.be short links
    - Ensures https scheme
    - Preserves playlist id if present
    """
    url = (url or "").strip()
    if not url:
        return url
    parsed = _urlparse.urlparse(url if "://" in url else f"https://{url}")
    netloc = parsed.netloc.lower()
    qs = _urlparse.parse_qs(parsed.query)
    # youtu.be/<id>
    if "youtu.be" in netloc and parsed.path.strip("/"):
        vid = parsed.path.strip("/")
        q = {}
        q.update({"v": [vid]})
        if "list" in qs:
            q["list"] = qs["list"]
        new = parsed._replace(scheme="https", netloc="www.youtube.com", path="/watch", query=_urlparse.urlencode({k: v[0] for k, v in q.items()}))
        return _urlparse.urlunparse(new)
    # youtube shorts -> watch
    if "youtube.com" in netloc and parsed.path.startswith("/shorts/"):
        vid = parsed.path.split("/shorts/")[-1]
        new = parsed._replace(path="/watch", query=_urlparse.urlencode({"v": vid}))
        return _urlparse.urlunparse(new)
    # force https and www
    host = "www.youtube.com" if "youtube.com" in netloc else netloc
    new = parsed._replace(scheme="https", netloc=host)
    return _urlparse.urlunparse(new)


