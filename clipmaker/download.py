from __future__ import annotations

import html
import json
import re
import shutil
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable

from .utils import ffmpeg_bin, probe_duration, run

Progress = Callable[[str], None]


def _no_progress(_: str) -> None:
    pass


def extract_info(url: str) -> dict:
    try:
        import yt_dlp
    except ImportError as exc:  # pragma: no cover - depends on user environment
        raise RuntimeError(
            "yt-dlp is not installed. Run: python -m pip install -r requirements.txt"
        ) from exc

    opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "skip_download": True,
        "ignoreerrors": False,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
    if not info:
        raise RuntimeError("Could not read video information from that link.")
    return info


def download_video(
    url: str,
    out_dir: Path,
    *,
    max_height: int = 1080,
    progress: Progress = _no_progress,
) -> Path:
    try:
        import yt_dlp
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(
            "yt-dlp is not installed. Run: python -m pip install -r requirements.txt"
        ) from exc

    out_dir.mkdir(parents=True, exist_ok=True)
    for stale in out_dir.glob("source.*"):
        if stale.name != "source.mp4":
            stale.unlink(missing_ok=True)

    fmt = (
        f"bestvideo[height<={max_height}][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<={max_height}]"
        f"+bestaudio/best[height<={max_height}][ext=mp4]/best[height<={max_height}]/best"
    )

    def hook(d: dict) -> None:
        if d.get("status") == "downloading":
            pct = d.get("_percent_str", "").strip()
            speed = d.get("_speed_str", "").strip()
            progress(f"Downloading video {pct} ({speed})")
        elif d.get("status") == "finished":
            progress("Download finished, preparing source file…")

    opts = {
        "format": fmt,
        "merge_output_format": "mp4",
        "outtmpl": str(out_dir / "source.%(ext)s"),
        "ffmpeg_location": ffmpeg_bin(),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "progress_hooks": [hook],
        "overwrites": True,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.download([url])

    candidates = sorted(out_dir.glob("source.*"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not candidates:
        raise RuntimeError("yt-dlp finished but no source file was produced.")
    source = candidates[0]
    if source.name != "source.mp4":
        target = out_dir / "source.mp4"
        target.unlink(missing_ok=True)
        source.rename(target)
        source = target
    progress(f"Source ready: {source.name}")
    return source


def recover_source(folder: Path, *, progress: Progress = _no_progress) -> Path:
    """Turn already-downloaded source.fXXX parts into one playable source.mp4.

    Never downloads anything: it just stream-copies the local video and audio
    tracks together. Useful after an interrupted download or when the web UI
    saved the video and audio as separate files.
    """
    folder = Path(folder)
    target = folder / "source.mp4"
    if target.is_file() and target.stat().st_size > 10_000 and probe_duration(target) > 0:
        return target

    videos = [
        p for p in folder.glob("source.f*.mp4")
        if "temp" not in p.name.lower() and probe_duration(p) > 0
    ]
    audios = [p for p in folder.glob("source.f*.m4a") if probe_duration(p) > 0]
    if not videos:
        raise RuntimeError(
            f"No usable downloaded video found in {folder}. Expected source.mp4 "
            "or separate source.f*.mp4 / source.f*.m4a files."
        )

    video = max(videos, key=lambda p: p.stat().st_size)
    audio = max(audios, key=lambda p: p.stat().st_size) if audios else None
    progress(f"Reusing local video parts: {video.name}" + (f" + {audio.name}" if audio else ""))

    if not audio:
        if video.resolve() != target.resolve():
            shutil.copy2(video, target)
        return target

    tmp = folder / "source.remux.mp4"
    run(
        [
            ffmpeg_bin(), "-y", "-hide_banner", "-loglevel", "error",
            "-i", str(video), "-i", str(audio),
            "-map", "0:v:0", "-map", "1:a:0", "-c", "copy", str(tmp),
        ],
        timeout=7200,
    )
    tmp.replace(target)
    progress("Muxed existing video + audio into source.mp4 — no download needed.")
    return target


def _lang_match(key: str, preferred: list[str]) -> int:
    key_l = key.lower().replace("_", "-")
    for i, lang in enumerate(preferred):
        want = lang.lower().replace("_", "-")
        if key_l == want:
            return i
        if key_l.startswith(want + "-") or want.startswith(key_l + "-"):
            return i + 0.5
    return 999


def pick_caption_track(info: dict, preferred: list[str]) -> tuple[str, str] | None:
    """Return (url, language) for the best available caption track."""
    for group in ("subtitles", "automatic_captions"):
        tracks = info.get(group) or {}
        if not tracks:
            continue
        ranked = sorted(tracks.items(), key=lambda kv: _lang_match(kv[0], preferred))
        for _lang, formats in ranked[:8]:
            usable = [f for f in (formats or []) if f.get("url") and f.get("ext") in {"vtt", "json3", "srv3", "srt"}]
            if not usable:
                continue
            chosen = next((f for f in usable if f["ext"] == "vtt"), usable[0])
            return chosen["url"], _lang
    return None


def fetch_caption(url: str, dest: Path) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (reel-clipper)"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, TimeoutError) as exc:
        raise RuntimeError(f"Could not download captions: {exc}") from exc

    if raw.lstrip().startswith("{") or '"events"' in raw[:200]:
        raw = json3_to_vtt(raw)
    if "-->" not in raw:
        raise RuntimeError("Caption track did not contain usable timing data.")
    dest.write_text(raw, encoding="utf-8")
    return dest


def json3_to_vtt(raw: str) -> str:
    data = json.loads(raw)
    out = ["WEBVTT", ""]
    for event in data.get("events", []):
        if "tStartMs" not in event:
            continue
        start = event["tStartMs"] / 1000.0
        duration = event.get("dDurationMs", 0) / 1000.0
        end = start + max(duration, 1.2)
        text = "".join(seg.get("utf8", "") for seg in event.get("segs", []))
        text = html.unescape(re.sub(r"<[^>]+>", "", text)).strip()
        if not text:
            continue
        out.append(f"{_vtt_ts(start)} --> {_vtt_ts(end)}")
        out.append(text)
        out.append("")
    return "\n".join(out)


def _vtt_ts(seconds: float) -> str:
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    return f"{int(hours):02d}:{int(minutes):02d}:{int(secs):02d}.{int(round((secs % 1) * 1000)):03d}"


def get_captions(
    info: dict,
    out_dir: Path,
    *,
    preferred: list[str] | None = None,
    progress: Progress = _no_progress,
) -> tuple[Path | None, str | None]:
    preferred = preferred or ["en", "en-US", "hi", "bn"]
    track = pick_caption_track(info, preferred)
    if not track:
        progress("No captions found on YouTube; local transcription will be used.")
        return None, None
    url, lang = track
    dest = out_dir / f"captions_{lang.replace('-', '_')}.vtt"
    try:
        fetch_caption(url, dest)
        progress(f"Using YouTube captions ({lang}).")
        return dest, lang
    except RuntimeError as exc:
        progress(f"Caption download failed ({exc}); falling back to transcription.")
        return None, None


def extract_audio(video: Path, out_dir: Path, *, progress: Progress = _no_progress) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    wav = out_dir / "audio.wav"
    run(
        [
            ffmpeg_bin(), "-y", "-hide_banner", "-loglevel", "error",
            "-i", str(video), "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(wav),
        ]
    )
    progress("Audio extracted for transcription.")
    return wav


def transcribe(
    video: Path,
    out_dir: Path,
    *,
    model_size: str = "small",
    language: str | None = None,
    progress: Progress = _no_progress,
) -> list[dict]:
    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(
            "This video has no captions. Install local transcription with:\n"
            "  python -m pip install -r requirements-whisper.txt\n"
            "Or pass a video that has subtitles."
        ) from exc

    wav = extract_audio(video, out_dir, progress=progress)
    progress(f"Loading Whisper model '{model_size}' (first run downloads weights)…")
    model = WhisperModel(model_size, device="cpu", compute_type="int8")
    segments, _ = model.transcribe(
        str(wav),
        language=language,
        vad_filter=True,
        vad_parameters={"min_silence_duration_ms": 400},
    )
    result = [{"start": float(s.start), "end": float(s.end), "text": s.text.strip()} for s in segments]
    progress(f"Transcribed {len(result)} segments locally.")
    return result
