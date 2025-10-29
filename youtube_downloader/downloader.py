from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional

from pytube import Playlist, YouTube
try:
    from pytube import request as _pt_request
except Exception:
    _pt_request = None
try:
    import yt_dlp as ytdlp
except Exception:
    ytdlp = None
from pytube.exceptions import PytubeError
from tqdm import tqdm

from . import config
from .utils import (
    ensure_directory,
    sanitize_filename,
    convert_to_mp3,
    is_ffmpeg_available,
    ffmpeg_trim,
    ffmpeg_convert,
    ensure_unique_path,
    normalize_youtube_url,
)


ProgressCallback = Callable[[int, int], None]


@dataclass
class VideoMetadata:
    title: str
    author: str
    length_seconds: int
    views: Optional[int]
    thumbnail_url: Optional[str]


def _make_tqdm_progress(total_bytes: int, description: str = "Downloading") -> ProgressCallback:
    bar = tqdm(
        total=total_bytes if total_bytes else None,
        unit="B",
        unit_scale=True,
        desc=description,
        mininterval=config.PROGRESS_BAR_MIN_INTERVAL,
        leave=config.PROGRESS_BAR_LEAVE,
    )

    last_downloaded = 0

    def update(downloaded: int, total: int) -> None:
        nonlocal last_downloaded
        delta = downloaded - last_downloaded
        last_downloaded = downloaded
        try:
            bar.update(delta)
        except Exception:
            pass
        if total and bar.total != total:
            bar.total = total

    return update


def _bind_pytube_progress(progress_cb: Optional[ProgressCallback], filesize: Optional[int]):
    """Return kwargs for YouTube with a wrapped on_progress_callback.

    Pytube passes (stream, chunk, bytes_remaining). We'll translate to bytes_downloaded.
    """
    if progress_cb is None:
        return {}

    state = {"filesize": filesize or 0, "downloaded": 0}

    def on_progress(stream, _chunk, bytes_remaining):
        total = state["filesize"] or getattr(stream, "filesize", None) or getattr(stream, "filesize_approx", 0)
        downloaded = (total - bytes_remaining) if total else 0
        state["downloaded"] = downloaded
        progress_cb(downloaded, total or 0)

    return {"on_progress_callback": on_progress}


def get_video_metadata(url: str) -> VideoMetadata:
    url = normalize_youtube_url(url)
    try:
        _init_pytube_client()
        yt = YouTube(url)
        return VideoMetadata(
            title=yt.title or "Untitled",
            author=yt.author or "",
            length_seconds=int(yt.length or 0),
            views=getattr(yt, "views", None),
            thumbnail_url=getattr(yt, "thumbnail_url", None),
        )
    except Exception:
        # Fallback to yt-dlp
        if not ytdlp:
            raise
        with ytdlp.YoutubeDL({"quiet": True, "noplaylist": True}) as ydl:
            info = ydl.extract_info(url, download=False)
            return VideoMetadata(
                title=info.get("title") or "Untitled",
                author=(info.get("uploader") or info.get("channel") or ""),
                length_seconds=int(info.get("duration") or 0),
                views=info.get("view_count"),
                thumbnail_url=(info.get("thumbnail") or None),
            )


def _select_video_stream(yt: YouTube, resolution: Optional[str], file_extension: str):
    if resolution in (None, "highest"):
        # Prefer progressive mp4; fallback to highest resolution adaptive video
        stream = yt.streams.filter(progressive=True, file_extension=file_extension).order_by("resolution").desc().first()
        if stream is None:
            stream = yt.streams.filter(only_video=True, file_extension=file_extension).order_by("resolution").desc().first()
        if stream is None:
            # last resort: any progressive
            stream = yt.streams.filter(progressive=True).order_by("resolution").desc().first()
        return stream

    # Specific resolution requested (e.g., "1080p")
    stream = yt.streams.filter(progressive=True, file_extension=file_extension, res=resolution).first()
    if stream is None:
        stream = yt.streams.filter(only_video=True, file_extension=file_extension, res=resolution).first()
    return stream


