from __future__ import annotations

import json
import math
import os
import re
import shutil
import statistics
import subprocess
import urllib.request
from pathlib import Path

from .utils import clamp, ffmpeg_bin, run, short_text, words

LAUGH = re.compile(
    r"\[?(?:laugh(?:ing|ter|s)?|haha+|hehe+|hehehe|lol+|lmao|rofl|😂|🤣)\]?", re.IGNORECASE
)
WOW = re.compile(
    r"(?:oh my god|omg+|omfg|wtf|no way|holy\s+\w+|insane|crazy|unbelievable|"
    r"are you serious|you\'re kidding|wait what|i can\'t|this is wild|oh no)",
    re.IGNORECASE,
)
PUNCHWORDS = {
    "funny": 2, "hilarious": 3, "dead": 1.5, "kill": 1.5, "crack": 1.5, "joke": 1.5,
    "weird": 1.5, "awkward": 2, "bruh": 2, "bro": 1, "dude": 1, "clip that": 3,
    "and then": 1, "but then": 1.5, "turns out": 1.5, "plot twist": 3, "oops": 2,
    "trap": 1.5, "choked": 2, "snapped": 2, "meltdown": 2, "instant": 1, "regret": 2,
    "worst": 1.5, "best": 1, "never": 0.5, "literally": 1, "somehow": 1, "exactly": 1,
}


def audio_levels(video: Path) -> list[tuple[float, float]]:
    """Return (time_seconds, RMS dB) pairs sampled twice per second."""
    cmd = [
        ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-i", str(video),
        "-vn", "-af",
        "aresample=48000,asetnsamples=n=24000:pad=0,astats=metadata=1:reset=1,"
        "ametadata=print:key=lavfi.astats.Overall.RMS_level:file=-",
        "-f", "null", "-",
    ]
    try:
        proc = run(cmd, timeout=1800)
    except RuntimeError:
        return []
    out = []
    ts = 0.0
    for line in (proc.stdout or "").splitlines():
        m = re.search(r"pts_time:([0-9.]+)", line)
        if m:
            ts = float(m.group(1))
            continue
        m = re.search(r"Overall\.RMS_level=(-?[0-9.eE]+|inf|-inf)", line)
        if m:
            try:
                out.append((ts, float(m.group(1))))
            except ValueError:
                pass
    return out


def _level_stats(levels: list[tuple[float, float]], start: float, end: float) -> tuple[float, float]:
    vals = [db for ts, db in levels if start <= ts <= end and math.isfinite(db)]
    if len(vals) < 3:
        return 0.0, 0.0
    mean = statistics.fmean(vals)
    std = statistics.pstdev(vals) or 1.0
    return mean, std


def build_candidates(
    segments: list[dict],
    *,
    clip_length: float = 30.0,
    duration: float | None = None,
    levels: list[tuple[float, float]] | None = None,
) -> list[dict]:
    """Sliding windows that begin and end near caption boundaries."""
    if not segments:
        return []
    duration = duration or max(s["end"] for s in segments)
    levels = levels or []
    ends = sorted({round(s["end"], 2) for s in segments})

    def snap(t: float) -> float:
        nearest = min(ends, key=lambda e: abs(e - t)) if ends else t
        return nearest if abs(nearest - t) <= 1.6 else t

    candidates: dict[tuple[int, int], dict] = {}
    for seg in segments:
        start = round(seg["start"], 2)
        end = min(snap(start + clip_length), duration)
        if end - start < clip_length * 0.6:
            continue
        key = (int(start * 10), int(end * 10))
        if key in candidates:
            continue
        text = " ".join(
            s["text"] for s in segments if s["end"] > start + 0.05 and s["start"] < end - 0.05
        ).strip()
        word_count = len(words(text))
        if word_count < 4:
            continue
        mean_db, std_db = _level_stats(levels, start, end)
        candidates[key] = {
            "start": start,
            "end": round(end, 2),
            "duration": round(end - start, 2),
            "text": text,
            "word_count": word_count,
            "mean_db": mean_db,
            "std_db": std_db,
        }
    return sorted(candidates.values(), key=lambda c: c["start"])


