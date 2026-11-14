import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from tgcompress import media
from tgcompress.errors import BotError
from tgcompress.state import default_state


class PickMediaTest(unittest.TestCase):
    def test_video(self):
        video = {"file_id": "x", "file_size": 1}
        self.assertEqual(media.pick_media({"video": video}), (video, "video"))

    def test_video_note(self):
        note = {"file_id": "y"}
        self.assertEqual(media.pick_media({"video_note": note}), (note, "video_note"))

    def test_video_document(self):
        document = {"file_id": "z", "mime_type": "video/mp4"}
        self.assertEqual(
            media.pick_media({"document": document}), (document, "document")
        )

    def test_video_document_by_suffix(self):
        document = {"file_id": "z", "file_name": "clip.MKV"}
        self.assertEqual(
            media.pick_media({"document": document}), (document, "document")
        )

    def test_text_is_ignored(self):
        self.assertIsNone(media.pick_media({"text": "hi"}))


class SettingsTest(unittest.TestCase):
    def test_resolution_caps_are_long_side(self):
        self.assertIn(
            "min(1920,iw)", media.video_filter("video", {"resolution": "1080"})
        )
        self.assertIn(
            "min(1280,iw)", media.video_filter("video", {"resolution": "720"})
        )
        self.assertIn("min(854,iw)", media.video_filter("video", {"resolution": "480"}))
        self.assertIn("min(426,iw)", media.video_filter("video", {"resolution": "240"}))
        self.assertIn(
            "min(640,iw)", media.video_filter("video_note", {"resolution": "1080"})
        )

    def test_quality_presets_choose_fast_encoders(self):
        self.assertEqual(media.QUALITIES["high"][1], "faster")
        self.assertEqual(media.QUALITIES["balanced"][1], "veryfast")
        self.assertEqual(media.QUALITIES["small"][1], "veryfast")

    def test_crf_quality_levels(self):
        self.assertEqual(media.QUALITIES["lossless"][2], 18)
        self.assertEqual(media.QUALITIES["high"][2], 22)
        self.assertEqual(media.QUALITIES["balanced"][2], 26)
        self.assertEqual(media.QUALITIES["small"][2], 30)
        self.assertEqual(media.QUALITIES["extreme"][2], 34)

    def test_round_note_falls_back_past_sixty_seconds(self):
        settings = dict(media.DEFAULT_SETTINGS, output="note")
        self.assertEqual(
            media.target_kind("video", settings, 60), ("video_note", False)
        )
        self.assertEqual(media.target_kind("video", settings, 61), ("video", True))

    def test_output_auto_keeps_input_kind(self):
        self.assertEqual(
            media.target_kind("document", dict(media.DEFAULT_SETTINGS), 10),
            ("document", False),
        )

    def test_settings_line(self):
        self.assertIn(
            "Balanced",
            media.settings_line({"resolution": "720", "quality": "balanced"}),
        )
        self.assertIn(
            "10 MB", media.settings_line({"resolution": "720", "quality": "10mb"})
        )
        self.assertIn(
            "50%", media.settings_line({"resolution": "720", "quality": "50%"})
        )

        size_line = media.settings_line(
            {
                "resolution": "720",
                "mode": "size",
                "target_size": "20mb",
                "quality": "balanced",
            }
        )
        self.assertIn("Target 20 MB", size_line)
        self.assertNotIn("Balanced", size_line)

        crf_line = media.settings_line(
            {
                "resolution": "720",
                "mode": "crf",
                "crf": 22,
                "quality": "balanced",
            }
        )
        self.assertIn("CRF 22", crf_line)
        self.assertNotIn("Balanced", crf_line)


class ProgressTest(unittest.TestCase):
    def test_out_time_is_parsed(self):
        self.assertAlmostEqual(media.parse_out_time("00:00:01.500000"), 1.5)
        self.assertAlmostEqual(media.parse_out_time("00:01:02.000000"), 62.0)
        self.assertEqual(media.parse_out_time("bogus"), 0.0)


