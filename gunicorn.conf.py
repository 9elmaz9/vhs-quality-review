"""One process owns the SQLite-backed queue; request threads remain responsive."""
import os

bind = "0.0.0.0:" + os.environ.get("PORT", "10000")
workers = 1
worker_class = "gthread"
threads = 4
timeout = 300
graceful_timeout = 30
preload_app = False
accesslog = "-"
errorlog = "-"


def worker_exit(server, worker):
    app = getattr(worker, "wsgi", None)
    runner = getattr(app, "extensions", {}).get("runner")
    if runner:
        runner.close()
