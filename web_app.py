"""Interface for uploading, analysing and reviewing VHS recordings."""
import csv
import hashlib
import io
import json
import os
import secrets
import threading
import time
import uuid
from pathlib import Path
from datetime import timedelta

from flask import Flask, abort, jsonify, redirect, render_template, request, send_file, session, url_for
from werkzeug.exceptions import HTTPException
from werkzeug.security import check_password_hash
from werkzeug.middleware.proxy_fix import ProxyFix

from job_runner import JobRunner
from settings import DETECTORS, validate_settings
from storage import Store, summarize
from localization import ENGLISH, LANGUAGES, translate, translate_result

BASE_DIR = Path(__file__).resolve().parent
# Render installs static FFmpeg binaries here; subprocesses inherit this PATH.
os.environ["PATH"] = str(BASE_DIR / "bin") + os.pathsep + os.environ.get("PATH", "")
ALLOWED_EXT = {".mp4", ".mov", ".mkv", ".avi", ".m4v"}


def create_app(config=None):
    app = Flask(__name__)
    production = os.environ.get("APP_ENV") == "production" or os.environ.get("RENDER") == "true"
    app.config.update(DATA_DIR=os.environ.get("APP_DATA_DIR", os.environ.get("QAS_DATA_DIR", str(BASE_DIR / "data"))),
                      LIBRARY_DIR=str(BASE_DIR / "uploads"),
                      START_RUNNER=os.environ.get("QAS_NO_RUNNER") != "1",
                      PRODUCTION=production, PASSWORD_HASH=os.environ.get("APP_PASSWORD_HASH", ""),
                      SESSION_COOKIE_SECURE=production, PERMANENT_SESSION_LIFETIME=timedelta(hours=8),
                      MAX_CONTENT_LENGTH=20 * 1024 ** 3,
                      SESSION_COOKIE_SAMESITE="Strict", SESSION_COOKIE_HTTPONLY=True)
    if config:
        app.config.update(config)
    if app.config["PRODUCTION"] and not app.config["PASSWORD_HASH"]:
        raise RuntimeError("APP_PASSWORD_HASH is required for a production deployment.")
    if app.config["PRODUCTION"] and not os.environ.get("APP_DATA_DIR") and not (config and config.get("DATA_DIR")):
        raise RuntimeError("APP_DATA_DIR must point to persistent storage in production.")
    if app.config["PRODUCTION"]:
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1)
    data_dir = Path(app.config["DATA_DIR"]).resolve()
    media_dir = data_dir / "media"
    media_dir.mkdir(parents=True, exist_ok=True)
    library_dir = Path(app.config["LIBRARY_DIR"]).resolve()
    secret_path = data_dir / ".session-secret"
    if not secret_path.exists():
        try:
            with secret_path.open("x") as handle:
                handle.write(secrets.token_hex(32))
            secret_path.chmod(0o600)
        except FileExistsError:
            pass
    app.secret_key = secret_path.read_text()
    auth_version = hashlib.sha256(app.config["PASSWORD_HASH"].encode()).hexdigest()
    login_attempts = {}
    login_lock = threading.Lock()
    store = Store(data_dir / "history.sqlite3")
    app.extensions["store"] = store
    runner = JobRunner(store, data_dir)
    source_lock = threading.RLock()
    app.extensions["runner"] = runner
    if app.config["START_RUNNER"]:
        runner.start()

    def csrf_token():
        if "csrf" not in session:
            session["csrf"] = secrets.token_hex(24)
        return session["csrf"]

    def language():
        value = request.cookies.get("language", "nl")
        return value if value in LANGUAGES else "nl"

    def localize(message, **values):
        return translate(message, language(), **values)

    @app.context_processor
    def localization_context():
        return {"_": localize, "language": language(), "locale": "en-GB" if language() == "en" else "nl-BE",
                "translations": ENGLISH if language() == "en" else {},
                "auth_enabled": bool(app.config["PASSWORD_HASH"]),
                "environment_label": "TEAM · beveiligde toegang" if app.config["PRODUCTION"] else "QAS · lokale werkkopie"}

    def safe_next(value):
        import re
        return value if isinstance(value, str) and re.fullmatch(r"/(?:login|jobs/[a-f0-9]{32})?", value) else "/"

    @app.before_request
    def protect_changes():
        if (app.config["PASSWORD_HASH"] and request.endpoint not in ("static", "login", "set_language", "health")
                and session.get("auth_version") != auth_version):
            if request.path.startswith(("/api/", "/media/")):
                abort(401, "Log opnieuw in om verder te gaan.")
            return redirect(url_for("login", next=safe_next(request.path)))
        if request.method in ("POST", "PATCH", "DELETE"):
            token = request.headers.get("X-CSRF-Token", "")
            if request.endpoint in ("login", "logout", "set_language"):
                token = request.form.get("csrf_token", token)
            if not token or not secrets.compare_digest(token.encode(), session.get("csrf", "").encode()):
                abort(400, "De pagina is verlopen. Vernieuw de pagina en probeer opnieuw.")

    @app.after_request
    def prevent_private_caching(response):
        if request.endpoint != "static":
            response.headers["Cache-Control"] = "private, no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["X-Frame-Options"] = "SAMEORIGIN"
        return response

    @app.get("/healthz")
    def health():
        with store.connection() as db:
            db.execute("SELECT 1").fetchone()
        return jsonify(status="ok")

    @app.post("/language")
    def set_language():
        selected = request.form.get("language")
        if selected not in LANGUAGES:
            abort(400, "Ongeldige aanvraag.")
        response = redirect(safe_next(request.form.get("return_to")))
        response.set_cookie("language", selected, max_age=365 * 24 * 3600,
                            httponly=True, secure=app.config["SESSION_COOKIE_SECURE"], samesite="Lax")
        return response

    @app.route("/login", methods=["GET", "POST"])
    def login():
        destination = safe_next(request.values.get("next"))
        if destination == "/login":
            destination = "/"
        if not app.config["PASSWORD_HASH"] or session.get("auth_version") == auth_version:
            return redirect(destination)
        error, code = None, 200
        if request.method == "POST":
            address, current = request.remote_addr, time.monotonic()
            with login_lock:
                expired = [key for key, times in login_attempts.items() if not times or times[-1] < current - 300]
                for key in expired:
                    login_attempts.pop(key, None)
                attempts = login_attempts.setdefault(address, [])
                attempts[:] = [at for at in attempts if at > current - 300]
                blocked = len(attempts) >= 10
                if not blocked:
                    attempts.append(current)
            password = request.form.get("password", "")
            if blocked:
                error, code = "Te veel pogingen. Probeer het over enkele minuten opnieuw.", 429
            elif len(password) <= 1024 and check_password_hash(app.config["PASSWORD_HASH"], password):
                with login_lock:
                    login_attempts.pop(address, None)
                session.clear()
                session.permanent = True
                session["auth_version"] = auth_version
                csrf_token()
                return redirect(destination)
            else:
                error, code = "Onjuist wachtwoord.", 401
        return render_template("login.html", csrf=csrf_token(), error=error, next_path=destination), code

    @app.post("/logout")
    def logout():
        session.clear()
        return redirect(url_for("login"))

    @app.errorhandler(HTTPException)
    def http_error(error):
        if request.path.startswith("/api/"):
            message = "De upload is te groot (maximaal 20 GB per aanvraag)." if error.code == 413 else error.description
            return jsonify(error=translate_result(message, language())), error.code
        return error

    def get_job(job_id):
        job = store.get_job(job_id)
        if job is None:
            abort(404, "Deze analyse bestaat niet meer.")
        return job

    def source_file(job):
        path = Path(job["source_path"]).resolve()
        if path.parent not in (media_dir, library_dir) or not path.is_file():
            abort(404, "Het oorspronkelijke videobestand is niet meer beschikbaar.")
        return path

    def serialize(job, include_events=False):
        events = store.events(job["id"])
        result = {key: value for key, value in job.items() if key not in ("source_path", "owned_file")}
        result["summary"] = summarize(events, job["duration"])
        result["media_url"] = url_for("media", job_id=job["id"])
        result["detail_url"] = url_for("detail", job_id=job["id"])
        path = Path(job["source_path"]).resolve()
        result["media_available"] = path.parent in (media_dir, library_dir) and path.is_file()
        for key in ("stage", "error", "warning"):
            result[key] = translate_result(result.get(key), language())
        if include_events:
            for item in events:
                item["details"] = translate_result(item["details"], language())
            result["events"] = events
        return result

    def body_json():
        value = request.get_json(silent=True)
        if not isinstance(value, dict):
            abort(400, "Ongeldige aanvraag.")
        return value

    def settings_from(value):
        try:
            return validate_settings(value)
        except ValueError as exc:
            abort(400, str(exc))

    @app.get("/")
    def index():
        return render_template("index.html", detectors=DETECTORS, csrf=csrf_token(), active_job=None)

    @app.get("/jobs/<job_id>")
    def detail(job_id):
        get_job(job_id)
        return render_template("index.html", detectors=DETECTORS, csrf=csrf_token(), active_job=job_id)

    @app.get("/api/jobs")
    def list_jobs():
        return jsonify(jobs=[serialize(job) for job in store.list_jobs()])

    @app.get("/api/jobs/<job_id>")
    def job_details(job_id):
        return jsonify(serialize(get_job(job_id), include_events=True))

    @app.post("/api/jobs")
    def upload():
        try:
            raw_settings = json.loads(request.form.get("settings", "{}"))
        except (ValueError, TypeError):
            abort(400, "Ongeldige analyse-instellingen.")
        settings = settings_from(raw_settings)
        files = request.files.getlist("videos")
        if not files or any(not f.filename for f in files):
            abort(400, "Selecteer minimaal één video.")
        if any(Path(f.filename).suffix.lower() not in ALLOWED_EXT for f in files):
            abort(400, "Ondersteunde bestanden: MP4, MOV, MKV, AVI en M4V.")
        saved = []
        try:
            for file in files:
                filename = Path(file.filename.replace("\\", "/")).name
                path = media_dir / (uuid.uuid4().hex + Path(filename).suffix.lower())
                saved.append((filename, path))
                file.save(path)
                if path.stat().st_size == 0:
                    abort(400, localize("{name} is leeg.", name=filename))
        except Exception:
            for _, path in saved:
                path.unlink(missing_ok=True)
            raise
        ids = [store.create_job(name, path, settings, owned_file=True) for name, path in saved]
        return jsonify(job_ids=ids), 202

    @app.get("/api/library")
    def library():
        files = []
        if library_dir.is_dir():
            for path in sorted(library_dir.iterdir(), key=lambda p: p.name.lower()):
                if not path.is_symlink() and path.is_file() and path.resolve().parent == library_dir and path.suffix.lower() in ALLOWED_EXT:
                    files.append({"name": path.name, "size": path.stat().st_size})
        return jsonify(files=files)

    def library_paths(names):
        if not isinstance(names, list) or not names:
            abort(400, "Selecteer minimaal één bestaand bestand.")
        paths = []
        for name in names:
            if not isinstance(name, str) or not name or "\x00" in name or Path(name).name != name:
                abort(400, "Ongeldige bestandsnaam.")
            candidate = library_dir / name
            path = candidate.resolve()
            if candidate.is_symlink() or path.parent != library_dir or not path.is_file() or path.suffix.lower() not in ALLOWED_EXT:
                abort(400, "Het geselecteerde bestand is niet beschikbaar.")
            if path not in paths:
                paths.append(path)
        return paths

    def stop_jobs(job_ids):
        # Cancel queued work immediately, and wait for a running worker to release
        # its file before deleting either the source or the analysis record.
        for job_id in job_ids:
            store.cancel(job_id)
        deadline = time.monotonic() + 8
        while any((store.get_job(job_id) or {}).get("status") == "running" for job_id in job_ids):
            if time.monotonic() >= deadline:
                abort(409, "De analyse wordt nog gestopt. Probeer het verwijderen over enkele seconden opnieuw.")
            time.sleep(0.05)

    @app.delete("/api/library")
    def delete_library_files():
        names = body_json().get("files")
        with source_lock:
            paths = library_paths(names)
            stop_jobs([job["id"] for path in paths for job in store.jobs_for_source(path)])
            for path in paths:
                path.unlink()
        return jsonify(ok=True, deleted=[path.name for path in paths])

    @app.post("/api/jobs/from-library")
    def from_library():
        value = body_json()
        settings = settings_from(value.get("settings", {}))
        with source_lock:
            paths = library_paths(value.get("files"))
            return jsonify(job_ids=[store.create_job(p.name, p, settings) for p in paths]), 202

    @app.post("/api/jobs/<job_id>/cancel")
    def cancel(job_id):
        get_job(job_id)
        store.cancel(job_id)
        return jsonify(serialize(get_job(job_id)))

    @app.post("/api/jobs/<job_id>/retry")
    def retry(job_id):
        with source_lock:
            job = get_job(job_id)
            if job["status"] in ("queued", "running"):
                abort(409, "Deze analyse loopt nog.")
            path = source_file(job)
            new_id = store.create_job(job["filename"], path, job["settings"], job["owned_file"])
        return jsonify(job_ids=[new_id]), 202

    @app.patch("/api/jobs/<job_id>/events/<int:event_id>")
    def review(job_id, event_id):
        job = get_job(job_id)
        if job["status"] != "completed":
            abort(409, "Wacht tot de analyse is afgerond.")
        value = body_json().get("review")
        if value not in ("pending", "confirmed", "dismissed"):
            abort(400, "Ongeldige beoordeling.")
        if not store.review(job_id, event_id, value):
            abort(404, "Deze gebeurtenis bestaat niet.")
        return jsonify(serialize(get_job(job_id), include_events=True))

    @app.delete("/api/jobs/<job_id>")
    def delete(job_id):
        with source_lock:
            get_job(job_id)
            stop_jobs([job_id])
            removable = store.delete(job_id)
            if removable:
                path = Path(removable).resolve()
                if path.parent == media_dir:
                    path.unlink(missing_ok=True)
            (data_dir / "logs" / (job_id + ".log")).unlink(missing_ok=True)
        return jsonify(ok=True)

    @app.get("/media/<job_id>")
    def media(job_id):
        job = get_job(job_id)
        path = source_file(job)
        return send_file(path, conditional=True, as_attachment=request.args.get("download") == "1",
                         download_name=job["filename"])

    def csv_response(rows, name):
        output = io.StringIO(newline="")
        writer = csv.writer(output)
        for row in rows:
            # Prevent filenames or source metadata from becoming spreadsheet formulas.
            writer.writerow(["'" + cell if isinstance(cell, str) and cell.startswith(("=", "+", "-", "@", "\t", "\r")) else cell for cell in row])
        return app.response_class("\ufeff" + output.getvalue(), mimetype="text/csv",
                                  headers={"Content-Disposition": f'attachment; filename="{name}"'})

    def export_events(jobs):
        rows = [["video_file", "analysis_id", "type", "start_seconds", "end_seconds", "duration_seconds", "review", "details"]]
        for job in jobs:
            for e in store.events(job["id"]):
                rows.append([job["filename"], job["id"], e["type"], round(e["start_sec"], 3), round(e["end_sec"], 3),
                             round(e["end_sec"] - e["start_sec"], 3), e["review"], translate_result(e["details"], language())])
        return csv_response(rows, "events.csv")

    @app.get("/api/jobs/<job_id>/events.csv")
    def events_csv(job_id):
        return export_events([get_job(job_id)])

    @app.get("/api/export/events.csv")
    def all_events_csv():
        return export_events(store.list_jobs())

    @app.get("/api/export/summary.csv")
    def summary_csv():
        rows = [["video_file", "analysis_id", "status", "video_duration_seconds", "events", "pending", "confirmed", "dismissed",
                 "suspected_seconds", "suspected_percent", "confirmed_seconds", "confirmed_percent"]]
        for job in store.list_jobs():
            s = summarize(store.events(job["id"]), job["duration"])
            rows.append([job["filename"], job["id"], job["status"], job["duration"], s["event_count"], s["pending_count"],
                         s["confirmed_count"], s["dismissed_count"], round(s["suspected_seconds"], 3),
                         round(s["suspected_percent"], 2), round(s["confirmed_seconds"], 3), round(s["confirmed_percent"], 2)])
        return csv_response(rows, "summary.csv")

    return app


app = create_app()

if __name__ == "__main__":
    import signal
    import sys
    def shutdown(signum, frame):
        app.extensions["runner"].close()
        sys.exit(0)
    signal.signal(signal.SIGTERM, shutdown)
    app.run(host="127.0.0.1", port=int(os.environ.get("PORT", "5009")), debug=False, threaded=True)
