"""web.py: article extraction, EPUB structure, PDF layout, fallbacks."""

from __future__ import annotations

import argparse
import re
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

import pymupdf
import pytest

from rmbridge import send, web

URL = "https://blog.example.org/2026/10/a-long-walk"

PARAGRAPH = ("Stockholm is built on fourteen islands, and walking between them takes longer than the map "
             "suggests because every bridge offers another view worth stopping for. ")

ARTICLE_HTML = f"""<!DOCTYPE html>
<html lang="sv"><head>
<meta charset="utf-8">
<title>A Long Walk Through Stockholm | Example Blog</title>
<meta property="og:site_name" content="Example Blog">
<meta name="author" content="Ada Lovelace">
<meta property="article:published_time" content="2026-10-01T08:00:00Z">
</head><body>
<nav><a href="/">Home</a> <a href="/about">About</a></nav>
<article>
<h1>A Long Walk Through Stockholm</h1>
<p>{PARAGRAPH * 3}<sup>[1]</sup></p>
<p><img src="/img/bridge.png" alt="A bridge"></p>
<h2>Gamla stan</h2>
<p>{PARAGRAPH * 2} Prices &amp; "quotes" &lt;survive&gt; escaping.</p>
<ul><li>Coffee at <b>Kaffekoppen</b></li><li>The narrowest alley, <i>Mårten Trotzigs gränd</i></li></ul>
<blockquote><p>The best way to see a city is on foot.</p></blockquote>
<h2>Södermalm</h2>
<p>{PARAGRAPH * 2}</p>
<p><img src="https://cdn.example.org/missing.png" alt="gone"> Text after a broken image stays.</p>
<table><tr><th>Island</th><th>Steps</th></tr><tr><td>Gamla stan</td><td>4,200</td></tr></table>
<h3>Getting back</h3>
<p>{PARAGRAPH * 2}</p>
<pre><code>walk --from gamla-stan --to sodermalm</code></pre>
</article>
<footer>Copyright Example Blog</footer>
</body></html>
""".encode()


def png(w: int = 300, h: int = 200) -> bytes:
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, w, h), False)
    pix.set_rect(pix.irect, (90, 120, 160))
    return pix.tobytes("png")


def fake_images(src: str) -> bytes | None:
    return png() if src.endswith("bridge.png") else None


@pytest.fixture
def article() -> web.Article:
    a = web.extract(ARTICLE_HTML, URL)
    assert a is not None
    web.attach_images(a, fake_images)
    return a


# --------------------------------------------------------------- extraction


def test_extract_metadata(article):
    assert article.title == "A Long Walk Through Stockholm"
    assert article.author == "Ada Lovelace"
    assert article.date.startswith("2026-10-01")
    assert article.site == "Example Blog"
    assert article.language == "sv"
    assert article.text_chars > web.MIN_ARTICLE_CHARS


def test_body_structure(article):
    body = article.body
    ET.fromstring(body)  # well-formed
    assert "<h1" not in body  # the page's own h1 is replaced by our header
    assert [t for _, _, t in article.headings] == ["Gamla stan", "Södermalm", "Getting back"]
    assert "<ul>" in body and "<b>Kaffekoppen</b>" in body
    assert "<blockquote>" in body and "<table>" in body and "<th>Island</th>" in body
    assert "<pre>walk --from gamla-stan" in body
    assert "[1]" not in body  # citation markers dropped
    assert "Prices &amp; \"quotes\" &lt;survive&gt;" in body


def test_images_downloaded_and_broken_ones_dropped(article):
    assert list(article.images) == ["images/img001.jpg"]
    body = article.body
    assert '<img src="images/img001.jpg"' in body
    assert "@img" not in body and "missing.png" not in body
    assert body.count("<img") == 1
    assert "Text after a broken image stays." in body
    assert '<div class="figure" />' not in body and '<div class="figure"></div>' not in body


def test_prepare_image_shrinks_and_skips_icons():
    data, media = web.prepare_image(png(3000, 1000))
    assert media == "image/jpeg"
    assert pymupdf.Pixmap(data).width == web.IMAGE_MAX_PX
    assert web.prepare_image(png(16, 16)) is None
    assert web.prepare_image(b"not an image") is None


