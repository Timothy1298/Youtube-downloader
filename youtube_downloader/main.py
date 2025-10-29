from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import List, Optional

# Support running as a script (python path fix) or as a module
try:
    from . import config
    from .downloader import (
        download_audio,
        download_playlist,
        download_video,
        get_video_metadata,
        DownloadManager,
        DownloadTask,
    )
    from .utils import notify, write_log_entry, suggest_best_format
except Exception:
    import os
    import sys

    sys.path.append(os.path.dirname(os.path.dirname(__file__)))
    from youtube_downloader import config  # type: ignore
    from youtube_downloader.downloader import (  # type: ignore
        download_audio,
        download_playlist,
        download_video,
        get_video_metadata,
        DownloadManager,
        DownloadTask,
    )
    from youtube_downloader.utils import notify, write_log_entry, suggest_best_format  # type: ignore


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="YouTube Downloader")
    parser.add_argument("urls", nargs="*", help="YouTube video URLs")
    parser.add_argument("--playlist", help="YouTube playlist URL", default=None)
    parser.add_argument("--batch", help="Path to a text file with one URL per line", default=None)
    parser.add_argument("--audio", action="store_true", help="Download audio only")
    parser.add_argument("--format", dest="format_", help="Target format (mp4, webm, mp3)", default=None)
    parser.add_argument("--quality", help="Video resolution, e.g., 1080p, 720p", default=None)
    parser.add_argument("--output", help="Output directory", default=str(config.DEFAULT_DOWNLOAD_DIR))
    parser.add_argument("--metadata", action="store_true", help="Print metadata before downloading")
    parser.add_argument("--gui", action="store_true", help="Launch GUI instead of CLI")
    # Advanced features
    parser.add_argument("--playlist-select", help="Comma-separated indices to download from playlist (0-based)", default=None)
    parser.add_argument("--subs", help="Comma-separated subtitle language codes to download (e.g., en,es)", default=None)
    parser.add_argument("--embed-subs", action="store_true", help="Burn first subtitle track into video (re-encode)")
    parser.add_argument("--start", help="Trim start time (SS, MM:SS, or HH:MM:SS)", default=None)
    parser.add_argument("--end", help="Trim end time (SS, MM:SS, or HH:MM:SS)", default=None)
    parser.add_argument("--threads", type=int, default=1, help="Number of parallel downloads")
    parser.add_argument("--notify", action="store_true", help="Desktop notification when done")
    parser.add_argument("--convert", help="Convert output to another format (e.g., avi, wav)", default=None)
    parser.add_argument("--safe-mode", action="store_true", help="Skip age-restricted videos")
    parser.add_argument("--log-file", help="Path to JSONL log file", default=None)
    parser.add_argument("--suggest-format", action="store_true", help="Suggest best format based on rough network speed")
    parser.add_argument("--rename-template", help="Filename template using {title},{author},{date}", default=None)
    parser.add_argument("--backend", choices=["auto", "ytdlp"], default="auto", help="Download backend")
    return parser.parse_args(argv)


