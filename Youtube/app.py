# app.py

import json
import threading
import subprocess
import os
from pathlib import Path
from typing import Optional, Dict, Any, List

import yt_dlp
from flask import Flask, request, jsonify, render_template, send_from_directory, current_app
import time
from collections import defaultdict, deque
from yt_dlp.utils import DownloadError
from app_logger import logger
import concurrent.futures
from flask_wtf.csrf import CSRFProtect
from flask_babel import Babel

app = Flask(__name__)
csrf = CSRFProtect(app)
app.config['BABEL_DEFAULT_LOCALE'] = 'en'
babel = Babel(app)
# Placeholder for future per-user language selection (see Flask-Babel docs)
# For REST endpoints: tokens must come in header (JS demo: see docs). For now, routes are exempted for easy migration, but a comment is left for hardening.
# Example for later: @csrf.exempt for specific API routes, or require X-CSRFToken in JS fetch requests.

# --- Configuration ---
# Use a default folder. WARNING: Web UI cannot trigger a local OS folder dialog.
# The user can only set the path string here or via a settings endpoint.
DOWNLOAD_DIR = Path.home() / "Downloads" / "YT_Web_Downloads"
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

# Global dictionary to track active downloads and their status
DOWNLOAD_TASKS: Dict[str, Dict[str, Any]] = {}
MAX_CONCURRENT_DOWNLOADS = 3
executor = concurrent.futures.ThreadPoolExecutor(max_workers=MAX_CONCURRENT_DOWNLOADS)

# --- Simple in-memory rate limiter (Per-IP) ---
RATE_LIMIT = 5  # requests per minute
rate_limit_window = 60  # seconds
ip_request_times = defaultdict(lambda: deque())

def check_rate_limit(ip):
    now = time.time()
    times = ip_request_times[ip]
    # Remove timestamps older than window
    while times and now - times[0] > rate_limit_window:
        times.popleft()
    if len(times) >= RATE_LIMIT:
        return False
    times.append(now)
    return True

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
    subs_list = options.get('subtitles_langs') # This is now a comma-separated string of languages
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
             ydl_opts['format'] = f'bestvideo[ext={fmt}]+bestaudio/best/best[ext={fmt}]' # Added best/best for robustness
        else:
             ydl_opts['format'] = f'bestvideo[height<={quality.replace("p", "")}][ext={fmt}]+bestaudio/best[ext={fmt}]'

        ydl_opts['merge_output_format'] = fmt

    # Subtitles
    if subs_list and subs_list != 'none': # Check for 'none' which is the default/empty selection
        ydl_opts['writesubtitles'] = True
        
        # If the user selected 'auto' and a language, handle it:
        if 'auto' in subs_list.lower():
            ydl_opts['writeautomaticsubs'] = True
            # Strip 'auto' and check for explicit lang fallback (e.g., 'auto,en')
            subs_list = subs_list.replace('auto', '').strip(', ')
            if subs_list:
                ydl_opts['subtitleslangs'] = [lang.strip() for lang in subs_list.split(',') if lang.strip()]
            else:
                # If only 'auto' was selected, yt-dlp will fetch the default autogen subs
                pass 
        else:
            ydl_opts['subtitleslangs'] = [lang.strip() for lang in subs_list.split(',') if lang.strip()]

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
        logger.error(f"yt-dlp error for {url}: {e}")
    except Exception as e:
        task['status'] = 'FAILED'
        task['message'] = f'Download failed: {str(e)}'
        logger.error(f"General error for {url}: {e}")
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
    """Fetches video metadata (Title, Author, Thumbnail URL, Subtitles, etc.)."""
    ip = request.remote_addr or 'unknown'
    if not check_rate_limit(ip):
        return jsonify({'status': 'error', 'message': 'Too many requests. Please wait and try again.'}), 429

    url = request.json.get('url')
    if not url or 'youtube.' not in url.lower():
        return jsonify({'status': 'error', 'message': 'Valid YouTube URL is required'}), 400
    
    try:
        ydl_opts = {
            'noplaylist': True,
            'quiet': True,
            'skip_download': True,
            'writesubtitles': True,
            'writeautomaticsubs': True,
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)

        if not info:
            return jsonify({'status': 'error', 'message': 'Could not extract metadata'}), 500

        # Handle playlists/mixes by using the first entry's data
        is_playlist = is_playlist_url(url)
        if 'entries' in info and info['entries']:
            # For playlists, still use the first entry for metadata preview
            info_preview = info['entries'][0]
        else:
            info_preview = info

        # Extract available subtitles
        available_subs = []
        if 'subtitles' in info_preview:
            # Keys are language codes, values are list of subtitle entries
            for lang_code in info_preview['subtitles'].keys():
                available_subs.append(lang_code)
        
        # Add automatic captions if available
        if 'automatic_captions' in info_preview:
            for lang_code in info_preview['automatic_captions'].keys():
                if lang_code not in available_subs:
                     available_subs.append(lang_code)

        metadata = {
            'title': info_preview.get('title', 'N/A'),
            'author': info_preview.get('uploader', 'N/A'),
            'duration_seconds': info_preview.get('duration', 0),
            'views': info_preview.get('view_count', 0),
            'thumbnail_url': info_preview.get('thumbnail'),
            'is_playlist': is_playlist,
            # NEW: Return sorted, unique list of subtitle codes
            'available_subtitles': sorted(list(set(available_subs)))
        }
        return jsonify({'status': 'success', 'metadata': metadata})

    except Exception as e:
        return jsonify({'status': 'error', 'message': f'Failed to fetch metadata: {str(e)}'}), 500


@app.route('/api/download', methods=['POST'])
def start_download():
    """Starts a new download task in a separate thread."""
    ip = request.remote_addr or 'unknown'
    if not check_rate_limit(ip):
        return jsonify({'status': 'error', 'message': 'Too many requests. Please wait and try again.'}), 429

    data = request.json
    url = data.get('url')
    if not url or 'youtube.' not in url.lower():
        return jsonify({'status': 'error', 'message': 'Valid YouTube URL is required'}), 400

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
    
    # Instead of thread, submit to executor (thread pool)
    future = executor.submit(_download_worker, url, DOWNLOAD_TASKS[task_id], task_id)
    DOWNLOAD_TASKS[task_id]['future'] = future

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


@app.route('/api/downloads', methods=['GET'])
def list_downloads():
    """Returns a JSON list of all download tasks and their metadata/status."""
    return jsonify({tid: {k:v for k,v in t.items() if k != 'future'} for tid, t in DOWNLOAD_TASKS.items()})


@app.route('/api/history', methods=['GET'])
def download_history():
    """Returns all completed download tasks (prototype: not filtered by user/session yet.)"""
    completed = {tid: t for tid, t in DOWNLOAD_TASKS.items() if t['status'] == 'COMPLETED'}
    return jsonify(completed)


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
    # Setting use_reloader=False stops Flask from running the background thread twice
    app.run(debug=True, host='0.0.0.0', port=5000)