#!/usr/bin/env python3
"""Voiceover alignment sheet + TTS + mux for story-to-handdrawn-video.

The renderer deliberately ships a *silent* picture track: the storyboard declares
``project.audio.voiceover == "post"`` and ``npm run render`` passes ``--muted``.
This script closes exactly that gap, and nothing else:

    plan    read the storyboard, derive an exact cue sheet with in/out timecodes
    synth   synthesize every cue with edge-tts, time-fit to its slot
    build   lay the cues on a full-length track and mux it onto the silent mp4

Every timecode is derived from the storyboard, so the cue sheet can never drift
away from the render: the reveal ratios below are the same ones ``src/Scene.tsx``
uses, and the scene cursor is the same accumulator ``src/StoryVideo.tsx`` uses.

Usage
-----
    # 1. cue sheet only (no network, no edge-tts needed)
    python scripts/voiceover_tts.py --storyboard storyboard.dandelion.json --mode plan

    # 2. synthesize the cues
    python scripts/voiceover_tts.py --storyboard storyboard.dandelion.json --mode synth \
        --voice zh-CN-XiaoxiaoNeural

    # 3. build the track and mux it onto out/picture_silent-dandelion.mp4
    python scripts/voiceover_tts.py --storyboard storyboard.dandelion.json --mode build

    # or all three at once
    python scripts/voiceover_tts.py --storyboard storyboard.dandelion.json --mode all

Optional background bed:

    python scripts/voiceover_tts.py --storyboard storyboard.dandelion.json --mode build \
        --bgm assets/bgm/soft-piano.mp3 --bgm-volume 0.10

Multi-language editions
-----------------------

A language pack overrides scene narration by id. One storyboard stays the single source
of truth for the cut, and every language is just a different script laid over it::

    out/voiceover/<slug>/<voice>/     one artifact set per (episode, voice)

    python scripts/voiceover_tts.py --storyboard storyboard.dandelion.json \
        --l10n l10n/dandelion.en.json --mode all

The pack carries its own ``voice`` / ``pitch``, and its ``lang`` names the edition
(``out/picture_silent-dandelion.en-voiced.mp4``). Pass ``--voice`` to override it.
Cue audio is namespaced by voice, so switching voices never silently reuses a take.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import math
import shutil
import subprocess
import sys
from dataclasses import dataclass, replace
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Reveal windows, copied verbatim from src/Scene.tsx (`at(ratio)` calls).
REVEAL_SPEED = {"text": (0.00, 0.22), "bw_full": (0.18, 0.58), "color": (0.52, 0.88)}
REVEAL_QUALITY = {
    "text": (0.00, 0.16),
    "bw_full": (0.16, 0.48),
    "detail": (0.48, 0.65),
    "color": (0.65, 0.88),
}

DEFAULT_VOICE = "zh-CN-XiaoxiaoNeural"
DEFAULT_PITCH = "-2Hz"


# --------------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------------- #


def js_round(value: float) -> int:
    """Math.round() semantics (half away from zero), unlike Python's banker rounding."""
    return math.floor(value + 0.5)


def timecode(seconds: float) -> str:
    minutes, rest = divmod(max(0.0, seconds), 60.0)
    return f"{int(minutes):02d}:{rest:06.3f}"


def srt_timecode(seconds: float) -> str:
    seconds = max(0.0, seconds)
    hours, rest = divmod(seconds, 3600.0)
    minutes, rest = divmod(rest, 60.0)
    whole = int(rest)
    millis = int(round((rest - whole) * 1000))
    if millis == 1000:
        whole, millis = whole + 1, 0
    return f"{int(hours):02d}:{int(minutes):02d}:{whole:02d},{millis:03d}"


def ffprobe_duration(path: Path) -> float:
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"ffprobe failed for {path}: {result.stderr.strip()}")
    return float(result.stdout.strip())


def ffmpeg(args: list[str], verbose: bool) -> None:
    if verbose:
        print("  ffmpeg " + " ".join(args))
    result = subprocess.run(
        ["ffmpeg", *args], capture_output=True, text=True, encoding="utf-8", errors="replace"
    )
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg failed:\n{result.stderr.strip()}")


# --------------------------------------------------------------------------- #
# cue sheet
# --------------------------------------------------------------------------- #


