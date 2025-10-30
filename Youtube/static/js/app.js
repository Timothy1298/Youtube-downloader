// static/js/app.js

const urlInput = document.getElementById('url-input');
const modeSelect = document.getElementById('mode-select');
const formatSelect = document.getElementById('format-select');
const qualitySelect = document.getElementById('quality-select');
const subsInput = document.getElementById('subs-input');
const embedSubsCheck = document.getElementById('embed-subs-check');
const startInput = document.getElementById('start-input');
const endInput = document.getElementById('end-input');

const downloadBtn = document.getElementById('download-btn');
const getInfoBtn = document.getElementById('get-info-btn');
const openFolderBtn = document.getElementById('open-folder-btn');
const taskList = document.getElementById('task-list');
const emptyQueueMsg = document.getElementById('empty-queue-msg');

const metadataPreview = document.getElementById('metadata-preview');
const thumbnailBox = document.getElementById('thumbnail-box');
const metaTitle = document.getElementById('meta-title');
const metaAuthor = document.getElementById('meta-author');
const metaDuration = document.getElementById('meta-duration');
const metaViews = document.getElementById('meta-views');
const metaPlaylistAlert = document.getElementById('meta-playlist-alert');


// Map to store interval IDs for polling
const activePolls = {};
const taskCache = {}; // Cache to store task details

// --- Utility Functions ---

function enableDownload() {
    downloadBtn.disabled = false;
    downloadBtn.textContent = "Start Download";
}

function disableDownload() {
    downloadBtn.disabled = true;
    downloadBtn.textContent = "Starting...";
}

function formatDuration(seconds) {
    const h = Math.floor(seconds / 3600);
    const m = Math.floor((seconds % 3600) / 60);
    const s = Math.round(seconds % 60);
    const parts = [h, m, s];
    
    // Ensure hours are shown if duration is >= 1 hour
    if (h > 0) {
        return parts.map(v => v.toString().padStart(2, '0')).join(':');
    }
    // Minutes:Seconds format
    return parts.slice(1).map(v => v.toString().padStart(2, '0')).join(':');
}


// --- Metadata Functions ---

function getMetadata() {
    const url = urlInput.value.trim();
    if (!url) {
        alert("Please enter a YouTube URL before getting info.");
        return;
    }

    getInfoBtn.disabled = true;
    getInfoBtn.textContent = "Fetching...";
    downloadBtn.disabled = true;

    // Clear previous metadata
    metadataPreview.classList.add('hidden');
    thumbnailBox.innerHTML = '<div class="loader-small"></div>';
    
    fetch('/api/metadata', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ url: url })
    })
    .then(response => response.json())
    .then(data => {
        getInfoBtn.disabled = false;
        getInfoBtn.textContent = "Get Info";
        
        if (data.status === 'success') {
            const md = data.metadata;
            
            // Populate text metadata
            metaTitle.textContent = md.title;
            metaAuthor.textContent = md.author;
            metaDuration.textContent = formatDuration(md.duration_seconds);
            metaViews.textContent = md.views ? md.views.toLocaleString() : 'N/A';
            
            // Handle playlist alert
            if (md.is_playlist) {
                metaPlaylistAlert.textContent = "Playlist/Mix detected. All videos will be downloaded.";
                metaPlaylistAlert.classList.remove('hidden');
            } else {
                metaPlaylistAlert.classList.add('hidden');
            }
            
            // Populate thumbnail
            if (md.thumbnail_url) {
                thumbnailBox.innerHTML = `<img src="${md.thumbnail_url}" alt="Thumbnail">`;
            } else {
                thumbnailBox.innerHTML = 'No Thumbnail';
            }

            metadataPreview.classList.remove('hidden');
            enableDownload(); // Enable download button only after successful metadata fetch

        } else {
            metadataPreview.classList.add('hidden');
            alert(`Metadata Error: ${data.message}`);
        }
    })
    .catch(error => {
        getInfoBtn.disabled = false;
        getInfoBtn.textContent = "Get Info";
        console.error('Metadata fetch error:', error);
        alert('An error occurred while fetching metadata.');
    });
}


// --- Download Functions ---

function startDownload() {
    const url = urlInput.value.trim();
    if (!url) {
        alert("Please enter a URL.");
        return;
    }

    disableDownload();

    // 1. Collect all options from the UI
    const options = {
        url: url,
        audio_only: modeSelect.value === 'audio',
        format: formatSelect.value,
        quality: qualitySelect.value,
        subtitles_langs: subsInput.value.trim() || null,
        embed_subtitles: embedSubsCheck.checked,
        trim_start: startInput.value.trim() || null,
        trim_end: endInput.value.trim() || null,
    };
    
    // 2. Adjust format based on mode for consistency (backend will handle the rest)
    if (options.audio_only && ['mp4', 'webm'].includes(options.format)) {
        options.format = 'mp3'; // Suggest MP3 for simplicity in the UI
    }

    fetch('/api/download', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(options)
    })
    .then(response => response.json())
    .then(data => {
        enableDownload(); // Re-enable button quickly
        
        if (data.status === 'success') {
            const taskId = data.task_id;
            // urlInput.value = ''; // Keep URL in case user wants to start another task

            // Add the task to the UI and start polling for status
            taskCache[taskId] = data.task_details;
            addTaskToUI(taskId, data.task_details);
            startPolling(taskId);

        } else {
            alert(`Download Start Error: ${data.message}`);
        }
    })
    .catch(error => {
        enableDownload();
        console.error('Download initiation error:', error);
        alert('An error occurred while connecting to the server to start the download.');
    });
}