def _download_subtitles(yt: YouTube, langs: Optional[List[str]], base_path: Path, embed_into: Optional[Path] = None) -> List[Path]:
    """Download subtitles in given language codes to .srt files; optionally burn into video."""
    results: List[Path] = []
    if not langs:
        return results
    try:
        captions = getattr(yt, "captions", None) or getattr(yt, "caption_tracks", None)
        if not captions:
            return results
        for lang in langs:
            track = None
            try:
                if hasattr(captions, "get_by_language_code"):
                    track = captions.get_by_language_code(lang)
                else:
                    for c in captions:
                        code = getattr(c, "code", None) or getattr(c, "language_code", None)
                        if code == lang:
                            track = c
                            break
            except Exception:
                track = None
            if not track:
                continue
            srt_text = None
            try:
                if hasattr(track, "generate_srt_captions"):
                    srt_text = track.generate_srt_captions()
                elif hasattr(track, "xml_captions"):
                    srt_text = getattr(track, "to_srt", lambda: None)() or ""
            except Exception:
                srt_text = None
            if not srt_text:
                continue
            srt_path = base_path.with_name(base_path.stem + f".{lang}.srt")
            srt_path.write_text(srt_text, encoding="utf-8")
            results.append(srt_path)
        if embed_into and results and is_ffmpeg_available():
            import subprocess
            sub_file = results[0]
            burned = embed_into.with_name(embed_into.stem + ".subs" + embed_into.suffix)
            subprocess.run([
                "ffmpeg","-y","-i",str(embed_into),"-vf",f"subtitles='{sub_file}'",str(burned)
            ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            embed_into.unlink(missing_ok=True)
            burned.rename(embed_into)
    except Exception:
        pass
    return results


def download_video(
    url: str,
    output_dir: Path,
    resolution: Optional[str] = None,  # None or "highest" or e.g., "1080p"
    file_format: str = config.DEFAULT_VIDEO_FORMAT,
    subtitles_langs: Optional[List[str]] = None,
    embed_subtitles: bool = False,
    trim_start: Optional[str] = None,
    trim_end: Optional[str] = None,
    convert_to: Optional[str] = None,
    safe_mode: bool = False,
    rename_template: Optional[str] = None,
    force_backend_ytdlp: bool = False,
) -> Path:
    ensure_directory(output_dir)
    url = normalize_youtube_url(url)
    _init_pytube_client()
    try:
        yt = None if force_backend_ytdlp else YouTube(url)
    except Exception:
        yt = None
    if safe_mode and getattr(yt, "age_restricted", False):
        raise RuntimeError("Video is age-restricted; skipped in safe mode")
    try:
        stream = _select_video_stream(yt, resolution or "highest", file_format) if yt else None
    except Exception:
        stream = None

    # Build filename
    safe_title = sanitize_filename((yt.title if yt else None) or "video")
    if rename_template:
        publish_date = getattr(yt, "publish_date", None)
        date_str = publish_date.strftime("%Y-%m-%d") if publish_date else ""
        composed = rename_template.format(title=yt.title or "video", author=yt.author or "", date=date_str)
        safe_title = sanitize_filename(composed) or safe_title
    chosen_ext = (getattr(stream, "subtype", None) or file_format)
    filename = f"{safe_title}.{chosen_ext}"
    target = ensure_unique_path(output_dir / filename)

    if stream is not None:
        try:
            progress = _make_tqdm_progress(getattr(stream, "filesize", None) or getattr(stream, "filesize_approx", 0), description=safe_title)
            kwargs = _bind_pytube_progress(progress, getattr(stream, "filesize", None))
            stream.download(output_path=str(target.parent), filename=target.name, **kwargs)
        except Exception:
            stream = None
        finally:
            try:
                progress(getattr(stream, "filesize", 0) or 0, getattr(stream, "filesize", 0) or 0)
            except Exception:
                pass
    if stream is None or force_backend_ytdlp:
        # Fallback to yt-dlp
        if not ytdlp:
            raise RuntimeError("pytube failed to initialize; yt-dlp not installed")
        if is_ffmpeg_available():
            fmt = f"bestvideo[ext={file_format}]+bestaudio/best"
            ydl_opts = {
                "quiet": True,
                "merge_output_format": file_format,
                "outtmpl": str(target),
                "format": fmt,
                "noplaylist": True,
            }
        else:
            fmt = f"best[ext={file_format}]/best"
            ydl_opts = {
                "quiet": True,
                "outtmpl": str(target),
                "format": fmt,
                "noplaylist": True,
            }
        with ytdlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])
    if subtitles_langs:
        _download_subtitles(yt, subtitles_langs, base_path=target, embed_into=(target if embed_subtitles else None))
    if trim_start or trim_end:
        target = ffmpeg_trim(target, trim_start, trim_end)
    if convert_to:
        target = ffmpeg_convert(target, convert_to)
    return target


