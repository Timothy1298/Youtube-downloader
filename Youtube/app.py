# app.py

import json
import threading
import subprocess
import os
from pathlib import Path
from typing import Optional, Dict, Any, List

import yt_dlp
from flask import Flask, request, jsonify, render_template, send_from_directory, current_app
from yt_dlp.utils import DownloadError

# --- Configuration ---
# Use a default folder. WARNING: Web UI cannot trigger a local OS folder dialog.
# The user can only set the path string here or via a settings endpoint.
DOWNLOAD_DIR = Path.home() / "Downloads" / "YT_Web_Downloads"
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

# Global dictionary to track active downloads and their status
DOWNLOAD_TASKS: Dict[str, Dict[str, Any]] = {}
app = Flask(__name__)

# --- Helper Functions ---

def is_playlist_url(url: str) -> bool:
    """Simple check for playlist/mix URL identifiers."""
    return "list=" in url or "/playlist" in url.lower() or "/user/" in url.lower() or "/channel/" in url.lower()

def _download_worker(url: str, options: Dict[str, Any], task_id: str):
    """
    The main worker function to run yt-dlp in a separate thread.
    Handles all the options passed from the frontend.
    """
    task = DOWNLOAD_TASKS[task_id]
    
    # 1. Setup variables
    is_audio_only = options.get('audio_only', False)
    is_playlist = options.get('is_playlist', False)
    fmt = options.get('format', 'mp4') # video or audio format
    quality = options.get('quality', 'highest') # 1080p, 720p, etc.
    subs = options.get('subtitles_langs')
    embed_subs = options.get('embed_subtitles', False)
    trim_start = options.get('trim_start')
    trim_end = options.get('trim_end')

    # 2. Update status
    task['status'] = 'DOWNLOADING'
    task['progress'] = 0.0
    task['message'] = 'Starting download process...'

    # 3. Setup yt-dlp hooks for progress updates
    def progress_hook(d: Dict[str, Any]):
        if d['status'] == 'finished':
            task['progress'] = 100.0
            task['filename'] = d.get('filename', task.get('filename', 'N/A'))
        elif d['status'] == 'downloading':
            if d.get('total_bytes') or d.get('total_bytes_estimate'):
                total = d.get('total_bytes') or d.get('total_bytes_estimate')
                percent = d.get('downloaded_bytes', 0) / total * 100
                task['progress'] = round(percent, 1)
            task['filename'] = d.get('filename', task.get('filename', 'N/A'))
            task['message'] = f"Progress: {task['progress']}% | Speed: {d.get('speed', 'N/A')}"

    # 4. Build yt-dlp options
    ydl_opts = {
        'outtmpl': str(DOWNLOAD_DIR / '%(title)s.%(ext)s'),
        'progress_hooks': [progress_hook],
        'retries': 5,
        'extractor_args': {'youtube': {'client': 'android'}},
        'geo_bypass': True,
        'ignoreerrors': is_playlist, # Allow playlist to skip failed videos
    }
    
    if not is_playlist:
        ydl_opts['noplaylist'] = True
    
    # Format selection based on mode
    if is_audio_only:
        ydl_opts['format'] = 'bestaudio/best'
        ydl_opts['postprocessors'] = [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': fmt, # Use fmt (mp3, aac, etc.)
            'preferredquality': '192',
        }]
    else:
        # Video format (combining best video and audio)
        # Using the resolution/quality option
        if quality == 'highest':
             ydl_opts['format'] = f'bestvideo[ext={fmt}]+bestaudio/best[ext={fmt}]'
        else:
             ydl_opts['format'] = f'bestvideo[height<={quality.replace("p", "")}][ext={fmt}]+bestaudio/best[ext={fmt}]'

        ydl_opts['merge_output_format'] = fmt

    # Subtitles
    if subs:
        ydl_opts['writesubtitles'] = True
        ydl_opts['subtitleslangs'] = subs.split(',')
        if embed_subs:
            ydl_opts['postprocessors'] = ydl_opts.get('postprocessors', []) + [
                {'key': 'FFmpegEmbedSubtitle'} # Note: This forces re-encode, which takes time
            ]

    # Trimming
    if trim_start or trim_end:
        ydl_opts['postprocessors'] = ydl_opts.get('postprocessors', []) + [
            {
                'key': 'FFmpegTrim',
                'start_time': trim_start,
                'end_time': trim_end,
                'trim_by_pattern': None
            }
        ]
        ydl_opts['force_keyframes_at_trim'] = True


    # 5. Execute Download
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info_dict = ydl.extract_info(url, download=True)
            
            # Get the final file path if available
            if info_dict and 'requested_downloads' in info_dict and len(info_dict['requested_downloads']) > 0:
                task['result_path'] = info_dict['requested_downloads'][0]['filepath']
            elif info_dict and '_filename' in info_dict:
                 task['result_path'] = info_dict['_filename']
            else:
                task['result_path'] = 'N/A'

        task['status'] = 'COMPLETED'
        task['message'] = 'Download complete!'

    except DownloadError as e:
        task['status'] = 'FAILED'
        task['message'] = f'Download failed: yt-dlp error: {e}'
        current_app.logger.error(f"yt-dlp error for {url}: {e}")
    except Exception as e:
        task['status'] = 'FAILED'
        task['message'] = f'Download failed: {str(e)}'
        current_app.logger.error(f"General error for {url}: {e}")
    finally:
        # If the filename is still 'N/A' but the status is COMPLETED, set it to the best guess
        if task['status'] == 'COMPLETED' and task['filename'] == 'N/A' and task['result_path'] != 'N/A':
             task['filename'] = Path(task['result_path']).name