@pytest.mark.parametrize("title,site,heading,host,expected", [
    ("E Ink - Wikipedia", "Wikimedia Foundation, Inc.", "E Ink", "en.wikipedia.org", "E Ink"),
    ("Popular git config options", "Julia Evans", "", "jvns.ca", "Popular git config options"),
    ("Some Story | The Verge", "The Verge", "", "www.theverge.com", "Some Story"),
    ("Some Story — Example", "", "", "example.com", "Some Story"),
    ("Keep - this - title", "Other", "", "x.org", "Keep - this - title"),
    ("", "", "Only a heading", "x.org", "Only a heading"),
])
def test_clean_title(title, site, heading, host, expected):
    assert web.clean_title(title, site, heading, host) == expected


def test_normalize_url():
    assert web.normalize_url("example.com/page") == "https://example.com/page"
    assert web.normalize_url(" <https://example.com/a?b=1> ") == "https://example.com/a?b=1"
    for bad in ("file:///etc/passwd", "javascript:alert(1)", "ftp://example.com/x", "https://"):
        with pytest.raises(web.WebError):
            web.normalize_url(bad)


# ---------------------------------------------------------------------- EPUB


def validate_epub(path: Path) -> dict[str, bytes]:
    """The OCF/OPF rules readers (and the reMarkable cloud) depend on."""
    with zipfile.ZipFile(path) as z:
        infos = z.infolist()
        assert infos[0].filename == "mimetype"
        assert infos[0].compress_type == zipfile.ZIP_STORED
        assert infos[0].extra == b""
        files = {i.filename: z.read(i) for i in infos}
    assert files["mimetype"] == b"application/epub+zip"

    container = ET.fromstring(files["META-INF/container.xml"])
    ns = {"c": "urn:oasis:names:tc:opendocument:xmlns:container"}
    rootfile = container.find("c:rootfiles/c:rootfile", ns)
    assert rootfile is not None and rootfile.get("media-type") == "application/oebps-package+xml"
    opf_path = rootfile.get("full-path")
    base = opf_path.rsplit("/", 1)[0] + "/"

    opf = ET.fromstring(files[opf_path])
    o = {"opf": "http://www.idpf.org/2007/opf", "dc": "http://purl.org/dc/elements/1.1/"}
    assert opf.get("version") == "3.0"
    uid = opf.get("unique-identifier")
    ident = opf.find(f"opf:metadata/dc:identifier[@id='{uid}']", o)
    assert ident is not None and ident.text.startswith("urn:uuid:")
    assert opf.find("opf:metadata/dc:title", o).text
    assert opf.find("opf:metadata/dc:language", o).text
    modified = opf.find("opf:metadata/opf:meta[@property='dcterms:modified']", o)
    assert modified is not None and re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", modified.text)

    manifest = {i.get("id"): i for i in opf.findall("opf:manifest/opf:item", o)}
    for item in manifest.values():
        assert base + item.get("href") in files, f"manifest lists missing {item.get('href')}"
    listed = {base + i.get("href") for i in manifest.values()}
    extra = set(files) - listed - {"mimetype", "META-INF/container.xml", opf_path}
    assert not extra, f"files not in the manifest: {extra}"
    navs = [i for i in manifest.values() if "nav" in (i.get("properties") or "").split()]
    assert len(navs) == 1
    spine = opf.find("opf:spine", o)
    assert spine.get("toc") in manifest
    refs = [r.get("idref") for r in spine.findall("opf:itemref", o)]
    assert refs and all(r in manifest for r in refs)

    for item in manifest.values():
        if item.get("media-type") == "application/xhtml+xml":
            doc = ET.fromstring(files[base + item.get("href")])  # well-formed XML
            assert doc.tag == "{http://www.w3.org/1999/xhtml}html"
    nav = ET.fromstring(files[base + navs[0].get("href")])
    toc = nav.find(".//{http://www.w3.org/1999/xhtml}nav")
    assert toc is not None and toc.get("{http://www.idpf.org/2007/ops}type") == "toc"
    chapter = files[base + "article.xhtml"].decode()
    for a in toc.iter("{http://www.w3.org/1999/xhtml}a"):
        target = a.get("href")
        assert target.split("#")[0] == "article.xhtml"
        if "#" in target:
            assert f'id="{target.split("#")[1]}"' in chapter
    for src in re.findall(r'<img src="([^"]+)"', chapter):
        assert base + src in listed
    return files


