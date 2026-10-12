"""Shared access protection and language selection, with isolated data only."""
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ["QAS_NO_RUNNER"] = "1"
from werkzeug.security import generate_password_hash
from localization import translate_result
from settings import validate_settings
from web_app import create_app


class AccessLanguageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.password_hash = generate_password_hash("test-only-password", method="pbkdf2:sha256:1000000")

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="qas-access-")
        self.root = Path(self.temp.name)
        self.config = dict(TESTING=True, START_RUNNER=False, PRODUCTION=False,
                           DATA_DIR=str(self.root / "data"), LIBRARY_DIR=str(self.root / "library"),
                           PASSWORD_HASH=self.password_hash, SESSION_COOKIE_SECURE=False)
        self.app = create_app(self.config)
        self.client = self.app.test_client()

    def tearDown(self):
        self.app.extensions["runner"].close()
        self.temp.cleanup()

    def token(self):
        self.client.get("/login")
        with self.client.session_transaction() as session:
            return session["csrf"]

    def login(self, **extra):
        return self.client.post("/login", data={"csrf_token": self.token(), "password": "test-only-password", **extra})

    def switch(self, language, return_to="/"):
        return self.client.post("/language", data={"csrf_token": self.token(), "language": language, "return_to": return_to})

    def test_unauthenticated_routes_do_not_expose_data(self):
        store = self.app.extensions["store"]
        path = self.root / "data" / "media" / "private.mp4"
        path.write_bytes(b"private-video")
        job_id = store.create_job("private.mp4", path, validate_settings({}))
        for route in ["/api/jobs", "/api/library", f"/api/jobs/{job_id}", f"/media/{job_id}",
                      f"/media/{job_id}?download=1", f"/api/jobs/{job_id}/events.csv",
                      "/api/export/events.csv", "/api/export/summary.csv"]:
            with self.subTest(route=route):
                response = self.client.get(route)
                self.assertEqual(response.status_code, 401)
                self.assertNotIn(b"private.mp4", response.data)
                self.assertIn("no-store", response.headers["Cache-Control"])
        self.assertEqual(self.client.delete(f"/api/jobs/{job_id}").status_code, 401)
        self.assertEqual(self.client.post("/api/jobs").status_code, 401)
        self.assertEqual(self.client.get("/").status_code, 302)
        self.assertEqual(self.client.get(f"/jobs/{job_id}").status_code, 302)
        self.assertEqual(self.client.get("/healthz").get_json(), {"status": "ok"})
        with self.client.get("/static/app.css") as response:
            self.assertEqual(response.status_code, 200)
        self.assertIsNotNone(store.get_job(job_id))

    def test_login_csrf_rotation_logout_and_redirect(self):
        before = self.token()
        self.assertEqual(self.client.post("/login", data={"password": "test-only-password"}).status_code, 400)
        self.assertEqual(self.client.post("/login", data={"password": "wrong", "csrf_token": before}).status_code, 401)
        response = self.login(next="https://example.com/steal")
        self.assertEqual(response.location, "/")
        after = self.token()
        self.assertNotEqual(before, after)
        self.assertEqual(self.client.get("/api/jobs").status_code, 200)
        self.assertEqual(self.client.post("/logout", data={"csrf_token": before}).status_code, 400)
        self.assertEqual(self.client.post("/logout", data={"csrf_token": after}).status_code, 302)
        self.assertEqual(self.client.get("/api/jobs").status_code, 401)

    def test_rate_limit_and_password_rotation(self):
        token = self.token()
        for _ in range(10):
            self.assertEqual(self.client.post("/login", data={"password": "wrong", "csrf_token": token}).status_code, 401)
        self.assertEqual(self.login().status_code, 429)
        with patch("web_app.time.monotonic", return_value=10**12):
            self.assertEqual(self.login().status_code, 302)
        with self.client.session_transaction() as old_session:
            authenticated_session = dict(old_session)
        other = create_app({**self.config, "PASSWORD_HASH": generate_password_hash("replacement-test-password", method="pbkdf2:sha256:1000000")})
        other_client = other.test_client()
        with other_client.session_transaction() as new_session:
            new_session.update(authenticated_session)
        self.assertEqual(other_client.get("/api/jobs").status_code, 401)

    def test_language_persists_across_login_and_logout(self):
        self.assertIn('lang="nl"', self.client.get("/login").text)
        response = self.switch("en", "/login")
        self.assertEqual(response.location, "/login")
        self.assertIn("Max-Age=31536000", response.headers.getlist("Set-Cookie")[0])
        page = self.client.get("/login").text
        self.assertIn('lang="en"', page)
        self.assertIn("Welcome back", page)
        self.assertEqual(self.login().status_code, 302)
        page = self.client.get("/").text
        self.assertIn("A clearer view of your recordings.", page)
        self.assertIn("Sign out", page)
        self.assertEqual(self.switch("xx").status_code, 400)
        self.assertEqual(self.switch("en", "//example.com").location, "/")
        self.assertEqual(self.client.get("/api/jobs/missing").get_json()["error"], "This analysis no longer exists.")
        self.client.post("/logout", data={"csrf_token": self.token()})
        self.assertIn('lang="en"', self.client.get("/login").text)

    def test_translated_results_do_not_modify_stored_data_or_filenames(self):
        self.login()
        self.switch("en")
        store = self.app.extensions["store"]
        job_id = store.create_job("Zwart beeld.mp4", self.root / "data" / "media" / "sample.mp4", validate_settings({}))
        store.claim_next()
        store.progress(job_id, "Zwart beeld afgerond", 2, duration=6,
                       warning="Geen geluidsspoor; de controle op 1 kHz is overgeslagen.")
        self.assertEqual(self.client.get(f"/api/jobs/{job_id}").get_json()["stage"], "Black frames completed")
        details = "Nauwelijks beeldverandering tussen opeenvolgende frames."
        store.finish(job_id, [{"type": "FREEZE", "start_sec": 0, "end_sec": 6, "details": details}])
        result = self.client.get(f"/api/jobs/{job_id}").get_json()
        self.assertEqual(result["stage"], "Ready for review")
        self.assertEqual(result["filename"], "Zwart beeld.mp4")
        self.assertIn("No audio track", result["warning"])
        self.assertIn("Very little visual change", result["events"][0]["details"])
        self.assertEqual(store.events(job_id)[0]["details"], details)
        self.assertIn("Very little visual change", self.client.get(f"/api/jobs/{job_id}/events.csv").text)
        self.switch("nl", f"/jobs/{job_id}")
        self.assertEqual(self.client.get(f"/api/jobs/{job_id}").get_json()["events"][0]["details"], details)

    def test_production_requires_access_and_storage(self):
        with self.assertRaisesRegex(RuntimeError, "APP_PASSWORD_HASH"):
            create_app({**self.config, "PRODUCTION": True, "PASSWORD_HASH": ""})
        with patch.dict(os.environ, {"APP_ENV": "production"}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "APP_DATA_DIR"):
                create_app({"PASSWORD_HASH": self.password_hash})
        production = create_app({**self.config, "PRODUCTION": True, "SESSION_COOKIE_SECURE": True})
        response = production.test_client().get("/login", base_url="https://app.example")
        self.assertIn("Secure", response.headers["Set-Cookie"])
        self.assertIn("HttpOnly", response.headers["Set-Cookie"])

    def test_empty_file_error_is_translated_without_changing_filename(self):
        self.login()
        self.switch("en")
        response = self.client.post("/api/jobs", data={"videos": (io.BytesIO(b""), "Zwart beeld.mp4")},
                                    headers={"X-CSRF-Token": self.token()})
        self.assertEqual(response.get_json()["error"], "Zwart beeld.mp4 is empty.")


if __name__ == "__main__":
    unittest.main()