function startPolling(taskId) {
    stopPolling(taskId); // Stop any existing poll
    
    // Start new poll interval (e.g., every 1.5 seconds)
    const intervalId = setInterval(() => {
        updateStatus(taskId);
    }, 1500);

    activePolls[taskId] = intervalId;
}

function stopPolling(taskId) {
    if (activePolls[taskId]) {
        clearInterval(activePolls[taskId]);
        delete activePolls[taskId];
    }
}

function updateStatus(taskId) {
    fetch(`/api/status/${taskId}`)
    .then(response => response.json())
    .then(task => {
        // Update local cache
        if (task.status !== 'error') {
            taskCache[taskId] = task;
        }

        updateTaskUI(taskId, task);

        // Stop polling if the task is finished or failed
        if (['COMPLETED', 'FAILED'].includes(task.status)) {
            stopPolling(taskId);
        }
    })
    .catch(error => {
        console.error(`Error fetching status for ${taskId}:`, error);
        stopPolling(taskId);
        // Only update UI if we have cache data
        if (taskCache[taskId]) {
            updateTaskUI(taskId, { status: 'FAILED', message: 'Connection lost or Server Error.', progress: taskCache[taskId].progress });
        }
    });
}


// --- UI Management Functions ---

function addTaskToUI(taskId, task) {
    emptyQueueMsg.classList.add('hidden');
    
    const li = document.createElement('li');
    li.id = `task-${taskId}`;
    li.innerHTML = `
        <div class="task-header">
            <strong>ID: ${taskId} | ${task.mode} ${task.is_playlist ? '(Playlist/Mix)' : ''}</strong>
            <span class="status ${task.status.toLowerCase()}">${task.status}</span>
        </div>
        <div class="task-body">
            <p class="task-url">${task.url}</p>
            <p class="task-filename" title="File Name">${task.filename}</p>
            <div class="progress-bar-container">
                <div class="progress-bar" style="width: ${task.progress}%;"></div>
            </div>
            <p class="task-message">${task.message}</p>
        </div>
    `;
    taskList.prepend(li);
}

function updateTaskUI(taskId, task) {
    const li = document.getElementById(`task-${taskId}`);
    if (!li) return;

    // Update status and progress
    li.querySelector('.status').textContent = task.status;
    li.querySelector('.status').className = `status ${task.status.toLowerCase()}`;
    li.querySelector('.progress-bar').style.width = `${task.progress}%`;
    li.querySelector('.task-message').textContent = task.message;
    li.querySelector('.task-filename').textContent = task.filename;

    // Optional: Add final styling
    if (task.status === 'COMPLETED') {
        li.classList.add('completed');
    } else if (task.status === 'FAILED') {
        li.classList.add('failed');
    }
}

function handleOpenFolder() {
    fetch('/api/open_folder')
    .then(response => response.json())
    .then(data => {
        if (data.status === 'error') {
            alert(data.message);
        }
    })
    .catch(error => {
        console.error('Open folder error:', error);
        alert('Server connection error while trying to open folder.');
    });
}

function handleModeChange() {
    const isAudio = modeSelect.value === 'audio';
    
    // Disable quality select for audio mode
    qualitySelect.disabled = isAudio;
    
    // Change format options
    if (isAudio) {
        formatSelect.innerHTML = `
            <option value="mp3" selected>MP3 (Preferred)</option>
            <option value="m4a">M4A</option>
            <option value="best">Best Audio</option>
        `;
    } else {
        formatSelect.innerHTML = `
            <option value="mp4" selected>MP4</option>
            <option value="webm">WebM</option>
        `;
    }
}


// --- Event Listeners and Init ---

downloadBtn.addEventListener('click', startDownload);
getInfoBtn.addEventListener('click', getMetadata);
openFolderBtn.addEventListener('click', handleOpenFolder);
modeSelect.addEventListener('change', handleModeChange);

// Allow pressing Enter in the URL input field
urlInput.addEventListener('keypress', (e) => {
    if (e.key === 'Enter') {
        getMetadata();
    }
});

// Initial state setup
document.addEventListener('DOMContentLoaded', () => {
    handleModeChange(); // Set initial format options
    urlInput.addEventListener('input', () => {
        metadataPreview.classList.add('hidden');
        downloadBtn.disabled = true;
    });
});