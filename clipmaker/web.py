from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import uuid
from pathlib import Path

from flask import Flask, jsonify, render_template, request, send_file

from .pipeline import Options, make_clips
from .utils import probe_duration, slugify

ROOT = Path(__file__).resolve().parent.parent
OUTPUT = ROOT / "output"
TEMPLATES = ROOT / "clipmaker" / "web" / "templates"

app = Flask(__name__, template_folder=str(TEMPLATES))
app.config["MAX_CONTENT_LENGTH"] = 64 * 1024
JOBS: dict[str, dict] = {}
LOCK = threading.Lock()


def _update(job_id: str, **changes) -> None:
    with LOCK:
        JOBS[job_id].update(changes)


def _infer_percent(msg: str) -> float | None:
    """Map a pipeline progress message to an overall 0-100 estimate."""
    lower = msg.lower()
    if "downloading video" in lower:
        match = re.search(r"(\d+(?:\.\d+)?)%", msg)
        return 5 + (float(match.group(1)) * 0.4 if match else 0.0)
    if "reading video information" in lower:
        return 2
    if "download finished" in lower:
        return 45
    if (
        "source ready" in lower or "using cached video" in lower
        or "using already downloaded video" in lower or "reusing local video parts" in lower
        or "muxed existing video" in lower
    ):
        return 47
    if "using cached transcript" in lower:
        return 60
    if any(word in lower for word in ("caption", "transcri", "whisper", "audio extracted")):
        return 53
    if "transcript ready" in lower or "fast test: skipping" in lower:
        return 62
    if "pickle" in lower or "ranking" in lower or " to rank " in lower or "ai to rank" in lower:
        return 68
    match = re.search(r"rendering clip (\d+)/(\d+)", lower)
    if match:
        index, total = int(match.group(1)), max(int(match.group(2)), 1)
        return 72 + ((index - 1) / total) * 25
    if lower.startswith("done"):
        return 100
    return None


def _worker(job_id: str, url: str, opts: Options) -> None:
    try:
        last_percent = 0.0

        def progress(msg: str) -> None:
            nonlocal last_percent
            percent = _infer_percent(msg)
            if percent is not None:
                last_percent = max(last_percent, min(100.0, percent))
            _update(job_id, progress=msg, percent=round(last_percent, 1))

        manifest = make_clips(url, opts, out_root=OUTPUT, progress=progress)
        _update(job_id, status="done", progress="Finished.", percent=100, manifest=manifest)
    except Exception as exc:
        _update(job_id, status="error", progress="Failed.", error=str(exc))


@app.get("/")
def index():
    return render_template("index.html")


@app.post("/api/jobs")
def create_job():
    data = request.get_json(silent=True) or {}
    local_folder = str(data.get("local_folder") or "").strip()
    fast_test = bool(data.get("fast_test", False))
    if local_folder:
        local_path = _folder_path(local_folder)
        if not local_path:
            return jsonify(error="Unknown downloaded video folder."), 404
        url = str(local_path)
    else:
        url = str(data.get("url") or "").strip()
        if not url.startswith(("http://", "https://")):
            return jsonify(error="Enter a valid http(s) video link or pick a downloaded video."), 400
    try:
        count = max(1, min(12, int(data.get("count", 4))))
        length = max(10, min(90, float(data.get("length", 30))))
        test_start = max(0.0, float(data.get("test_start", 300)))
    except (TypeError, ValueError):
        return jsonify(error="Invalid count or length."), 400
    if fast_test:
        count, length = 1, min(length, 90)
    style = "crop" if data.get("style") == "crop" else "blur"
    api_key = str(data.get("api_key") or "").strip() or None
    opts = Options(
        count=count,
        length=length,
        style=style,
        captions=bool(data.get("captions", True)),
        languages=[x.strip() for x in str(data.get("lang") or "en,en-US,hi,bn").split(",") if x.strip()],
        whisper_model=str(data.get("whisper_model") or "small"),
        language=str(data.get("language") or "").strip() or None,
        openai_key=api_key,
        ai_model=str(data.get("ai_model") or "opencode/big-pickle"),
        keep_source=bool(data.get("keep_source", False)),
        local_source=url if local_folder else None,
        fast_test=fast_test,
        test_start=test_start,
    )
    job_id = uuid.uuid4().hex[:12]
    with LOCK:
        JOBS[job_id] = {
            "id": job_id,
            "status": "queued",
            "progress": "Queued…",
            "percent": 0,
            "error": None,
            "manifest": None,
            "ai": bool(api_key),
            "fast_test": fast_test,
        }
    threading.Thread(target=_worker, args=(job_id, url, opts), daemon=True).start()
    return jsonify(id=job_id)