@dataclass
class Cue:
    index: int
    id: str
    narration: str
    scene_start: float
    scene_end: float
    scene_duration: float
    slot_start: float
    slot_end: float
    slot_duration: float
    color_land: float
    text_land: float
    # filled in by `synth` / `build`
    measured: float | None = None
    tempo: float = 1.0
    rate_pct: int = 0
    status: str = "pending"
    audio: str | None = None

    @property
    def chars(self) -> int:
        return len(self.narration.strip())

    @property
    def target_cps(self) -> float:
        return self.chars / self.slot_duration if self.slot_duration else 0.0

    @property
    def actual_cps(self) -> float | None:
        return self.chars / self.measured if self.measured else None


def load_storyboard(path: Path) -> dict:
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def load_l10n(path: Path) -> dict:
    if not path.exists():
        raise SystemExit(f"language pack not found: {path}")
    return load_storyboard(path)


def apply_l10n(storyboard: dict, l10n: dict) -> None:
    """Overlay a language pack onto the storyboard, keyed by scene id.

    Timing, visuals and assets are deliberately left alone: the cut belongs to the
    storyboard, the words belong to the language pack.

    Each line is either a plain string (spoken narration only) or an object with
    ``narration`` (what is spoken) and ``text`` (what is drawn on screen, already
    broken into caption lines). Editions that localise the on-screen captions need
    the object form, because the caption face and line breaks differ per script.
    """
    lines = l10n.get("lines") or {}
    if not lines:
        raise SystemExit("language pack has no `lines` object")

    ids = [str(scene["id"]) for scene in storyboard["scenes"]]
    missing = [scene_id for scene_id in ids if scene_id not in lines]
    if missing:
        raise SystemExit(
            "language pack is missing scenes: "
            + ", ".join(missing)
            + " — every scene needs a line, otherwise that cue would fall back to the "
            "original narration and the track would mix two languages"
        )
    extra = [key for key in lines if key not in ids]
    if extra:
        raise SystemExit("language pack has unknown scene ids: " + ", ".join(extra))

    for scene in storyboard["scenes"]:
        entry = lines[str(scene["id"])]
        if isinstance(entry, dict):
            narration = entry.get("narration")
            caption = entry.get("text")
        else:
            narration, caption = entry, None
        if narration:
            scene["narration"] = str(narration).strip()
        if caption:
            scene["text"] = str(caption).strip()

    if l10n.get("title"):
        storyboard["project"]["title"] = l10n["title"]
    if l10n.get("lang"):
        storyboard["project"]["lang"] = l10n["lang"]
    if l10n.get("font"):
        storyboard["project"]["caption_font"] = l10n["font"]


def build_cues(storyboard: dict, head_pad: float, tail_pad: float) -> list[Cue]:
    project = storyboard["project"]
    scenes = storyboard["scenes"]
    fps = int(project["fps"])
    reveal = REVEAL_QUALITY if project.get("mode") == "quality" else REVEAL_SPEED

    transition_frames = 0
    if project.get("transition") == "page-flip" and len(scenes) > 1:
        requested = max(1, js_round(float(project.get("transition_sec", 0.7)) * fps))
        shortest = min(js_round(scene["duration_sec"] * fps) for scene in scenes)
        transition_frames = min(requested, max(1, int(shortest * 0.45)))

    cues: list[Cue] = []
    cursor = 0
    for index, scene in enumerate(scenes):
        duration_frames = js_round(scene["duration_sec"] * fps)
        start_frame = cursor
        end_frame = start_frame + duration_frames
        is_last = index == len(scenes) - 1
        cursor = end_frame - (0 if is_last else transition_frames)

        start_sec = start_frame / fps
        end_sec = end_frame / fps
        duration_sec = end_sec - start_sec
        slot_start = start_sec + head_pad
        slot_end = end_sec - tail_pad
        if slot_end <= slot_start:
            raise SystemExit(
                f"scene {scene['id']}: duration {duration_sec:.2f}s is shorter than the "
                f"padding ({head_pad}s + {tail_pad}s); lower --head-pad/--tail-pad"
            )

        text_land = start_sec + reveal["text"][1] * duration_sec
        color_land = start_sec + reveal.get("color", (0.0, 1.0))[1] * duration_sec

        cues.append(
            Cue(
                index=index + 1,
                id=str(scene["id"]),
                narration=str(scene.get("narration") or scene.get("text", "")).strip(),
                scene_start=start_sec,
                scene_end=end_sec,
                scene_duration=duration_sec,
                slot_start=slot_start,
                slot_end=slot_end,
                slot_duration=slot_end - slot_start,
                color_land=color_land,
                text_land=text_land,
            )
        )
    return cues


