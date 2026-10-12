"""Persistent local analysis history. One connection per operation/thread."""
import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, path):
        self.path = str(path)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY, filename TEXT NOT NULL,
                    source_path TEXT NOT NULL, owned_file INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'queued', created_at TEXT NOT NULL,
                    started_at TEXT, finished_at TEXT, settings TEXT NOT NULL,
                    stage TEXT NOT NULL DEFAULT 'In wachtrij',
                    completed_steps INTEGER NOT NULL DEFAULT 0,
                    total_steps INTEGER NOT NULL, duration REAL NOT NULL DEFAULT 0,
                    cancel_requested INTEGER NOT NULL DEFAULT 0,
                    error TEXT, warning TEXT
                );
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
                    type TEXT NOT NULL, start_sec REAL NOT NULL, end_sec REAL NOT NULL,
                    details TEXT NOT NULL, review TEXT NOT NULL DEFAULT 'pending'
                        CHECK(review IN ('pending', 'confirmed', 'dismissed'))
                );
                CREATE INDEX IF NOT EXISTS events_job ON events(job_id);
                CREATE INDEX IF NOT EXISTS jobs_status ON jobs(status, created_at);
            """)

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            with db:
                yield db
        finally:
            db.close()

    def create_job(self, filename, source_path, settings, owned_file=False):
        job_id = uuid.uuid4().hex
        with self.connection() as db:
            db.execute("""INSERT INTO jobs
                (id, filename, source_path, owned_file, created_at, settings, total_steps)
                VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (job_id, filename, str(Path(source_path).resolve()), int(owned_file), now(),
                 json.dumps(settings), len(settings["detectors"]) + 1))
        return job_id

    @staticmethod
    def decode(row):
        if row is None:
            return None
        result = dict(row)
        result["settings"] = json.loads(result["settings"])
        return result

    def get_job(self, job_id):
        with self.connection() as db:
            return self.decode(db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())

    def list_jobs(self):
        with self.connection() as db:
            return [self.decode(r) for r in db.execute("SELECT * FROM jobs ORDER BY created_at DESC, rowid DESC")]

    def jobs_for_source(self, path):
        with self.connection() as db:
            return [self.decode(r) for r in db.execute("SELECT * FROM jobs WHERE source_path=?", (str(Path(path).resolve()),))]

    def events(self, job_id):
        with self.connection() as db:
            return [dict(r) for r in db.execute(
                "SELECT * FROM events WHERE job_id=? ORDER BY start_sec, end_sec, id", (job_id,))]

    def claim_next(self):
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM jobs WHERE status='queued' ORDER BY created_at, rowid LIMIT 1").fetchone()
            if row is None:
                return None
            db.execute("UPDATE jobs SET status='running', started_at=?, stage='Video controleren' WHERE id=?",
                       (now(), row["id"]))
            return row["id"]

    def progress(self, job_id, stage, completed_steps, duration=None, warning=None):
        with self.connection() as db:
            db.execute("""UPDATE jobs SET stage=?, completed_steps=?,
                duration=COALESCE(?, duration), warning=COALESCE(?, warning)
                WHERE id=? AND status='running'""", (stage, completed_steps, duration, warning, job_id))

    def finish(self, job_id, events):
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            job = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if job is None or job["status"] != "running":
                return
            if job["cancel_requested"]:
                db.execute("UPDATE jobs SET status='cancelled', stage='Geannuleerd', finished_at=? WHERE id=?",
                           (now(), job_id))
                return
            for event in events:
                start = max(0.0, min(float(event["start_sec"]), job["duration"]))
                end = max(start, min(float(event["end_sec"]), job["duration"]))
                if end > start:
                    db.execute("""INSERT INTO events (job_id, type, start_sec, end_sec, details)
                        VALUES (?, ?, ?, ?, ?)""", (job_id, event["type"], start, end, event["details"]))
            db.execute("""UPDATE jobs SET status='completed', stage='Klaar voor beoordeling',
                completed_steps=total_steps, finished_at=? WHERE id=?""", (now(), job_id))

    def fail(self, job_id, error):
        with self.connection() as db:
            db.execute("""UPDATE jobs SET status=CASE WHEN cancel_requested THEN 'cancelled' ELSE 'failed' END,
                stage=CASE WHEN cancel_requested THEN 'Geannuleerd' ELSE 'Analyse mislukt' END,
                error=CASE WHEN cancel_requested THEN NULL ELSE ? END, finished_at=?
                WHERE id=? AND status='running'""", (error, now(), job_id))

    def cancel(self, job_id):
        with self.connection() as db:
            db.execute("""UPDATE jobs SET cancel_requested=1,
                status=CASE WHEN status='queued' THEN 'cancelled' ELSE status END,
                stage=CASE WHEN status='queued' THEN 'Geannuleerd' ELSE 'Annuleren…' END,
                finished_at=CASE WHEN status='queued' THEN ? ELSE finished_at END
                WHERE id=? AND status IN ('queued','running')""", (now(), job_id))

    def recover_interrupted(self):
        with self.connection() as db:
            db.execute("""UPDATE jobs SET status='failed', stage='Onderbroken', finished_at=?,
                error='De app is gestopt tijdens deze analyse. Start de analyse opnieuw.'
                WHERE status='running'""", (now(),))

    def review(self, job_id, event_id, value):
        with self.connection() as db:
            return db.execute("UPDATE events SET review=? WHERE id=? AND job_id=?",
                              (value, event_id, job_id)).rowcount == 1

    def delete(self, job_id):
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            job = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if job is None:
                return None
            if job["status"] in ("queued", "running"):
                raise ValueError("Annuleer eerst de lopende analyse.")
            db.execute("DELETE FROM jobs WHERE id=?", (job_id,))
            references = db.execute("SELECT COUNT(*) FROM jobs WHERE source_path=?", (job["source_path"],)).fetchone()[0]
            return job["source_path"] if job["owned_file"] and references == 0 else None


def covered_seconds(events):
    intervals = sorted((e["start_sec"], e["end_sec"]) for e in events)
    total, end = 0.0, 0.0
    for start, stop in intervals:
        total += max(0.0, stop - max(start, end))
        end = max(end, stop)
    return total


def summarize(events, duration):
    pending = sum(e["review"] == "pending" for e in events)
    suspected = covered_seconds([e for e in events if e["review"] != "dismissed"])
    confirmed = covered_seconds([e for e in events if e["review"] == "confirmed"])
    return {"event_count": len(events), "pending_count": pending,
            "confirmed_count": sum(e["review"] == "confirmed" for e in events),
            "dismissed_count": sum(e["review"] == "dismissed" for e in events),
            "suspected_seconds": suspected, "confirmed_seconds": confirmed,
            "suspected_percent": suspected / duration * 100 if duration else 0,
            "confirmed_percent": confirmed / duration * 100 if duration else 0}
