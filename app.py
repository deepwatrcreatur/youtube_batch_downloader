import os
import threading
import queue
import uuid
import logging
import re # Added
from flask import ( # Added session, flash, url_for
    Flask, request, render_template, redirect, url_for, jsonify, session, flash
)
import yt_dlp # type: ignore

def format_filesize(byte_size):
    """ Converts bytes to human-readable string """
    if byte_size is None:
        return None # Return None if input is None
    # Use 1024 (KiB, MiB, GiB) for binary prefixes
    power = 1024
    n = 0
    power_labels = {0 : ' B', 1: ' KiB', 2: ' MiB', 3: ' GiB', 4: ' TiB'}
    while byte_size >= power and n < len(power_labels) - 1:
        byte_size /= power
        n += 1
    # Format to one decimal place if not bytes, otherwise integer
    if n > 0:
        return f"{byte_size:.1f}{power_labels[n]}"
    else:
        return f"{byte_size}{power_labels[n]}"

# --- (Keep existing logging, Flask app setup) ---
app = Flask(__name__)
# Required for session management
app.secret_key = os.urandom(24) # Or set a fixed secret in production

# --- (Keep existing queue, status list, lock, DOWNLOAD_FOLDER setup) ---
download_queue = queue.Queue()
download_status_list = []
status_lock = threading.Lock()
DOWNLOAD_FOLDER = "/app/downloads"
os.makedirs(DOWNLOAD_FOLDER, exist_ok=True)

# --- (Keep existing helper functions: format_selector, format_filesize, update_status, download_worker, hook, post_hook) ---

# --- Helper to extract Video ID ---
def extract_video_id(text):
    """ Extracts YouTube video ID from URL or plain ID string. """
    # Regex patterns
    # Source: https://stackoverflow.com/a/7936523/1307658
    patterns = [
        r'(?:https?:\/\/)?(?:www\.)?(?:youtube\.com|youtu\.be)\/(?:watch\?v=)?([\w-]{11})', # Standard URLs
        r'^([\w-]{11})$' # Plain ID
    ]
    for pattern in patterns:
        match = re.search(pattern, text.strip())
        if match:
            return match.group(1) # Return the 11-character ID
    return None

# --- Modified Index Route ---
@app.route('/')
def index():
    """ Render the main page with multi-URL input """
    with status_lock:
        current_status = list(download_status_list)
    # Clear any leftover batch from session if user navigates back home
    session.pop('batch_urls', None)
    session.pop('batch_title', None)
    return render_template('index.html', download_status=current_status)

# --- New Route to Process Batch Input ---
@app.route('/process_batch', methods=['POST'])
def process_batch():
    """ Parses the multi-URL input and prepares the batch configuration. """
    raw_input = request.form.get('urls_input', '')
    lines = raw_input.strip().splitlines()
    batch_urls_data = [] # Will store {'original': str, 'id': str, 'url': str, 'status': 'pending', 'formats': None, 'title': None}

    if not lines:
        flash("Please paste at least one YouTube URL or Video ID.", "warning")
        return redirect(url_for('index'))

    processed_ids = set() # To avoid duplicates

    for line in lines:
        line = line.strip()
        if not line:
            continue

        video_id = extract_video_id(line)

        if video_id and video_id not in processed_ids:
            processed_ids.add(video_id)
            batch_urls_data.append({
                'original': line,
                'id': video_id,
                'url': f"https://www.youtube.com/watch?v={video_id}",
                'status': 'pending', # Statuses: pending, configured, error
                'selected_formats': None, # Will store {'video': 'id', 'audio': 'id'}
                'title': None, # Will be fetched later
                'error': None
            })
        elif video_id in processed_ids:
             flash(f"Skipped duplicate video ID: {video_id}", "info")
        else:
            flash(f"Skipped invalid input: {line}", "warning")

    if not batch_urls_data:
        flash("No valid YouTube URLs or Video IDs found in the input.", "error")
        return redirect(url_for('index'))

    # Store batch data in session (simple approach)
    session['batch_urls'] = batch_urls_data
    session['batch_title'] = f"Batch of {len(batch_urls_data)} videos" # Optional title

    logging.info(f"Created batch with {len(batch_urls_data)} potential videos.")
    # Redirect to the configuration page
    return redirect(url_for('configure_batch'))