# --------------------------------------------------------------------------- #
# plan
# --------------------------------------------------------------------------- #


def slug_for(storyboard_path: Path) -> str:
    stem = storyboard_path.stem
    return stem[len("storyboard.") :] if stem.startswith("storyboard.") else stem


def write_plan(out_dir: Path, episode: dict, cues: list[Cue]) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    payload = {"episode": episode, "cues": [cue_to_dict(cue) for cue in cues]}
    (out_dir / "align.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (out_dir / "align.md").write_text(render_markdown(episode, cues), encoding="utf-8")
    (out_dir / "align.srt").write_text(render_srt(cues), encoding="utf-8")

    with (out_dir / "align.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "index",
                "scene_id",
                "scene_start_sec",
                "scene_end_sec",
                "scene_duration_sec",
                "cue_start_sec",
                "cue_end_sec",
                "cue_duration_sec",
                "chars",
                "target_chars_per_sec",
                "color_land_sec",
                "narration",
            ]
        )
        for cue in cues:
            writer.writerow(
                [
                    cue.index,
                    cue.id,
                    f"{cue.scene_start:.3f}",
                    f"{cue.scene_end:.3f}",
                    f"{cue.scene_duration:.3f}",
                    f"{cue.slot_start:.3f}",
                    f"{cue.slot_end:.3f}",
                    f"{cue.slot_duration:.3f}",
                    cue.chars,
                    f"{cue.target_cps:.2f}",
                    f"{cue.color_land:.3f}",
                    cue.narration,
                ]
            )


def cue_to_dict(cue: Cue) -> dict:
    data = {
        "index": cue.index,
        "scene_id": cue.id,
        "narration": cue.narration,
        "chars": cue.chars,
        "scene_start_sec": round(cue.scene_start, 3),
        "scene_end_sec": round(cue.scene_end, 3),
        "scene_duration_sec": round(cue.scene_duration, 3),
        "cue_start_sec": round(cue.slot_start, 3),
        "cue_end_sec": round(cue.slot_end, 3),
        "cue_duration_sec": round(cue.slot_duration, 3),
        "text_land_sec": round(cue.text_land, 3),
        "color_land_sec": round(cue.color_land, 3),
        "target_chars_per_sec": round(cue.target_cps, 2),
        "status": cue.status,
        "audio": cue.audio,
    }
    if cue.measured:
        data.update(
            {
                "measured_sec": round(cue.measured, 3),
                "actual_chars_per_sec": round(cue.actual_cps or 0.0, 2),
                "applied_tempo": round(cue.tempo, 4),
                "tts_rate_pct": cue.rate_pct,
                "fill_ratio": round(cue.measured / cue.slot_duration, 4),
            }
        )
    return data