class TargetSizeTest(unittest.TestCase):
    def test_parse_target_size(self):
        self.assertEqual(media.parse_target_size("10mb"), 10 * 1024 * 1024)
        self.assertEqual(media.parse_target_size("20MB"), 20 * 1024 * 1024)
        self.assertEqual(media.parse_target_size("50mb"), 50 * 1024 * 1024)
        self.assertEqual(
            media.parse_target_size("50%", source_bytes=10_000_000), 5_000_000
        )
        self.assertEqual(
            media.parse_target_size("25%", source_bytes=20_000_000), 5_000_000
        )
        self.assertEqual(
            media.parse_target_size("50pct", source_bytes=20_000_000), 10_000_000
        )
        self.assertEqual(
            media.parse_target_size("25pct", source_bytes=20_000_000), 5_000_000
        )
        self.assertEqual(media.parse_target_size(1024), 1024)
        self.assertIsNone(media.parse_target_size("invalid"))

    def test_calculate_target_bitrate(self):
        # 10 MB in 60s with 96k audio
        # available_bits = 10 * 1024 * 1024 * 8 * 0.98 = 82208358.4
        # total_bps = 82208358.4 / 60 = 1370139.3
        # video_bps = 1370139.3 - 96000 = 1274139 bps
        target_bytes = 10 * 1024 * 1024
        duration = 60.0
        bitrate = media.calculate_target_bitrate(target_bytes, duration, 96_000)
        self.assertAlmostEqual(bitrate, 1274139, delta=50)

    def test_calculate_target_bitrate_floor(self):
        # Very tiny target bytes should not produce negative or sub-32k bitrate
        bitrate = media.calculate_target_bitrate(100, 60.0, 96_000)
        self.assertEqual(bitrate, 32_000)

    def test_get_target_bytes(self):
        self.assertEqual(
            media.get_target_bytes({"target_size": "20mb"}), 20 * 1024 * 1024
        )
        self.assertEqual(media.get_target_bytes({"quality": "50mb"}), 50 * 1024 * 1024)
        self.assertEqual(
            media.get_target_bytes({"quality": "50%"}, source_bytes=1_000_000), 500_000
        )
        self.assertIsNone(media.get_target_bytes({"quality": "balanced"}))

    def test_mode_is_authoritative(self):
        crf_settings = default_state(1, 1)["settings"]
        self.assertEqual(crf_settings["target_size"], "20mb")
        self.assertIsNone(media.get_target_bytes(crf_settings))
        self.assertIsNone(
            media.get_target_bytes(
                {"mode": "crf", "target_size": "20mb", "quality": "50%"},
                source_bytes=2_000_000,
            )
        )
        self.assertEqual(
            media.get_target_bytes(
                {"mode": "size", "target_size": "20mb", "quality": "balanced"}
            ),
            20 * 1024 * 1024,
        )
        self.assertEqual(
            media.get_target_bytes(
                {"mode": "size", "target_size": "50pct"}, source_bytes=2_000_000
            ),
            1_000_000,
        )


class FilterConstructionTest(unittest.TestCase):
    def test_long_side_scaling(self):
        self.assertIn(
            "scale=trunc(iw/2)*2",
            media.build_video_filter("video", {"resolution": "original"}),
        )
        for res, cap in media.RESOLUTION_CAPS.items():
            vf = media.build_video_filter("video", {"resolution": res})
            self.assertIn(f"min({cap},iw)", vf)

    def test_fps_cap(self):
        vf_30 = media.build_video_filter("video", {"fps": "30"})
        self.assertIn("fps=fps=min(30\\,source_fps)", vf_30)

        vf_24 = media.build_video_filter("video", {"fps": "24"})
        self.assertIn("fps=fps=min(24\\,source_fps)", vf_24)

        vf_orig = media.build_video_filter("video", {"fps": "orig"})
        self.assertNotIn("fps=", vf_orig)

    def test_round_video_crop(self):
        vf = media.build_video_filter("video_note", {"resolution": "720"})
        self.assertIn("crop='min(iw,ih)':'min(iw,ih)'", vf)
        self.assertIn("min(640,iw)", vf)

    def test_hdr_tonemapping_detection(self):
        hdr_info = {
            "width": 1920,
            "height": 1080,
            "duration": 10.0,
            "has_audio": True,
            "pix_fmt": "yuv420p10le",
            "color_space": "bt2020nc",
            "color_transfer": "smpte2084",
            "color_primaries": "bt2020",
        }
        vf = media.build_video_filter("video", {"resolution": "720"}, info=hdr_info)
        self.assertIn("tonemap=mobius,format=yuv420p", vf)

        sdr_info = {
            "width": 1920,
            "height": 1080,
            "duration": 10.0,
            "has_audio": True,
            "pix_fmt": "yuv420p",
            "color_space": "bt709",
            "color_transfer": "bt709",
            "color_primaries": "bt709",
        }
        vf_sdr = media.build_video_filter("video", {"resolution": "720"}, info=sdr_info)
        self.assertNotIn("tonemap=", vf_sdr)

        vf_off = media.build_video_filter(
            "video", {"resolution": "720", "tonemap": "off"}, info=hdr_info
        )
        self.assertNotIn("tonemap=", vf_off)