# --- New Route to Configure Batch ---
@app.route('/configure_batch')
def configure_batch():
    """ Displays the list of videos in the batch for format selection. """
    batch_urls = session.get('batch_urls')
    if not batch_urls:
        flash("No batch found or session expired.", "error")
        return redirect(url_for('index'))

    return render_template('configure_batch.html',
                           batch_urls=batch_urls,
                           batch_title=session.get('batch_title', 'Configure Batch'))

# --- Modified Route to Fetch Formats (now handles batch context) ---
@app.route('/select_formats_for_batch/<int:index>')
def select_formats_for_batch(index):
    """ Fetch and display formats for a specific video within the batch. """
    batch_urls = session.get('batch_urls')
    if not batch_urls or index >= len(batch_urls):
        flash("Batch data not found or invalid index.", "error")
        return redirect(url_for('configure_batch'))

    item = batch_urls[index]
    url = item['url']
    title = item.get('title', 'Loading title...') # Use stored title if available

    logging.info(f"Fetching formats for batch item {index}: {url}")
    try:
        # --- Reusing format fetching logic ---
        ydl_opts = {'quiet': True, 'listformats': False}
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            formats = info.get('formats', [])
            fetched_title = info.get('title', 'Unknown Title')
            item['title'] = fetched_title # Update title in session batch data

        video_formats = []
        audio_formats = []
        for f in formats:
            if not f.get('format_id'): continue
            f['filesize_approx'] = format_filesize(f.get('filesize') or f.get('filesize_approx'))
            if f.get('vcodec') != 'none' and f.get('resolution') != 'audio only':
                if f.get('acodec') == 'none' or f.get('ext') in ['mp4', 'webm', 'mkv']:
                    video_formats.append(f)
            if f.get('acodec') != 'none' and f.get('vcodec') == 'none':
                audio_formats.append(f)

        video_formats.sort(key=lambda x: (x.get('height') or 0, x.get('fps') or 0, x.get('tbr') or 0), reverse=True)
        audio_formats.sort(key=lambda x: x.get('abr') or 0, reverse=True)
        # --- End of reused logic ---

        logging.info(f"Found formats for batch item {index}")
        # Update session immediately with fetched title
        session['batch_urls'] = batch_urls
        session.modified = True

        return render_template('select_formats.html',
                               url=url,
                               title=fetched_title,
                               video_formats=video_formats,
                               audio_formats=audio_formats,
                               error=None,
                               # Add batch context for the form action
                               batch_mode=True,
                               batch_index=index)

    except yt_dlp.utils.DownloadError as e:
        logging.error(f"Error fetching formats for batch item {index} ({url}): {e}")
        item['status'] = 'error'
        item['error'] = str(e)
        session['batch_urls'] = batch_urls # Update session with error status
        session.modified = True
        flash(f"Error fetching formats for {url}: {e}", "error")
        return redirect(url_for('configure_batch')) # Go back to batch list on error
    except Exception as e:
        logging.error(f"Unexpected error fetching formats for batch item {index} ({url}): {e}", exc_info=True)
        item['status'] = 'error'
        item['error'] = f"Unexpected error: {e}"
        session['batch_urls'] = batch_urls # Update session with error status
        session.modified = True
        flash(f"Unexpected error fetching formats for {url}: {e}", "error")
        return redirect(url_for('configure_batch'))

