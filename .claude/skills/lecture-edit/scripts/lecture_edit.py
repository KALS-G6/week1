#!/usr/bin/env python3
"""Lecture recording post-processing built on auto-editor (CLI v31+).

Pipeline: (calibrate) -> cut list (v1 JSON) -> safety check -> NLE export
          -> (optional) whisper SRT on the original -> remap SRT to the edited timeline
          -> cuts.csv + report.md

Standard library only. auto-editor is called as an external binary.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import shutil
import subprocess
import sys
from fractions import Fraction
from pathlib import Path

CUT_SPEEDS = (0.0, 99999.0)
EXPORT_EXT = {"premiere": ".xml", "resolve": ".fcpxml", "final-cut-pro": ".fcpxml",
              "shotcut": ".mlt", "kdenlive": ".kdenlive", "edl": ".edl", "mp4": ".mp4"}


# ---------------------------------------------------------------- auto-editor
def find_ae(explicit: str | None) -> str:
    cand = explicit or os.environ.get("AUTO_EDITOR") or shutil.which("auto-editor")
    if not cand or not (Path(cand).exists() or shutil.which(cand)):
        sys.exit("auto-editor not found. Set --ae PATH or AUTO_EDITOR env "
                 "(binary: github.com/WyattBlue/auto-editor/releases).")
    return cand


def run(cmd: list[str], capture: bool = True) -> str:
    p = subprocess.run(cmd, capture_output=capture, text=True)
    if p.returncode != 0:
        sys.exit(f"command failed ({p.returncode}): {' '.join(cmd)}\n{p.stderr[-2000:] if p.stderr else ''}")
    return p.stdout if capture else ""


def edit_expr(board: str, audio_th: float, motion_th: float) -> str:
    if board == "low":
        return f"audio:{audio_th}"
    # board writing / tablet derivation: silent but visually active -> keep
    return f"(or audio:{audio_th} motion:{motion_th})"


def base_args(src: Path, a: argparse.Namespace) -> list[str]:
    return [str(src), "--edit", edit_expr(a.board, a.audio_th, a.motion_th),
            "--margin", a.margin, "--smooth", a.smooth, "--no-open"]


# ---------------------------------------------------------------- calibration
def levels(ae: str, src: Path, method: str) -> list[float]:
    out = run([ae, "levels", str(src), "--edit", method])
    vals = []
    for line in out.splitlines():
        try:
            vals.append(float(line))
        except ValueError:
            pass
    return vals


def pct(vals: list[float], q: float) -> float:
    s = sorted(vals)
    return s[min(len(s) - 1, int(q * len(s)))] if s else 0.0


def calibrate(ae: str, src: Path, a: argparse.Namespace) -> None:
    au = levels(ae, src, "audio")
    mo = levels(ae, src, "motion")
    quiet = [m for x, m in zip(au, mo) if x < a.audio_th]
    print(f"frames: {len(au)}  silent(audio<{a.audio_th}): {len(quiet)} "
          f"({len(quiet) / max(1, len(au)):.0%})")
    print("audio  p10/p50/p90 :", *(f"{pct(au, q):.4f}" for q in (.1, .5, .9)))
    print("motion p50/p90/p99 (silent frames only):",
          *(f"{pct(quiet, q):.4f}" for q in (.5, .9, .99)))
    print("hint: motion-th should sit above camera noise (silent p50) and below "
          "board-writing frames (silent p90~p99). Default 0.02.")


# ---------------------------------------------------------------- timeline math
def load_v1(path: Path) -> tuple[Fraction, list[tuple[int, int, float]]]:
    d = json.loads(path.read_text())
    tb = Fraction(d.get("timebase", "30/1"))
    return tb, [(int(s), int(e), float(sp)) for s, e, sp in d["chunks"]]


def kept_segments(tb: Fraction, chunks):
    """[(src_start_s, src_end_s, out_start_s, speed)] for kept chunks."""
    segs, out_t = [], 0.0
    for s, e, sp in chunks:
        if sp in CUT_SPEEDS or sp <= 0:
            continue
        a, b = float(s / tb), float(e / tb)
        segs.append((a, b, out_t, sp))
        out_t += (b - a) / sp
    return segs


def cut_ranges(tb: Fraction, chunks):
    return [(float(s / tb), float(e / tb)) for s, e, sp in chunks if sp in CUT_SPEEDS or sp <= 0]


def remap_time_range(a: float, b: float, segs) -> tuple[float, float] | None:
    pieces = []
    for s0, s1, o0, sp in segs:
        lo, hi = max(a, s0), min(b, s1)
        if hi > lo:
            pieces.append((o0 + (lo - s0) / sp, o0 + (hi - s0) / sp))
    if not pieces:
        return None
    return pieces[0][0], pieces[-1][1]


# ---------------------------------------------------------------- SRT
TS = re.compile(r"(\d+):(\d+):(\d+)[,.](\d+)")


def ts2s(t: str) -> float:
    h, m, s, ms = TS.match(t.strip()).groups()
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms.ljust(3, "0")[:3]) / 1000


def s2ts(x: float) -> str:
    ms = int(round(x * 1000))
    h, ms = divmod(ms, 3600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02}:{m:02}:{s:02},{ms:03}"


def parse_srt(text: str):
    cues = []
    for block in re.split(r"\n\s*\n", text.strip().replace("\r\n", "\n")):
        lines = block.strip().split("\n")
        idx = next((i for i, l in enumerate(lines) if "-->" in l), None)
        if idx is None:
            continue
        a, b = lines[idx].split("-->")
        cues.append((ts2s(a), ts2s(b), "\n".join(lines[idx + 1:]).strip()))
    return cues


def remap_srt(text: str, segs, min_dur: float = 0.3) -> tuple[str, int]:
    out, dropped = [], 0
    for a, b, body in parse_srt(text):
        r = remap_time_range(a, b, segs)
        if r is None or not body:
            dropped += 1
            continue
        x, y = r
        if y - x < min_dur:
            y = x + min_dur
        out.append((x, y, body))
    srt = "\n".join(f"{i}\n{s2ts(x)} --> {s2ts(y)}\n{body}\n"
                    for i, (x, y, body) in enumerate(out, 1))
    return srt, dropped


# ---------------------------------------------------------------- main
def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("input", type=Path)
    p.add_argument("--board", choices=["high", "low"], default="high",
                   help="high: keep silent-but-moving parts (board/tablet writing). low: audio only.")
    p.add_argument("--export", default="premiere", choices=list(EXPORT_EXT))
    p.add_argument("--audio-th", type=float, default=0.04)
    p.add_argument("--motion-th", type=float, default=0.02)
    p.add_argument("--margin", default="0.3s,0.5s", help="before,after kept-padding")
    p.add_argument("--smooth", default="0.5s,0.2s", help="mincut,minclip (lecture default: ignore <0.5s pauses)")
    p.add_argument("--max-cut", type=float, default=0.40, help="abort if cut ratio exceeds this")
    p.add_argument("--force", action="store_true", help="export even if --max-cut is exceeded")
    p.add_argument("--calibrate", action="store_true", help="print level distributions and exit")
    p.add_argument("--whisper-model", type=Path, help="whisper.cpp ggml model (e.g. ggml-medium.bin)")
    p.add_argument("--lang", default="ko")
    p.add_argument("--prompt", default=None, help="vocabulary bias, e.g. structural terms")
    p.add_argument("--srt", type=Path, help="existing SRT on the ORIGINAL timeline to remap (skips whisper)")
    p.add_argument("--outdir", type=Path)
    p.add_argument("--ae", help="path to auto-editor binary")
    a = p.parse_args()

    ae = find_ae(a.ae)
    src = a.input.resolve()
    if not src.exists():
        sys.exit(f"input not found: {src}")
    if a.calibrate:
        calibrate(ae, src, a)
        return

    outdir = (a.outdir or src.parent / f"{src.stem}_edit").resolve()
    outdir.mkdir(parents=True, exist_ok=True)
    stem = src.stem

    # 1) authoritative cut list
    v1 = outdir / f"{stem}.v1.json"
    run([ae, *base_args(src, a), "--export", "v1", "-o", str(v1)])
    tb, chunks = load_v1(v1)
    total = float(chunks[-1][1] / tb) if chunks else 0.0
    cuts = cut_ranges(tb, chunks)
    cut_s = sum(e - s for s, e in cuts)
    ratio = cut_s / total if total else 0.0

    with open(outdir / f"{stem}.cuts.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["src_start_s", "src_end_s", "dur_s"])
        for s, e in cuts:
            w.writerow([f"{s:.3f}", f"{e:.3f}", f"{e - s:.3f}"])

    report = [f"# lecture-edit report: {src.name}", "",
              f"- edit: `{edit_expr(a.board, a.audio_th, a.motion_th)}`  margin `{a.margin}`  smooth `{a.smooth}`",
              f"- length: {total:.1f}s -> {total - cut_s:.1f}s  (cut {ratio:.1%}, {len(cuts)} cuts)",
              f"- longest cut: {max((e - s for s, e in cuts), default=0):.1f}s"]

    # 2) safety gate
    if ratio > a.max_cut and not a.force:
        report += ["", f"**STOPPED**: cut ratio {ratio:.1%} > max-cut {a.max_cut:.0%}. "
                   "Check cuts.csv, run --calibrate, or rerun with --force."]
        (outdir / f"{stem}.report.md").write_text("\n".join(report) + "\n")
        print("\n".join(report))
        sys.exit(2)

    # 3) NLE export / render
    target = outdir / f"{stem}_edited{EXPORT_EXT[a.export]}"
    cmd = [ae, *base_args(src, a), "-o", str(target)]
    if a.export != "mp4":
        cmd += ["--export", a.export]
    run(cmd)
    report.append(f"- output: `{target.name}`")

    # 4) subtitles
    srt_text = None
    if a.srt:
        srt_text = a.srt.read_text(encoding="utf-8")
    elif a.whisper_model:
        orig_srt = outdir / f"{stem}.orig.srt"
        wcmd = [ae, "whisper", str(src), str(a.whisper_model), "-f", "srt", "-l", a.lang,
                "-o", str(orig_srt)]
        if a.prompt:
            wcmd += ["--prompt", a.prompt]
        run(wcmd)
        srt_text = orig_srt.read_text(encoding="utf-8")
        report.append(f"- original-timeline SRT: `{orig_srt.name}`")
    if srt_text is not None:
        new, dropped = remap_srt(srt_text, kept_segments(tb, chunks))
        (outdir / f"{stem}_edited.srt").write_text(new, encoding="utf-8")
        report.append(f"- edited-timeline SRT: `{stem}_edited.srt` (dropped {dropped} cues inside cuts)")

    report += ["", "Review in the NLE before publishing; cuts.csv lists every removed range "
               "on the original timeline."]
    (outdir / f"{stem}.report.md").write_text("\n".join(report) + "\n")
    print("\n".join(report))


if __name__ == "__main__":
    main()
