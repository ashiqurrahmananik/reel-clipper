from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path


def ffmpeg_bin() -> str:
    path = shutil.which("ffmpeg") or shutil.which("ffmpeg.exe")
    if not path:
        raise RuntimeError(
            "ffmpeg was not found on PATH. Install it from https://www.gyan.dev/ffmpeg/builds/ "
            "(Windows) or `brew install ffmpeg` (macOS) / `apt install ffmpeg` (Linux)."
        )
    return path


def ffprobe_bin() -> str:
    path = shutil.which("ffprobe") or shutil.which("ffprobe.exe")
    if not path:
        sibling = Path(ffmpeg_bin()).with_name(
            "ffprobe.exe" if sys.platform == "win32" else "ffprobe"
        )
        if sibling.is_file():
            return str(sibling)
    return path or "ffprobe"


def probe_duration(path: Path) -> float:
    import json

    proc = subprocess.run(
        [ffprobe_bin(), "-v", "error", "-show_entries", "format=duration",
         "-of", "json", str(path)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if proc.returncode != 0:
        return 0.0
    try:
        return float(json.loads(proc.stdout)["format"]["duration"])
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        return 0.0


def run(cmd: list[str], *, cwd: Path | None = None, timeout: int | None = None) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip()[-2500:]
        raise RuntimeError(f"Command failed ({proc.returncode}): {' '.join(cmd[:4])}...\n{tail}")
    return proc


def ts_to_seconds(value: str) -> float:
    value = value.strip().replace(",", ".")
    parts = value.split(":")
    if len(parts) == 1:
        return float(parts[0])
    total = 0.0
    for part in parts:
        total = total * 60 + float(part)
    return total


def seconds_to_ts(value: float, *, comma: bool = False) -> str:
    value = max(0.0, value)
    hours, rem = divmod(value, 3600)
    minutes, seconds = divmod(rem, 60)
    sep = "," if comma else "."
    return f"{int(hours):02d}:{int(minutes):02d}:{int(seconds):02d}{sep}{int(round((seconds % 1) * 1000)):03d}"


def slugify(text: str, max_len: int = 55) -> str:
    text = re.sub(r"[^\w\s-]", "", text or "", flags=re.UNICODE).strip().lower()
    text = re.sub(r"[-\s]+", "-", text).strip("-")
    return (text[:max_len] or "video").rstrip("-")


def ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def words(text: str) -> list[str]:
    return re.findall(r"[\w']+", text or "", flags=re.UNICODE)


def clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


def short_text(text: str, limit: int = 140) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"