# --- New Route to Save Selected Formats for a Batch Item ---
@app.route('/save_formats_for_batch/<int:index>', methods=['POST'])
def save_formats_for_batch(index):
    """ Saves the selected formats for one video back into the session batch. """
    batch_urls = session.get('batch_urls')
    if not batch_urls or index >= len(batch_urls):
        flash("Batch data not found or invalid index.", "error")
        return redirect(url_for('index')) # Go home if session lost

    video_format = request.form.get('video_format')
    audio_format = request.form.get('audio_format')

    if video_format == 'none' and audio_format == 'none':
        flash("You must select at least one format (video or audio).", "warning")
        # Redirect back to the selection page for the same item
        return redirect(url_for('select_formats_for_batch', index=index))

    # Update the specific item in the batch list
    batch_urls[index]['selected_formats'] = {
        'video': video_format,
        'audio': audio_format
    }
    batch_urls[index]['status'] = 'configured'
    batch_urls[index]['error'] = None # Clear previous error if re-configuring

    # Save changes back to session
    session['batch_urls'] = batch_urls
    session.modified = True # Important: Mark session as modified

    logging.info(f"Saved formats for batch item {index}: v={video_format}, a={audio_format}")
    flash(f"Formats selected for '{batch_urls[index]['title'] or batch_urls[index]['id']}'.", "success")
    # Redirect back to the main batch configuration page
    return redirect(url_for('configure_batch'))

# --- New Route to Queue the Entire Configured Batch ---
@app.route('/queue_batch', methods=['POST'])
def queue_batch():
    """ Adds all 'configured' videos from the session batch to the download queue. """
    batch_urls = session.get('batch_urls')
    if not batch_urls:
        flash("No batch found to queue or session expired.", "error")
        return redirect(url_for('index'))

    queued_count = 0
    skipped_count = 0
    for item in batch_urls:
        if item['status'] == 'configured' and item['selected_formats']:
            job_id = str(uuid.uuid4())[:8]
            job = {
                'id': job_id,
                'url': item['url'],
                'title': item.get('title', 'Loading title...'),
                'status': 'queued',
                'error': None,
                'video_format': item['selected_formats']['video'],
                'audio_format': item['selected_formats']['audio'],
            }
            # Add to persistent status list and queue
            with status_lock:
                download_status_list.append(job)
            download_queue.put(job)
            logging.info(f"Added batch job {job_id} to queue: {item['url']} (v:{job['video_format']}, a:{job['audio_format']})")
            queued_count += 1
        else:
            skipped_count += 1
            logging.info(f"Skipping batch item (not configured or error): {item['url']}")

    # Clear the batch from the session after queuing
    session.pop('batch_urls', None)
    session.pop('batch_title', None)

    if queued_count > 0:
        flash(f"Successfully added {queued_count} videos to the download queue.", "success")
    if skipped_count > 0:
         flash(f"Skipped {skipped_count} videos (not configured or had errors).", "info")
    if queued_count == 0 and skipped_count > 0:
         flash("No videos were configured for download in the batch.", "warning")

    return redirect(url_for('index'))

def update_status(job_id, new_status, error_message=None, title=None):
    """ Safely update the status of a download job """
    with status_lock: # Use the lock to prevent race conditions
        for item in download_status_list:
            if item['id'] == job_id:
                item['status'] = new_status
                if error_message:
                    item['error'] = str(error_message)
                # Update title only if it's provided and wasn't set before or was 'Loading title...'
                if title and (not item.get('title') or item.get('title') == 'Loading title...'):
                     item['title'] = title
                logging.info(f"Status updated for {job_id}: {new_status} - Title: {item.get('title', 'N/A')}")
                break
        else:
            # This case should ideally not happen if the job was added correctly
            logging.warning(f"Attempted to update status for unknown job_id: {job_id}")

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

