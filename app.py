import os
import threading
import queue
import uuid
import logging
from flask import Flask, request, render_template, redirect, url_for, jsonify
import yt_dlp # type: ignore

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

app = Flask(__name__)

# In-memory queue and status tracking
# For production, consider Redis/Celery for persistence and scalability
download_queue = queue.Queue()
# Store status of all downloads (including completed/failed)
# Items: {'id': str, 'url': str, 'title': str, 'status': str, 'error': str|None, 'video_format': str, 'audio_format': str}
download_status_list = []
status_lock = threading.Lock() # To safely update the status list

DOWNLOAD_FOLDER = "/app/downloads" # Inside the container

# Ensure download folder exists
os.makedirs(DOWNLOAD_FOLDER, exist_ok=True)

def format_selector(ctx):
    """ Select the best video and the best audio that matches the request """
    # formats are already filtered by the time they reach here
    formats = ctx.get('formats')[::-1]

    # acodec='none' means there is no audio
    # vcodec='none' means there is no video
    # break into video only and audio only formats
    audio_formats = [
        f for f in formats
        if f.get('vcodec') == 'none' and f.get('acodec') != 'none'
    ]
    video_formats = [
        f for f in formats
        if f.get('acodec') == 'none' and f.get('vcodec') != 'none'
    ]

    # find the format requested by the user
    req_video_format = ctx['requested_formats'][0]['format_id']
    req_audio_format = ctx['requested_formats'][1]['format_id']

    best_video = None
    best_audio = None

    for f in video_formats:
        if f['format_id'] == req_video_format:
            best_video = f
            logging.info(f"Selected video format: {f['format_id']}")
            break

    for f in audio_formats:
         if f['format_id'] == req_audio_format:
            best_audio = f
            logging.info(f"Selected audio format: {f['format_id']}")
            break

    # If the user explicitly selected "none", we respect that
    if req_video_format == 'none':
        best_video = None
    if req_audio_format == 'none':
        best_audio = None

    # If we didn't find the specific requested format, maybe log an error or fallback?
    # For now, we assume the IDs passed from the form are valid.

    # Communicate the choice back to yt-dlp
    yield {
        'video': best_video,
        'audio': best_audio,
    }


def format_filesize(byte_size):
    """ Converts bytes to human-readable string """
    if byte_size is None:
        return None
    if byte_size < 1024:
        return f"{byte_size} B"
    elif byte_size < 1024**2:
        return f"{byte_size/1024:.1f} KiB"
    elif byte_size < 1024**3:
        return f"{byte_size/1024**2:.1f} MiB"
    else:
        return f"{byte_size/1024**3:.1f} GiB"

def update_status(job_id, new_status, error_message=None, title=None):
    """ Safely update the status of a download job """
    with status_lock:
        for item in download_status_list:
            if item['id'] == job_id:
                item['status'] = new_status
                if error_message:
                    item['error'] = str(error_message)
                if title and not item.get('title'): # Update title if not already set
                     item['title'] = title
                logging.info(f"Status updated for {job_id}: {new_status}")
                break

def download_worker():
    """ Worker thread to process downloads from the queue """
    while True:
        job = download_queue.get() # Blocks until an item is available
        job_id = job['id']
        url = job['url']
        video_format = job['video_format']
        audio_format = job['audio_format']
        title = job.get('title', 'Unknown Title') # Use title passed from form

        logging.info(f"Worker picked up job {job_id}: {url} (v:{video_format}, a:{audio_format})")
        update_status(job_id, 'downloading', title=title)

        try:
            # Determine the format string for yt-dlp
            # We rely on the custom format selector now
            format_string = f"{video_format}+{audio_format}"
            if video_format == 'none' and audio_format == 'none':
                 raise ValueError("Cannot download with both video and audio set to 'none'")
            elif video_format == 'none':
                 format_string = audio_format
            elif audio_format == 'none':
                 format_string = video_format

            # Define yt-dlp options
            ydl_opts = {
                'format': format_string,
                'outtmpl': os.path.join(DOWNLOAD_FOLDER, '%(title)s [%(id)s].%(ext)s'),
                'merge_output_format': 'mkv', # Merge to MKV if separate streams are downloaded
                'quiet': False, # Show yt-dlp output in logs
                'progress_hooks': [lambda d: hook(d, job_id)],
                'postprocessor_hooks': [lambda d: post_hook(d, job_id)],
                'logger': logging.getLogger('yt_dlp'),
                # Use custom format selector if both video and audio are requested
                # 'format_sort': ['+res,+fps,+abr'], # Prioritize quality if needed, but selector overrides
            }

            # Only add format_selector if both video and audio are requested and not 'none'
            if video_format != 'none' and audio_format != 'none':
                 ydl_opts['format'] = 'bestvideo+bestaudio/best' # Request best available initially
                 ydl_opts['format_selector'] = format_selector
                 # Pass requested formats to the context for the selector
                 ydl_opts['requested_formats'] = [{'format_id': video_format}, {'format_id': audio_format}]


            logging.info(f"Starting download for {job_id} with options: { {k: v for k, v in ydl_opts.items() if k != 'logger'} }") # Avoid logging the logger object

            # Create YoutubeDL instance and download
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                # Extract info again to get the final title if not passed reliably
                info_dict = ydl.extract_info(url, download=False)
                final_title = info_dict.get('title', title)
                update_status(job_id, 'downloading', title=final_title) # Update title if fetched

                # Start the download
                ydl.download([url])

            logging.info(f"Download completed successfully for job {job_id}")
            update_status(job_id, 'completed', title=final_title)

        except yt_dlp.utils.DownloadError as e:
            logging.error(f"yt-dlp DownloadError for job {job_id}: {e}")
            update_status(job_id, 'failed', error_message=f"Download Error: {e}")
        except Exception as e:
            logging.error(f"Unexpected error for job {job_id}: {e}", exc_info=True)
            update_status(job_id, 'failed', error_message=f"Unexpected Error: {e}")
        finally:
            download_queue.task_done() # Signal that the task is complete