def test_epub_is_valid(article, tmp_path):
    out = web.build_epub(article, tmp_path / "a.epub")
    files = validate_epub(out)
    chapter = files["OEBPS/article.xhtml"].decode()
    assert "<h1>A Long Walk Through Stockholm</h1>" in chapter
    assert "Ada Lovelace · 1 October 2026 · Example Blog" in chapter
    assert "blog.example.org/2026/10/a-long-walk" in chapter
    nav = files["OEBPS/nav.xhtml"].decode()
    assert "Gamla stan" in nav and "Södermalm" in nav and "Getting back" not in nav  # top level only
    # A real EPUB engine opens it and lays it out.
    with pymupdf.open(out) as d:
        assert d.page_count >= 1
        assert "A Long Walk Through Stockholm" in d[0].get_text()


def test_epub_without_images_or_headings(tmp_path):
    a = web.Article(url="https://x.org/p", title="Plain & simple", tree=ET.fromstring("<div><p>Hi</p></div>"))
    files = validate_epub(web.build_epub(a, tmp_path / "p.epub"))
    assert "<ol>" not in files["OEBPS/nav.xhtml"].decode().split("<ol>", 1)[1]  # no empty nested list
    assert b"Plain &amp; simple" in files["OEBPS/content.opf"]


# ----------------------------------------------------------------------- PDF


def test_pdf_layout(article, tmp_path):
    out = web.build_pdf(article, tmp_path / "a.pdf")
    with pymupdf.open(out) as d:
        assert d.page_count >= 1
        page = d[0]
        assert abs(page.rect.width - send.PAGE_W) < 0.5 and abs(page.rect.height - send.PAGE_H) < 0.5
        text = page.get_text()
        assert "A Long Walk Through Stockholm" in text
        assert "Ada Lovelace" in text and "blog.example.org" in text
        assert d.metadata["title"] == "A Long Walk Through Stockholm"
        assert sum(len(p.get_images()) for p in d) == 1
        # Images are scaled to the text column.
        for p in d:
            for info in p.get_image_info():
                x0, _, x1, _ = info["bbox"]
                assert x1 - x0 <= send.PAGE_W - 80 + 1


# ----------------------------------------------------------------- pipeline


@pytest.fixture
def no_progress(monkeypatch):
    from rmbridge import cli
    monkeypatch.setattr(cli, "progress", lambda *a, **k: None)


def fake_pdf(path: Path) -> Path:
    d = pymupdf.open()
    d.new_page().insert_text((72, 72), "printed by the browser")
    d.save(path)
    return path


def test_build_article(monkeypatch, tmp_path, no_progress):
    monkeypatch.setattr(web, "fetch", lambda url, **kw: (ARTICLE_HTML, "text/html", url) if url == URL
                        else (png(), "image/png", url))
    made = web.build(URL, "epub", tmp_path)
    assert made["mode"] == "article" and made["format"] == "epub"
    assert made["title"] == "A Long Walk Through Stockholm"
    validate_epub(made["path"])


def test_build_falls_back_to_full_page(monkeypatch, tmp_path, no_progress):
    thin = b"<html><head><title>App</title></head><body><div id=root>Loading</div></body></html>"
    monkeypatch.setattr(web, "fetch", lambda url, **kw: (thin, "text/html", url))
    printed = []
    monkeypatch.setattr(web, "print_page", lambda url, target, **kw: printed.append(url) or fake_pdf(target))
    made = web.build("https://app.example.com/", "epub", tmp_path)
    assert printed == ["https://app.example.com/"]
    assert made["mode"] == "full-page" and made["format"] == "pdf" and made["title"] == "App"


def test_blocked_fetch_tries_the_browser(monkeypatch, tmp_path, no_progress):
    def blocked(url, **kw):
        raise web.WebError("example.com answered 403 Forbidden")
    monkeypatch.setattr(web, "fetch", blocked)
    monkeypatch.setattr(web, "print_page", lambda url, target, **kw: fake_pdf(target))
    assert web.build("https://example.com/x", "pdf", tmp_path)["mode"] == "full-page"

    def no_browser(url, target, **kw):
        raise web.WebError("full-page mode needs Chromium or Google Chrome installed")
    monkeypatch.setattr(web, "print_page", no_browser)
    with pytest.raises(web.WebError, match="403"):  # the original reason is the useful one
        web.build("https://example.com/x", "pdf", tmp_path)


