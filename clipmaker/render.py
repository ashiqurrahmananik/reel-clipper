from __future__ import annotations

from pathlib import Path

from .transcript import segments_to_ass
from .utils import ffmpeg_bin, run


def build_filter(style: str, captions: bool) -> str:
    if style == "crop":
        base = "scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,setsar=1"
    else:  # blur background, full frame centered
        base = (
            "[0:v]split=2[bg][fg];"
            "[bg]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,"
            "setsar=1,boxblur=18:2,eq=brightness=-0.07[bgout];"
            "[fg]scale=1080:1920:force_original_aspect_ratio=decrease,setsar=1[fgout];"
            "[bgout][fgout]overlay=(W-w)/2:(H-h)/2:format=auto,setsar=1[vbase]"
        )
    if style == "crop":
        base = f"[0:v]{base}[vbase]"

    if not captions:
        return f"{base};[vbase]null[v]"
    return f"{base};[vbase]subtitles=captions.ass[v]"


def render_clip(
    video: Path,
    segments: list[dict],
    *,
    start: float,
    end: float,
    output: Path,
    style: str = "blur",
    captions: bool = True,
    preset: str = "veryfast",
    crf: int = 21,
) -> Path:
    video = Path(video).resolve()
    output = Path(output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    if captions:
        ass = segments_to_ass(segments, start=start, end=end)
        (output.parent / "captions.ass").write_text(ass, encoding="utf-8")

    cmd = [
        ffmpeg_bin(), "-y", "-hide_banner", "-loglevel", "error",
        "-ss", f"{start:.3f}", "-i", str(video), "-t", f"{end - start:.3f}",
        "-filter_complex", build_filter(style, captions),
        "-map", "[v]", "-map", "0:a:0?",
        "-c:v", "libx264", "-preset", preset, "-crf", str(crf),
        "-profile:v", "high", "-level", "4.1", "-pix_fmt", "yuv420p", "-r", "30",
        "-c:a", "aac", "-b:a", "128k", "-ar", "44100",
        "-movflags", "+faststart", "-shortest", str(output),
    ]
    run(cmd, cwd=output.parent, timeout=1200)
    if not output.exists() or output.stat().st_size < 10_000:
        raise RuntimeError(f"Rendered clip is empty: {output.name}")
    return output