class CodecArgumentsTest(unittest.TestCase):
    def test_h264_args(self):
        args = media.build_ffmpeg_args(
            Path("in.mp4"),
            Path("out.mp4"),
            "video",
            {"codec": "h264", "quality": "balanced", "resolution": "720"},
        )
        self.assertIn("libx264", args)
        self.assertIn("-crf", args)
        self.assertIn("26", args)
        self.assertIn("-threads", args)
        self.assertIn("0", args)
        self.assertIn("-maxrate", args)
        self.assertIn("-bufsize", args)

    def test_hevc_args(self):
        args = media.build_ffmpeg_args(
            Path("in.mp4"),
            Path("out.mp4"),
            "video",
            {"codec": "hevc", "quality": "high"},
        )
        self.assertIn("libx265", args)
        self.assertIn("-tag:v", args)
        self.assertIn("hvc1", args)
        self.assertIn("-threads", args)
        self.assertIn("0", args)

    def test_svtav1_args(self):
        args = media.build_ffmpeg_args(
            Path("in.mp4"),
            Path("out.mp4"),
            "video",
            {"codec": "av1", "quality": "balanced"},
        )
        self.assertIn("libsvtav1", args)
        self.assertIn("-svtav1-params", args)
        self.assertIn("preset=8", args)
        self.assertIn("-threads", args)
        self.assertIn("0", args)

    def test_vp9_args(self):
        args = media.build_ffmpeg_args(
            Path("in.mp4"),
            Path("out.webm"),
            "video",
            {"codec": "vp9", "quality": "balanced"},
        )
        self.assertIn("libvpx-vp9", args)
        self.assertIn("-row-mt", args)
        self.assertIn("1", args)
        self.assertIn("-threads", args)
        self.assertIn("0", args)

    def test_two_pass_args(self):
        p1 = media.build_ffmpeg_args(
            Path("in.mp4"),
            Path("out.mp4"),
            "video",
            {"codec": "h264"},
            pass_num=1,
            passlog_prefix=Path("/dev/shm/testpass"),
            target_bitrate=500000,
        )
        self.assertIn("-pass", p1)
        self.assertIn("1", p1)
        self.assertIn("-an", p1)
        self.assertIn("/dev/null", p1)

        p2 = media.build_ffmpeg_args(
            Path("in.mp4"),
            Path("out.mp4"),
            "video",
            {"codec": "h264"},
            pass_num=2,
            passlog_prefix=Path("/dev/shm/testpass"),
            target_bitrate=500000,
        )
        self.assertIn("-pass", p2)
        self.assertIn("2", p2)
        self.assertIn(str(Path("out.mp4")), p2)

    def test_default_state_and_every_crf_option_control_encoding(self):
        for crf in (18, 22, 26, 30, 34):
            with self.subTest(crf=crf):
                settings = default_state(1, 1)["settings"]
                settings["crf"] = crf
                args = media.build_ffmpeg_args(
                    Path("in.mp4"), Path("out.mp4"), "video", settings
                )
                self.assertEqual(args[args.index("-crf") + 1], str(crf))
                self.assertNotIn("-pass", args)
                self.assertNotIn("-b:v", args)

    def test_explicit_crf_overrides_legacy_quality(self):
        args = media.build_ffmpeg_args(
            Path("in.mp4"),
            Path("out.mp4"),
            "video",
            {"codec": "h264", "quality": "high", "crf": 30},
        )
        self.assertEqual(args[args.index("-crf") + 1], "30")

    def test_tune_only_applies_to_supported_codec_combinations(self):
        for codec, tune in (
            ("h264", "film"),
            ("h264", "animation"),
            ("hevc", "animation"),
        ):
            with self.subTest(codec=codec, tune=tune):
                args = media.build_ffmpeg_args(
                    Path("in.mp4"),
                    Path("out.mp4"),
                    "video",
                    {"codec": codec, "quality": "balanced", "tune": tune},
                )
                self.assertEqual(args[args.index("-tune") + 1], tune)

        for codec, tune in (
            ("h264", "none"),
            ("hevc", "none"),
            ("hevc", "film"),
            ("av1", "film"),
            ("vp9", "animation"),
        ):
            with self.subTest(codec=codec, tune=tune):
                args = media.build_ffmpeg_args(
                    Path("in.mp4"),
                    Path("out.mp4"),
                    "video",
                    {"codec": codec, "quality": "balanced", "tune": tune},
                )
                self.assertNotIn("-tune", args)

    def test_two_pass_also_applies_supported_tune(self):
        for pass_num in (1, 2):
            with self.subTest(pass_num=pass_num):
                args = media.build_ffmpeg_args(
                    Path("in.mp4"),
                    Path("out.mp4"),
                    "video",
                    {"codec": "h264", "tune": "film"},
                    pass_num=pass_num,
                    passlog_prefix=Path("/dev/shm/testpass"),
                    target_bitrate=500000,
                )
                self.assertEqual(args[args.index("-tune") + 1], "film")


