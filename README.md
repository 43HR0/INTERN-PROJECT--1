# StreamBox — Video Streaming Platform

A Flask + SQLite video library with authentication, uploads, FFmpeg processing, HTML5 playback, local storage, and optional AWS S3 storage.

## Requirements

- Python 3.10+
- FFmpeg (recommended; required for transcoding)
- AWS account and an S3 bucket only if you want cloud storage

## Run locally (Windows PowerShell)

```powershell
cd streambox
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
```

Edit `.env` and set a long random `SECRET_KEY`. Keep `STORAGE_BACKEND=local` for the first run.

Install FFmpeg and make sure `ffmpeg -version` works in PowerShell. Then run:

```powershell
py app.py
```

Open http://127.0.0.1:5000 and create an account.

## Run locally (Linux / macOS)

```bash
cd streambox
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# Edit .env and set SECRET_KEY
python app.py
```

## AWS S3 setup

1. Create a private S3 bucket in your preferred region.
2. Create an IAM identity with only the required `s3:PutObject`, `s3:GetObject`, and `s3:DeleteObject` permissions scoped to that bucket.
3. Configure AWS credentials using an IAM role (preferred) or the AWS CLI credential chain. Do not put AWS keys in source code or commit them.
4. In `.env`, set:
   ```
   STORAGE_BACKEND=s3
   S3_BUCKET=your-private-bucket
   AWS_REGION=ap-south-1
   ```
5. Restart the app. S3 playback uses short-lived presigned URLs.

## How it works

- Flask handles registration, login, upload, dashboard, and playback routes.
- SQLite stores users and video metadata.
- FFmpeg converts uploaded videos to H.264/AAC MP4 and moves metadata-friendly MP4 output to the front for faster playback.
- Local mode stores files in `storage/`; S3 mode stores them in a private bucket.
- Local playback uses Flask's conditional file response, which supports HTTP range requests for seeking.
- S3 playback uses a 1-hour presigned URL.

## Important limitations before production

This is a runnable learning/MVP project, not a hardened public streaming service.

- Video conversion currently runs during the upload request. For larger workloads, move processing to a background worker (Celery/RQ) and show queued/processing/failed states.
- The current player uses progressive MP4 streaming, not adaptive HLS/DASH. A production service should generate multiple renditions and HLS playlists.
- Add CSRF protection, rate limiting, email verification, password reset, malware scanning, quotas, and stronger operational logging before public deployment.
- Use HTTPS, secure cookie settings, a production WSGI server, and a reverse proxy.
- S3 bucket should remain private. Presigned URLs are temporary bearer links.
- For large uploads, implement multipart/resumable uploads rather than sending the whole file through Flask.
- The local development server is not intended for production.

## Project structure

```text
streambox/
├── app.py
├── requirements.txt
├── .env.example
├── README.md
├── templates/
│   ├── base.html
│   ├── landing.html
│   ├── login.html
│   ├── register.html
│   ├── dashboard.html
│   └── watch.html
├── static/
│   └── style.css
├── storage/          # created automatically; ignored by Git
└── tmp/              # temporary processing files
```
