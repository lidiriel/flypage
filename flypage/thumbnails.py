"""Small renderings of the first page of a scan, cached on disk.

``render()`` is slow (it decodes the full scan) and is meant to run in a
worker thread; it only touches its own output file.
"""

from __future__ import annotations

import hashlib
import os
import threading
from pathlib import Path

from PIL import Image, ImageOps

from flypage import paths

THUMBNAIL = 256  # grid
PREVIEW = 1600  # large preview pane


def cache_path(src: Path, size: int) -> Path:
    """Cache file for ``src``: a new one whenever the file changes."""
    st = src.stat()
    key = f"{src.resolve()}\0{st.st_size}\0{st.st_mtime_ns}\0{size}"
    return paths.cache_dir() / "thumbs" / (hashlib.sha1(key.encode()).hexdigest() + ".png")


def render(src: Path, size: int) -> Path:
    """Return a PNG of the first page of ``src``, fitting in ``size``×``size`` pixels."""
    out = cache_path(src, size)
    if out.exists():
        return out
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(f".{out.stem}.{os.getpid()}-{threading.get_ident()}.png")
    try:
        with src.open("rb") as f:
            is_pdf = f.read(5) == b"%PDF-"
        (_render_pdf if is_pdf else _render_image)(src, size, tmp)
        os.replace(tmp, out)  # atomic: a concurrent reader never sees half a PNG
    finally:
        tmp.unlink(missing_ok=True)
    return out


def _render_image(src: Path, size: int, dst: Path) -> None:
    with Image.open(src) as im:
        im.draft("RGB", (size, size))  # JPEG: decode at a reduced scale, much faster
        im = ImageOps.exif_transpose(im)
        im.thumbnail((size, size))
        if im.mode not in ("RGB", "RGBA", "L"):
            im = im.convert("RGBA")
        im.save(dst, "PNG")


def _render_pdf(src: Path, size: int, dst: Path) -> None:
    import cairo
    import gi

    gi.require_version("Poppler", "0.18")
    from gi.repository import Poppler

    page = Poppler.Document.new_from_file(src.resolve().as_uri(), None).get_page(0)
    width, height = page.get_size()
    scale = size / max(width, height)
    surface = cairo.ImageSurface(cairo.FORMAT_RGB24, max(1, round(width * scale)), max(1, round(height * scale)))
    ctx = cairo.Context(surface)
    ctx.set_source_rgb(1, 1, 1)  # PDF pages have no background of their own
    ctx.paint()
    ctx.scale(scale, scale)
    page.render(ctx)
    surface.write_to_png(str(dst))