# --- API Routes ---

@app.route('/')
def index():
    """Renders the main HTML page."""
    return render_template('index.html', default_output_dir=str(DOWNLOAD_DIR))

@app.route('/api/metadata', methods=['POST'])
def get_metadata():
    """Fetches video metadata (Title, Author, Thumbnail URL, etc.)."""
    url = request.json.get('url')
    if not url:
        return jsonify({'status': 'error', 'message': 'URL is required'}), 400
    
    try:
        ydl_opts = {
            'noplaylist': True,
            'quiet': True,
            'skip_download': True,
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)

        if not info:
            return jsonify({'status': 'error', 'message': 'Could not extract metadata'}), 500

        # Handle playlists/mixes by using the first entry's data
        if 'entries' in info and info['entries']:
            info = info['entries'][0]

        metadata = {
            'title': info.get('title', 'N/A'),
            'author': info.get('uploader', 'N/A'),
            'duration_seconds': info.get('duration', 0),
            'views': info.get('view_count', 0),
            'thumbnail_url': info.get('thumbnail'),
            'is_playlist': is_playlist_url(url)
        }
        return jsonify({'status': 'success', 'metadata': metadata})

    except Exception as e:
        return jsonify({'status': 'error', 'message': f'Failed to fetch metadata: {str(e)}'}), 500


@app.route('/api/download', methods=['POST'])
def start_download():
    """Starts a new download task in a separate thread."""
    data = request.json
    url = data.get('url')
    
    if not url:
        return jsonify({'status': 'error', 'message': 'URL is required'}), 400

    # Collect all options
    options = {
        'audio_only': data.get('audio_only', False),
        'format': data.get('format', 'mp4'),
        'quality': data.get('quality', 'highest'),
        'subtitles_langs': data.get('subtitles_langs'),
        'embed_subtitles': data.get('embed_subtitles', False),
        'trim_start': data.get('trim_start'),
        'trim_end': data.get('trim_end'),
    }

    # Generate a unique ID for the task
    task_id = str(len(DOWNLOAD_TASKS) + 1)
    is_pl = is_playlist_url(url)

    # Initialize task status
    DOWNLOAD_TASKS[task_id] = {
        'url': url,
        'mode': 'Audio' if options['audio_only'] else 'Video',
        'is_playlist': is_pl,
        'status': 'QUEUED',
        'progress': 0.0,
        'filename': 'N/A',
        'result_path': 'N/A',
        'message': f'Queued. Playlist: {is_pl}',
        **options # Store options for reference
    }
    
    # Start the download in a new thread
    thread = threading.Thread(
        target=_download_worker, 
        args=(url, DOWNLOAD_TASKS[task_id], task_id)
    )
    thread.start()

    return jsonify({
        'status': 'success', 
        'task_id': task_id,
        'message': 'Download started in background.',
        'task_details': DOWNLOAD_TASKS[task_id]
    })


@app.route('/api/status/<task_id>', methods=['GET'])
def get_status(task_id):
    """Retrieves the current status of a download task."""
    task = DOWNLOAD_TASKS.get(task_id)
    if not task:
        return jsonify({'status': 'error', 'message': 'Task not found'}), 404
        
    return jsonify(task)


@app.route('/api/open_folder', methods=['GET'])
def open_folder():
    """Attempts to open the download directory on the server's OS."""
    try:
        path = str(DOWNLOAD_DIR)
        if os.name == 'nt':  # Windows
            subprocess.Popen(['explorer', path])
        elif os.name == 'posix':
            if 'darwin' in os.sys.platform: # macOS
                subprocess.Popen(['open', path])
            else: # Linux (xdg-open)
                subprocess.Popen(['xdg-open', path])
        
        return jsonify({'status': 'success', 'message': f'Attempted to open folder: {path}'})
    except Exception as e:
        return jsonify({'status': 'error', 'message': f'Failed to open folder: {str(e)}. This feature only works when running locally.'}), 500


if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)