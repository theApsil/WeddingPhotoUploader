"""Video poster frames via ffmpeg.

Posters are best-effort: a missing or failed frame simply leaves the video
tile without a preview — it must never break an upload.
"""

from __future__ import annotations

import logging
import subprocess
import tempfile
from pathlib import Path

logger = logging.getLogger("wedding.poster")

_POSTER_TIMEOUT_SECONDS = 30


def _extract(path: str | Path, ffmpeg: str) -> bytes | None:
    """Extract one JPEG frame near the start of the video."""
    try:
        proc = subprocess.run(
            [
                ffmpeg,
                "-ss",
                "1",
                "-i",
                str(path),
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
        logger.warning("ffmpeg не вернул кадр — постер видео не создан")
        return None
    return proc.stdout


def generate_poster_from_path(path: str | Path, ffmpeg: str) -> bytes | None:
    return _extract(path, ffmpeg)


def generate_poster_from_bytes(data: bytes, ffmpeg: str) -> bytes | None:
    """Extract a poster frame from an in-memory video (e.g. a bucket object)."""
    if not data:
        return None
    with tempfile.NamedTemporaryFile(suffix=".video") as tmp:
        tmp.write(data)
        tmp.flush()
        return _extract(tmp.name, ffmpeg)