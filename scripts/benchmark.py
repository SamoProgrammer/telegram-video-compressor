#!/usr/bin/env python3
"""Benchmark codec speed at matched CRF; never changes production defaults."""

from __future__ import annotations

import argparse
import json
import platform
import statistics
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from tgcompress.media import DEFAULT_SETTINGS, build_ffmpeg_args, probe

VARIANTS = {
    "h264": [("ultrafast", {"-preset": "ultrafast"})],
    "hevc": [("faster", {"-preset": "faster"})],
    "av1": [("preset-9", {"-preset": "9"}), ("preset-10", {"-preset": "10"})],
    "vp9": [
        ("cpu-used-2", {"-cpu-used": "2"}),
        ("cpu-used-3", {"-cpu-used": "3"}),
        ("cpu-used-4", {"-cpu-used": "4"}),
    ],
}
GENERATORS = {
    "motion": "testsrc2=size=1280x720:rate=30",
    "noise": "testsrc2=size=1280x720:rate=30,noise=alls=16:allf=t+u:all_seed=42",
    "flat": "smptehdbars=size=1280x720:rate=30",
}


def run(command: list[str]) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            text=True, check=True, timeout=360,
        )
    except subprocess.CalledProcessError as exc:
        detail = exc.stderr.strip().splitlines()
        raise RuntimeError(
            f"{Path(command[0]).name} failed: {detail[-1] if detail else 'unknown error'}"
        ) from None


def make_fixture(directory: Path, name: str, duration: float) -> Path:
    destination = directory / f"{name}.mp4"
    run([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", GENERATORS[name], "-t", str(duration),
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "16",
        "-pix_fmt", "yuv420p", str(destination),
    ])
    return destination


def encode(source: Path, output: Path, codec: str, changes: dict) -> dict:
    settings = dict(DEFAULT_SETTINGS, codec=codec, mode="crf", crf=26)
    info = probe(source)
    command = build_ffmpeg_args(source, output, "video", settings, info=info)
    for option, value in changes.items():
        if option in command:
            command[command.index(option) + 1] = value
        else:
            command[-1:-1] = [option, value]
    # SVT has both native and dictionary preset arguments in the current wrapper.
    if "-svtav1-params" in command and "-preset" in changes:
        index = command.index("-svtav1-params") + 1
        command[index] = f"preset={changes['-preset']}"
    started = time.monotonic()
    run(command)
    elapsed = time.monotonic() - started
    returned = probe(output)
    return {
        "seconds": elapsed,
        "bytes": output.stat().st_size,
        "width": returned["width"],
        "height": returned["height"],
    }


def quality(reference: Path, encoded: Path, width: int, height: int, log: Path) -> dict:
    # libvmaf takes distorted video first and the untouched reference second.
    graph = (
        "[0:v]setpts=PTS-STARTPTS,setsar=1[distorted];"
        f"[1:v]setpts=PTS-STARTPTS,scale={width}:{height}:flags=bicubic,"
        "setsar=1[reference];"
        "[distorted][reference]"
        f"libvmaf=feature=name=float_ssim:eof_action=endall:n_threads=2:log_fmt=json:log_path={log}"
    )
    run([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(encoded),
        "-i", str(reference), "-filter_complex", graph, "-f", "null", "-",
    ])
    data = json.loads(log.read_text())
    metrics = data["pooled_metrics"]
    return {
        "vmaf": metrics["vmaf"]["mean"],
        "ssim": metrics.get("float_ssim", {}).get("mean"),
    }


def median(records: list[dict], field: str):
    values = [record[field] for record in records if record.get(field) is not None]
    return statistics.median(values) if values else None


