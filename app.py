import os
import json
import threading
import uuid
import time
import tempfile
from flask import Flask, request, jsonify
import yt_dlp
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

app = Flask(__name__)

GOOGLE_DRIVE_FOLDER_ID = os.environ.get("DRIVE_FOLDER_ID")
GOOGLE_CREDENTIALS_JSON = os.environ.get("GOOGLE_CREDENTIALS_JSON")
YOUTUBE_COOKIES = os.environ.get("YOUTUBE_COOKIES")

JOBS = {}

def get_drive_service():
    if not GOOGLE_CREDENTIALS_JSON:
        raise ValueError("שגיאה: משתנה GOOGLE_CREDENTIALS_JSON חסר!")
    creds_dict = json.loads(GOOGLE_CREDENTIALS_JSON)
    creds = service_account.Credentials.from_service_account_info(
        creds_dict, scopes=["https://www.googleapis.com/auth/drive.file"]
    )
    return build("drive", "v3", credentials=creds)

def write_cookie_file():
    if not YOUTUBE_COOKIES:
        return None
    cookie_file_path = "/tmp/youtube_cookies.txt"
    with open(cookie_file_path, "w", encoding="utf-8") as f:
        f.write(YOUTUBE_COOKIES)
    return cookie_file_path

def update_download_progress(d, job_id):
    if d['status'] == 'downloading':
        total = d.get('total_bytes') or d.get('total_bytes_estimate', 0)
        downloaded = d.get('downloaded_bytes', 0)
        if total > 0:
            pct = int(downloaded / total * 50)
            JOBS[job_id]['progress'] = pct
            JOBS[job_id]['status'] = f'מוריד... {pct * 2}%'
    elif d['status'] == 'finished':
        JOBS[job_id]['status'] = 'מעלה לדרייב...'
        JOBS[job_id]['progress'] = 50

def download_video(youtube_url, output_path, job_id):
    cookie_file_path = write_cookie_file()

    ydl_opts = {
        'format': 'best',
        'outtmpl': output_path,
        'quiet': True,
        'no_warnings': True,
        'nocheckcertificate': True,
        'progress_hooks': [lambda d: update_download_progress(d, job_id)],
    }

    if cookie_file_path:
        ydl_opts['cookiefile'] = cookie_file_path

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(youtube_url, download=True)
            return info.get('title', 'video')
    finally:
        if cookie_file_path and os.path.exists(cookie_file_path):
            os.remove(cookie_file_path)

def process_video_background(job_id, youtube_url):
    tmp_dir = tempfile.mkdtemp()
    tmp_template = os.path.join(tmp_dir, f"{job_id}.%(ext)s")
    actual_file = None

    try:
        service = get_drive_service()

        JOBS[job_id]['status'] = 'מוריד מיוטיוב...'
        JOBS[job_id]['progress'] = 0

        title = download_video(youtube_url, tmp_template, job_id)

        # מצא את הקובץ שנוצר
        for f in os.listdir(tmp_dir):
            if f.startswith(job_id):
                actual_file = os.path.join(tmp_dir, f)
                ext = f.split('.')[-1]
                file_name = f"{title}.{ext}"
                break

        if not actual_file or not os.path.exists(actual_file):
            raise FileNotFoundError("הקובץ לא נמצא לאחר ההורדה")

        JOBS[job_id]['file_name'] = file_name
        JOBS[job_id]['status'] = 'מעלה לדרייב...'
        JOBS[job_id]['progress'] = 50

        file_metadata = {
            'name': file_name,
            'parents': [GOOGLE_DRIVE_FOLDER_ID] if GOOGLE_DRIVE_FOLDER_ID else []
        }

        mimetype = 'video/mp4' if file_name.endswith('.mp4') else 'video/webm'
        media = MediaFileUpload(
            actual_file,
            mimetype=mimetype,
            chunksize=5 * 1024 * 1024,
            resumable=True
        )

        request_upload = service.files().create(
            body=file_metadata,
            media_body=media,
            fields='id'
        )

        response_upload = None
        retries = 0
        MAX_RETRIES = 5

        while response_upload is None:
            try:
                status, response_upload = request_upload.next_chunk()
                if status:
                    upload_pct = int(status.progress() * 50) + 50
                    JOBS[job_id]['progress'] = upload_pct
                    JOBS[job_id]['status'] = f'מעלה לדרייב... {upload_pct}%'
                    retries = 0
            except Exception as chunk_error:
                retries += 1
                if retries > MAX_RETRIES:
                    raise Exception(f"ההעלאה נכשלה לאחר {MAX_RETRIES} ניסיונות: {str(chunk_error)}")
                time.sleep(2 ** retries)

        file_id = response_upload.get('id')
        JOBS[job_id]['status'] = 'הסתיים בהצלחה ✅'
        JOBS[job_id]['drive_file_id'] = file_id
        JOBS[job_id]['drive_link'] = f"https://drive.google.com/file/d/{file_id}/view"
        JOBS[job_id]['progress'] = 100

    except Exception as e:
        JOBS[job_id]['status'] = 'שגיאה ❌'
        JOBS[job_id]['error'] = str(e)

    finally:
        if actual_file and os.path.exists(actual_file):
            os.remove(actual_file)
        try:
            os.rmdir(tmp_dir)
        except:
            pass

@app.route('/health', methods=['GET'])
def health():
    return jsonify({
        "status": "השרת פועל וממתין",
        "yt_dlp_version": yt_dlp.version.__version__,
        "cookies_loaded": bool(YOUTUBE_COOKIES)
    }), 200

@app.route('/download', methods=['POST'])
def start_download():
    data = request.get_json()
    if not data or 'url' not in data:
        return jsonify({"error": "חסר פרמטר 'url'"}), 400

    url = data['url']
    job_id = str(uuid.uuid4())
    JOBS[job_id] = {
        'status': 'ממתין להתחלה',
        'progress': 0,
        'url': url
    }

    thread = threading.Thread(target=process_video_background, args=(job_id, url))
    thread.daemon = True
    thread.start()

    return jsonify({
        "message": "התהליך התחיל לרוץ ברקע",
        "job_id": job_id,
        "status_url": f"/status/{job_id}"
    }), 202

@app.route('/status/<job_id>', methods=['GET'])
def get_status(job_id):
    job_info = JOBS.get(job_id)
    if not job_info:
        return jsonify({"error": "לא נמצאה עבודה כזו"}), 404
    return jsonify(job_info), 200

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)