def render_markdown(episode: dict, cues: list[Cue]) -> str:
    measured = any(cue.measured for cue in cues)
    lines = [
        f"# 配音对齐表 · {episode['title']}",
        "",
        f"- 分镜文件：`{episode['storyboard']}`",
        f"- 画面文件：`{episode['video']}`",
        f"- 画幅 / 帧率：{episode['width']}×{episode['height']} @ {episode['fps']}fps，转场 `{episode['transition']}`",
        f"- 总时长：{episode['total_sec']:.3f}s（{episode['total_frames']} 帧）",
        f"- 配音槽留白：句首 +{episode['head_pad']:.2f}s / 句尾 -{episode['tail_pad']:.2f}s",
        f"- 配音音色：`{episode['voice']}`" if episode.get("voice") else "",
        f"- 语种：`{episode['locale']}`" if episode.get("locale") else "",
        f"- 语言包：`{episode['l10n']}`" if episode.get("l10n") else "",
        "",
        "每场旁白都落在本场的「配音槽」内。槽起点略晚于文字擦入，槽终点早于本场结束，",
        "因此旁白绝不会跨场叠到下一句台词上。`彩画落定` 是本场彩色插画擦入结束的时刻——",
        "旁白读完时画面已经完整上色。",
        "",
    ]
    if measured:
        lines += [
            f"实测列由 edge-tts 合成后回填；`速度` 是对白实测语速，`填充率` = 实测时长 / 槽长。",
            "",
        ]

    header = "| # | 场次 | 画面区间 | 配音槽 | 槽长 | 字数 | 目标语速 | 彩画落定 | 旁白 |"
    sep = "|---:|:---:|:---:|:---:|---:|---:|---:|:---:|---|"
    if measured:
        header = (
            "| # | 场次 | 画面区间 | 配音槽 | 槽长 | 字数 | 目标语速 | 实测 | 速度 | 填充率 | 状态 | 旁白 |"
        )
        sep = "|---:|:---:|:---:|:---:|---:|---:|---:|---:|---:|---:|:---:|---|"

    lines += [header, sep]
    for cue in cues:
        cells = [
            str(cue.index),
            cue.id,
            f"{timecode(cue.scene_start)}–{timecode(cue.scene_end)}",
            f"{timecode(cue.slot_start)}–{timecode(cue.slot_end)}",
            f"{cue.slot_duration:.2f}s",
            str(cue.chars),
            f"{cue.target_cps:.2f} 字/秒",
            timecode(cue.color_land),
            cue.narration,
        ]
        if measured:
            cells = [
                str(cue.index),
                cue.id,
                f"{timecode(cue.scene_start)}–{timecode(cue.scene_end)}",
                f"{timecode(cue.slot_start)}–{timecode(cue.slot_end)}",
                f"{cue.slot_duration:.2f}s",
                str(cue.chars),
                f"{cue.target_cps:.2f} 字/秒",
                f"{cue.measured:.2f}s" if cue.measured else "—",
                f"{cue.actual_cps:.2f} 字/秒" if cue.actual_cps else "—",
                f"{cue.measured / cue.slot_duration * 100:.0f}%" if cue.measured else "—",
                STATUS_LABEL.get(cue.status, cue.status),
                cue.narration,
            ]
        lines.append("| " + " | ".join(cells) + " |")

    total_chars = sum(cue.chars for cue in cues)
    lines += [
        "",
        f"合计 {len(cues)} 句 / {total_chars} 字 / {episode['total_sec']:.3f}s，"
        f"平均目标语速 {total_chars / sum(c.slot_duration for c in cues):.2f} 字/秒。",
        "",
    ]
    overflows = [cue for cue in cues if cue.status == "overflow"]
    if overflows:
        lines += [
            "> **越界告警**：" + "、".join(f"第 {cue.id} 场" for cue in overflows) + " 的配音超出本场画面时长，",
            "> 会压到下一句台词上。请缩短原文，或把该场 `duration_sec` 调大后重新渲染。",
            "",
        ]
    return "\n".join(line for line in lines if line is not None) + "\n"


STATUS_LABEL = {
    "pending": "待合成",
    "ok": "✅ 合槽",
    "gap": "留白偏多",
    "tempo": "微调语速",
    "tight": "紧贴下场",
    "overflow": "越界",
    "failed": "合成失败",
}


def render_srt(cues: list[Cue]) -> str:
    blocks = []
    for index, cue in enumerate(cues, start=1):
        blocks.append(
            "\n".join(
                [
                    str(index),
                    f"{srt_timecode(cue.slot_start)} --> {srt_timecode(cue.slot_end)}",
                    cue.narration,
                ]
            )
        )
    return "\n\n".join(blocks) + "\n"


# --------------------------------------------------------------------------- #
# synth
# --------------------------------------------------------------------------- #


def fmt_rate(pct: int) -> str:
    return f"{pct:+d}%"


def require_edge_tts() -> None:
    try:
        import edge_tts  # noqa: F401
    except ImportError:
        raise SystemExit(
            "edge-tts is not installed for this interpreter.\n"
            f"  {sys.executable} -m pip install edge-tts\n"
            "or run the script with an interpreter that has it."
        )


def list_voices(prefixes: list[str] | None) -> int:
    require_edge_tts()
    import edge_tts

    voices = asyncio.run(edge_tts.list_voices())
    if prefixes:
        wanted = tuple(prefix.lower() for prefix in prefixes)
        voices = [v for v in voices if v["Locale"].lower().startswith(wanted)]
    else:
        voices = [v for v in voices if v["Locale"].startswith("zh")]
    for voice in sorted(voices, key=lambda item: (item["Locale"], item["ShortName"])):
        print(
            f"{voice['ShortName']:<34} {voice['Gender']:<7} "
            f"{voice['Locale']:<12} {voice['FriendlyName']}"
        )
    print(f"{len(voices)} voices")
    return 0