def hook(d, job_id):
    """ Progress hook for yt-dlp """
    if d['status'] == 'downloading':
        # You could potentially update the status more granularly here
        # e.g., showing percentage, but it adds complexity to the UI
        pass
    elif d['status'] == 'error':
         logging.error(f"yt-dlp hook reported error for {job_id}: {d.get('error', 'Unknown error')}")
         # Status will be updated in the main exception handler
    elif d['status'] == 'finished':
        logging.info(f"yt-dlp hook reported finished for {job_id}. Filename: {d.get('filename', 'N/A')}")
        # Final status update happens after ydl.download() completes

def post_hook(d, job_id):
    """ Post-processor hook (e.g., for merging) """
    if d['status'] == 'finished' and d['postprocessor'] == 'Merger':
        logging.info(f"Merging finished for job {job_id}")
    elif d['status'] == 'error':
        logging.error(f"Post-processing error for job {job_id}: {d.get('error', 'Unknown error')}")


@app.route('/')
def index():
    """ Render the main page """
    with status_lock:
        # Pass a copy to avoid modification during iteration in template
        current_status = list(download_status_list)
    return render_template('index.html', download_status=current_status)

@app.route('/get_formats', methods=['POST'])
def get_formats():
    """ Fetch available formats for a given URL """
    url = request.form.get('url')
    if not url:
        return redirect(url_for('index')) # Or show an error

    logging.info(f"Fetching formats for URL: {url}")
    try:
        ydl_opts = {'quiet': True, 'listformats': False} # Don't list to console here
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            formats = info.get('formats', [])
            title = info.get('title', 'Unknown Title')

        # Separate video and audio, filter out bad data
        video_formats = []
        audio_formats = []
        for f in formats:
            # Basic check for necessary keys
            if not f.get('format_id'):
                continue

            # Add approximate filesize
            f['filesize_approx'] = format_filesize(f.get('filesize') or f.get('filesize_approx'))

            # Check if it's video-only or has video component
            if f.get('vcodec') != 'none' and f.get('resolution') != 'audio only':
                 # Prefer formats that need merging (separate streams often higher quality)
                 # Or formats that already contain video
                 if f.get('acodec') == 'none' or f.get('ext') in ['mp4', 'webm', 'mkv']: # Adjust as needed
                    video_formats.append(f)

            # Check if it's audio-only
            if f.get('acodec') != 'none' and f.get('vcodec') == 'none':
                audio_formats.append(f)

        # Sort formats for better presentation (optional)
        # Use 'or 0' to handle cases where the value itself is None
        video_formats.sort(key=lambda x: (x.get('height') or 0, x.get('fps') or 0, x.get('tbr') or 0), reverse=True)
        audio_formats.sort(key=lambda x: x.get('abr') or 0, reverse=True)

        logging.info(f"Found {len(video_formats)} video formats and {len(audio_formats)} audio formats for {url}")
        return render_template('select_formats.html',
                               url=url,
                               title=title,
                               video_formats=video_formats,
                               audio_formats=audio_formats,
                               error=None)

    except yt_dlp.utils.DownloadError as e:
        logging.error(f"Error fetching formats for {url}: {e}")
        # Render the same template but show an error message
        return render_template('select_formats.html', url=url, title=f"Error for {url}", error=str(e))
    except Exception as e:
        logging.error(f"Unexpected error fetching formats for {url}: {e}", exc_info=True)
        return render_template('select_formats.html', url=url, title=f"Error for {url}", error=f"Unexpected error: {e}")


@app.route('/add_download', methods=['POST'])
def add_download():
    """ Add a download job to the queue """
    url = request.form.get('url')
    title = request.form.get('title', 'Loading title...') # Get title passed from form
    video_format = request.form.get('video_format')
    audio_format = request.form.get('audio_format')

    if not url or (video_format == 'none' and audio_format == 'none'):
        # Handle error - maybe flash message or redirect with error param
        logging.warning("Attempted to add download with no URL or no formats selected.")
        return redirect(url_for('index')) # Redirect home for simplicity

    job_id = str(uuid.uuid4())[:8] # Short unique ID
    job = {
        'id': job_id,
        'url': url,
        'title': title, # Store the title immediately
        'status': 'queued',
        'error': None,
        'video_format': video_format,
        'audio_format': audio_format,
    }

    with status_lock:
        download_status_list.append(job)

    download_queue.put(job)
    logging.info(f"Added job {job_id} to queue: {url} (v:{video_format}, a:{audio_format})")

    return redirect(url_for('index'))

# Start the background worker thread
worker_thread = threading.Thread(target=download_worker, daemon=True)
worker_thread.start()
logging.info("Download worker thread started.")

if __name__ == '__main__':
    # Use Waitress or Gunicorn in production instead of Flask's dev server
    # Example: waitress-serve --host 0.0.0.0 --port 5000 app:app
    app.run(host='0.0.0.0', port=5000, debug=False) # Debug=False for production/threading

