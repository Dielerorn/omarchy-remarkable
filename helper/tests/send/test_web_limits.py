"""A hostile web server can't make the import use unbounded memory or time:
compressed bodies are capped after decoding, downloads have a deadline, and
images are size-checked and decoded in a limited child process."""

from __future__ import annotations

import gzip
import http.server
import struct
import threading
import time
import zlib

import pytest

from rmbridge import web


def _zeros_gzip(n: int) -> bytes:
    """n zero bytes, gzipped, without building them in memory."""
    comp = zlib.compressobj(9, zlib.DEFLATED, 16 + zlib.MAX_WBITS)
    block = b"\0" * (1 << 20)
    out = [comp.compress(block) for _ in range(n >> 20)]
    return b"".join(out) + comp.flush()


@pytest.fixture
def server():
    routes: dict[str, tuple[dict, object]] = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            headers, body = routes[self.path]
            self.send_response(200)
            for k, v in headers.items():
                self.send_header(k, v)
            self.end_headers()
            try:
                if callable(body):
                    body(self.wfile)
                else:
                    self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    def add(path, body, **headers):
        routes[path] = ({"Content-Type": "text/html", **headers}, body)
        return f"http://127.0.0.1:{httpd.server_port}{path}"
    yield add
    httpd.shutdown()


def test_gzip_bomb_stops_at_the_limit(server):
    bomb = _zeros_gzip(512 << 20)  # 512 MiB of zeros in about half a MB
    url = server("/bomb", bomb, **{"Content-Encoding": "gzip", "Content-Length": str(len(bomb))})
    t = time.monotonic()
    with pytest.raises(web.WebError, match="more than 1 MB"):
        web.fetch(url, limit=1 << 20)
    assert time.monotonic() - t < 5


def test_compressed_size_is_capped_too(server):
    url = server("/big", b"x" * (3 << 20))
    with pytest.raises(web.WebError, match="more than 1 MB"):
        web.fetch(url, limit=1 << 20)


def test_gzip_deflate_and_multi_member_still_work(server):
    page = b"<html><body>" + b"hello " * 1000 + b"</body></html>"
    two = gzip.compress(page[:500]) + gzip.compress(page[500:])
    assert web.fetch(server("/g", two, **{"Content-Encoding": "gzip"}))[0] == page
    assert web.fetch(server("/d", zlib.compress(page), **{"Content-Encoding": "deflate"}))[0] == page
    assert web.fetch(server("/p", page))[0] == page


def test_unknown_or_damaged_encodings_fail_cleanly(server):
    with pytest.raises(web.WebError, match="br-encoded"):
        web.fetch(server("/br", b"xx", **{"Content-Encoding": "br"}))
    with pytest.raises(web.WebError, match="damaged gzip"):
        web.fetch(server("/bad", b"\x1f\x8bnot gzip at all", **{"Content-Encoding": "gzip"}))


def test_a_trickling_server_hits_the_deadline(server):
    def trickle(w):
        for _ in range(100):
            w.write(b"x")
            w.flush()
            time.sleep(0.2)
    url = server("/slow", trickle)
    t = time.monotonic()
    with pytest.raises(web.WebError, match="too long"):
        web.fetch(url, timeout=0.5)  # 0.5 s per read, 2 s in all
    assert time.monotonic() - t < 4


def _png_header(w: int, h: int) -> bytes:
    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"\0" * 64)) + chunk(b"IEND", b""))


def test_declared_sizes():
    assert web.declared_size(_png_header(100000, 100000)) == (100000, 100000)
    assert web.declared_size(b"GIF89a" + struct.pack("<HH", 640, 480) + b"\0" * 8) == (640, 480)
    jpeg = b"\xff\xd8\xff\xe0" + struct.pack(">H", 16) + b"JFIF\0" + b"\0" * 9 + \
        b"\xff\xc0" + struct.pack(">HBHH", 17, 8, 3000, 4000) + b"\0" * 12
    assert web.declared_size(jpeg) == (4000, 3000)
    vp8x = b"RIFF" + b"\0" * 4 + b"WEBPVP8X" + b"\0" * 8 + (99999).to_bytes(3, "little") + (49).to_bytes(3, "little")
    assert web.declared_size(vp8x) == (100000, 50)
    assert web.declared_size(b"not an image") is None


def test_pixel_bombs_are_refused_before_decoding():
    t = time.monotonic()
    assert web.prepare_image(_png_header(100000, 100000)) is None
    assert web.prepare_image(_png_header(6000, 6000)) is None  # 36 MP > MAX_IMAGE_PIXELS
    assert time.monotonic() - t < 1


def test_tall_svg_renders_within_the_pixel_budget():
    svg = b'<svg xmlns="http://www.w3.org/2000/svg" width="400" height="9000000"><rect width="400" height="9000000"/></svg>'
    t = time.monotonic()
    result = web.prepare_image(svg)
    assert time.monotonic() - t < 10
    if result is not None:
        import pymupdf
        pix = pymupdf.Pixmap(result[0])
        assert pix.width * pix.height <= web.MAX_IMAGE_PIXELS


def test_decoder_process_survives_bad_input_and_failures():
    import pymupdf
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 300, 200), False)
    pix.clear_with(200)
    png = pix.tobytes("png")
    with web.ImageDecoder() as d:
        assert d.prepare(png)[1] == "image/jpeg"
        assert d.prepare(b"junk") is None
        assert d.prepare(_png_header(100000, 100000)) is None
        assert d.prepare(png)[1] == "image/jpeg"
    # A child that can't get going (here: starved of memory) costs one timeout
    # and returns nothing; it never raises into the import.
    with web.ImageDecoder(memory=1 << 30, timeout=3) as d:
        assert d.prepare(png) is None
        assert d.proc is None


def test_data_uris_are_capped():
    import base64
    huge = "data:image/png;base64," + base64.b64encode(b"\0" * (web.MAX_IMAGE_BYTES + 10)).decode()
    assert web._decode_data_uri(huge) is None
    assert web._decode_data_uri("data:image/png;base64," + base64.b64encode(b"abc").decode()) == b"abc"