async def _tts_save(text: str, voice: str, rate: str, pitch: str, volume: str, dest: Path) -> None:
    import edge_tts

    communicate = edge_tts.Communicate(text, voice, rate=rate, pitch=pitch, volume=volume)
    await communicate.save(str(dest))


def classify(cue: Cue, measured: float, tempo: float) -> str:
    """Where the finished line lands, after the atempo rescue is applied."""
    if measured <= cue.slot_duration:
        return "gap" if measured < cue.slot_duration * 0.82 else "ok"
    if measured / tempo <= cue.slot_duration + 0.02:
        return "tempo"
    if measured <= cue.scene_duration - 0.05:
        return "tight"
    return "overflow"


def synth_cue(cue: Cue, dest: Path, args: argparse.Namespace) -> Cue:
    attempts = max(1, int(args.passes))
    rate_pct = 0
    last: Path | None = None
    measured = 0.0

    for attempt in range(attempts):
        temp = dest.with_name(f"{dest.stem}.pass{attempt}{dest.suffix}")
        asyncio.run(
            _tts_save(
                cue.narration,
                args.voice,
                fmt_rate(rate_pct),
                args.pitch,
                args.volume,
                temp,
            )
        )
        measured = ffprobe_duration(temp)
        if last and last.exists():
            last.unlink()
        last = temp

        delta = (measured / cue.slot_duration - 1.0) * 100.0
        if attempt == attempts - 1 or abs(delta) <= args.tolerance:
            break
        target = int(round(rate_pct + delta))
        target = max(-args.max_slow, min(args.max_fast, target))
        if target == rate_pct:
            break
        rate_pct = target

    assert last is not None
    shutil.move(str(last), str(dest))

    tempo = 1.0
    if measured > cue.slot_duration:
        tempo = round(min(args.max_tempo, measured / cue.slot_duration), 3)
    return replace(
        cue,
        measured=measured,
        tempo=tempo,
        rate_pct=rate_pct,
        status=classify(cue, measured, tempo),
        audio=str(dest.relative_to(ROOT)).replace("\\", "/"),
    )


def run_synth(out_dir: Path, cues: list[Cue], args: argparse.Namespace) -> list[Cue]:
    require_edge_tts()

    audio_dir = out_dir / "cues"
    audio_dir.mkdir(parents=True, exist_ok=True)
    results: list[Cue] = []

    for cue in cues:
        dest = audio_dir / f"{cue.id}.mp3"
        if args.only and cue.id not in args.only:
            results.append(cue)
            continue
        if dest.exists() and not args.force:
            measured = ffprobe_duration(dest)
            tempo = (
                round(min(args.max_tempo, measured / cue.slot_duration), 3)
                if measured > cue.slot_duration
                else 1.0
            )
            results.append(
                replace(
                    cue,
                    measured=measured,
                    tempo=tempo,
                    status=classify(cue, measured, tempo),
                    audio=str(dest.relative_to(ROOT)).replace("\\", "/"),
                )
            )
            print(f"  {cue.id} 复用已有音频 {measured:.2f}s")
            continue

        try:
            fitted = synth_cue(cue, dest, args)
        except Exception as error:  # keep the batch alive, report at the end
            results.append(replace(cue, status="failed"))
            print(f"  {cue.id} 合成失败：{error}", file=sys.stderr)
            continue

        results.append(fitted)
        print(
            f"  {cue.id} {fitted.measured:.2f}s / 槽 {cue.slot_duration:.2f}s "
            f"({fitted.measured / cue.slot_duration * 100:.0f}%) rate={fmt_rate(fitted.rate_pct)} "
            f"tempo={fitted.tempo:.3f} → {STATUS_LABEL.get(fitted.status, fitted.status)}"
        )

    return results


# --------------------------------------------------------------------------- #
# build
# --------------------------------------------------------------------------- #