class AudioArgumentsTest(unittest.TestCase):
    def test_auto_audio_copies_aac_under_128k(self):
        info = {
            "has_audio": True,
            "audio_codec": "aac",
            "audio_bitrate": 128000,
        }
        args = media.build_audio_args({"audio": "auto"}, info)
        self.assertEqual(args, ["-c:a", "copy"])

    def test_auto_audio_transcodes_aac_over_128k(self):
        info = {
            "has_audio": True,
            "audio_codec": "aac",
            "audio_bitrate": 192000,
        }
        args = media.build_audio_args({"audio": "auto"}, info)
        self.assertEqual(args, ["-c:a", "aac", "-b:a", "96k", "-ac", "2"])

    def test_auto_audio_transcodes_non_aac(self):
        info = {
            "has_audio": True,
            "audio_codec": "opus",
            "audio_bitrate": 96000,
        }
        args = media.build_audio_args({"audio": "auto"}, info)
        self.assertEqual(args, ["-c:a", "aac", "-b:a", "96k", "-ac", "2"])

    def test_audio_options(self):
        self.assertEqual(media.build_audio_args({"audio": "copy"}), ["-c:a", "copy"])
        self.assertEqual(
            media.build_audio_args({"audio": "aac_96k"}),
            ["-c:a", "aac", "-b:a", "96k", "-ac", "2"],
        )
        self.assertEqual(
            media.build_audio_args({"audio": "opus_64k"}),
            ["-c:a", "libopus", "-b:a", "64k"],
        )
        self.assertEqual(
            media.build_audio_args({"audio": "aac"}),
            ["-c:a", "aac", "-b:a", "96k", "-ac", "2"],
        )
        self.assertEqual(
            media.build_audio_args({"audio": "opus"}),
            ["-c:a", "libopus", "-b:a", "64k"],
        )
        self.assertEqual(
            media.build_audio_args({"audio": "mono"}),
            ["-c:a", "aac", "-b:a", "48k", "-ac", "1"],
        )
        self.assertEqual(media.build_audio_args({"audio": "mute"}), ["-an"])

    def test_video_without_audio_drops_audio(self):
        info = {"has_audio": False}
        self.assertEqual(media.build_audio_args({"audio": "copy"}, info), ["-an"])