def download_audio(
    url: str,
    output_dir: Path,
    audio_format: str = config.DEFAULT_AUDIO_FORMAT,  # "mp3" requires ffmpeg; fallback to source subtype
    trim_start: Optional[str] = None,
    trim_end: Optional[str] = None,
    convert_to: Optional[str] = None,
    safe_mode: bool = False,
    rename_template: Optional[str] = None,
    force_backend_ytdlp: bool = False,
) -> Path:
    ensure_directory(output_dir)
    url = normalize_youtube_url(url)
    _init_pytube_client()
    try:
        yt = None if force_backend_ytdlp else YouTube(url)
    except Exception:
        yt = None
    if safe_mode and getattr(yt, "age_restricted", False):
        raise RuntimeError("Video is age-restricted; skipped in safe mode")
    try:
        stream = yt.streams.filter(only_audio=True).order_by("abr").desc().first() if yt else None
    except Exception:
        stream = None

    safe_title = sanitize_filename(yt.title or "audio")
    if rename_template:
        publish_date = getattr(yt, "publish_date", None)
        date_str = publish_date.strftime("%Y-%m-%d") if publish_date else ""
        composed = rename_template.format(title=yt.title or "audio", author=yt.author or "", date=date_str)
        safe_title = sanitize_filename(composed) or safe_title
    source_ext = (getattr(stream, "subtype", None) or "m4a")
    temp_filename = f"{safe_title}.{source_ext}"

    temp_path = ensure_unique_path(output_dir / temp_filename)
    if stream is not None:
        try:
            progress = _make_tqdm_progress(getattr(stream, "filesize", None) or getattr(stream, "filesize_approx", 0), description=safe_title)
            kwargs = _bind_pytube_progress(progress, getattr(stream, "filesize", None))
            stream.download(output_path=str(temp_path.parent), filename=temp_path.name, **kwargs)
        except Exception:
            stream = None
        finally:
            try:
                progress(getattr(stream, "filesize", 0) or 0, getattr(stream, "filesize", 0) or 0)
            except Exception:
                pass
    if stream is None or force_backend_ytdlp:
        if not ytdlp:
            raise RuntimeError("pytube failed to initialize; yt-dlp not installed")
        ydl_opts = {
            "quiet": True,
            "outtmpl": str(temp_path),
            "format": "bestaudio/best",
            "noplaylist": True,
        }
        with ytdlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])

    # Convert to mp3 if requested and possible
    if audio_format.lower() == "mp3":
        if is_ffmpeg_available():
            return convert_to_mp3(temp_path)
        # ffmpeg not available: keep original format
        return temp_path

    # If a different extension was requested (e.g., "m4a"), rename if matches
    if audio_format and audio_format.lower() != source_ext.lower():
        # Without ffmpeg we won't transcode; keep original format
        return temp_path

    final_path = temp_path
    if trim_start or trim_end:
        final_path = ffmpeg_trim(final_path, trim_start, trim_end)
    if convert_to:
        final_path = ffmpeg_convert(final_path, convert_to)
    return final_path