def test_pdf_links_pass_through(monkeypatch, tmp_path, no_progress):
    pdf = fake_pdf(tmp_path / "src.pdf").read_bytes()
    monkeypatch.setattr(web, "fetch", lambda url, **kw: (pdf, "application/pdf", url))
    made = web.build("https://example.com/papers/On%20Walking.pdf", "epub", tmp_path)
    assert made["format"] == "pdf" and made["mode"] == "file" and made["title"] == "On Walking"
    assert made["path"].read_bytes() == pdf


def test_full_page_flag_skips_extraction(monkeypatch, tmp_path, no_progress):
    monkeypatch.setattr(web, "fetch", lambda *a, **k: pytest.fail("should not fetch"))
    monkeypatch.setattr(web, "print_page", lambda url, target, **kw: fake_pdf(target))
    assert web.build("https://example.com", "epub", tmp_path, full_page=True)["mode"] == "full-page"


@pytest.mark.parametrize("clip,expected", [
    ("https://example.com/a", "https://example.com/a"),
    ("look at this: https://example.com/a?b=1). thanks", "https://example.com/a?b=1"),
    ("example.com/page", "example.com/page"),
])
def test_clipboard_url(monkeypatch, clip, expected):
    import subprocess
    monkeypatch.setattr(web.subprocess, "run",
                        lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout=clip, stderr=""))
    assert web.clipboard_url() == expected


def test_clipboard_without_url(monkeypatch):
    import subprocess
    monkeypatch.setattr(web.subprocess, "run",
                        lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout="just some words", stderr=""))
    with pytest.raises(web.WebError, match="doesn't hold a web address"):
        web.clipboard_url()


def web_args(**kw) -> argparse.Namespace:
    base = dict(url=URL, format=None, full_page=False, folder=None, name=None, out=None, no_upload=False,
                notify=False)
    base.update(kw)
    return argparse.Namespace(**base)


def test_cmd_web_uploads_to_settings_folder(monkeypatch, fake_cloud, no_progress, notifications):
    monkeypatch.setattr(web, "fetch", lambda url, **kw: (ARTICLE_HTML, "text/html", url) if url == URL
                        else (png(), "image/png", url))
    fake_cloud.add("Web/A Long Walk Through Stockholm")  # same name already there
    result = web.cmd_web(web_args())
    assert result["ok"] and result["folder"] == "Web" and result["format"] == "epub"
    assert result["name"] == "A Long Walk Through Stockholm (2)"
    upload = fake_cloud.uploads[-1]
    assert upload["folder"] == "Web" and upload["bytes"][:2] == b"PK"
    assert notifications == []  # only with --notify or clipboard

    result = web.cmd_web(web_args(format="pdf", folder="Omarchy Plugin Test", name="Custom"))
    assert result["name"] == "Custom" and fake_cloud.uploads[-1]["bytes"][:5] == b"%PDF-"
    assert fake_cloud.uploads[-1]["folder"] == "Omarchy Plugin Test"


def test_cmd_web_no_upload_keeps_file(monkeypatch, tmp_path, no_progress):
    monkeypatch.setattr(web, "fetch", lambda url, **kw: (ARTICLE_HTML, "text/html", url) if url == URL
                        else (png(), "image/png", url))
    out = tmp_path / "keep.pdf"
    result = web.cmd_web(web_args(no_upload=True, out=str(out), format="epub"))
    assert result["ok"] and result["file"] == str(out.with_suffix(".epub"))
    validate_epub(Path(result["file"]))


def test_clipboard_errors_notify(monkeypatch, notifications, no_progress):
    monkeypatch.setattr(web, "clipboard_url", lambda: (_ for _ in ()).throw(web.WebError("the clipboard doesn't hold a web address")))
    result = web.cmd_web(web_args(url="clipboard"))
    assert result == {"ok": False, "error": "the clipboard doesn't hold a web address", "code": "web"}
    assert notifications and notifications[0][0] == "Couldn't send the page to reMarkable"