class RotationAndProbingTest(unittest.TestCase):
    def test_rotation_extraction(self):
        # 90 degrees from tags
        video_90 = {"tags": {"rotate": "90"}}
        self.assertEqual(media.extract_rotation(video_90), 90)

        # 270 degrees from tags
        video_270 = {"tags": {"ROTATE": "270"}}
        self.assertEqual(media.extract_rotation(video_270), 270)

        # -90 degrees from side_data_list Display Matrix
        video_side = {
            "side_data_list": [{"side_data_type": "Display Matrix", "rotation": -90}]
        }
        self.assertEqual(media.extract_rotation(video_side), 270)

    @patch("subprocess.check_output")
    def test_probe_rotation_flips_dimensions(self, mock_output):
        probe_data = {
            "streams": [
                {
                    "codec_type": "video",
                    "codec_name": "h264",
                    "width": 1920,
                    "height": 1080,
                    "tags": {"rotate": "90"},
                    "r_frame_rate": "30/1",
                }
            ],
            "format": {"duration": "12.5"},
        }
        mock_output.return_value = json.dumps(probe_data)

        info = media.probe(Path("dummy.mp4"))
        self.assertEqual(info["width"], 1080)
        self.assertEqual(info["height"], 1920)
        self.assertEqual(info["rotation"], 90)
        self.assertEqual(info["duration"], 12.5)

    @patch("subprocess.check_output")
    def test_probe_ignores_cover_art_streams(self, mock_output):
        probe_data = {
            "streams": [
                {
                    "codec_type": "video",
                    "codec_name": "mjpeg",
                    "width": 500,
                    "height": 500,
                    "disposition": {"attached_pic": 1},
                },
                {
                    "codec_type": "video",
                    "codec_name": "h264",
                    "width": 1280,
                    "height": 720,
                    "pix_fmt": "yuv420p",
                    "color_space": "bt709",
                    "color_transfer": "bt709",
                },
                {
                    "codec_type": "audio",
                    "codec_name": "aac",
                    "bit_rate": "128000",
                },
            ],
            "format": {"duration": "30.0"},
        }
        mock_output.return_value = json.dumps(probe_data)

        info = media.probe(Path("dummy.mp4"))
        self.assertEqual(info["codec_name"], "h264")
        self.assertEqual(info["width"], 1280)
        self.assertEqual(info["height"], 720)
        self.assertTrue(info["has_audio"])
        self.assertEqual(info["audio_codec"], "aac")
        self.assertEqual(info["audio_bitrate"], 128000)
        self.assertEqual(info["color_space"], "bt709")

    def test_encoder_maps_the_non_cover_video_stream(self):
        args = media.build_ffmpeg_args(
            Path("in.mp4"),
            Path("out.mp4"),
            "video",
            {"codec": "h264", "quality": "balanced"},
            info={"video_stream_index": 1, "has_audio": False},
        )
        self.assertIn("0:1", args)
        self.assertNotIn("0:v:0", args)

    @patch("subprocess.check_output")
    def test_probe_fails_if_only_cover_art(self, mock_output):
        probe_data = {
            "streams": [
                {
                    "codec_type": "video",
                    "codec_name": "mjpeg",
                    "width": 500,
                    "height": 500,
                    "disposition": {"attached_pic": 1},
                }
            ],
            "format": {"duration": "10.0"},
        }
        mock_output.return_value = json.dumps(probe_data)

        with self.assertRaises(BotError):
            media.probe(Path("audio_with_cover.mp3"))

    @patch("subprocess.check_output")
    def test_probe_reports_non_cover_stream_index(self, mock_output):
        mock_output.return_value = json.dumps(
            {
                "streams": [
                    {
                        "codec_type": "video",
                        "codec_name": "mjpeg",
                        "disposition": {"attached_pic": 1},
                    },
                    {"codec_type": "video", "codec_name": "h264"},
                ],
                "format": {},
            }
        )
        self.assertEqual(media.probe(Path("clip.mov"))["video_stream_index"], 1)


class ProcessCallbackTest(unittest.TestCase):
    def test_run_step_exposes_process_for_cancellation(self):
        process = MagicMock()
        process.stdout.__iter__.return_value = iter([])
        process.wait.return_value = 0
        process.returncode = 0
        seen = []
        with tempfile.TemporaryDirectory() as directory:
            with patch("tgcompress.media.subprocess.Popen", return_value=process):
                media._run_ffmpeg_step(
                    ["ffmpeg"],
                    Path(directory) / "ffmpeg.err",
                    duration=1,
                    on_process=seen.append,
                )
        self.assertEqual(seen, [process])


class ProcessCancellationTest(unittest.TestCase):
    def test_cancellation_registry(self):
        mock_proc = MagicMock()
        media.register_process("job-123", mock_proc)
        self.assertIn("job-123", media.ACTIVE_PROCESSES)

        cancelled = media.cancel_process("job-123")
        self.assertTrue(cancelled)
        mock_proc.kill.assert_called_once()
        self.assertTrue(media.is_job_cancelled("job-123"))

        media.unregister_process("job-123")
        self.assertNotIn("job-123", media.ACTIVE_PROCESSES)
        self.assertFalse(media.is_job_cancelled("job-123"))