def markdown(report: dict) -> str:
    lines = [
        "# Codec performance",
        "",
        f"Runner: `{report['machine']}`; FFmpeg: `{report['ffmpeg']}`",
        "",
        "Synthetic clips are a smoke comparison, not a substitute for representative real media.",
        "Gate: VMAF >= baseline - 1.0 and >= 95, output no larger than baseline.",
        "No production defaults change automatically.",
        "",
        "| Fixture | Codec/profile | Median encode s | Bytes | vs baseline | VMAF | SSIM | Gate |",
        "|---|---|---:|---:|---:|---:|---:|---|",
    ]
    for row in report["rows"]:
        baseline = "" if row["baseline"] else (
            f"{row['seconds_ratio']:.2f}x time; {row['bytes_ratio']:.3f}x size"
        )
        status = "baseline" if row["baseline"] else ("PASS" if row["passes"] else "reject")
        ssim = "" if row["ssim"] is None else f"{row['ssim']:.4f}"
        lines.append(
            f"| {row['fixture']} | {row['profile']} | {row['seconds']:.3f} | "
            f"{row['bytes']} | {baseline} | {row['vmaf']:.2f} | {ssim} | {status} |"
        )
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, action="append", help="Real clip; repeatable")
    parser.add_argument("--repetitions", type=int, default=3)
    parser.add_argument("--duration", type=float, default=3.0, help="Synthetic seconds per fixture")
    parser.add_argument("--fixtures", default="motion,noise,flat")
    parser.add_argument("--report", type=Path, help="Machine-readable JSON output")
    args = parser.parse_args()
    if args.repetitions < 1 or args.duration <= 0:
        parser.error("--repetitions must be positive and --duration must be greater than zero")
    fixtures = args.fixtures.split(",")
    if args.input and args.fixtures != parser.get_default("fixtures"):
        parser.error("Use either --input or --fixtures, not both")
    if not args.input and any(name not in GENERATORS for name in fixtures):
        parser.error(f"Unknown fixture; choose from {','.join(GENERATORS)}")

    report = {
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "machine": f"{platform.system()} {platform.machine()}",
        "ffmpeg": subprocess.check_output(["ffmpeg", "-version"], text=True).splitlines()[0],
        "gates": {"min_vmaf": 95, "vmaf_slack": 1.0, "max_bytes_ratio": 1.0},
        "parameters": {
            "repetitions": args.repetitions,
            "synthetic_seconds": None if args.input else args.duration,
            "crf": 26,
            "candidate_options": VARIANTS,
        },
        "rows": [],
    }
    with tempfile.TemporaryDirectory(prefix="tgcompress-benchmark-") as directory:
        temporary = Path(directory)
        sources = args.input or [
            make_fixture(temporary, name, args.duration) for name in fixtures
        ]
        for fixture_index, source in enumerate(sources):
            # Input-index IDs avoid publishing arbitrary user filenames in CI logs.
            fixture = f"input-{fixture_index + 1}"
            if not args.input:
                fixture = fixtures[fixture_index]
            for codec in VARIANTS:
                profiles = [("baseline", {})] + VARIANTS[codec]
                baseline = None
                for name, changes in profiles:
                    records = []
                    for repetition in range(args.repetitions):
                        # Match Pipeline's out-*.mp4 output. VP9-in-MP4 also
                        # allows the current AAC copy/re-encode audio behavior.
                        output = temporary / f"{fixture_index}-{codec}-{name}-{repetition}.mp4"
                        try:
                            record = encode(source, output, codec, changes)
                            record.update(quality(
                                source, output, record["width"], record["height"],
                                output.with_suffix(".vmaf.json"),
                            ))
                        except Exception as exc:
                            # Never expose media paths or command lines in Actions.
                            raise RuntimeError(
                                f"Benchmark failed at {fixture}, {codec}/{name}: "
                                f"{type(exc).__name__}"
                            ) from None
                        records.append(record)
                    summary = {
                        "fixture": fixture, "codec": codec,
                        "profile": f"{codec}/{name}", "baseline": name == "baseline",
                        "seconds": median(records, "seconds"),
                        "bytes": median(records, "bytes"),
                        "vmaf": median(records, "vmaf"),
                        "ssim": median(records, "ssim"),
                        "samples": records,
                    }
                    if baseline is None:
                        baseline = summary
                    summary["seconds_ratio"] = summary["seconds"] / baseline["seconds"]
                    summary["bytes_ratio"] = summary["bytes"] / baseline["bytes"]
                    summary["passes"] = (
                        summary["baseline"]
                        or (
                            summary["vmaf"] >= max(95.0, baseline["vmaf"] - 1.0)
                            and summary["bytes_ratio"] <= 1.0
                        )
                    )
                    report["rows"].append(summary)
                    print(json.dumps(summary, separators=(",", ":")), flush=True)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2) + "\n")
    print(markdown(report))


if __name__ == "__main__":
    main()
