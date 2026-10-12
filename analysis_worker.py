"""One isolated process per recording; cancellation also stops FFmpeg children."""
import sys
import signal
import traceback
import cv2
from analyzer_core import analyze, AnalysisError
from storage import Store


def main():
    # Let context managers remove temporary audio when a running job is cancelled.
    signal.signal(signal.SIGTERM, lambda signum, frame: sys.exit(143))
    store, job_id = Store(sys.argv[1]), sys.argv[2]
    job = store.get_job(job_id)
    if not job or job["status"] != "running":
        return
    cv2.setNumThreads(2)
    try:
        events = analyze(job["source_path"], job["settings"],
                         lambda stage, completed, **kw: store.progress(job_id, stage, completed, **kw))
        store.finish(job_id, events)
    except Exception as exc:
        traceback.print_exc()
        message = str(exc) if isinstance(exc, AnalysisError) else "De analyse is mislukt. Controleer of het videobestand volledig en leesbaar is."
        store.fail(job_id, message[:1200])


if __name__ == "__main__":
    main()