@unittest.skipUnless(
    shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg not installed"
)
class CompressTest(unittest.TestCase):
    def make_clip(self, directory, duration="1", pix_fmt="yuv420p"):
        src = Path(directory) / "in.mp4"
        subprocess.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-f",
                "lavfi",
                "-i",
                "testsrc=size=640x360:rate=30",
                "-t",
                duration,
                "-pix_fmt",
                pix_fmt,
                str(src),
            ],
            check=True,
        )
        return src

    def test_compress_short_video_h264(self):
        with tempfile.TemporaryDirectory() as directory:
            src = self.make_clip(directory)
            out = Path(directory) / "out.mp4"
            info = media.compress(src, out, "video", dict(media.DEFAULT_SETTINGS))
            self.assertTrue(out.exists())
            self.assertEqual(info["width"], 640)
            self.assertEqual(info["height"], 360)
            self.assertEqual(info["codec_name"], "h264")

    def test_compress_hevc(self):
        with tempfile.TemporaryDirectory() as directory:
            src = self.make_clip(directory)
            out = Path(directory) / "out_hevc.mp4"
            settings = dict(media.DEFAULT_SETTINGS, codec="hevc")
            info = media.compress(src, out, "video", settings)
            self.assertTrue(out.exists())
            self.assertEqual(info["codec_name"], "hevc")

    def test_compress_target_size_two_pass(self):
        with tempfile.TemporaryDirectory() as directory:
            src = self.make_clip(directory, duration="1")
            out = Path(directory) / "out_target.mp4"
            settings = dict(media.DEFAULT_SETTINGS, mode="size", target_size="10mb")
            info = media.compress(src, out, "video", settings, duration=1.0)
            self.assertTrue(out.exists())
            self.assertGreater(out.stat().st_size, 0)
            self.assertEqual(info["codec_name"], "h264")
    def test_av1_and_vp9_real_encodes_in_both_modes(self):
        for codec in ("av1", "vp9"):
            for mode in ("crf", "size"):
                with self.subTest(codec=codec, mode=mode):
                    with tempfile.TemporaryDirectory() as directory:
                        src = self.make_clip(directory)
                        out = Path(directory) / f"out-{codec}-{mode}.mp4"
                        settings = dict(media.DEFAULT_SETTINGS, codec=codec, mode=mode)
                        if mode == "size":
                            settings["target_size"] = "1mb"
                        info = media.compress(src, out, "video", settings, duration=1.0)
                        self.assertTrue(out.exists())
                        self.assertEqual(info["codec_name"], codec)

    def test_hdr_auto_and_off_encode(self):
        with tempfile.TemporaryDirectory() as directory:
            src = Path(directory) / "hdr.mkv"
            subprocess.run(
                [
                    "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                    "-f", "lavfi", "-i", "testsrc2=size=128x72:rate=10",
                    "-t", "1", "-c:v", "libx265", "-pix_fmt", "yuv420p10le",
                    "-color_primaries", "bt2020", "-color_trc", "smpte2084",
                    "-colorspace", "bt2020nc", str(src),
                ],
                check=True,
            )
            self.assertTrue(media.is_hdr(media.probe(src)))
            for tonemap in ("auto", "off"):
                with self.subTest(tonemap=tonemap):
                    out = Path(directory) / f"out-{tonemap}.mp4"
                    settings = dict(media.DEFAULT_SETTINGS, tonemap=tonemap)
                    info = media.compress(src, out, "video", settings, duration=1.0)
                    self.assertEqual(info["pix_fmt"], "yuv420p")

    def test_opus_audio_real_encode(self):
        with tempfile.TemporaryDirectory() as directory:
            src = Path(directory) / "audio-input.mp4"
            subprocess.run(
                [
                    "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                    "-f", "lavfi", "-i", "testsrc=size=640x360:rate=30",
                    "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000",
                    "-t", "1", "-c:a", "aac", str(src),
                ],
                check=True,
            )
            out = Path(directory) / "opus-output.mp4"
            settings = dict(media.DEFAULT_SETTINGS, audio="opus_64k")
            info = media.compress(src, out, "video", settings, duration=1.0)
            self.assertEqual(info["audio_codec"], "opus")


if __name__ == "__main__":
    unittest.main()
