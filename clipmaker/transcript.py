from __future__ import annotations

import html
import json
import re
from pathlib import Path

from .utils import ts_to_seconds, words


def parse_vtt(raw: str) -> list[dict]:
    lines = raw.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    segments: list[dict] = []
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if "-->" not in line:
            i += 1
            continue
        parts = line.split("-->")
        if len(parts) < 2:
            i += 1
            continue
        start = ts_to_seconds(parts[0].strip().split()[0])
        end = ts_to_seconds(parts[1].strip().split()[0])
        i += 1
        buf: list[str] = []
        while i < len(lines) and lines[i].strip():
            buf.append(lines[i].strip())
            i += 1
        text = " ".join(buf)
        text = html.unescape(re.sub(r"<[^>]+>", "", text))
        text = re.sub(r"\s+", " ", text).strip()
        if text and end > start:
            segments.append({"start": start, "end": end, "text": text})
    return _dedupe_rolling(segments)


def _dedupe_rolling(segments: list[dict]) -> list[dict]:
    """YouTube auto-captions repeat a rolling window; keep only new words."""
    out: list[dict] = []
    for seg in segments:
        if not out:
            out.append(dict(seg))
            continue
        prev = out[-1]
        a, b = prev["text"].lower(), seg["text"].lower()
        if a == b:
            prev["end"] = max(prev["end"], seg["end"])
            continue
        if b in a:
            prev["end"] = max(prev["end"], seg["end"])
            continue
        overlap = _word_overlap(a, b)
        if overlap and overlap >= min(len(words(a)), len(words(b))) * 0.35:
            new_words = words(seg["text"])[overlap:]
            if new_words:
                prev["text"] = (prev["text"] + " " + " ".join(new_words)).strip()
            prev["end"] = max(prev["end"], seg["end"])
            continue
        out.append(dict(seg))
    return out


def _word_overlap(a: str, b: str) -> int:
    at, bt = words(a), words(b)
    for k in range(min(len(at), len(bt)), 0, -1):
        if [x.lower() for x in at[-k:]] == [x.lower() for x in bt[:k]]:
            return k
    return 0


def merge_segments(segments: list[dict], *, max_chars: int = 200, max_dur: float = 11.0) -> list[dict]:
    """Group caption cues into readable sentence-ish chunks for scoring and captions."""
    merged: list[dict] = []
    for seg in segments:
        text = re.sub(r"\s+", " ", seg.get("text", "")).strip()
        if not text:
            continue
        if merged:
            cur = merged[-1]
            gap = seg["start"] - cur["end"]
            length_words = len(words(cur["text"] + " " + text))
            if (
                gap <= 0.8
                and len(cur["text"]) + len(text) < max_chars
                and cur["end"] - cur["start"] < max_dur
                and length_words < 45
                and not re.search(r"[.!?][\"'\u201d]?$", cur["text"])
            ):
                cur["text"] = (cur["text"] + " " + text).strip()
                cur["end"] = max(cur["end"], seg["end"])
                continue
        merged.append({"start": float(seg["start"]), "end": float(seg["end"]), "text": text})
    return merged


def load_transcript(path: Path) -> list[dict]:
    raw = path.read_text(encoding="utf-8", errors="replace")
    if raw.lstrip().startswith("["):
        data = json.loads(raw)
        return [{"start": float(x["start"]), "end": float(x["end"]), "text": x["text"]} for x in data]
    return parse_vtt(raw)


def segments_to_srt(segments: list[dict], *, start: float = 0.0, end: float | None = None) -> str:
    from .utils import seconds_to_ts

    blocks: list[str] = []
    n = 1
    for seg in segments:
        s = max(seg["start"], start)
        e = min(seg["end"], end if end is not None else float("inf"))
        if e <= s + 0.15:
            continue
        text = wrap_caption(seg["text"])
        if not text:
            continue
        blocks.append(
            f"{n}\n{seconds_to_ts(s - start, comma=True)} --> {seconds_to_ts(e - start, comma=True)}\n{text}\n"
        )
        n += 1
    return "\n".join(blocks)


def _ass_ts(value: float) -> str:
    """ASS time: H:MM:SS.cc (centiseconds)."""
    centiseconds = int(round(max(0.0, value) * 100))
    hours, centiseconds = divmod(centiseconds, 360000)
    minutes, centiseconds = divmod(centiseconds, 6000)
    seconds, centiseconds = divmod(centiseconds, 100)
    return f"{hours}:{minutes:02d}:{seconds:02d}.{centiseconds:02d}"


def segments_to_ass(
    segments: list[dict],
    *,
    start: float = 0.0,
    end: float | None = None,
    width: int = 1080,
    height: int = 1920,
    font_name: str = "Arial Black",
    font_size: int = 54,
    margin_v: int = 240,
    primary_color: str = "&H0000FFFF",
) -> str:
    """Build ASS captions with a fixed vertical-video resolution.

    Using explicit PlayRes values keeps the font size and top placement
    predictable on 1080x1920 reels; plain SRT inherits a small libass script
    resolution and scales the font/margins incorrectly.
    """
    header = (
        "[Script Info]\n"
        "ScriptType: v4.00+\n"
        f"PlayResX: {width}\n"
        f"PlayResY: {height}\n"
        "WrapStyle: 0\n"
        "ScaledBorderAndShadow: yes\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
        "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
        "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"Style: Default,{font_name},{font_size},{primary_color},&H000000FF,"
        "&H00000000,&H64000000,-1,0,0,0,100,100,0,0,1,5,2,8,80,80,"
        f"{margin_v},1\n\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )
    lines = [header]
    for seg in segments:
        s = max(seg["start"], start)
        e = min(seg["end"], end if end is not None else float("inf"))
        if e <= s + 0.15:
            continue
        text = wrap_caption(seg["text"], width=22).replace("\n", "\\N")
        text = text.replace("{", "(").replace("}", ")")
        if not text:
            continue
        lines.append(
            f"Dialogue: 0,{_ass_ts(s - start)},{_ass_ts(e - start)},"
            f"Default,,0,0,0,,{text}\n"
        )
    return "\n".join(lines)


def wrap_caption(text: str, width: int = 24) -> str:
    tokens = words(text)
    if not tokens:
        return ""
    lines: list[str] = []
    current = ""
    for token in tokens:
        candidate = f"{current} {token}".strip()
        if len(candidate) > width and current:
            lines.append(current)
            current = token
        else:
            current = candidate
    if current:
        lines.append(current)
    if len(lines) > 2:
        lines = [" ".join(lines[: len(lines) // 2]), " ".join(lines[len(lines) // 2 :])]
    return "\n".join(lines)