def run_build(out_dir: Path, cues: list[Cue], args: argparse.Namespace) -> Path:
    missing = [cue.id for cue in cues if not cue.audio or not (ROOT / cue.audio).exists()]
    if missing:
        raise SystemExit(f"missing cue audio for scenes: {', '.join(missing)} — run --mode synth first")

    target = ROOT / args.video
    if not target.exists():
        raise SystemExit(f"silent video not found: {target}")

    video_duration = ffprobe_duration(target)
    total = video_duration

    inputs: list[str] = [
        "-y",
        "-f",
        "lavfi",
        "-t",
        f"{total:.6f}",
        "-i",
        "anullsrc=r=44100:cl=stereo",
    ]
    chains: list[str] = []
    index = 1
    for cue in cues:
        inputs += ["-i", str(ROOT / cue.audio)]
        delay_ms = int(round(cue.slot_start * 1000))
        chain = f"[{index}:a]aresample=44100,aformat=channel_layouts=stereo"
        if abs(cue.tempo - 1.0) > 1e-4:
            chain += f",atempo={cue.tempo:.4f}"
        chain += f",adelay={delay_ms}|{delay_ms}[c{index}]"
        chains.append(chain)
        index += 1

    audio_inputs = 1 + len(cues)
    mix_sources = "[0:a]" + "".join(f"[c{i}]" for i in range(1, len(cues) + 1))
    if args.bgm:
        bgm = Path(args.bgm)
        if not bgm.is_absolute():
            bgm = ROOT / bgm
        inputs += ["-stream_loop", "-1", "-i", str(bgm)]
        chains.append(
            f"[{index}:a]aresample=44100,aformat=channel_layouts=stereo,"
            f"volume={args.bgm_volume}[bgm]"
        )
        mix_sources += "[bgm]"
        audio_inputs += 1
        index += 1

    chains.append(
        f"{mix_sources}amix=inputs={audio_inputs}:normalize=0:dropout_transition=0[mix]"
    )

    track = out_dir / "voiceover.wav"
    ffmpeg(
        [
            *inputs,
            "-filter_complex",
            ";".join(chains),
            "-map",
            "[mix]",
            "-t",
            f"{total:.6f}",
            "-c:a",
            "pcm_s16le",
            "-ar",
            "44100",
            "-ac",
            "2",
            str(track),
        ],
        args.verbose,
    )

    output = ROOT / args.output
    output.parent.mkdir(parents=True, exist_ok=True)
    ffmpeg(
        [
            "-y",
            "-i",
            str(target),
            "-i",
            str(track),
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
            "-c:v",
            "copy",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-shortest",
            str(output),
        ],
        args.verbose,
    )
    return output