@app.get("/api/jobs/<job_id>")
def job_status(job_id: str):
    with LOCK:
        job = JOBS.get(job_id)
        if not job:
            return jsonify(error="Unknown job."), 404
        return jsonify(dict(job))


@app.get("/api/jobs/<job_id>/clips/<filename>")
def download_clip(job_id: str, filename: str):
    if not re.fullmatch(r"clip_\d+\.mp4", filename):
        return jsonify(error="Invalid filename."), 400
    with LOCK:
        job = JOBS.get(job_id)
    if not job or not job.get("manifest"):
        return jsonify(error="Unknown job."), 404
    path = Path(job["manifest"]["folder"]) / filename
    if not path.is_file():
        return jsonify(error="File missing."), 404
    return send_file(path, mimetype="video/mp4", as_attachment=True, download_name=f"{slugify(job['manifest']['title'])}_{filename}")


@app.get("/api/library")
def library():
    items = []
    for manifest_path in sorted(OUTPUT.glob("*/manifest.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            items.append(
                {
                    "title": manifest.get("title"),
                    "folder": Path(manifest.get("folder", "")).name,
                    "source_url": manifest.get("source_url"),
                    "created_at": manifest.get("created_at"),
                    "clips": [
                        {
                            "filename": c["filename"],
                            "preview": c.get("preview", ""),
                            "score": c.get("score"),
                            "start": c.get("start"),
                            "end": c.get("end"),
                        }
                        for c in manifest.get("clips", [])
                    ],
                }
            )
        except (json.JSONDecodeError, OSError, KeyError):
            continue
    return jsonify(items=items[:30])


@app.get("/api/library/<folder>/<filename>")
def library_clip(folder: str, filename: str):
    if not re.fullmatch(r"[\w-]+", folder) or not re.fullmatch(r"clip_\d+\.mp4", filename):
        return jsonify(error="Invalid path."), 400
    path = OUTPUT / folder / filename
    if not path.is_file():
        return jsonify(error="File missing."), 404
    return send_file(path, mimetype="video/mp4", as_attachment=True, download_name=filename)


def _folder_path(folder: str) -> Path | None:
    if not re.fullmatch(r"[\w-]+", folder):
        return None
    path = (OUTPUT / folder).resolve()
    if not path.is_relative_to(OUTPUT.resolve()):
        return None
    return path if path.is_dir() else None


@app.get("/api/local-sources")
def local_sources():
    items = []
    if not OUTPUT.is_dir():
        return jsonify(items=items)
    for path in sorted(OUTPUT.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
        if not path.is_dir() or path.name == "_cache":
            continue
        complete = path / "source.mp4"
        parts = sorted(path.glob("source.f*.mp4")) + sorted(path.glob("source.f*.m4a"))
        if not complete.is_file() and not parts:
            continue
        ready = complete.is_file() and complete.stat().st_size > 10_000 and probe_duration(complete) > 0
        video = complete if ready else (parts[0] if parts else complete)
        duration = probe_duration(video) if video.is_file() else 0.0
        items.append(
            {
                "folder": path.name,
                "label": re.sub(r"-[A-Za-z0-9_-]{10}$", "", path.name).replace("-", " ").strip().title()
                or path.name,
                "duration": round(duration, 1),
                "size": video.stat().st_size if video.is_file() else 0,
                "needs_mux": not ready,
                "clips": len(list(path.glob("clip_*.mp4"))),
            }
        )
    return jsonify(items=items[:20])


@app.post("/api/open/<folder>")
def open_folder(folder: str):
    path = _folder_path(folder)
    if not path:
        return jsonify(error="Unknown folder."), 404
    try:
        if sys.platform == "win32":
            os.startfile(str(path))
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path)])
    except OSError as exc:
        return jsonify(error=f"Could not open folder: {exc}"), 500
    return jsonify(ok=True, path=str(path))


@app.get("/api/source/<folder>")
def download_source(folder: str):
    path = _folder_path(folder)
    if not path:
        return jsonify(error="Unknown folder."), 404
    source = path / "source.mp4"
    if not source.is_file():
        return jsonify(error="Full video was not kept. Enable 'Keep full video' when creating the job."), 404
    return send_file(source, mimetype="video/mp4", as_attachment=True, download_name="source.mp4")


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Local web UI for reel-clipper")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=7860)
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()
    print(f"Open http://{args.host}:{args.port} in your browser")
    app.run(host=args.host, port=args.port, debug=args.debug, threaded=True)


if __name__ == "__main__":
    main()