def heuristic_score(c: dict, *, duration: float) -> float:
    text = c["text"]
    lower = text.lower()
    tokens = words(text)
    n = max(len(tokens), 1)
    laugh = min(len(LAUGH.findall(text)), 5)
    wow = min(len(WOW.findall(text)), 5)
    excl = min(text.count("!"), 7)
    quest = min(text.count("?"), 6)
    punch = min(sum(PUNCHWORDS.get(t.lower(), 0) for t in tokens), 8)
    caps = sum(1 for t in tokens if len(t) >= 3 and t.isupper()) / n
    repeat = 0
    for i in range(1, len(tokens)):
        if tokens[i].lower() == tokens[i - 1].lower() and len(tokens[i]) > 1:
            repeat += 1
    density = c["word_count"] / max(c["duration"], 1.0)
    mean_db = c.get("mean_db", 0.0)
    std_db = c.get("std_db", 0.0)
    loudness = clamp((mean_db + 32) / 24, 0.0, 1.0) if mean_db else 0.5

    score = 26.0
    score += laugh * 9.0
    score += wow * 5.0
    score += excl * 2.6
    score += quest * 1.8
    score += punch * 2.2
    score += min(caps * 60, 10)
    score += min(repeat * 3, 6)
    score += min(density, 3.2) * 3.0
    score += min(std_db, 12) * 0.9 if std_db else 0
    score += loudness * 6

    if c["start"] < 18:
        score -= 12 * (1 - c["start"] / 18)
    if duration and duration - c["end"] < 12:
        score -= 8
    if c["word_count"] < 9:
        score -= 10
    if mean_db and mean_db < -30:
        score -= 8
    return clamp(score)


def ai_score(candidates: list[dict], api_key: str, *, model: str = "gpt-4o-mini", duration: float = 0) -> dict[int, tuple[float, str]]:
    """Score windows with an LLM. Returns {index: (0-100 score, reason)}."""
    items = [
        {
            "i": i,
            "start": round(c["start"], 1),
            "end": round(c["end"], 1),
            "transcript": short_text(c["text"], 650),
        }
        for i, c in enumerate(candidates)
    ]
    results: dict[int, tuple[float, str]] = {}
    batch_size = 24
    for offset in range(0, len(items), batch_size):
        batch = items[offset : offset + batch_size]
        payload = {
            "model": model,
            "temperature": 0.2,
            "response_format": {"type": "json_object"},
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You are a viral short-video editor. Rate each transcript window for how "
                        "interesting, surprising, or funny it would be as a 30-second YouTube Short. "
                        "Favor punchlines, tension, reactions, awkward moments, and clear payoffs. "
                        "Penalize intros, dull exposition, and fragments with no payoff. "
                        "Reply with ONLY JSON: {\"ratings\":[{\"i\":0,\"score\":0,\"why\":\"short reason\"}]} "
                        "where score is an integer 0-10. Include every index exactly once."
                    ),
                },
                {"role": "user", "content": json.dumps(batch, ensure_ascii=False)},
            ],
        }
        req = urllib.request.Request(
            "https://api.openai.com/v1/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=90) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        content = body["choices"][0]["message"]["content"]
        start, end = content.find("["), content.rfind("]")
        if start < 0 or end < start:
            continue
        try:
            ratings = json.loads(content[start : end + 1])
        except json.JSONDecodeError:
            continue
        for item in ratings:
            try:
                idx = int(item["i"])
                score = clamp(float(item["score"]) * 10)
                results[idx] = (score, str(item.get("why", ""))[:160])
            except (KeyError, TypeError, ValueError):
                continue
    return results


RANK_RULES = (
    "You are a viral short-video editor. For every transcript window, rate how likely it is to be an "
    "interesting or funny moment in a 30-second YouTube Short. Favor punchlines, surprise, tension, "
    "reactions and awkward moments; penalize intros, dull exposition and fragments without payoff.\n"
    "Return exactly one line per window, in the same order, with NO other text, NO markdown, NO code fences:\n"
    "INDEX|SCORE|SHORT_REASON\n"
    "INDEX is the integer i from the window. SCORE is an integer from 0 to 10. "
    "SHORT_REASON is at most 8 words."
)


def _opencode_bin() -> str | None:
    override = os.environ.get("OPENCODE_BIN")
    if override and Path(override).is_file():
        return override
    # Prefer the real executable: the npm .CMD/.PS1 wrappers mangle '|' in the prompt.
    installed = Path(os.environ.get("APPDATA", "")) / "npm/node_modules/@opencode/cli/bin/opencode.exe"
    if installed.is_file():
        return str(installed)
    found = shutil.which("opencode.exe")
    if found:
        return found
    return None


def _opencode_text(stdout: str) -> str:
    parts: list[str] = []
    for line in stdout.splitlines():
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if obj.get("type") == "text":
            parts.append(str(obj.get("part", {}).get("text", "")))
    return "\n".join(parts) if parts else stdout


def _parse_rank_text(text: str) -> dict[int, tuple[float, str]]:
    results: dict[int, tuple[float, str]] = {}
    start, end = text.find("["), text.rfind("]")
    if start >= 0 and end > start:
        try:
            for item in json.loads(text[start : end + 1]):
                results[int(item["i"])] = (
                    clamp(float(item["score"]) * 10),
                    str(item.get("why", ""))[:160],
                )
            if results:
                return results
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            pass
    for line in text.splitlines():
        match = re.search(r"\b(\d+)\s*\|\s*(\d+(?:\.\d+)?)\s*\|\s*(.+)", line)
        if match:
            results[int(match.group(1))] = (
                clamp(float(match.group(2)) * 10),
                match.group(3).strip().strip("`")[:160],
            )
    return results


def opencode_score(candidates: list[dict], model: str = "opencode/big-pickle") -> dict[int, tuple[float, str]]:
    """Rank windows with Big Pickle through the local OpenCode CLI."""
    binary = _opencode_bin()
    if not binary:
        raise RuntimeError("OpenCode CLI not found (set OPENCODE_BIN to its executable).")

    results: dict[int, tuple[float, str]] = {}
    batch_size = 20
    for offset in range(0, len(candidates), batch_size):
        batch = [
            {"i": i, "start": round(c["start"], 1), "end": round(c["end"], 1),
             "transcript": short_text(c["text"], 600)}
            for i, c in enumerate(candidates[offset : offset + batch_size], start=offset)
        ]
        prompt = RANK_RULES + "\n\nWINDOWS (JSON):\n" + json.dumps(batch, ensure_ascii=False)
        cmd = [
            binary, "run", "-m", model, "--format", "json", "--archived", "--auto",
            "--title", "reel-clipper ranking", prompt,
        ]
        proc = subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300
        )
        if proc.returncode != 0:
            tail = (proc.stderr or proc.stdout or "").strip()[-800:]
            raise RuntimeError(f"OpenCode ranking failed: {tail}")
        results.update(_parse_rank_text(_opencode_text(proc.stdout)))
    return results