# --------------------------------------------------------------------------- #
# cli
# --------------------------------------------------------------------------- #


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build a voiceover alignment sheet, synthesize it with edge-tts, "
        "and mux it onto the silent hand-drawn render.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--storyboard", default="storyboard.json", help="storyboard JSON to read")
    parser.add_argument(
        "--l10n",
        help="language pack JSON: {'lang','locale','voice','pitch','title','lines':{scene_id: text}}",
    )
    parser.add_argument(
        "--mode",
        choices=("plan", "synth", "build", "all"),
        default="all",
        help="plan prints the cue sheet, synth renders the audio, build muxes the video",
    )
    parser.add_argument("--slug", help="output namespace (default: storyboard file stem)")
    parser.add_argument(
        "--out-dir", help="artifacts directory (default: out/voiceover/<slug>/<voice>)"
    )
    parser.add_argument("--video", help="silent mp4 (default: out/picture_silent-<slug>.mp4)")
    parser.add_argument("--output", help="muxed mp4 (default: out/picture_silent-<slug>-voiced.mp4)")
    parser.add_argument("--head-pad", type=float, default=0.20, help="breath before the line")
    parser.add_argument("--tail-pad", type=float, default=0.15, help="breath after the line")
    parser.add_argument(
        "--voice", help=f"edge-tts voice short name (default: {DEFAULT_VOICE}, or the pack's)"
    )
    parser.add_argument("--pitch", help=f"edge-tts pitch (default: {DEFAULT_PITCH})")
    parser.add_argument("--volume", default="+0%", help="edge-tts volume, e.g. +0%%")
    parser.add_argument("--passes", type=int, default=3, help="edge-tts rate refinement passes")
    parser.add_argument("--tolerance", type=float, default=3.0, help="stop refining within this %%")
    parser.add_argument("--max-fast", type=int, default=40, help="hard cap for edge-tts speed-up %%")
    parser.add_argument("--max-slow", type=int, default=25, help="hard cap for edge-tts slow-down %%")
    parser.add_argument("--max-tempo", type=float, default=1.15, help="cap on the ffmpeg atempo rescue")
    parser.add_argument("--only", nargs="*", help="synthesize only these scene ids")
    parser.add_argument("--force", action="store_true", help="re-synthesize existing cues")
    parser.add_argument("--bgm", help="optional background music file")
    parser.add_argument("--bgm-volume", type=float, default=0.10, help="BGM bed level")
    parser.add_argument("--list-voices", action="store_true", help="print voices and exit")
    parser.add_argument(
        "--locale", nargs="*", help="with --list-voices: locale prefixes, e.g. ja en-US zh-HK"
    )
    parser.add_argument("--verbose", action="store_true", help="echo the ffmpeg commands")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")

    args = parse_args(argv)
    if args.list_voices:
        return list_voices(args.locale)

    storyboard_path = Path(args.storyboard)
    if not storyboard_path.is_absolute():
        storyboard_path = ROOT / storyboard_path
    if not storyboard_path.exists():
        raise SystemExit(f"storyboard not found: {storyboard_path}")

    storyboard = load_storyboard(storyboard_path)

    l10n = None
    lang = None
    l10n_label = None
    if args.l10n:
        l10n_path = Path(args.l10n)
        if not l10n_path.is_absolute():
            l10n_path = ROOT / l10n_path
        l10n = load_l10n(l10n_path)
        apply_l10n(storyboard, l10n)
        lang = l10n.get("lang") or l10n_path.stem.split(".")[-1]
        try:
            l10n_label = str(l10n_path.relative_to(ROOT)).replace("\\", "/")
        except ValueError:
            l10n_label = str(l10n_path)

    args.voice = args.voice or (l10n or {}).get("voice") or DEFAULT_VOICE
    args.pitch = args.pitch or (l10n or {}).get("pitch") or DEFAULT_PITCH

    base = args.slug or slug_for(storyboard_path)
    slug = f"{base}.{lang}" if lang else base
    voice_tag = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in args.voice)
    out_dir = (
        Path(args.out_dir) if args.out_dir else ROOT / "out" / "voiceover" / slug / voice_tag
    )
    if not out_dir.is_absolute():
        out_dir = ROOT / out_dir
    args.video = args.video or f"out/picture_silent-{base}.mp4"
    args.output = args.output or f"out/picture_silent-{slug}-voiced.mp4"

    project = storyboard["project"]
    cues = build_cues(storyboard, args.head_pad, args.tail_pad)

    video_path = ROOT / args.video
    total_frames = max(
        int(round(cue.scene_end * project["fps"])) for cue in cues
    )
    episode = {
        "title": project.get("title", "未命名"),
        "storyboard": str(storyboard_path.relative_to(ROOT)).replace("\\", "/"),
        "l10n": l10n_label,
        "locale": (l10n or {}).get("locale"),
        "video": args.video,
        "output": args.output,
        "fps": project["fps"],
        "width": project["width"],
        "height": project["height"],
        "transition": project.get("transition", "cut"),
        "total_sec": cues[-1].scene_end,
        "total_frames": total_frames,
        "head_pad": args.head_pad,
        "tail_pad": args.tail_pad,
        "voice": args.voice,
    }

    if args.mode == "plan":
        write_plan(out_dir, episode, cues)
        print(f"cue sheet → {out_dir / 'align.md'}")
        print(render_markdown(episode, cues))
        return 0

    if args.mode in {"synth", "all"}:
        print(f"synth {len(cues)} cues with {args.voice}")
        cues = run_synth(out_dir, cues, args)
        failed = [cue.id for cue in cues if cue.status == "failed"]
        if failed:
            print(f"  failed scenes: {', '.join(failed)}", file=sys.stderr)

    if args.mode in {"build", "all"}:
        if video_path.exists():
            print(f"build track over {args.video}")
            print(f"  video duration {ffprobe_duration(video_path):.3f}s")
            output = run_build(out_dir, cues, args)
            print(f"  muxed → {output}")
        else:
            print(f"skip build: {video_path} not found", file=sys.stderr)

    write_plan(out_dir, episode, cues)
    print(f"cue sheet → {out_dir / 'align.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
