#!/usr/bin/env python3
"""Produce one story in several languages: localised captions *and* localised dub.

Everything about an edition derives from its name, so there is no registry to keep
in sync:

    storyboard.<story>.json      base cut, timings, artwork (the single source of truth)
    l10n/<story>.<lang>.json     language pack: caption lines, narration, voice, font
    out/<title>/<title>-<lang>.mp4

The captions are drawn by Remotion at render time, so an edition with a different
script needs its own render — the dub alone would leave the words on screen in the
original language.

    python scripts/multilingual_video.py --story dandelion
    python scripts/multilingual_video.py --story dandelion --langs ja --mode render
"""

from __future__ import annotations

import argparse
import copy
import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from voiceover_tts import DEFAULT_PITCH, DEFAULT_VOICE, apply_l10n  # noqa: E402

DEFAULT_LANGS = ("zh", "yue", "ja", "ko", "en")


def load_json(path: Path) -> dict:
    if not path.exists():
        raise SystemExit(f"not found: {path}")
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


def quote(value: str) -> str:
    return f'"{value}"' if (" " in value or not value.isascii()) else value


def run(command: list[str]) -> None:
    line = " ".join(quote(str(part)) for part in command)
    print(f"  $ {line}", flush=True)
    subprocess.run(line, shell=True, cwd=ROOT, check=True)


def edition_storyboard(story: str, lang: str, base: dict, pack_path: Path | None) -> dict:
    """Base cut + language pack = one renderable edition. Timings are never touched."""
    edition = copy.deepcopy(base)
    if pack_path is not None:
        apply_l10n(edition, load_json(pack_path))
    edition["project"]["lang"] = edition["project"].get("lang") or lang
    return edition


def build_one(story: str, lang: str, mode: str, force: bool) -> None:
    storyboard_path = ROOT / f"storyboard.{story}.json"
    base = load_json(storyboard_path)
    title = base["project"]["title"]

    pack_path = ROOT / "l10n" / f"{story}.{lang}.json"
    if lang != base["project"].get("lang") and not pack_path.exists():
        raise SystemExit(
            f"{lang}: no language pack at l10n/{story}.{lang}.json — "
            "the captions and the narration would stay in the original language"
        )
    pack = load_json(pack_path) if pack_path.exists() else None

    out_dir = ROOT / "out" / title
    editions_dir = out_dir / "editions"
    silent_dir = out_dir / "silent"
    voice_dir = out_dir / "voiceover" / lang

    edition = edition_storyboard(story, lang, base, pack_path if pack else None)
    edition_path = editions_dir / f"{story}.{lang}.json"
    props_path = editions_dir / f"{story}.{lang}.props.json"
    silent_path = silent_dir / f"{title}-{lang}.mp4"
    final_path = out_dir / f"{title}-{lang}.mp4"

    print(f"[{story} / {lang}] {title} · scenes={len(base['scenes'])}", flush=True)

    # The props file is what Remotion actually reads; keep it beside the edition so
    # every artifact needed to reproduce this render travels together.
    write_json(edition_path, edition)
    write_json(props_path, {"storyboard": edition})

    if mode in ("prepare", "render", "all"):
        if silent_path.exists() and not force:
            print(f"  silent exists, reuse: {silent_path.relative_to(ROOT)}", flush=True)
        else:
            silent_dir.mkdir(parents=True, exist_ok=True)
            run([
                "npx", "remotion", "render", "src/index.ts", "Edition", str(silent_path),
                "--codec=h264", "--crf=18", "--pixel-format=yuv420p",
                "--muted", "--concurrency=1", f"--props={props_path}",
            ])

    if mode in ("voice", "all"):
        voice = (pack or {}).get("voice") or DEFAULT_VOICE
        pitch = (pack or {}).get("pitch") or DEFAULT_PITCH
        if not silent_path.exists():
            raise SystemExit(f"[{lang}] silent video missing: {silent_path}")
        command = [
            sys.executable, str(ROOT / "scripts" / "voiceover_tts.py"),
            "--storyboard", str(edition_path),
            "--video", str(silent_path),
            "--output", str(final_path),
            "--out-dir", str(voice_dir),
            # `=` form: a bare `-2Hz` would be read by argparse as an option, not a value.
            f"--voice={voice}", f"--pitch={pitch}", "--mode", "all",
        ]
        if pack:
            command += ["--l10n", str(pack_path)]
        if force:
            command.append("--force")
        run(command)

    print(f"  -> {final_path.relative_to(ROOT)}", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="Render and dub one story per language")
    parser.add_argument("--story", required=True, help="storyboard.<story>.json")
    parser.add_argument("--langs", default=",".join(DEFAULT_LANGS))
    parser.add_argument(
        "--mode",
        choices=["prepare", "render", "voice", "all"],
        default="all",
        help="prepare writes the edition storyboards only, without rendering",
    )
    parser.add_argument("--force", action="store_true", help="rebuild even if outputs exist")
    args = parser.parse_args()

    langs = [item.strip() for item in args.langs.split(",") if item.strip()]
    unknown = [item for item in langs if item not in DEFAULT_LANGS]
    if unknown:
        raise SystemExit(
            "unsupported language(s): " + ", ".join(unknown)
            + " — supported: " + ", ".join(DEFAULT_LANGS)
        )

    if shutil.which("npx") is None and args.mode != "voice":
        raise SystemExit("npx not found on PATH; needed to render")

    for lang in langs:
        build_one(args.story, lang, args.mode, args.force)

    title = load_json(ROOT / f"storyboard.{args.story}.json")["project"]["title"]
    print()
    print(f"done: out/{title}/ — {len(langs)} edition(s): {', '.join(langs)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
