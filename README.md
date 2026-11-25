# Reel Clipper

Turn a long YouTube video into short, vertical, captioned clips ready for **YouTube Shorts, Instagram Reels and TikTok**.

Paste a link into the local web UI or run one CLI command. Reel Clipper finds energetic, funny or surprising transcript moments, renders them at **1080×1920 / 30 fps**, and caches the download so the same video is never fetched twice.

> Only clip content you have the rights to reuse.

## Features

- **CLI + local web UI** at <http://127.0.0.1:7860>.
- **1080×1920, 30 fps, H.264/AAC, `+faststart`** output.
- **Blur-background** vertical layout by default; **center crop** alternative.
- **Burned captions** styled for vertical video: bold yellow Arial Black, top-centered, two lines max.
- Free **heuristic scoring** by default, with optional **Big Pickle** ranking via the local OpenCode CLI, or an OpenAI model.
- Uses **YouTube captions**; falls back to local **Whisper** transcription when needed.
- **Persistent cache** for video, transcript and audio levels — changing clip count, length or style never re-downloads the source.
- **Local-source / fast-test mode** for reusing an already downloaded video or recovering split `source.f*.mp4` + `source.f*.m4a` parts.

## Quick start

Full platform instructions are in [SETUP.md](SETUP.md). Minimal Windows setup:

```bat
cd reel-clipper
python -m pip install -r requirements.txt
run_web.bat
```

Open <http://127.0.0.1:7860>, paste a link, choose the number/length of clips and click **Find & cut clips**.

CLI:

```bat
python -m clipmaker "https://www.youtube.com/watch?v=VIDEO_ID" -n 5 --length 30
```

Clips land in `output/<video-title>/clip_01.mp4`, `clip_02.mp4`, … with a `manifest.json` describing each moment.

A real 30-second sample output is included at [`examples/clip_01.mp4`](examples/clip_01.mp4).

## Requirements

- **Python 3.10+**
- **ffmpeg** on `PATH` — `ffmpeg -version` should work.
- `yt-dlp` and `Flask`, installed by `requirements.txt`.

Optional:

- Local transcription for videos without subtitles:

  ```bat
  python -m pip install -r requirements-whisper.txt
  ```

  First run downloads a Whisper model (~500 MB for `small`). `av>=11,<18` is pinned because PyAV 19 breaks `faster-whisper`.

