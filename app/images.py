"""Product photo uploads. The page sends a data URL; we check it really is an image and store it as a file."""
import base64
import binascii
import re
import secrets
from pathlib import Path

from . import config

_RE = re.compile(r"^data:image/(?:jpeg|jpg|png|webp);base64,([A-Za-z0-9+/=\s]+)$")


class ImageError(ValueError):
    pass


def _kind(raw):
    if raw[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if raw[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return ".webp"
    return None


def validate(data_url):
    """Checks a data URL really holds a JPEG, PNG or WebP of allowed size. Returns (bytes, extension)."""
    m = _RE.match(data_url or "")
    if not m:
        raise ImageError("Please upload a JPEG, PNG or WebP image.")
    try:
        raw = base64.b64decode(m.group(1), validate=False)
    except (binascii.Error, ValueError):
        raise ImageError("The image could not be read.")
    if len(raw) > config.MAX_IMAGE_BYTES:
        raise ImageError("The image is too large (maximum 1.5 MB).")
    ext = _kind(raw)
    if not ext:
        raise ImageError("The file is not a valid image.")
    return raw, ext


def save(data_url):
    """Returns the public path, for example 'uploads/ab12.jpg'."""
    raw, ext = validate(data_url)
    config.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    name = secrets.token_hex(12) + ext
    (config.UPLOAD_DIR / name).write_bytes(raw)
    return "uploads/" + name


def remove(public_path):
    if not public_path:
        return
    try:
        (config.UPLOAD_DIR / Path(public_path).name).unlink(missing_ok=True)
    except OSError:
        pass


ORPHAN_MIN_AGE = 600   # seconds: a photo uploaded a moment ago may not be attached to its product yet
NAME_RE = re.compile(r"^[0-9a-f]{24}\.(?:jpg|png|webp)$")


def list_files():
    """[(file name, size in bytes, age in seconds)] for everything in the uploads folder."""
    import time
    out = []
    if config.UPLOAD_DIR.exists():
        for f in config.UPLOAD_DIR.iterdir():
            if f.is_file() and NAME_RE.match(f.name):
                st = f.stat()
                out.append((f.name, st.st_size, time.time() - st.st_mtime))
    return out


def size_of(public_path):
    try:
        return (config.UPLOAD_DIR / Path(public_path).name).stat().st_size
    except OSError:
        return 0


def delete_file(name):
    if NAME_RE.match(name or ""):
        (config.UPLOAD_DIR / name).unlink(missing_ok=True)


HERO = config.BASE / "static" / "img" / "hero.jpg"


def save_hero(data_url):
    """Replace the home page banner photo. Must be a JPEG."""
    raw, ext = validate(data_url)
    if ext != ".jpg":
        raise ImageError("The banner must be a JPEG (.jpg) image.")
    HERO.parent.mkdir(parents=True, exist_ok=True)
    tmp = HERO.with_suffix(".tmp")
    tmp.write_bytes(raw)
    tmp.replace(HERO)

