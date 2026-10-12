"""Regression checks with real FFmpeg fixtures and isolated temporary histories."""
import io
import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

os.environ["QAS_NO_RUNNER"] = "1"
from analyzer_core import AnalysisError, analyze, detect_1khz_tone, detect_black_segments, detect_freezes, probe_video
from job_runner import JobRunner
from settings import validate_settings
from storage import Store, summarize
from web_app import create_app


def ffmpeg(*args):
    subprocess.run(["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y", *args], check=True,
                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)


class VideoFixtures(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixtures = tempfile.TemporaryDirectory(prefix="qas-fixtures-")
        root = Path(cls.fixtures.name)
        cls.black = root / "black.mp4"
        cls.tones = root / "tones.mp4"
        ffmpeg("-f", "lavfi", "-i", "color=c=black:s=160x120:r=10:d=6", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(cls.black))
        expression = "aevalsrc=if(between(t\\,1\\,3)+between(t\\,5\\,7)\\,0.5*sin(2*PI*1000*t)\\,0):s=8000:d=8"
        ffmpeg("-f", "lavfi", "-i", "color=c=blue:s=160x120:r=10:d=8", "-f", "lavfi", "-i", expression,
               "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(cls.tones))

    @classmethod
    def tearDownClass(cls):
        cls.fixtures.cleanup()


class DetectorTests(VideoFixtures):
    def test_black_and_freeze_at_end(self):
        black = detect_black_segments(self.black, min_duration=1)
        freeze = detect_freezes(self.black, min_duration=1)
        self.assertEqual(len(black), 1)
        self.assertAlmostEqual(black[0]["start_sec"], 0, delta=0.1)
        self.assertAlmostEqual(black[0]["end_sec"], 6, delta=0.2)
        self.assertEqual(len(freeze), 1)
        self.assertAlmostEqual(freeze[0]["end_sec"], 6, delta=0.1)

    def test_finds_both_separate_tones(self):
        tones = detect_1khz_tone(self.tones, min_duration=1.5)
        self.assertEqual(len(tones), 2, tones)
        self.assertAlmostEqual(tones[0]["start_sec"], 1, delta=0.26)
        self.assertAlmostEqual(tones[0]["end_sec"], 3, delta=0.26)
        self.assertAlmostEqual(tones[1]["start_sec"], 5, delta=0.26)
        self.assertAlmostEqual(tones[1]["end_sec"], 7, delta=0.26)

    def test_missing_audio_is_skipped_with_warning(self):
        progress = []
        events = analyze(self.black, validate_settings({"detectors": ["1KHZ_TONE"]}),
                         lambda stage, count, **kwargs: progress.append((stage, count, kwargs)))
        self.assertEqual(events, [])
        self.assertIn("Geen geluidsspoor", progress[0][2]["warning"])
        self.assertEqual(progress[-1][1], 2)

    def test_invalid_video_fails_instead_of_clean_result(self):
        path = Path(self.fixtures.name) / "broken.mp4"
        path.write_bytes(b"this is not a video")
        with self.assertRaises(AnalysisError):
            probe_video(path)

    def test_minimum_duration_is_per_analysis(self):
        self.assertEqual(detect_black_segments(self.black, min_duration=10), [])
        self.assertEqual(len(detect_black_segments(self.black, min_duration=1)), 1)


class ApiTests(VideoFixtures):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="qas-api-")
        root = Path(self.temp.name)
        self.library = root / "library"
        self.library.mkdir()
        shutil.copy2(self.black, self.library / "existing.mp4")
        self.app_config = {"TESTING": True, "START_RUNNER": False, "DATA_DIR": str(root / "data"), "LIBRARY_DIR": str(self.library)}
        self.app = create_app(self.app_config)
        self.store = self.app.extensions["store"]
        self.client = self.app.test_client()
        self.client.get("/")
        with self.client.session_transaction() as session:
            self.headers = {"X-CSRF-Token": session["csrf"]}

    def tearDown(self):
        self.app.extensions["runner"].close()
        self.temp.cleanup()

    def make_completed(self, filename="existing.mp4"):
        job_id = self.store.create_job(filename, self.library / "existing.mp4", validate_settings({"detectors": ["BLACK"]}))
        self.assertEqual(self.store.claim_next(), job_id)
        self.store.progress(job_id, "Done", 2, duration=6)
        self.store.finish(job_id, [{"type": "BLACK", "start_sec": 0.1, "end_sec": 4.25, "details": "black"},
                                  {"type": "FREEZE", "start_sec": 2.25, "end_sec": 6, "details": "freeze"}])
        return job_id

    def test_review_coverage_export_and_persistence(self):
        job_id = self.make_completed()
        details = self.client.get(f"/api/jobs/{job_id}").get_json()
        self.assertAlmostEqual(details["summary"]["suspected_seconds"], 5.9)
        event_id = details["events"][0]["id"]
        response = self.client.patch(f"/api/jobs/{job_id}/events/{event_id}", json={"review": "confirmed"}, headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertAlmostEqual(response.get_json()["summary"]["confirmed_seconds"], 4.15)
        second = details["events"][1]["id"]
        response = self.client.patch(f"/api/jobs/{job_id}/events/{second}", json={"review": "dismissed"}, headers=self.headers)
        summary = response.get_json()["summary"]
        self.assertAlmostEqual(summary["suspected_seconds"], 4.15)
        self.assertEqual(summary["pending_count"], 0)
        export = self.client.get(f"/api/jobs/{job_id}/events.csv").get_data(as_text=True)
        self.assertIn("0.1,4.25,4.15,confirmed", export)
        self.assertIn("dismissed", export)
        self.assertIn("confirmed_percent", self.client.get("/api/export/summary.csv").get_data(as_text=True))
        reopened = create_app(self.app_config).test_client().get(f"/api/jobs/{job_id}").get_json()
        self.assertEqual(reopened["events"][0]["review"], "confirmed")
        self.assertNotIn("source_path", reopened)

    def test_media_supports_seeking(self):
        job_id = self.make_completed()
        response = self.client.get(f"/media/{job_id}", headers={"Range": "bytes=0-127"})
        self.assertEqual(response.status_code, 206)
        self.assertEqual(len(response.data), 128)
        self.assertEqual(response.headers["Accept-Ranges"], "bytes")
        response.close()

    def test_uploads_with_same_name_do_not_overwrite(self):
        paths = []
        for _ in range(2):
            response = self.client.post("/api/jobs", data={"videos": (io.BytesIO(self.black.read_bytes()), "same.mp4")}, headers=self.headers)
            self.assertEqual(response.status_code, 202)
            job = self.store.get_job(response.get_json()["job_ids"][0])
            paths.append(job["source_path"])
        self.assertNotEqual(*paths)
        self.assertTrue(all(Path(path).is_file() for path in paths))

    def test_empty_upload_rolls_back_files(self):
        response = self.client.post("/api/jobs", data={"videos": [(io.BytesIO(self.black.read_bytes()), "good.mp4"),
                                                                   (io.BytesIO(b""), "empty.mp4")]}, headers=self.headers)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.store.list_jobs(), [])
        self.assertEqual(list((Path(self.app_config["DATA_DIR"]) / "media").iterdir()), [])

    def test_library_path_traversal_is_rejected(self):
        response = self.client.post("/api/jobs/from-library", json={"files": ["../outside.mp4"]}, headers=self.headers)
        self.assertEqual(response.status_code, 400)

    def test_delete_queued_directly_preserves_existing_file(self):
        response = self.client.post("/api/jobs/from-library", json={"files": ["existing.mp4"]}, headers=self.headers)
        job_id = response.get_json()["job_ids"][0]
        self.assertEqual(self.client.delete(f"/api/jobs/{job_id}", headers=self.headers).status_code, 200)
        self.assertIsNone(self.store.get_job(job_id))
        self.assertTrue((self.library / "existing.mp4").exists())

    def test_cancel_keeps_analysis_in_history(self):
        job_id = self.store.create_job("existing.mp4", self.library / "existing.mp4", validate_settings({}))
        response = self.client.post(f"/api/jobs/{job_id}/cancel", headers=self.headers)
        self.assertEqual(response.get_json()["status"], "cancelled")
        self.assertIsNotNone(self.store.get_job(job_id))

    def test_delete_library_files_keeps_results_and_cancels_queued_jobs(self):
        first = self.make_completed()
        second = self.store.create_job("existing.mp4", self.library / "existing.mp4", validate_settings({}))
        shutil.copy2(self.black, self.library / "second.mp4")
        response = self.client.delete("/api/library", json={"files": ["existing.mp4", "second.mp4"]}, headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.client.get("/api/library").get_json()["files"], [])
        self.assertEqual(len(self.store.events(first)), 2)
        details = self.client.get(f"/api/jobs/{first}").get_json()
        self.assertEqual(details["status"], "completed")
        self.assertFalse(details["media_available"])
        self.assertEqual(self.store.get_job(second)["status"], "cancelled")
        self.assertEqual(self.client.get(f"/media/{first}").status_code, 404)

    def test_delete_library_validates_whole_selection_before_deleting(self):
        outside = Path(self.temp.name) / "outside.mp4"
        shutil.copy2(self.black, outside)
        for name in ["../outside.mp4", "bad\x00.mp4", "missing.mp4"]:
            with self.subTest(name=name):
                response = self.client.delete("/api/library", json={"files": ["existing.mp4", name]}, headers=self.headers)
                self.assertEqual(response.status_code, 400)
                self.assertTrue((self.library / "existing.mp4").exists())
                self.assertTrue(outside.exists())
        self.assertEqual(self.client.delete("/api/library", json={"files": ["existing.mp4"]}).status_code, 400)

    def start_running_job(self):
        runner = self.app.extensions["runner"]
        runner.start()
        job_id = self.store.create_job("existing.mp4", self.library / "existing.mp4", validate_settings({}))
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if self.store.get_job(job_id)["status"] == "running":
                return job_id
            time.sleep(0.01)
        self.fail("Worker did not start")

    def test_delete_running_analysis_stops_worker_and_preserves_video(self):
        job_id = self.start_running_job()
        response = self.client.delete(f"/api/jobs/{job_id}", headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(self.store.get_job(job_id))
        self.assertTrue((self.library / "existing.mp4").exists())
        self.assertTrue(self.app.extensions["runner"].thread.is_alive())

    def test_delete_library_stops_running_analysis_before_removing_video(self):
        job_id = self.start_running_job()
        response = self.client.delete("/api/library", json={"files": ["existing.mp4"]}, headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertFalse((self.library / "existing.mp4").exists())
        self.assertEqual(self.store.get_job(job_id)["status"], "cancelled")

    def test_retry_owns_shared_upload_until_last_result_is_deleted(self):
        response = self.client.post("/api/jobs", data={"videos": (io.BytesIO(self.black.read_bytes()), "owned.mp4")}, headers=self.headers)
        first = response.get_json()["job_ids"][0]
        source = Path(self.store.get_job(first)["source_path"])
        self.store.cancel(first)
        retry = self.client.post(f"/api/jobs/{first}/retry", headers=self.headers)
        second = retry.get_json()["job_ids"][0]
        self.client.delete(f"/api/jobs/{first}", headers=self.headers)
        self.assertTrue(source.exists())
        self.store.cancel(second)
        self.client.delete(f"/api/jobs/{second}", headers=self.headers)
        self.assertFalse(source.exists())

    def test_review_requires_matching_job_and_valid_state(self):
        first = self.make_completed()
        second = self.make_completed("second.mp4")
        event_id = self.store.events(first)[0]["id"]
        self.assertEqual(self.client.patch(f"/api/jobs/{second}/events/{event_id}", json={"review": "confirmed"}, headers=self.headers).status_code, 404)
        self.assertEqual(self.client.patch(f"/api/jobs/{first}/events/{event_id}", json={"review": "oops"}, headers=self.headers).status_code, 400)
        self.assertEqual(self.client.patch(f"/api/jobs/{first}/events/{event_id}", json={"review": "confirmed"}).status_code, 400)

    def test_csv_formula_filename_is_escaped(self):
        self.make_completed("=HYPERLINK(1).mp4")
        export = self.client.get("/api/export/events.csv").get_data(as_text=True)
        self.assertIn("'=HYPERLINK", export)

    def test_settings_reject_invalid_values(self):
        for value in ({"detectors": []}, {"detectors": ["unknown"]}, {"min_durations": {"BLACK": float("nan")}},
                      {"min_durations": {"BLACK": -1}}, {"min_durations": {"BLACK": True}}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_settings(value)


class QueueTests(VideoFixtures):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="qas-queue-")
        self.store = Store(Path(self.temp.name) / "history.sqlite3")
        self.runner = JobRunner(self.store, self.temp.name)

    def tearDown(self):
        self.runner.close()
        self.temp.cleanup()

    def wait_for(self, job_id, expected, timeout=30):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            job = self.store.get_job(job_id)
            if job["status"] in expected:
                return job
            time.sleep(0.05)
        self.fail(f"Job did not reach {expected}: {self.store.get_job(job_id)}")

    def test_worker_completion_and_running_cancellation(self):
        self.runner.start()
        settings = validate_settings({"detectors": ["BLACK", "FREEZE"], "min_durations": {"BLACK": 1, "FREEZE": 1}})
        first = self.store.create_job("first.mp4", self.black, settings)
        self.wait_for(first, ["running"])
        self.store.cancel(first)
        second = self.store.create_job("second.mp4", self.black, settings)
        self.assertEqual(self.wait_for(first, ["cancelled", "completed"])["status"], "cancelled")
        finished = self.wait_for(second, ["completed", "failed"])
        self.assertEqual(finished["status"], "completed", finished.get("error"))
        self.assertEqual(finished["completed_steps"], finished["total_steps"])
        self.assertEqual({e["type"] for e in self.store.events(second)}, {"BLACK", "FREEZE"})

    def test_interrupted_job_is_not_reported_as_clean(self):
        job_id = self.store.create_job("interrupted.mp4", self.black, validate_settings({}))
        self.store.claim_next()
        self.runner.start()
        job = self.store.get_job(job_id)
        self.assertEqual(job["status"], "failed")
        self.assertIn("gestopt", job["error"])

    def test_second_runner_does_not_recover_live_jobs(self):
        self.runner.start()
        other = JobRunner(self.store, self.temp.name)
        other.start()
        self.assertIsNone(other.thread)
        other.close()


if __name__ == "__main__":
    unittest.main()
