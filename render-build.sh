#!/usr/bin/env bash
set -euo pipefail

# Installing Python dependencies
pip install --upgrade pip
pip install -r requirements.txt


# Installing ffmpeg (static binaries for Linux x64)
# Source: John Van Sickle — stable static builds
FFMPEG_URL="https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-amd64-static.tar.xz"
curl --fail --location --retry 3 "$FFMPEG_URL" -o ffmpeg.tar.xz
mkdir -p .ffmpeg
tar -xJf ffmpeg.tar.xz --strip-components=1 -C .ffmpeg
mkdir -p bin
cp .ffmpeg/ffmpeg .ffmpeg/ffprobe bin/
chmod +x bin/ffmpeg bin/ffprobe
bin/ffmpeg -version
bin/ffprobe -version
rm -rf .ffmpeg ffmpeg.tar.xz
