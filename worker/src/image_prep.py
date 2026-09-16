# ABOUTME: Image preprocessing — resize, thumbnail, base64 encoding for VLLM input.
# ABOUTME: Handles format detection, aspect-preserving resize, and Anthropic content block creation.

import base64
from io import BytesIO
from pathlib import Path

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}


def is_image_file(filename: str) -> bool:
    """Check if a filename has an image extension."""
    return Path(filename).suffix.lower() in IMAGE_EXTENSIONS


def resize_image(path: Path, max_edge: int = 1024):
    """Resize image so longest edge is at most max_edge, preserving aspect ratio."""
    from PIL import Image
    img = Image.open(path)
    if max(img.size) <= max_edge:
        return img
    ratio = max_edge / max(img.size)
    new_size = (int(img.size[0] * ratio), int(img.size[1] * ratio))
    return img.resize(new_size, Image.LANCZOS)


def make_thumbnail(path: Path, output_path: Path, max_edge: int = 256) -> Path:
    """Create a thumbnail for UI display."""
    from PIL import Image
    img = Image.open(path)
    img.thumbnail((max_edge, max_edge), Image.LANCZOS)
    img.save(output_path, quality=80)
    return output_path


def image_to_base64(path: Path, max_edge: int = 1024) -> tuple[str, str]:
    """Resize and encode image as base64. Returns (base64_string, media_type)."""
    img = resize_image(path, max_edge)
    buf = BytesIO()
    fmt = "JPEG"
    media_type = "image/jpeg"
    suffix = Path(path).suffix.lower()
    if suffix == ".png":
        fmt = "PNG"
        media_type = "image/png"
    elif suffix == ".webp":
        fmt = "WEBP"
        media_type = "image/webp"
    elif suffix == ".gif":
        fmt = "GIF"
        media_type = "image/gif"
    img.save(buf, format=fmt, quality=85)
    return base64.b64encode(buf.getvalue()).decode(), media_type


def make_image_content_block(path: Path, max_edge: int = 1024) -> dict:
    """Create an Anthropic image content block from a file path."""
    b64, media_type = image_to_base64(path, max_edge)
    return {
        "type": "image",
        "source": {"type": "base64", "media_type": media_type, "data": b64},
    }