def load_batch_file(path: str) -> List[str]:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"Batch file not found: {p}")
    return [line.strip() for line in p.read_text().splitlines() if line.strip()]


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    if args.gui:
        from .gui import launch_gui

        launch_gui()
        return 0

    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)

    urls: List[str] = list(args.urls)
    if args.batch:
        urls.extend(load_batch_file(args.batch))

    if args.playlist:
        try:
            select = None
            if args.playlist_select:
                try:
                    select = [int(x.strip()) for x in args.playlist_select.split(",") if x.strip()]
                except Exception:
                    print("Invalid --playlist-select; expected comma-separated indices")
                    return 2
            results = download_playlist(
                args.playlist,
                output_dir=output_dir,
                audio_only=bool(args.audio),
                resolution=args.quality,
                video_format=(args.format_ or config.DEFAULT_VIDEO_FORMAT),
                audio_format=(args.format_ or config.DEFAULT_AUDIO_FORMAT),
                select_indices=select,
                subtitles_langs=(args.subs.split(",") if args.subs else None),
                embed_subtitles=bool(args.embed_subs),
                trim_start=args.start,
                trim_end=args.end,
                convert_to=args.convert,
                safe_mode=bool(args.safe_mode),
                rename_template=args.rename_template,
            )
            print(f"Downloaded {len(results)} items to {output_dir}")
            if args.notify:
                notify("Playlist Download", f"Downloaded {len(results)} items")
        except Exception as exc:
            print(f"Error downloading playlist: {exc}")
            return 1
        return 0

    if not urls:
        print("Provide at least one URL, or use --playlist/--batch. Use --help for options.")
        return 2

    # Suggest format
    if args.suggest_format and not args.format_:
        # naive assumption on speed: use 10 Mbps default for now
        suggestion = suggest_best_format(10.0)
        print(f"Suggested format: {suggestion}")

    options_common = {
        "output_dir": output_dir,
        "trim_start": args.start,
        "trim_end": args.end,
        "convert_to": args.convert,
        "safe_mode": bool(args.safe_mode),
        "rename_template": args.rename_template,
        "force_backend_ytdlp": (args.backend == "ytdlp"),
    }
    if not args.audio:
        options_common.update({
            "resolution": args.quality or "highest",
            "file_format": (args.format_ or config.DEFAULT_VIDEO_FORMAT),
            "subtitles_langs": (args.subs.split(",") if args.subs else None),
            "embed_subtitles": bool(args.embed_subs),
        })
    else:
        options_common.update({
            "audio_format": (args.format_ or config.DEFAULT_AUDIO_FORMAT),
        })

    # Multithreaded queue
    if args.threads and args.threads > 1 and len(urls) > 1:
        tasks = [DownloadTask(u, audio_only=bool(args.audio), options=options_common) for u in urls]
        mgr = DownloadManager(max_workers=args.threads)
        tasks = mgr.run(tasks)
        for t in tasks:
            if t.status == "completed":
                print(f"Saved: {t.result_path}")
                if args.log_file:
                    write_log_entry(Path(args.log_file), {"url": t.url, "path": str(t.result_path), "status": t.status})
            else:
                print(f"Failed: {t.url} ({t.status}) {t.error or ''}")
        if args.notify:
            notify("Downloads finished", f"{sum(1 for t in tasks if t.status=='completed')} completed, {sum(1 for t in tasks if t.status!='completed')} failed")
        return 0 if all(t.status == "completed" for t in tasks) else 1

    # Sequential
    exit_code = 0
    for url in urls:
        try:
            if args.metadata:
                md = get_video_metadata(url)
                print(
                    f"Title: {md.title}\nChannel: {md.author}\nDuration: {md.length_seconds}s\n"
                    f"Views: {md.views}\nThumbnail: {md.thumbnail_url}\n"
                )
            if args.audio:
                target = download_audio(url, **{k: v for k, v in options_common.items() if k != "resolution" and k != "file_format" and k != "subtitles_langs" and k != "embed_subtitles"})
            else:
                target = download_video(url, **options_common)
            print(f"Saved: {target}")
            if args.log_file:
                write_log_entry(Path(args.log_file), {"url": url, "path": str(target), "status": "completed"})
        except KeyboardInterrupt:
            print("Interrupted by user")
            exit_code = 130
            break
        except Exception as exc:
            print(f"Failed to download {url}: {exc}")
            if args.log_file:
                write_log_entry(Path(args.log_file), {"url": url, "status": "failed", "error": str(exc)})
            exit_code = 1
    if args.notify:
        notify("Downloads finished", "All tasks processed")
    return exit_code


if __name__ == "__main__":
    # Launch GUI by default if no args provided; otherwise pass through
    import sys as _sys
    argv = ["--gui"] if len(_sys.argv) <= 1 else None
    raise SystemExit(main(argv))


