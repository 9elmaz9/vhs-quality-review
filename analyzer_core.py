#!/usr/bin/env python3
"""VHS candidate detection. Findings always need human review."""
import csv
import json
import math
import re
import subprocess
import tempfile
from pathlib import Path

import cv2
import numpy as np
from scipy.fft import rfft, rfftfreq
from scipy.io import wavfile

VIDEO_EXTS = (".mp4", ".mov", ".mkv", ".avi", ".m4v")


class AnalysisError(RuntimeError):
    pass


def to_hms(seconds):
    value = max(0, int(round(seconds)))
    hours, rest = divmod(value, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def hms_to_seconds(value):
    result = 0.0
    for part in str(value).split(":"):
        result = result * 60 + float(part)
    return result


def merge_intervals(intervals):
    merged = []
    for start, end in sorted(intervals):
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return [tuple(item) for item in merged]


def run_command(args):
    try:
        result = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    except FileNotFoundError as exc:
        raise AnalysisError(f"{args[0]} is niet geïnstalleerd of niet beschikbaar.") from exc
    if result.returncode:
        detail = "\n".join(result.stderr.strip().splitlines()[-4:])
        raise AnalysisError(f"Het videobestand kon niet worden verwerkt. {detail}")
    return result


def probe_video(filepath):
    result = run_command(["ffprobe", "-v", "error", "-show_format", "-show_streams", "-of", "json", str(filepath)])
    try:
        metadata = json.loads(result.stdout)
        video = next(s for s in metadata["streams"] if s.get("codec_type") == "video"
                     and not s.get("disposition", {}).get("attached_pic"))
        duration = float(video.get("duration") or metadata.get("format", {}).get("duration") or 0)
    except (ValueError, KeyError, StopIteration, TypeError) as exc:
        raise AnalysisError("Dit bestand bevat geen leesbare video met een geldige duur.") from exc
    if not math.isfinite(duration) or duration <= 0:
        raise AnalysisError("De videoduur kon niet worden bepaald.")
    return {"duration": duration, "has_audio": any(s.get("codec_type") == "audio" for s in metadata["streams"])}


def get_video_duration_seconds(filepath):
    return probe_video(filepath)["duration"]


def event(kind, start, end, details):
    return {"type": kind, "start_sec": float(start), "end_sec": float(end),
            "start": to_hms(start), "end": to_hms(end), "duration": float(end - start), "details": details}


def video_filter(filepath, expression):
    return run_command(["ffmpeg", "-nostdin", "-hide_banner", "-nostats", "-i", str(filepath),
                        "-map", "0:v:0", "-vf", expression, "-an", "-sn", "-dn", "-f", "null", "-"]).stderr


def detect_black_segments(filepath, min_duration=10.0, **kwargs):
    log = video_filter(filepath, f"blackdetect=d={min_duration}:pix_th=0.10:pic_th=0.98")
    matches = re.findall(r"black_start:([\d.eE+-]+)\s+black_end:([\d.eE+-]+)\s+black_duration:([\d.eE+-]+)", log)
    return [event("BLACK", float(start), float(end), "Minimaal 98% van het beeld is bijna zwart (helderheid < 10%).")
            for start, end, duration in matches if float(duration) + 1e-6 >= min_duration]


def detect_freezes(filepath, min_duration=5.0, metadata=None, **kwargs):
    metadata = metadata or probe_video(filepath)
    log = video_filter(filepath, f"freezedetect=n=0.003:d={min_duration}")
    results, start = [], None
    for match in re.finditer(r"freeze_(start|end):\s*([\d.eE+-]+)", log):
        kind, value = match.group(1), float(match.group(2))
        if kind == "start":
            start = value
        elif start is not None:
            if value - start + 1e-6 >= min_duration:
                results.append(event("FREEZE", start, value, "Nauwelijks beeldverandering tussen opeenvolgende frames."))
            start = None
    if start is not None and metadata["duration"] - start + 1e-6 >= min_duration:
        results.append(event("FREEZE", start, metadata["duration"], "Stilstaand beeld tot het einde van de video."))
    return results


def scan_frames(filepath, kind, predicate, min_duration, sample_fps=None, details=""):
    cap = cv2.VideoCapture(str(filepath))
    fps = cap.get(cv2.CAP_PROP_FPS)
    expected = cap.get(cv2.CAP_PROP_FRAME_COUNT)
    if not cap.isOpened() or not math.isfinite(fps) or fps <= 0:
        cap.release()
        raise AnalysisError("OpenCV kan de videobeelden niet lezen.")
    step = max(1, int(round(fps / sample_fps))) if sample_fps else 1
    start, index, results = None, 0, []
    try:
        while cap.grab():
            if index % step == 0:
                ok, frame = cap.retrieve()
                if not ok:
                    raise AnalysisError("Een videoframe kon niet worden gedecodeerd.")
                flagged = predicate(frame)
                timestamp = index / fps
                if flagged and start is None:
                    start = timestamp
                elif not flagged and start is not None:
                    if timestamp - start + 1e-6 >= min_duration:
                        results.append(event(kind, start, timestamp, details))
                    start = None
            index += 1
        if index == 0 or (expected > 0 and index < expected - max(2, fps * 0.1)):
            raise AnalysisError("Het uitlezen van de video is voortijdig gestopt. Controleer het bestand.")
        end = index / fps
        if start is not None and end - start + 1e-6 >= min_duration:
            results.append(event(kind, start, end, details))
    finally:
        cap.release()
    return results


def detect_glitches(filepath, crop_top_ratio=0.0, min_duration=10.0, **kwargs):
    def predicate(frame):
        if crop_top_ratio:
            frame = frame[int(frame.shape[0] * min(0.9, max(0, crop_top_ratio))):]
        b, g, r = cv2.mean(frame)[:3]
        return (g > 180 and g > r and g > b) or (r > 180 and b > 180 and g < 130) or max(r, g, b) > 230
    return scan_frames(filepath, "GLITCH", predicate, min_duration,
                       details="Sterke groene/roze kleurzweem of zeer hoge gemiddelde kanaalhelderheid. Controleer of dit bij de scène hoort.")


def detect_ruis_gray_stripes(filepath, min_duration=10.0, fps_sample=2, **kwargs):
    def predicate(frame):
        height, width = frame.shape[:2]
        if height > 480:
            frame = cv2.resize(frame, (max(1, int(width * 480 / height)), 480), interpolation=cv2.INTER_AREA)
        saturation = float(cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)[..., 1].mean())
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        return saturation <= 40 and (cv2.Laplacian(gray, cv2.CV_64F, ksize=3).var() >= 120 or gray.mean(axis=0).std() >= 10)
    return scan_frames(filepath, "RUIS/STRIPES", predicate, min_duration, sample_fps=fps_sample,
                       details="Lage kleurverzadiging met sterke fijne details of verticale helderheidsverschillen. Ook normaal zwart-witbeeld kan zo scoren.")


