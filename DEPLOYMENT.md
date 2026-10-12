# Optional deployment on Render

Deploy this project as a **separate service** from the original Zwartruimte app.
Publishing the source code to GitHub does not deploy it or migrate any recordings.

## Configuration

Create a Python web service connected to this repository and configure:

| Setting | Value |
| --- | --- |
| Branch | `main` |
| Python version | `3.11.9` (see `.python-version`) |
| Build command | `bash render-build.sh` |
| Start command | `gunicorn --config gunicorn.conf.py web_app:app` |
| Health check | `/healthz` |
| Instances | `1` |
| Persistent disk mount | `/var/data` |
| `APP_ENV` | `production` |
| `APP_DATA_DIR` | `/var/data/vhs-quality-review` |
| `APP_PASSWORD_HASH` | A password hash generated locally; configure it as a secret |

An attached persistent disk requires a paid Render service. Check the current
compute and storage prices and obtain the project owner's approval before adding
paid resources. Size the disk for the team's recordings and allow room for new
uploads. The application accepts up to 20 GB per upload request; verify hosting
limits and test representative recordings before using it for large batches.

Set up the disk and environment variables before the first deployment. The app
refuses to start in production without the password hash or an explicit data
directory. Setting `APP_DATA_DIR` alone does not provision persistent storage.

Generate the hash from the project directory:

```sh
.venv-run/bin/python password_hash.py
```

The password is entered privately and is not echoed. Put the printed hash in
`APP_PASSWORD_HASH`; share the original password through your usual private channel.
Changing the hash signs out existing sessions. Sessions expire after eight hours
without activity. Login attempts are limited to ten per IP per five minutes in
the running application process.

## Verify a release

1. Deploy the intended source revision with the configuration above.
2. Check HTTPS: `/healthz` returns `{"status":"ok"}`; the homepage asks for a
   password; history, CSV exports, and direct media links require login.
3. Sign in, switch NL / EN, upload a small test recording, and complete an analysis.
4. Review an event, download CSV, and check cancellation and deletion using test data.
5. Restart the service and confirm that the recording, history, and assessment
   remain available. Delete the test recording through the app afterwards.
6. Test a representative team recording to check processing time, memory usage,
   upload limits, and browser playback.

## Queue and storage

Gunicorn runs one process with four request threads. The queue starts one analysis
subprocess at a time. Do not enable preloading, increase the Gunicorn worker count,
or add service instances: queue ownership and cancellation locks are designed for
one application process.

The build script installs Python dependencies and static FFmpeg/ffprobe binaries.
The application adds `bin/` to `PATH`; OpenCV uses its headless server package.

`APP_DATA_DIR` holds `history.sqlite3`, `media/`, `logs/`, and `.session-secret`.
Keep them together in backups. Use a SQLite backup or stop the application before
copying the database. Neither local recordings nor the optional `uploads/` library
are included in the Git repository or automatically transferred to the server.

On shutdown, the current analysis stops and can be retried after restart. Queued
work continues when the service starts again. A Render service with an attached
disk has a brief outage during deployment.

Before a rollback, back up the online data and choose a release that preserves
password protection and supports the stored data format.

References: [Flask deployment](https://render.com/docs/deploy-flask),
[persistent disks](https://render.com/docs/disks).
