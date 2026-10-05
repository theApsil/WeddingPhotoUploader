"""Video poster frames via ffmpeg.

Posters are best-effort: a missing or failed frame simply leaves the video
tile without a preview — it must never break an upload.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from functools import lru_cache
from pathlib import Path

logger = logging.getLogger("wedding.poster")

_POSTER_TIMEOUT_SECONDS = 30


@lru_cache(maxsize=None)
def ffmpeg_available(ffmpeg: str) -> bool:
    """Checked once per binary; a missing ffmpeg is logged a single time."""
    if shutil.which(ffmpeg):
        return True
    logger.warning("ffmpeg (%s) не найден — постеры видео создаваться не будут", ffmpeg)
    return False


def _extract(source: str | Path, ffmpeg: str) -> bytes | None:
    """Extract one JPEG frame near the start of the video (file path or URL)."""
    if not ffmpeg_available(ffmpeg):
        return None
    # 1 s in skips black first frames; clips shorter than that get frame 0.
    for seek in ("1", "0"):
        frame = _run_ffmpeg(source, ffmpeg, seek)
        if frame:
            return frame
    logger.warning("ffmpeg не вернул кадр — постер видео не создан")
    return None


def _run_ffmpeg(source: str | Path, ffmpeg: str, seek: str) -> bytes | None:
    try:
        proc = subprocess.run(
            [
                ffmpeg,
                "-ss",
                seek,
                "-i",
                str(source),
                "-frames:v",
                "1",
                "-vf",
                "scale='min(640,iw)':-2",
                "-f",
                "image2pipe",
                "-vcodec",
                "mjpeg",
                "-",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=_POSTER_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired):
        logger.warning("ffmpeg недоступен или завис — постер видео не создан")
        return None
    if proc.returncode != 0 or not proc.stdout:
        return None
    return proc.stdout


def generate_poster_from_path(path: str | Path, ffmpeg: str) -> bytes | None:
    return _extract(path, ffmpeg)


def generate_poster_from_url(url: str, ffmpeg: str) -> bytes | None:
    """Extract a poster frame from a remote video (e.g. a signed bucket URL).

    ffmpeg seeks with HTTP range requests, fetching only what it needs.
    """
    if not url:
        return None
    return _extract(url, ffmpeg)