"""A persistent queue with one active analysis, suitable for the local QAS app."""
import atexit
import fcntl
import os
import signal
import subprocess
import sys
import threading
from pathlib import Path


class JobRunner:
    def __init__(self, store, data_dir):
        self.store = store
        self.data_dir = Path(data_dir)
        self.stop_event = threading.Event()
        self.thread = None
        self.lock_file = None

    def start(self):
        self.lock_file = (self.data_dir / "runner.lock").open("a")
        try:
            fcntl.flock(self.lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.lock_file.close()
            self.lock_file = None
            return
        self.store.recover_interrupted()
        self.thread = threading.Thread(target=self.run, name="analysis-queue", daemon=True)
        self.thread.start()
        atexit.register(self.close)

    def close(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=8)
        if self.lock_file and (not self.thread or not self.thread.is_alive()):
            self.lock_file.close()
            self.lock_file = None

    @staticmethod
    def terminate(process):
        try:
            os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=3)
        except ProcessLookupError:
            pass
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()

    def run(self):
        while not self.stop_event.is_set():
            job_id = self.store.claim_next()
            if not job_id:
                self.stop_event.wait(0.5)
                continue
            process = None
            try:
                logs = self.data_dir / "logs"
                logs.mkdir(exist_ok=True)
                with (logs / (job_id + ".log")).open("ab") as output:
                    process = subprocess.Popen(
                        [sys.executable, "-B", str(Path(__file__).with_name("analysis_worker.py")), self.store.path, job_id],
                        stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT,
                        start_new_session=True)
                    while process.poll() is None:
                        job = self.store.get_job(job_id)
                        if self.stop_event.is_set() or not job or job["cancel_requested"]:
                            self.terminate(process)
                            break
                        self.stop_event.wait(0.25)
                job = self.store.get_job(job_id)
                if job and job["status"] == "running":
                    self.store.fail(job_id, "De verwerking is onderbroken. Start de analyse opnieuw.")
            except Exception:
                if process and process.poll() is None:
                    self.terminate(process)
                self.store.fail(job_id, "De analyse kon niet worden gestart. Controleer de lokale installatie.")
