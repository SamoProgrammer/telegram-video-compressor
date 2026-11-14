# Performance decisions

## Rules

- Keep one FFmpeg encoder, bounded transfers, and no forced quality reduction.
- Compare median time, output bytes, VMAF/SSIM, and end-to-end cost. Encoding a
  larger file faster can make the overall job slower once upload is included.
- Never change a production codec preset from synthetic evidence alone; repeat
  the workflow on a standard runner, then use representative real clips.
- Use `pipeline_stage` logs to identify download, encode, and upload bottlenecks.

## 2026-11-14: local synthetic smoke

Environment: local AMD Ryzen 5 5625U, FFmpeg n9.0.2. One 0.5-second 720p
`testsrc2` clip per profile, one repetition. This was a harness smoke test, not
a production benchmark and not evidence of a statistically reliable improvement.

| Candidate vs existing same-codec profile | Time ratio | Bytes ratio | Decision |
|---|---:|---:|---|
| H.264 ultrafast / veryfast | 0.731 | 2.476 | Reject: much more upload traffic |
| HEVC faster / fast | 0.956 | 0.970 | Reject: below VMAF floor; too few samples |
| AV1 preset 9 / 8 | 0.918 | 1.060 | Reject: larger despite passing the VMAF gates |
| AV1 preset 10 / 8 | 1.008 | 1.274 | Reject: slower and larger here |
| VP9 cpu-used 2 / default 1 | 0.722 | 1.027 | Reject: bigger; baseline below strict VMAF floor |
| VP9 cpu-used 3 / default 1 | 0.729 | 1.043 | Reject: bigger; baseline below strict VMAF floor |
| VP9 cpu-used 4 / default 1 | 0.762 | 1.051 | Reject: bigger; baseline below strict VMAF floor |

No codec defaults changed. The smoke JSON was temporary, not committed; regenerate
it with `python -m scripts.benchmark --fixtures motion --duration 0.5 --repetitions 1`.
The performance workflow retains its JSON result as a 30-day Actions artifact.

A subsequent local three-repetition sweep over motion, seeded noise, and flat
bars found flat-only passes for HEVC `faster` and AV1 presets 9/10. Those gains
do not generalize to motion/noise and are not sufficient to change production
presets. VP9 candidates remained rejected. The reproducible input generator and
all variants are in `scripts/benchmark.py`.

## 2026-11-14: transport and scheduling

Implemented four shared MTProto connections, persistent clients, concurrent
large-file parts, two-download/two-upload pools, four FIFO conversation workers,
single-video pending queues, probe reuse, and structured timings. These do not
change encoded bytes by themselves. Production throughput must be compared from
Actions logs after real transfers; synthetic RPC tests are not bandwidth proof.

Pending: `codec-performance` GitHub-run results, repeated real-codec evidence,
and download/upload rates from a deployed full shift. Keep or revert changes
only after comparing those results with operational variability.