def detect_1khz_tone(filepath, min_duration=5.0, metadata=None, **kwargs):
    metadata = metadata or probe_video(filepath)
    if not metadata["has_audio"]:
        return []
    # Each analysis owns its audio file; the source recording is never modified.
    with tempfile.TemporaryDirectory(prefix="zwartruimte-audio-") as temp_dir:
        audio_path = Path(temp_dir) / "audio.wav"
        run_command(["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-i", str(filepath),
                     "-map", "0:a:0", "-vn", "-ac", "1", "-ar", "8000", "-c:a", "pcm_s16le", str(audio_path)])
        rate, data = wavfile.read(str(audio_path), mmap=True)
        window_size = int(rate * 0.25)
        start, results = None, []
        try:
            for offset in range(0, len(data), window_size):
                window = np.asarray(data[offset:offset + window_size], dtype=np.float64) / 32768.0
                if len(window) < 64:
                    break
                spectrum = np.abs(rfft(window * np.hanning(len(window)))) ** 2
                frequencies = rfftfreq(len(window), 1.0 / rate)
                total = float(spectrum.sum())
                band = float(spectrum[(frequencies >= 950) & (frequencies <= 1050)].sum())
                flagged = np.sqrt(np.mean(window ** 2)) >= 0.001 and total > 0 and band / total >= 0.65
                timestamp = offset / rate
                if flagged and start is None:
                    start = timestamp
                elif not flagged and start is not None:
                    if timestamp - start + 1e-6 >= min_duration:
                        results.append(event("1KHZ_TONE", start, timestamp, "Minimaal 65% van de gemeten spectrale energie ligt rond 1 kHz (±50 Hz)."))
                    start = None
            end = len(data) / rate
            if start is not None and end - start + 1e-6 >= min_duration:
                results.append(event("1KHZ_TONE", start, end, "Aanhoudende 1 kHz-toon tot het einde van het geluidsspoor."))
        finally:
            # Close the mmap before removing its temporary directory.
            if getattr(data, "_mmap", None) is not None:
                data._mmap.close()
    return results


def analyze(filepath, settings, on_progress=lambda *args, **kwargs: None):
    from settings import DETECTORS
    metadata = probe_video(filepath)
    warning = "Geen geluidsspoor; de controle op 1 kHz is overgeslagen." if not metadata["has_audio"] and "1KHZ_TONE" in settings["detectors"] else None
    on_progress("Video gecontroleerd", 1, duration=metadata["duration"], warning=warning)
    functions = {"BLACK": detect_black_segments, "FREEZE": detect_freezes, "GLITCH": detect_glitches,
                 "RUIS/STRIPES": detect_ruis_gray_stripes, "1KHZ_TONE": detect_1khz_tone}
    events = []
    for completed, key in enumerate(settings["detectors"], start=1):
        on_progress(DETECTORS[key][0], completed)
        events.extend(functions[key](filepath, min_duration=settings["min_durations"][key], metadata=metadata))
        on_progress(DETECTORS[key][0] + " afgerond", completed + 1)
    return sorted(events, key=lambda e: (e["start_sec"], e["end_sec"]))


def main():
    import argparse
    from settings import validate_settings
    from storage import covered_seconds
    parser = argparse.ArgumentParser(description="Analyseer lokale VHS-video's en exporteer gebeurtenissen.")
    parser.add_argument("folder", nargs="?", default="videos")
    args = parser.parse_args()
    with open("report_black_glitch_tone2.csv", "w", newline="", encoding="utf-8-sig") as output, \
            open("report_summary.csv", "w", newline="", encoding="utf-8-sig") as summary_output:
        writer = csv.writer(output)
        summary = csv.writer(summary_output)
        writer.writerow(["video_file", "type", "start_time", "end_time", "duration_sec", "details"])
        summary.writerow(["video_file", "video_duration_sec", "events_count", "suspected_seconds", "suspected_percent"])
        for path in sorted(Path(args.folder).iterdir()):
            if path.suffix.lower() in VIDEO_EXTS:
                duration = get_video_duration_seconds(path)
                events = analyze(path, validate_settings({}))
                for item in events:
                    writer.writerow([path.name, item["type"], item["start"], item["end"], item["duration"], item["details"]])
                covered = covered_seconds(events)
                summary.writerow([path.name, duration, len(events), covered, covered / duration * 100])


if __name__ == "__main__":
    main()