def select_clips(
    candidates: list[dict],
    *,
    count: int = 4,
    clip_length: float = 30.0,
    duration: float = 0.0,
    ai_key: str | None = None,
    ai_model: str = "opencode/big-pickle",
    min_gap: float = 6.0,
    progress=lambda _msg: None,
) -> list[dict]:
    if not candidates:
        return []

    # Long videos can produce thousands of caption windows. Sample them evenly
    # so AI ranking stays bounded (roughly a dozen CLI calls for a 6-hour video).
    max_windows = 240
    if len(candidates) > max_windows:
        step = len(candidates) / max_windows
        candidates = [
            candidates[min(len(candidates) - 1, int(i * step))]
            for i in range(max_windows)
        ]

    for c in candidates:
        c["heuristic"] = round(heuristic_score(c, duration=duration), 1)
        c["score"] = c["heuristic"]
        c["reason"] = "heuristic"

    use_opencode = bool(ai_model) and (
        ai_model == "big-pickle" or ai_model.startswith("opencode/")
    )
    use_openai = bool(ai_key) and bool(ai_model) and str(ai_model).lower() != "none"
    if use_opencode or use_openai:
        label = "Big Pickle" if use_opencode else "AI"
        progress(f"Using {label} to rank the interesting moments…")
        try:
            if use_opencode:
                model = "opencode/big-pickle" if ai_model == "big-pickle" else ai_model
                ratings = opencode_score(candidates, model=model)
            else:
                ratings = ai_score(candidates, ai_key, model=ai_model, duration=duration)
            for i, (score, why) in ratings.items():
                if 0 <= i < len(candidates):
                    c = candidates[i]
                    blended = 0.4 * c["heuristic"] + 0.6 * score
                    c["score"] = round(blended, 1)
                    c["ai_score"] = round(score, 1)
                    c["reason"] = why or c["reason"]
            progress(f"{label} ranked {len(ratings)} windows.")
        except Exception as exc:
            progress(f"{label} ranking unavailable ({exc}); using local scoring.")

    ranked = sorted(candidates, key=lambda c: c["score"], reverse=True)
    chosen: list[dict] = []
    for c in ranked:
        if len(chosen) >= count:
            break
        clash = False
        for other in chosen:
            overlap = min(c["end"], other["end"]) - max(c["start"], other["start"])
            if overlap > 0:
                shorter = min(c["duration"], other["duration"])
                if overlap / max(shorter, 1) > 0.45 or abs(c["start"] - other["start"]) < min_gap:
                    clash = True
                    break
        if not clash:
            chosen.append(c)

    chosen.sort(key=lambda c: c["start"])
    for n, c in enumerate(chosen, 1):
        c["index"] = n
        c["filename"] = f"clip_{n:02d}.mp4"
        c["preview"] = short_text(c["text"], 180)
    return chosen
