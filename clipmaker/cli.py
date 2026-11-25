from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from .pipeline import Options, make_clips


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="reel-clipper",
        description="Cut the funniest / most interesting parts of a long YouTube video into vertical Reels.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python -m clipmaker 'https://youtube.com/watch?v=xxxx' -n 5\n"
            "  python -m clipmaker LINK --length 45 --style crop --lang bn\n"
            "  python -m clipmaker LINK --ai-model none       # skip Big Pickle ranking\n"
            "  python -m clipmaker LINK --ai-model gpt-4o-mini --openai-key sk-...\n"
        ),
    )
    parser.add_argument("url", nargs="?", default="", help="YouTube video URL (or use --local-file)")
    parser.add_argument("-n", "--count", type=int, default=4, help="number of clips (default: 4)")
    parser.add_argument("--length", type=float, default=30.0, help="clip length in seconds (default: 30)")
    parser.add_argument("--style", choices=["blur", "crop"], default="blur", help="vertical layout")
    parser.add_argument("--no-captions", action="store_true", help="do not burn subtitles into clips")
    parser.add_argument(
        "--lang", default="en,en-US,hi,bn", help="preferred caption languages, comma separated"
    )
    parser.add_argument("--whisper-model", default="small", help="Whisper model for videos without captions")
    parser.add_argument("--language", default=None, help="force transcription language, e.g. bn")
    parser.add_argument("--openai-key", default=os.environ.get("OPENAI_API_KEY"), help="optional AI ranking key")
    parser.add_argument(
        "--ai-model", default="opencode/big-pickle",
        help="ranking model: opencode/big-pickle (default), an OpenAI model name, or none",
    )
    parser.add_argument("--max-height", type=int, default=1080, help="max source resolution")
    parser.add_argument("--out", default="output", help="output folder (default: ./output)")
    parser.add_argument("--keep-source", action="store_true", help="keep the downloaded full video")
    parser.add_argument("--json", action="store_true", help="print final manifest as JSON")
    parser.add_argument(
        "--local-file", default=None,
        help="use an already downloaded video file or its output folder (no download)",
    )
    parser.add_argument(
        "--fast-test", action="store_true",
        help="render one short clip immediately: no captions, transcription or AI ranking",
    )
    parser.add_argument(
        "--test-start", type=float, default=300.0,
        help="start second for --fast-test (default: 300)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):  # pragma: no cover
            pass
    args = build_parser().parse_args(argv)
    if not args.url and not args.local_file:
        print("Provide a URL or --local-file PATH.", file=sys.stderr)
        return 2
    if args.count < 1 or args.length < 5:
        print("--count must be >= 1 and --length >= 5", file=sys.stderr)
        return 2

    def progress(msg: str) -> None:
        stream = sys.stderr if args.json else sys.stdout
        print(f"[reel-clipper] {msg}", file=stream, flush=True)

    opts = Options(
        count=args.count,
        length=args.length,
        style=args.style,
        captions=not args.no_captions,
        languages=[x.strip() for x in args.lang.split(",") if x.strip()],
        whisper_model=args.whisper_model,
        language=args.language,
        openai_key=args.openai_key,
        ai_model=args.ai_model,
        max_height=args.max_height,
        keep_source=args.keep_source,
        local_source=args.local_file,
        fast_test=args.fast_test,
        test_start=args.test_start,
    )

    try:
        manifest = make_clips(
            args.url, opts, out_root=Path(args.out), progress=progress
        )
    except Exception as exc:
        print(f"\nERROR: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(manifest, ensure_ascii=False, indent=2))
    else:
        print(f"\nSource: {manifest['title']}")
        for clip in manifest["clips"]:
            path = Path(manifest["folder"]) / clip["filename"]
            print(
                f"  {clip['index']}. {path}  ({clip['start']:.0f}s-{clip['end']:.0f}s, "
                f"score {clip['score']})"
            )
            print(f"     {clip['preview']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