def format_selector(ctx):
    """ Select the specific video and audio formats requested by the user. """
    # formats are filtered by yt-dlp based on the initial 'format' string
    # (like 'bestvideo+bestaudio/best') before reaching the selector.
    # We just need to pick the exact IDs passed in the context.
    formats = ctx.get('formats')[::-1] # Reverse to potentially check better formats first if needed

    # Get the format IDs requested via the 'requested_formats' key in ydl_opts
    req_video_format_id = None
    req_audio_format_id = None
    if 'requested_formats' in ctx:
        for req_fmt in ctx['requested_formats']:
            if 'format_id' in req_fmt:
                # Simple check: assume first is video, second is audio based on how we add them
                if req_video_format_id is None:
                    req_video_format_id = req_fmt['format_id']
                else:
                    req_audio_format_id = req_fmt['format_id']

    logging.debug(f"Format selector called. Requested video ID: {req_video_format_id}, audio ID: {req_audio_format_id}")
    logging.debug(f"Available formats for selection: {[f.get('format_id') for f in formats]}")


    best_video = None
    best_audio = None

    # Find the exact requested video format
    if req_video_format_id and req_video_format_id != 'none':
        for f in formats:
            # Ensure it has video
            if f.get('vcodec') != 'none' and f.get('format_id') == req_video_format_id:
                best_video = f
                logging.info(f"Selector found requested video format: {f['format_id']}")
                break # Found the exact one

    # Find the exact requested audio format
    if req_audio_format_id and req_audio_format_id != 'none':
         for f in formats:
             # Ensure it has audio
             if f.get('acodec') != 'none' and f.get('format_id') == req_audio_format_id:
                best_audio = f
                logging.info(f"Selector found requested audio format: {f['format_id']}")
                break # Found the exact one

    # If only one type was requested, yt-dlp might have already filtered
    # If only video requested, best_audio remains None
    # If only audio requested, best_video remains None

    # Fallback logic (optional, could be removed if strict matching is desired)
    # If the exact format wasn't found (e.g., due to yt-dlp filtering),
    # maybe pick the best available of the type? Or just let it fail?
    # For now, we rely on finding the exact match passed in context.
    if req_video_format_id != 'none' and best_video is None:
        logging.warning(f"Requested video format {req_video_format_id} not found among available formats after initial filter.")
        # Optionally, find the best available video format here as a fallback
        # for f in formats: if f.get('vcodec') != 'none': best_video = f; break

    if req_audio_format_id != 'none' and best_audio is None:
         logging.warning(f"Requested audio format {req_audio_format_id} not found among available formats after initial filter.")
         # Optionally, find the best available audio format here as a fallback
         # for f in formats: if f.get('acodec') != 'none': best_audio = f; break


    # Communicate the choice back to yt-dlp
    # It expects a dictionary (or an iterable yielding dictionaries)
    # with 'video' and 'audio' keys.
    yield {
        'video': best_video,
        'audio': best_audio,
    }

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
            format_string = f"{video_format}+{audio_format}"
            if video_format == 'none' and audio_format == 'none':
                 # This case should ideally be prevented by the UI/add logic
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
            }

            # Use custom format selector only if both video and audio are requested and not 'none'
            if video_format != 'none' and audio_format != 'none':
                 # Request best available initially, selector will refine
                 ydl_opts['format'] = 'bestvideo+bestaudio/best'
                 ydl_opts['format_selector'] = format_selector
                 # Pass requested formats to the context for the selector
                 ydl_opts['requested_formats'] = [{'format_id': video_format}, {'format_id': audio_format}]


            logging.info(f"Starting download for {job_id} with options: { {k: v for k, v in ydl_opts.items() if k != 'logger'} }")

            # Create YoutubeDL instance and download
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                # Extract info again to get the final title if not passed reliably
                # Do this *before* download starts to update status early
                info_dict = ydl.extract_info(url, download=False)
                final_title = info_dict.get('title', title)
                update_status(job_id, 'downloading', title=final_title) # Update title if fetched

                # Start the download
                ydl.download([url])

            logging.info(f"Download completed successfully for job {job_id}")
            update_status(job_id, 'completed', title=final_title) # Use final title here too

        except yt_dlp.utils.DownloadError as e:
            logging.error(f"yt-dlp DownloadError for job {job_id}: {e}")
            update_status(job_id, 'failed', error_message=f"Download Error: {e}")
        except ValueError as e: # Catch the specific ValueError we added
             logging.error(f"Configuration error for job {job_id}: {e}")
             update_status(job_id, 'failed', error_message=str(e))
        except Exception as e:
            logging.error(f"Unexpected error for job {job_id}: {e}", exc_info=True)
            update_status(job_id, 'failed', error_message=f"Unexpected Error: {e}")
        finally:
            download_queue.task_done() # Signal that the task is complete

# --- (Keep worker thread start and main execution block) ---
# Start the background worker thread
worker_thread = threading.Thread(target=download_worker, daemon=True)
worker_thread.start()
logging.info("Download worker thread started.")

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=False)