def download_playlist(
    playlist_url: str,
    output_dir: Path,
    audio_only: bool = False,
    resolution: Optional[str] = None,
    video_format: str = config.DEFAULT_VIDEO_FORMAT,
    audio_format: str = config.DEFAULT_AUDIO_FORMAT,
    select_indices: Optional[List[int]] = None,
    **kwargs,
) -> List[Path]:
    _init_pytube_client()
    results: List[Path] = []
    urls: List[str]
    try:
        pl = Playlist(normalize_youtube_url(playlist_url))
        urls = pl.video_urls
    except Exception:
        if not ytdlp:
            raise
        with ytdlp.YoutubeDL({"quiet": True, "extract_flat": True}) as ydl:
            info = ydl.extract_info(normalize_youtube_url(playlist_url), download=False)
            urls = [e.get("url") for e in info.get("entries", []) if e.get("url")]
    if select_indices:
        idx_set = set(select_indices)
        urls = [u for i, u in enumerate(urls) if i in idx_set]
    for url in urls:
        try:
            if audio_only:
                results.append(download_audio(url, output_dir, audio_format=audio_format, trim_start=kwargs.get("trim_start"), trim_end=kwargs.get("trim_end"), convert_to=kwargs.get("convert_to"), safe_mode=kwargs.get("safe_mode", False)))
            else:
                results.append(download_video(url, output_dir, resolution=resolution, file_format=video_format, subtitles_langs=kwargs.get("subtitles_langs"), embed_subtitles=kwargs.get("embed_subtitles", False), trim_start=kwargs.get("trim_start"), trim_end=kwargs.get("trim_end"), convert_to=kwargs.get("convert_to"), safe_mode=kwargs.get("safe_mode", False)))
        except Exception:
            # Skip failed items; in a real app you might collect/report
            continue
    return results


_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"


def _init_pytube_client() -> None:
    """Ensure pytube uses a desktop-like User-Agent to reduce 400 errors."""
    if not _pt_request:
        return
    try:
        if hasattr(_pt_request, "default_client") and hasattr(_pt_request.default_client, "headers"):
            _pt_request.default_client.headers.setdefault("User-Agent", _UA)
            _pt_request.default_client.headers.setdefault("Accept-Language", "en-US,en;q=0.9")
    except Exception:
        pass


class DownloadStatus:
    PENDING = "pending"
    RUNNING = "downloading"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELED = "canceled"


class DownloadTask:
    def __init__(self, url: str, audio_only: bool = False, options: Optional[dict] = None):
        self.url = url
        self.audio_only = audio_only
        self.options = options or {}
        self.status = DownloadStatus.PENDING
        self.error: Optional[str] = None
        self.result_path: Optional[Path] = None
        self._cancelled = False

    def cancel(self):
        self._cancelled = True


from concurrent.futures import ThreadPoolExecutor, as_completed


class DownloadManager:
    def __init__(self, max_workers: int = 2):
        self.max_workers = max_workers

    def run(self, tasks: List[DownloadTask]) -> List[DownloadTask]:
        def worker(task: DownloadTask):
            if task._cancelled:
                task.status = DownloadStatus.CANCELED
                return task
            task.status = DownloadStatus.RUNNING
            try:
                out_dir: Path = task.options.get("output_dir", config.DEFAULT_DOWNLOAD_DIR)
                if task.audio_only:
                    task.result_path = download_audio(task.url, out_dir, **{k: v for k, v in task.options.items() if k != "output_dir"})
                else:
                    task.result_path = download_video(task.url, out_dir, **{k: v for k, v in task.options.items() if k != "output_dir"})
                task.status = DownloadStatus.COMPLETED
            except Exception as e:
                task.status = DownloadStatus.FAILED
                task.error = str(e)
            return task

        with ThreadPoolExecutor(max_workers=self.max_workers) as ex:
            futures = [ex.submit(worker, t) for t in tasks]
            for _ in as_completed(futures):
                pass
        return tasks


