# Setup guide

Reel Clipper needs Python 3.10+ and `ffmpeg`. Everything else is installed with pip.

## 1. Get the code

```bash
git clone https://github.com/ashiqurrahmananik/reel-clipper.git
cd reel-clipper
```

## 2. Install Python dependencies

### Windows

```bat
py -3 -m venv .venv
.venv\Scripts\activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

You can also just double-click `run_web.bat` after installing the requirements.

### macOS / Linux

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

Alternatively, install the package and its `reel-clipper` command:

```bash
python -m pip install .
reel-clipper --help
```

## 3. Install ffmpeg

Check first:

```bash
ffmpeg -version
```

Install if missing:

- **Windows**: download a build from [gyan.dev](https://www.gyan.dev/ffmpeg/builds/), extract it, and add its `bin` folder to your `PATH`.
- **macOS**: `brew install ffmpeg`
- **Linux**: `sudo apt install ffmpeg` (or your distro's equivalent)

Restart your terminal after changing `PATH`.

## 4. Optional: local transcription

Needed only for videos that have **no YouTube captions**:

```bash
python -m pip install -r requirements-whisper.txt
```

The first transcription downloads a Whisper model (~500 MB for `small`). Use `--whisper-model base` for a smaller/faster model.

> `av>=11,<18` is pinned because PyAV 19 removed an argument used by `faster-whisper`.

## 5. Optional: Big Pickle ranking

The default ranking model is **Big Pickle**, run through the local [OpenCode CLI](https://opencode.ai/docs/). If OpenCode is installed and `opencode.exe`/`opencode` is on your `PATH`, nothing else is needed.

If it is not auto-detected:

```bash
set OPENCODE_BIN=C:\path\to\opencode.exe    # Windows
export OPENCODE_BIN=/path/to/opencode        # macOS/Linux
```

To skip AI ranking entirely, use `--ai-model none` or select **None (free heuristics)** in the web UI. To use an OpenAI model instead, set `--ai-model gpt-4o-mini --openai-key sk-...`.

## 6. Run it

Web UI:

```bash
python -m clipmaker.web
```

Open <http://127.0.0.1:7860>.

CLI:

```bash
python -m clipmaker "https://www.youtube.com/watch?v=VIDEO_ID" -n 5 --length 30
```

See [README.md](README.md) for all options, caching behaviour, and examples.

## Updating

```bash
git pull
python -m pip install -r requirements.txt
```

Generated videos live in `output/` and are ignored by git, so they survive updates.
