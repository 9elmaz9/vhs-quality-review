#!/bin/zsh
set -e
cd -- "$(dirname "$0")"
export PATH="/opt/homebrew/bin:$PATH"
export PYTHONDONTWRITEBYTECODE=1
echo 'VHS Quality Review: http://127.0.0.1:5009'
echo 'Keep this window open while using the app. Stop with Ctrl+C.'
if /usr/bin/curl -fsS --max-time 2 http://127.0.0.1:5009/ >/dev/null 2>&1; then
  open 'http://127.0.0.1:5009/'
  exit 0
fi
if [[ ! -x .venv-run/bin/python ]]; then
  echo 'The Python environment is missing. See README.md for setup instructions.'
  exit 1
fi
(sleep 2; open 'http://127.0.0.1:5009/') &
exec .venv-run/bin/python -B web_app.py
