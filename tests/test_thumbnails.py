import os

from PIL import Image

from flypage import thumbnails


def test_image_thumbnail_fits_and_is_cached(samples):
    src = samples.jpeg()
    out = thumbnails.render(src, 128)
    with Image.open(out) as im:
        assert max(im.size) == 128
    mtime = out.stat().st_mtime_ns
    assert thumbnails.render(src, 128) == out
    assert out.stat().st_mtime_ns == mtime  # not rendered again


def test_cache_follows_file_changes(samples):
    src = samples.png()
    first = thumbnails.render(src, 64)
    st = src.stat()
    os.utime(src, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))
    assert thumbnails.render(src, 64) != first


def test_pdf_first_page(samples):
    out = thumbnails.render(samples.pdf(pages=2), 200)
    with Image.open(out) as im:
        assert max(im.size) == 200
        # The rendering is not blank: the sample has black text on white.
        assert im.convert("L").getextrema()[0] < 100


def test_scanner_jpeg_quirk_renders(samples):
    out = thumbnails.render(samples.scanner_jpeg(), 64)
    assert out.exists()


def test_transparent_png(samples):
    out = thumbnails.render(samples.png(mode="RGBA"), 64)
    with Image.open(out) as im:
        assert im.mode == "RGBA"
