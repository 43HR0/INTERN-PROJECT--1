import os
import sqlite3
import subprocess
import tempfile
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path

import boto3
from botocore.exceptions import BotoCoreError, ClientError
from flask import (
    Flask, abort, flash, redirect, render_template, request, send_file,
    session, url_for
)
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

BASE_DIR = Path(__file__).resolve().parent
DATABASE = BASE_DIR / "streambox.db"
LOCAL_STORAGE = BASE_DIR / "storage"
TEMP_DIR = BASE_DIR / "tmp"
ALLOWED_EXTENSIONS = {"mp4", "mov", "mkv", "webm", "avi"}
MAX_UPLOAD_MB = int(os.getenv("MAX_UPLOAD_MB", "1024"))

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "change-this-development-secret")
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_MB * 1024 * 1024

STORAGE_BACKEND = os.getenv("STORAGE_BACKEND", "local").lower()
app.config["STORAGE_BACKEND"] = STORAGE_BACKEND
S3_BUCKET = os.getenv("S3_BUCKET", "")
AWS_REGION = os.getenv("AWS_REGION", "ap-south-1")

LOCAL_STORAGE.mkdir(exist_ok=True)
TEMP_DIR.mkdir(exist_ok=True)


def db():
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    with db() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS videos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            title TEXT NOT NULL,
            description TEXT DEFAULT '',
            filename TEXT NOT NULL,
            storage_key TEXT NOT NULL,
            storage_backend TEXT NOT NULL,
            size_bytes INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'ready',
            created_at TEXT NOT NULL
        );
        """)


def current_user():
    user_id = session.get("user_id")
    if not user_id:
        return None
    with db() as conn:
        return conn.execute("SELECT id, username FROM users WHERE id = ?", (user_id,)).fetchone()


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not current_user():
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)
    return wrapped


def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


def s3_client():
    return boto3.client("s3", region_name=AWS_REGION)


def save_object(local_path: Path, key: str):
    if STORAGE_BACKEND == "s3":
        if not S3_BUCKET:
            raise RuntimeError("S3_BUCKET must be set when STORAGE_BACKEND=s3")
        s3_client().upload_file(str(local_path), S3_BUCKET, key)
    else:
        destination = LOCAL_STORAGE / key
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(local_path.read_bytes())


def delete_object(key: str, backend: str):
    if backend == "s3":
        s3_client().delete_object(Bucket=S3_BUCKET, Key=key)
    else:
        path = LOCAL_STORAGE / key
        if path.exists():
            path.unlink()


def stream_url(video):
    if video["storage_backend"] == "s3":
        return s3_client().generate_presigned_url(
            "get_object",
            Params={"Bucket": S3_BUCKET, "Key": video["storage_key"]},
            ExpiresIn=3600,
        )
    return url_for("stream_video", video_id=video["id"])


def process_video(source: Path, output: Path):
    """Transcode to browser-friendly H.264/AAC MP4 when FFmpeg is installed."""
    command = [
        os.getenv("FFMPEG_BINARY", "ffmpeg"), "-y", "-i", str(source),
        "-c:v", "libx264", "-preset", "fast", "-crf", "23",
        "-c:a", "aac", "-b:a", "128k",
        "-movflags", "+faststart", str(output),
    ]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=7200)
        if result.returncode != 0:
            raise RuntimeError(result.stderr[-1500:] or "FFmpeg failed")
        return output
    except FileNotFoundError:
        # Keep the original upload if FFmpeg is not installed.
        return source


@app.errorhandler(413)
def too_large(_error):
    flash(f"That file is too large. Maximum upload size is {MAX_UPLOAD_MB} MB.", "error")
    return redirect(url_for("dashboard"))


@app.route("/")
def index():
    if current_user():
        return redirect(url_for("dashboard"))
    return render_template("landing.html")


@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        if len(username) < 3 or len(password) < 8:
            flash("Use a username of at least 3 characters and a password of at least 8.", "error")
            return render_template("register.html")
        try:
            with db() as conn:
                cur = conn.execute(
                    "INSERT INTO users(username, password_hash, created_at) VALUES (?, ?, ?)",
                    (username, generate_password_hash(password), datetime.now(timezone.utc).isoformat()),
                )
                session["user_id"] = cur.lastrowid
            return redirect(url_for("dashboard"))
        except sqlite3.IntegrityError:
            flash("That username is already taken.", "error")
    return render_template("register.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        with db() as conn:
            user = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
        if user and check_password_hash(user["password_hash"], password):
            session.clear()
            session["user_id"] = user["id"]
            return redirect(url_for("dashboard"))
        flash("Incorrect username or password.", "error")
    return render_template("login.html")


@app.route("/logout", methods=["POST"])
@login_required
def logout():
    session.clear()
    return redirect(url_for("index"))


@app.route("/dashboard")
@login_required
def dashboard():
    user = current_user()
    with db() as conn:
        videos = conn.execute(
            "SELECT * FROM videos WHERE user_id = ? ORDER BY created_at DESC", (user["id"],)
        ).fetchall()
    total_bytes = sum(v["size_bytes"] for v in videos)
    return render_template("dashboard.html", user=user, videos=videos, total_bytes=total_bytes)


@app.route("/upload", methods=["POST"])
@login_required
def upload():
    user = current_user()
    uploaded = request.files.get("video")
    title = request.form.get("title", "").strip()
    description = request.form.get("description", "").strip()

    if not uploaded or not uploaded.filename:
        flash("Choose a video file first.", "error")
        return redirect(url_for("dashboard"))
    if not allowed_file(uploaded.filename):
        flash("Unsupported format. Use MP4, MOV, MKV, WEBM or AVI.", "error")
        return redirect(url_for("dashboard"))

    original_name = secure_filename(uploaded.filename)
    title = title or Path(original_name).stem
    suffix = Path(original_name).suffix.lower()
    with tempfile.TemporaryDirectory(dir=TEMP_DIR) as work_dir:
        work = Path(work_dir)
        source = work / f"source{suffix}"
        output = work / "processed.mp4"
        uploaded.save(source)
        try:
            processed = process_video(source, output)
            key = f"users/{user['id']}/{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S%f')}{processed.suffix.lower()}"
            save_object(processed, key)
            size = processed.stat().st_size
            with db() as conn:
                conn.execute("""
                    INSERT INTO videos(user_id, title, description, filename, storage_key,
                                       storage_backend, size_bytes, status, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, 'ready', ?)
                """, (user["id"], title, description, original_name, key,
                      STORAGE_BACKEND, size, datetime.now(timezone.utc).isoformat()))
            flash("Video uploaded and ready to watch.", "success")
        except (RuntimeError, BotoCoreError, ClientError, OSError) as exc:
            app.logger.exception("Upload failed")
            flash(f"Upload failed: {str(exc)[:250]}", "error")
    return redirect(url_for("dashboard"))


@app.route("/watch/<int:video_id>")
@login_required
def watch(video_id):
    user = current_user()
    with db() as conn:
        video = conn.execute(
            "SELECT * FROM videos WHERE id = ? AND user_id = ?", (video_id, user["id"])
        ).fetchone()
    if not video:
        abort(404)
    return render_template("watch.html", video=video, playback_url=stream_url(video))


@app.route("/stream/<int:video_id>")
@login_required
def stream_video(video_id):
    user = current_user()
    with db() as conn:
        video = conn.execute(
            "SELECT * FROM videos WHERE id = ? AND user_id = ?", (video_id, user["id"])
        ).fetchone()
    if not video or video["storage_backend"] != "local":
        abort(404)
    path = LOCAL_STORAGE / video["storage_key"]
    if not path.is_file():
        abort(404)
    return send_file(path, conditional=True, etag=True)


@app.route("/delete/<int:video_id>", methods=["POST"])
@login_required
def delete_video(video_id):
    user = current_user()
    with db() as conn:
        video = conn.execute(
            "SELECT * FROM videos WHERE id = ? AND user_id = ?", (video_id, user["id"])
        ).fetchone()
        if not video:
            abort(404)
        delete_object(video["storage_key"], video["storage_backend"])
        conn.execute("DELETE FROM videos WHERE id = ?", (video_id,))
    flash("Video deleted.", "success")
    return redirect(url_for("dashboard"))


if __name__ == "__main__":
    init_db()
    app.run(host="127.0.0.1", port=int(os.getenv("PORT", "5000")), debug=os.getenv("FLASK_DEBUG") == "1")
