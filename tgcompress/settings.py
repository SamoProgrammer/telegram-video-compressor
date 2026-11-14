"""Canonical compression defaults, option IDs, and saved-setting migrations."""

DEFAULT_SETTINGS = {
    "codec": "h264",
    "mode": "crf",
    "crf": 26,
    "target_size": "20mb",
    "resolution": "720",
    "fps": "orig",
    "audio": "auto",
    "tune": "none",
    "tonemap": "auto",
    "output": "auto",
    "quality": "balanced",
}

QUALITIES = {
    "lossless": ("Near Lossless", "faster", 18),
    "high": ("High", "faster", 22),
    "balanced": ("Balanced", "veryfast", 26),
    "small": ("Small", "veryfast", 30),
    "extreme": ("Extreme", "veryfast", 34),
}

CODECS = [
    ("h264", "H.264"),
    ("hevc", "H.265 (HEVC)"),
    ("av1", "AV1 (SVT)"),
    ("vp9", "VP9"),
]
MODES = [("crf", "Quality (CRF)"), ("size", "Target File Size")]
CRF_OPTIONS = [(str(crf), str(crf)) for crf in (18, 22, 26, 30, 34)]
TARGET_SIZES = [
    ("10mb", "10 MB"),
    ("20mb", "20 MB"),
    ("50mb", "50 MB"),
    ("50%", "50%"),
    ("25%", "25%"),
]
RESOLUTIONS = [
    ("original", "Original"),
    ("2160", "2160p"),
    ("1440", "1440p"),
    ("1080", "1080p"),
    ("720", "720p"),
    ("480", "480p"),
    ("360", "360p"),
    ("240", "240p"),
]
RES_OPTIONS = RESOLUTIONS
FPS_OPTIONS = [("orig", "Original"), ("30", "30 fps"), ("24", "24 fps")]
AUDIO_OPTIONS = [
    ("auto", "Auto"),
    ("copy", "Pass-through"),
    ("aac_96k", "AAC 96k"),
    ("opus_64k", "Opus 64k"),
    ("mono", "Mono"),
    ("mute", "Mute"),
]
OUTPUT_OPTIONS = [
    ("auto", "Auto"),
    ("video", "Video"),
    ("document", "Document"),
    ("note", "Round Note"),
]
TUNE_OPTIONS = [
    ("none", "Tune: None"),
    ("film", "Tune: Film"),
    ("animation", "Tune: Animation"),
]
TONEMAP_OPTIONS = [("auto", "HDR Tonemap: Auto"), ("off", "HDR Tonemap: Off")]

TARGET_SIZE_ALIASES = {"50pct": "50%", "25pct": "25%"}
AUDIO_ALIASES = {"aac": "aac_96k", "opus": "opus_64k"}


def canonical_target_size(value: str) -> str:
    value = value.strip().lower()
    return TARGET_SIZE_ALIASES.get(value, value)


def canonical_audio(value: str) -> str:
    value = value.strip().lower()
    return AUDIO_ALIASES.get(value, value)


def migrate_settings(settings: dict) -> dict:
    if isinstance(settings.get("target_size"), str):
        settings["target_size"] = canonical_target_size(settings["target_size"])
    if isinstance(settings.get("audio"), str):
        settings["audio"] = canonical_audio(settings["audio"])
    return settings