- **Big Pickle ranking** through the local [OpenCode CLI](https://opencode.ai/docs/). No OpenAI key is needed. If OpenCode is installed but not detected, set `OPENCODE_BIN` to its executable. Use `--ai-model none` to stay fully free/offline.

- OpenAI ranking instead: `--ai-model gpt-4o-mini --openai-key sk-...`.

## Web UI

1. Start with `run_web.bat` or `python -m clipmaker.web`.
2. Paste a public YouTube URL, or pick an already downloaded video from **Already downloaded video**.
3. Set clip count, length, layout, caption languages and ranking model.
4. Tick **Fast test** to render one clip immediately from a fixed start second — no captions, transcription, audio analysis or AI ranking.
5. Watch the progress bar; finished clips appear as playable cards with source link, folder, download and full-video buttons.

## CLI

```
python -m clipmaker URL [options]
```

| Option | Meaning |
| --- | --- |
| `URL` | YouTube video URL. Optional when `--local-file` is used. |
| `-n, --count` | Number of clips to export (default 4). Overlaps are filtered. |
| `--length` | Target clip length in seconds (default 30). |
| `--style blur\|crop` | Blurred background or center crop. |
| `--no-captions` | Export without burned subtitles. |
| `--lang` | Preferred caption languages, comma separated (default `en,en-US,hi,bn`). |
| `--language` | Force Whisper language, e.g. `bn`. |
| `--whisper-model` | Whisper model for videos without captions (default `small`). |
| `--ai-model` | `opencode/big-pickle` (default), an OpenAI model name, or `none`. |
| `--openai-key` | Required only for OpenAI models; defaults to `OPENAI_API_KEY`. |
| `--max-height` | Maximum source resolution (default 1080). |
| `--out` | Output root (default `output`). |
| `--keep-source` | Keep the full downloaded video in the output folder. |
| `--json` | Print the final manifest as JSON. |
| `--local-file` | Use an existing video file or output folder — never downloads. |
| `--fast-test` | Render one short clip immediately, skipping captions/transcription/analysis/AI. |
| `--test-start` | Start second for `--fast-test` (default 300). |

Examples:

```bat
REM Free heuristic ranking, four 30s blur-background clips
python -m clipmaker "LINK" -n 4 --length 30 --ai-model none

REM Center crop, Bengali captions, GPT ranking
python -m clipmaker "LINK" --style crop --lang bn --ai-model gpt-4o-mini --openai-key sk-...

REM Reuse an already downloaded video folder
python -m clipmaker --local-file "output\my-video-VIDEOID" -n 5

REM Fast 15-second render test from 10:00
python -m clipmaker --local-file "output\my-video-VIDEOID" --fast-test --test-start 600 --length 15
```

## How it picks the "interesting" part

1. Downloads the best source up to 1080p, unless a local source is supplied.
2. Reads existing YouTube captions; otherwise transcribes locally with Whisper.
3. Measures audio loudness/variation twice per second with ffmpeg.
4. Scores ~30-second caption windows using laughter, exclamations, questions, punchwords, repetition, speech density, audio energy and intro/outro penalties.
5. Big Pickle (or another selected model) re-ranks the windows; the two scores are blended 60/40.
6. Removes overlapping moments and renders the best N clips.

Scoring is heuristic/LLM-assisted — a best guess, not a guarantee. Increase `--count` for more choices.

## Output details

- 1080×1920, exact 9:16, 30 fps, H.264/AAC, `+faststart`.
- Captions burned in as bold yellow Arial Black ASS subtitles: 54 px, top-centered with a 240 px safe margin, two lines max.
- Each clip has a manifest entry with its source timestamp, score, AI reason and transcript preview.
- The web results panel shows source URL, folder, **Open folder**, download and **Full video** links.

## Caching and local reuse

The first run stores the video in `output/_cache/<video-id>.mp4`, plus transcript and audio-level caches. Running the same link again — even with a different clip count, length or style — **does not download the video again** and skips transcription. The UI shows `Reused cached download · cached transcript`.

If an earlier download was interrupted, the folder may contain `source.f*.mp4` and `source.f*.m4a`. Reel Clipper stream-copies them into `source.mp4` locally using `--local-file <folder>`, or you can pick the folder in the web UI. No re-download.

Delete `output/_cache` to force fresh downloads.

## Troubleshooting

- **`ffmpeg was not found on PATH`** — install ffmpeg and restart the terminal.
- **`yt-dlp is not installed`** — run `python -m pip install -r requirements.txt`.
- **No captions found** — install `requirements-whisper.txt`, or use a video with subtitles.
- **Sign-in / members-only / age-gated video** — yt-dlp cannot access it; use a public video.
- **AI ranking fails** — set `OPENCODE_BIN`, use `--ai-model none`, or provide an OpenAI key.
- **Clips look small in the middle** — use `--style crop` for already-vertical source videos.

## Project layout

```
clipmaker/
  cli.py                     command-line interface
  web.py                     local Flask web UI + job API
  pipeline.py                orchestration and caching
  download.py                yt-dlp download, captions, local-source recovery
  transcript.py              VTT/ASS parsing and caption formatting
  scoring.py                 moment detection, Big Pickle/OpenAI ranking
  render.py                  ffmpeg vertical rendering
  utils.py                   ffmpeg helpers
  web/templates/index.html   web UI
output/                      generated clips, manifests and cache (git-ignored)
SETUP.md                     detailed setup guide
```

## Contributing

Issues and pull requests are welcome. Please keep generated media out of commits — `output/` is git-ignored. For large changes, open an issue first so the approach can be discussed.

## License

[MIT](LICENSE) © 2026 Ashiqur Rahman Anik.
