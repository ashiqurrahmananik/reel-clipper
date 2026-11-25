from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .download import download_video, extract_info, get_captions, recover_source, transcribe
from .render import render_clip
from .scoring import audio_levels, build_candidates, select_clips
from .transcript import load_transcript, merge_segments
from .utils import ensure_dir, probe_duration, slugify

Progress = Callable[[str], None]


@dataclass
class Options:
    count: int = 4
    length: float = 30.0
    style: str = "blur"  # blur | crop
    captions: bool = True
    languages: list[str] = field(default_factory=lambda: ["en", "en-US", "hi", "bn"])
    whisper_model: str = "small"
    language: str | None = None
    openai_key: str | None = None
    ai_model: str = "opencode/big-pickle"
    max_height: int = 1080
    keep_source: bool = False
    local_source: str | None = None
    fast_test: bool = False
    test_start: float = 300.0


def _report(progress: Progress | None, message: str) -> None:
    if progress:
        progress(message)


def make_clips(
    url: str,
    options: Options | None = None,
    *,
    out_root: Path | None = None,
    progress: Progress | None = None,
) -> dict:
    opts = options or Options()
    out_root = Path(out_root or Path.cwd() / "output")
    local_path = Path(opts.local_source).expanduser() if opts.local_source else None

    if local_path:
        _report(progress, "Using already downloaded video — skipping download.")
        if not local_path.exists():
            raise RuntimeError(f"Local video not found: {local_path}")
        source = (
            recover_source(local_path, progress=progress)
            if local_path.is_dir() else local_path.resolve()
        )
        if not source.is_file() or source.stat().st_size < 10_000:
            raise RuntimeError(f"Local video is missing or empty: {source}")
        duration = probe_duration(source)
        inside_output = source.parent.resolve().is_relative_to(out_root.resolve())
        folder = ensure_dir(
            out_root / (source.parent.name if inside_output else f"local-{slugify(source.stem)}")
        )
        saved_meta: dict = {}
        meta_file = source.parent / "source.meta.json"
        if meta_file.is_file():
            try:
                saved_meta = json.loads(meta_file.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                saved_meta = {}
        id_match = re.search(r"([A-Za-z0-9_-]{11})$", folder.name) or re.search(
            r"([A-Za-z0-9_-]{10})$", folder.name
        )
        video_id = str(saved_meta.get("video_id") or "") or (id_match.group(1) if id_match else "")
        title = str(saved_meta.get("title") or "") or (
            re.sub(r"-[A-Za-z0-9_-]{10,11}$", "", folder.name).replace("-", " ").strip().title()
            or source.stem
        )
        url = f"https://www.youtube.com/watch?v={video_id}" if video_id else str(source)
        info = {}
        if video_id and not opts.fast_test:
            try:
                info = extract_info(url)
            except Exception as exc:
                _report(
                    progress,
                    f"Could not read online caption info ({exc}); using cached transcript if available.",
                )
        from_cache = True
    else:
        _report(progress, "Reading video information…")
        info = extract_info(url)
        title = str(info.get("title") or "video")
        duration = float(info.get("duration") or 0.0)
        video_id = str(info.get("id") or "")
        folder = ensure_dir(out_root / f"{slugify(title)}-{video_id[:10]}".strip("-"))
        (folder / "source.meta.json").write_text(
            json.dumps({"video_id": video_id, "title": title}, ensure_ascii=False),
            encoding="utf-8",
        )
        cache_dir = ensure_dir(out_root / "_cache")
        cache_key = video_id or slugify(title)
        cached_video = cache_dir / f"{cache_key}.mp4"
        cache_meta = cache_dir / f"{cache_key}.json"

        meta: dict = {}
        if cache_meta.is_file():
            try:
                meta = json.loads(cache_meta.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                meta = {}
        can_reuse = (
            cached_video.is_file()
            and cached_video.stat().st_size > 10_000
            and int(meta.get("max_height", 0)) >= opts.max_height
        )

        if can_reuse:
            source = cached_video
            from_cache = True
            _report(progress, "Using cached video — skipping download.")
        else:
            downloaded = download_video(url, folder, max_height=opts.max_height, progress=progress)
            shutil.copy2(downloaded, cached_video)
            cache_meta.write_text(json.dumps({"max_height": opts.max_height}), encoding="utf-8")
            source = cached_video
            from_cache = False

        local_source = folder / "source.mp4"
        if opts.keep_source:
            if source.resolve() != local_source.resolve():
                shutil.copy2(source, local_source)
        else:
            for stale in folder.glob("source.*"):
                if stale.name != "source.meta.json":
                    stale.unlink(missing_ok=True)

    for old in folder.glob("clip_*.mp4"):
        old.unlink(missing_ok=True)
    (folder / "manifest.json").unlink(missing_ok=True)

    cache_dir = ensure_dir(out_root / "_cache")
    cache_key = video_id or slugify(source.stem)
    transcript_cache = cache_dir / f"{cache_key}.transcript.json"
    levels_cache = cache_dir / f"{cache_key}.levels.json"

    from_transcript_cache = False
    mode = "none"

    if opts.fast_test:
        _report(progress, "Fast test: skipping captions, transcription and AI ranking.")
        raw_segments: list[dict] = []
        segments: list[dict] = []
        mode = "fast-test (no transcript)"
    else:
        if transcript_cache.is_file():
            try:
                cached = json.loads(transcript_cache.read_text(encoding="utf-8"))
                raw_segments = cached["segments"]
                mode = cached["mode"]
                from_transcript_cache = True
                _report(progress, "Using cached transcript — skipping captions/transcription.")
            except (json.JSONDecodeError, KeyError, TypeError):
                from_transcript_cache = False

        if not from_transcript_cache:
            if local_path and not info:
                raise RuntimeError(
                    "No captions or cached transcript for this local video. "
                    "Enable Fast test to render one clip immediately."
                )
            caption_path, caption_lang = get_captions(
                info, folder, preferred=opts.languages, progress=progress
            )
            if caption_path:
                raw_segments = load_transcript(caption_path)
                mode = f"youtube-captions:{caption_lang}"
            elif local_path:
                raise RuntimeError(
                    "This local video has no online captions and no cached transcript. "
                    "Enable Fast test to render one clip immediately."
                )
            else:
                raw_segments = transcribe(
                    source,
                    folder,
                    model_size=opts.whisper_model,
                    language=opts.language,
                    progress=progress,
                )
                mode = f"whisper:{opts.whisper_model}"
            transcript_cache.write_text(
                json.dumps({"mode": mode, "segments": raw_segments}, ensure_ascii=False),
                encoding="utf-8",
            )

        segments = merge_segments(raw_segments)
        _report(progress, f"Transcript ready: {len(segments)} lines. Analysing energy…")
        if levels_cache.is_file():
            try:
                levels = [tuple(pair) for pair in json.loads(levels_cache.read_text(encoding="utf-8"))]
            except (json.JSONDecodeError, TypeError, ValueError):
                levels = audio_levels(source)
        else:
            levels = audio_levels(source)
            levels_cache.write_text(json.dumps(levels), encoding="utf-8")
        candidates = build_candidates(
            segments,
            clip_length=opts.length,
            duration=duration or max((s["end"] for s in segments), default=0.0),
            levels=levels,
        )
        if not candidates:
            raise RuntimeError("No usable transcript windows were found in this video.")

        chosen = select_clips(
            candidates,
            count=opts.count,
            clip_length=opts.length,
            duration=duration,
            ai_key=opts.openai_key,
            ai_model=opts.ai_model,
            progress=progress,
        )
        if not chosen:
            raise RuntimeError("Could not select distinct clips from this video.")

    if opts.fast_test:
        start = max(0.0, opts.test_start)
        end = start + max(5.0, min(opts.length, 90.0))
        if duration:
            start = min(start, max(0.0, duration - 5.0))
            end = min(end, duration)
        chosen = [
            {
                "index": 1,
                "filename": "clip_01.mp4",
                "start": round(start, 2),
                "end": round(end, 2),
                "duration": round(end - start, 2),
                "text": "",
                "word_count": 0,
                "score": 100.0,
                "heuristic": 100.0,
                "ai_score": None,
                "reason": "fast test",
                "preview": f"Test render from {start:.0f}s",
            }
        ]

    clips = []
    for c in chosen:
        _report(
            progress,
            f"Rendering clip {c['index']}/{len(chosen)} · {c['start']:.0f}s–{c['end']:.0f}s · score {c['score']}",
        )
        output = folder / c["filename"]
        use_captions = opts.captions and not opts.fast_test
        render_clip(
            source,
            segments,
            start=c["start"],
            end=c["end"],
            output=output,
            style=opts.style,
            captions=use_captions,
            preset="ultrafast" if opts.fast_test else "veryfast",
            crf=25 if opts.fast_test else 21,
        )
        clips.append(
            {
                "index": c["index"],
                "filename": c["filename"],
                "start": round(c["start"], 2),
                "end": round(c["end"], 2),
                "duration": c["duration"],
                "score": c["score"],
                "heuristic": c.get("heuristic"),
                "ai_score": c.get("ai_score"),
                "reason": c.get("reason", ""),
                "preview": c.get("preview", ""),
                "url": f"https://www.youtube.com/watch?v={video_id}&t={int(c['start'])}s" if video_id else "",
                "bytes": output.stat().st_size,
            }
        )

    manifest = {
        "source_url": url,
        "title": title,
        "video_id": video_id,
        "duration": duration,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "transcript_mode": mode,
        "ai_model": "none (fast test)" if opts.fast_test else opts.ai_model,
        "style": opts.style,
        "captions": opts.captions and not opts.fast_test,
        "keep_source": opts.keep_source,
        "fast_test": opts.fast_test,
        "local_source": str(local_path) if local_path else None,
        "from_cache": from_cache,
        "from_transcript_cache": from_transcript_cache,
        "folder": str(folder),
        "clips": clips,
    }
    (folder / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    _report(progress, f"Done: {len(clips)} clips in {folder}")
    return manifest
