"""Image uploads for the server start pages.

Every upload is decoded with Pillow, downscaled and re-encoded as WebP under a random
name. That strips metadata (EXIF/GPS) and rejects files that only pretend to be images.
"""
import os
import re
import secrets

from PIL import Image, ImageOps, UnidentifiedImageError

ALLOWED_FORMATS = {"JPEG", "PNG", "WEBP", "GIF"}
MAX_SIZE = {"banner": (2400, 1200), "gallery": (2000, 2000)}
MAX_UPLOAD_BYTES = 10 * 1024 * 1024
# Refuse decompression bombs (a tiny file that expands to a gigantic image).
Image.MAX_IMAGE_PIXELS = 40_000_000
FILENAME_RE = re.compile(r"^[a-f0-9]{32}\.webp$")


class InvalidImage(ValueError):
    pass


def save_image(file, kind, upload_dir):
    """Validate and store an uploaded image (a file-like object). Returns the new filename."""
    if kind not in MAX_SIZE:
        raise InvalidImage("unknown image kind")
    try:
        with Image.open(file) as image:
            if image.format not in ALLOWED_FORMATS:
                raise InvalidImage("Erlaubt sind JPG, PNG, WebP und GIF.")
            image.load()
            image = ImageOps.exif_transpose(image)  # keep the orientation, EXIF itself is dropped
            image = image.convert("RGBA" if image.mode in ("RGBA", "LA", "P") else "RGB")
            image.thumbnail(MAX_SIZE[kind], Image.LANCZOS)
            filename = secrets.token_hex(16) + ".webp"
            os.makedirs(upload_dir, exist_ok=True)
            image.save(os.path.join(upload_dir, filename), "WEBP", quality=85, method=4)
            return filename
    except InvalidImage:
        raise
    except (UnidentifiedImageError, Image.DecompressionBombError, OSError, SyntaxError) as e:
        raise InvalidImage("Die Datei ist kein gültiges Bild.") from e


def delete_images(filenames, upload_dir):
    for filename in filenames:
        if FILENAME_RE.match(filename or ""):
            try:
                os.remove(os.path.join(upload_dir, filename))
            except FileNotFoundError:
                pass
