---
name: lecture-edit
description: Post-process lecture or seminar recordings with auto-editor - remove dead air while keeping silent board/tablet-writing segments, export a Premiere/Resolve/FCP timeline for human review, and optionally generate Korean subtitles (whisper) remapped to the edited timeline. Use when the user asks to trim, cut silence from, clean up, or subtitle a lecture/class/seminar/presentation recording (강의 영상 편집, 무음 제거, 강의 자막).
---

# lecture-edit

A wrapper around the auto-editor CLI (v31+, Nim binary) for lecture recordings.
The script is `scripts/lecture_edit.py` (stdlib only).

## Core rule
In a lecture, **silence ≠ waste**. Board writing and equation derivation are quiet.
The default (`--board high`) is `(or audio motion)`: keep a section if there is either sound or on-screen motion.
Use `--board low` (audio only) only for talking-head or slide-only recordings.

## Prerequisites
1. auto-editor binary: download `auto-editor-linux-x86_64` / macOS / Windows from GitHub Releases, or `brew install auto-editor`.
   Point to it with `--ae PATH` or the `AUTO_EDITOR` env var (otherwise it is looked up on PATH).
2. (For subtitles) a whisper.cpp ggml model file, e.g. `ggml-medium.bin` or `ggml-large-v3-turbo.bin` (from huggingface `ggerganov/whisper.cpp`).
   For Korean, `tiny`/`base` quality is too low, so use medium or larger.

## Procedure
1. **Calibrate** (first time for each recording setup or room)
   `python3 scripts/lecture_edit.py LECTURE.mp4 --calibrate`
   → `motion-th` should sit above the silent-frame motion p50 (camera noise) and below p90 (writing).
2. **Run**
   `python3 scripts/lecture_edit.py LECTURE.mp4 --export premiere`
   - It stops (exit 2) if the cut ratio exceeds `--max-cut` (default 40%). Look at `cuts.csv`, re-calibrate, or use `--force`.
3. **Subtitles (optional)**
   `... --whisper-model ggml-medium.bin --lang ko --prompt "내진성능평가, 점성댐퍼, NLTHA, 층간변위비"`
   Transcribes on the original timeline, then remaps to the edited timeline using the v1 cut list (`*_edited.srt`).
   If an SRT already exists, use `--srt ORIG.srt` (skips whisper).
4. **Review**: open `*_edited.xml/.fcpxml` in the NLE and check the cuts before final render. Use `--export mp4` only when you need a direct render without review.

## Output (`<stem>_edit/`)
| File | Contents |
|---|---|
| `*.v1.json` | auto-editor cut list (source of truth) |
| `*.cuts.csv` | removed ranges on the original timeline (in seconds) |
| `*_edited.xml` / `.fcpxml` | NLE timeline |
| `*.orig.srt`, `*_edited.srt` | subtitles on the original and edited timelines |
| `*.report.md` | length before/after, cut ratio, settings used |

## Key options
- `--margin 0.3s,0.5s`: extra time kept before/after speech (prevents clipped word endings)
- `--smooth 0.5s,0.2s`: pauses shorter than 0.5s are not cut (keeps the lecture's rhythm)
- `--audio-th 0.04`, `--motion-th 0.02`: thresholds
- `--export premiere|resolve|final-cut-pro|shotcut|kdenlive|edl|mp4`

## Known limitations
- Motion detects whole-frame pixel change. Camera shake, auto-exposure changes, and webcam-in-corner (PIP) footage can count as motion and keep silent parts.
  Use calibrate to raise `motion-th`.
- If the screen is a static slide with laser-pointer motion only, it is likely to be cut.
- The SRT remap assumes speed 1.0 (in practice the script outputs only cut/keep). A cue spanning a cut is shortened to its kept part only.
- Whisper output for jargon is unreliable → bias it with `--prompt`, and proofread the subtitles before publishing.
