"""Send a web page to the tablet: the article as an EPUB or PDF, or the whole
page printed by headless Chromium when there's no article to pull out."""

from __future__ import annotations

import argparse
import gzip
import html
import re
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile
import zlib
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from . import config

USER_AGENT = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/141.0.0.0 Safari/537.36")
PAGE_TIMEOUT = 25
IMAGE_TIMEOUT = 15
MAX_PAGE_BYTES = 20 * 1024 * 1024
MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_IMAGES = 40
MAX_IMAGES_TOTAL = 30 * 1024 * 1024
IMAGE_MAX_PX = 1400  # wider than this only costs upload time on a 1620 px screen
MIN_IMAGE_PX = 40  # smaller ones are icons and tracking pixels
MIN_ARTICLE_CHARS = 400  # below this the extraction missed the article
BROWSERS = ("chromium", "google-chrome-stable", "google-chrome", "chromium-browser", "brave")
XHTML_NS = "http://www.w3.org/1999/xhtml"

EPUB_CSS = """
body { font-family: serif; line-height: 1.45; margin: 0 4%; }
h1 { font-size: 1.6em; line-height: 1.2; margin: 0.4em 0 0.3em 0; }
h2 { font-size: 1.3em; margin: 1.2em 0 0.4em 0; }
h3 { font-size: 1.1em; margin: 1em 0 0.3em 0; }
h4 { font-size: 1em; margin: 1em 0 0.3em 0; }
p { margin: 0 0 0.7em 0; text-align: justify; }
.meta { color: #555; font-size: 0.85em; margin: 0 0 1.4em 0; text-align: left; }
.source { color: #555; font-size: 0.75em; word-wrap: break-word; }
blockquote { margin: 0.6em 1.4em; font-style: italic; }
pre { font-family: monospace; font-size: 0.8em; white-space: pre-wrap; background: #eee; padding: 0.5em; }
code { font-family: monospace; font-size: 0.9em; }
img { max-width: 100%; height: auto; }
div.figure { text-align: center; margin: 0.8em 0; }
table { border-collapse: collapse; margin: 0.8em 0; font-size: 0.85em; }
td, th { border: 1px solid #999; padding: 0.2em 0.4em; vertical-align: top; }
th { background: #eee; }
"""

PDF_CSS_EXTRA = """
code { font-size: 0.85em; }
pre { font-size: 8.5pt; }
blockquote p { text-align: left; }
.source { color: #555; font-size: 8pt; }
table { margin: 6pt 0; font-size: 9.5pt; }
td, th { border: 0.5pt solid #999; padding: 2pt 3pt; }
th { background: #eee; font-weight: bold; }
div.figure { text-align: center; margin: 8pt 0; }
pre { white-space: pre-wrap; }
"""


class WebError(RuntimeError):
    pass


@dataclass
class Article:
    url: str
    title: str
    author: str = ""
    date: str = ""
    site: str = ""
    language: str = "en"
    tree: Any = None  # xml.etree element: the article body as XHTML
    text_chars: int = 0
    image_srcs: list[str] = field(default_factory=list)  # <img src="@imgN"> -> image_srcs[N]
    images: dict[str, tuple[bytes, str]] = field(default_factory=dict)  # "images/img001.jpg" -> (data, type)
    headings: list[tuple[str, int, str]] = field(default_factory=list)  # (anchor id, level, text)

    @property
    def body(self) -> str:
        """The body as a well-formed XHTML fragment."""
        import xml.etree.ElementTree as ET
        return ET.tostring(self.tree, encoding="unicode", method="xml") if self.tree is not None else ""


# ---------------------------------------------------------------- fetching


def normalize_url(raw: str) -> str:
    url = raw.strip().strip("<>\"'")
    bad = WebError(f"{raw.strip()[:80]} isn't a web address")
    scheme = re.match(r"^([a-zA-Z][a-zA-Z0-9+.-]*):(?!\d)", url)  # "host:8080" is not a scheme
    if not scheme:
        url = "https://" + url
    parts = urllib.parse.urlsplit(url)
    # Only the web: a URL from the clipboard must not read local files.
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise bad
    try:
        parts.port
    except ValueError:
        raise bad from None
    return url


def fetch(url: str, *, limit: int = MAX_PAGE_BYTES, timeout: float = PAGE_TIMEOUT,
          accept: str = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8") -> tuple[bytes, str, str]:
    """GET a URL like a browser would. Returns (body, media type, final URL)."""
    req = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT, "Accept": accept,
        "Accept-Language": "en-US,en;q=0.9", "Accept-Encoding": "gzip",
    })
    host = urllib.parse.urlsplit(url).netloc
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = r.read(limit + 1)
            if len(data) > limit:
                raise WebError(f"{host} sent more than {limit // (1024 * 1024)} MB")
            encoding = (r.headers.get("Content-Encoding") or "").lower()
            if encoding == "gzip":
                data = gzip.decompress(data)
            elif encoding == "deflate":
                data = zlib.decompress(data)
            return data, r.headers.get_content_type(), r.geturl()
    except urllib.error.HTTPError as e:
        raise WebError(f"{host} answered {e.code} {e.reason}") from e
    except urllib.error.URLError as e:
        reason = getattr(e, "reason", e)
        if isinstance(reason, TimeoutError):
            raise WebError(f"{host} took too long to answer") from e
        raise WebError(f"couldn't reach {host} ({reason})") from e
    except TimeoutError as e:
        raise WebError(f"{host} took too long to answer") from e


def page_language(raw: bytes) -> str:
    m = re.search(rb"<html[^>]*\blang=[\"']?([a-zA-Z]{2,3}(?:-[a-zA-Z0-9]{2,8})?)", raw[:20000])
    return m.group(1).decode() if m else "en"


def page_title(raw: bytes) -> str:
    m = re.search(rb"<title[^>]*>(.*?)</title>", raw[:200000], re.S | re.I)
    if not m:
        return ""
    return " ".join(html.unescape(m.group(1).decode("utf-8", "replace")).split())


# -------------------------------------------------------------- extraction


_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f￾￿]")
_CITATION = re.compile(r"^\s*\[(?:\d+|[a-z]|citation needed|note \d+)\]\s*$", re.I)
_JUNK_TAIL = re.compile(r"^\s*\[edit\]\s*", re.I)
INLINE_REND = {"#b": "b", "#i": "i", "#u": "u", "#t": "code", "#sup": "sup", "#sub": "sub"}


def _clean(text: str | None) -> str:
    return _CTRL.sub("", text or "")


TITLE_SEPS = (" - ", " | ", " – ", " — ", " :: ", " · ")


def clean_title(title: str, site: str, first_heading: str, host: str = "") -> str:
    """The article's own title, without the " - Site name" pages tack on."""
    title = " ".join((title or "").split())
    heading = " ".join((first_heading or "").split())
    if heading:
        if not title or title == heading:
            return heading
        if any(title.startswith(heading + sep) or title.endswith(sep + heading) for sep in TITLE_SEPS):
            return heading
    host = host.lower().removeprefix("www.")
    for sep in TITLE_SEPS:
        if sep in title:
            head, _, tail = title.rpartition(sep)
            t = tail.lower().strip()
            if head.strip() and t and ((site and (t in site.lower() or site.lower() in t))
                                       or t.replace(" ", "") in host):
                return head.strip()
    return title


def _to_xhtml(body, page_url: str, image_srcs: list[str], headings: list[tuple[str, int, str]],
              title: str):
    """Turn trafilatura's internal XML tree into an XHTML element tree."""
    import xml.etree.ElementTree as ET

    out = ET.Element("div", {"class": "article"})
    skipped_title = False

    def add_text(parent, text: str) -> None:
        text = _clean(text)
        if not text:
            return
        if len(parent):
            last = parent[-1]
            last.tail = (last.tail or "") + text
        else:
            parent.text = (parent.text or "") + text

    def image(src: str | None):
        if not src:
            return None
        src = urllib.parse.urljoin(page_url, src.strip())
        if not src.startswith(("http://", "https://", "data:image/")):
            return None
        if src not in image_srcs:
            if len(image_srcs) >= MAX_IMAGES:
                return None
            image_srcs.append(src)
        # Placeholder, swapped for the local file name once downloaded.
        return ET.Element("img", {"src": f"@img{image_srcs.index(src)}", "alt": ""})

    def inline(src, parent) -> None:
        """Copy src's children (inline content) into parent."""
        add_text(parent, src.text)
        for child in src:
            convert_inline(child, parent)
            add_text(parent, child.tail)

    def convert_inline(el, parent) -> None:
        tag = el.tag if isinstance(el.tag, str) else ""
        if tag == "hi":
            rend = el.get("rend", "")
            if rend == "#sup" and _CITATION.match("".join(el.itertext())):
                return  # "[12]" reference markers
            new = ET.SubElement(parent, INLINE_REND.get(rend, "span"))
            inline(el, new)
        elif tag == "code":
            new = ET.SubElement(parent, "code")
            add_text(new, "".join(el.itertext()))
        elif tag == "del":
            new = ET.SubElement(parent, "del")
            inline(el, new)
        elif tag == "lb":
            ET.SubElement(parent, "br")
        elif tag == "graphic":
            img = image(el.get("src"))
            if img is not None:
                parent.append(img)
        elif tag in ("p", "head", "quote", "list", "table"):
            # Blocks nested inside inline content: keep the words, drop the box.
            inline(el, parent)
            add_text(parent, " ")
        else:  # ref, span and anything unknown: keep the text
            inline(el, parent)

    def convert_block(el, parent) -> None:
        nonlocal skipped_title
        tag = el.tag if isinstance(el.tag, str) else ""
        if tag == "head":
            text = " ".join("".join(el.itertext()).split())
            if not text:
                return
            rend = el.get("rend", "h2")
            level = int(rend[1]) if re.fullmatch(r"h[1-6]", rend) else 2
            if not skipped_title and level == 1 and title and (text in title or title in text):
                skipped_title = True  # we print our own title
                return
            level = min(max(level, 2), 4)
            anchor = f"s{len(headings) + 1}"
            h = ET.SubElement(parent, f"h{level}", {"id": anchor})
            inline(el, h)
            headings.append((anchor, level, text))
        elif tag == "p":
            p = ET.SubElement(parent, "p")
            inline(el, p)
            if not len(p) and not (p.text or "").strip():
                parent.remove(p)
        elif tag == "quote":
            q = ET.SubElement(parent, "blockquote")
            if any(isinstance(c.tag, str) and c.tag in ("p", "list", "quote", "head", "table") for c in el):
                add_text_block(el, q)
            else:
                p = ET.SubElement(q, "p")
                inline(el, p)
        elif tag == "list":
            rend = el.get("rend", "ul")
            lst = ET.SubElement(parent, "ol" if rend == "ol" else "ul")
            for item in el:
                if not isinstance(item.tag, str):
                    continue
                li = ET.SubElement(lst, "li")
                if any(isinstance(c.tag, str) and c.tag in ("list", "p", "quote", "table") for c in item):
                    add_text(li, item.text)
                    for c in item:
                        if isinstance(c.tag, str) and c.tag in ("list", "p", "quote", "table"):
                            convert_block(c, li)
                        else:
                            convert_inline(c, li)
                        add_text(li, c.tail)
                else:
                    inline(item, li)
            if not len(lst):
                parent.remove(lst)
        elif tag == "table":
            table = ET.SubElement(parent, "table")
            for row in el.iter("row"):
                tr = ET.SubElement(table, "tr")
                for cell in row:
                    if not isinstance(cell.tag, str) or cell.tag != "cell":
                        continue
                    td = ET.SubElement(tr, "th" if cell.get("role") == "head" else "td")
                    inline(cell, td)
                if not len(tr):
                    table.remove(tr)
            if not len(table):
                parent.remove(table)
        elif tag == "code":
            pre = ET.SubElement(parent, "pre")
            add_text(pre, "".join(el.itertext()).strip("\n"))
        elif tag == "graphic":
            img = image(el.get("src"))
            if img is not None:
                fig = ET.SubElement(parent, "div", {"class": "figure"})
                fig.append(img)
        elif tag in ("div", "body", "section", "main", "article"):
            add_text_block(el, parent)
        elif tag:
            p = ET.SubElement(parent, "p")
            inline(el, p)
            if not len(p) and not (p.text or "").strip():
                parent.remove(p)

    def add_text_block(el, parent) -> None:
        loose = _clean(el.text).strip()
        if loose and not _JUNK_TAIL.fullmatch(loose):
            ET.SubElement(parent, "p").text = loose
        for child in el:
            convert_block(child, parent)
            tail = _JUNK_TAIL.sub("", _clean(child.tail)).strip()
            if tail:
                ET.SubElement(parent, "p").text = tail

    add_text_block(body, out)
    return out


def extract(raw: bytes, url: str) -> Article | None:
    """Pull the article out of a page, or None when there isn't one."""
    import trafilatura

    doc = trafilatura.bare_extraction(
        raw, url=url, include_images=True, include_tables=True, include_formatting=True,
        include_links=False, include_comments=False, with_metadata=True,
    )
    if doc is None or doc.body is None:
        return None
    body = doc.body
    host = urllib.parse.urlsplit(url).netloc
    first_heading = next(("".join(h.itertext()) for h in body.iter("head") if h.get("rend") == "h1"), "")
    site = (doc.sitename or doc.hostname or host).strip()
    title = clean_title(doc.title or page_title(raw), site, first_heading, host) or host
    image_srcs: list[str] = []
    headings: list[tuple[str, int, str]] = []
    tree = _to_xhtml(body, url, image_srcs, headings, title)
    return Article(
        url=url, title=title, author=(doc.author or "").strip(), date=(doc.date or "").strip(),
        site=site, language=page_language(raw), tree=tree, image_srcs=image_srcs, headings=headings,
        text_chars=len(" ".join("".join(tree.itertext()).split())),
    )


# ------------------------------------------------------------------ images


def _decode_data_uri(uri: str) -> bytes | None:
    import base64
    head, _, payload = uri.partition(",")
    try:
        if ";base64" in head:
            return base64.b64decode(payload)
        return urllib.parse.unquote_to_bytes(payload)
    except ValueError:
        return None


def _sniff_modern(data: bytes) -> str | None:
    """WebP/AVIF, which MuPDF can't read."""
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    if data[4:12] in (b"ftypavif", b"ftypavis"):
        return "avif"
    return None


def _convert_modern(data: bytes) -> bytes | None:
    """WebP/AVIF to PNG with ImageMagick, when it's installed."""
    kind = _sniff_modern(data)
    magick = shutil.which("magick")
    if not kind or not magick:
        return None
    try:
        # The explicit "webp:"/"avif:" coder keeps ImageMagick from guessing
        # some other (scriptable) format from the bytes.
        proc = subprocess.run([magick, f"{kind}:-[0]", "png:-"], input=data, capture_output=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return proc.stdout if proc.returncode == 0 and proc.stdout else None


def prepare_image(data: bytes) -> tuple[bytes, str] | None:
    """Decode, shrink and re-encode one image; None if it isn't worth keeping."""
    import pymupdf

    pix = None
    if _sniff_modern(data):
        data = _convert_modern(data) or b""
    try:
        pix = pymupdf.Pixmap(data)
    except Exception:
        try:  # SVG and other formats MuPDF opens as documents
            with pymupdf.open(stream=data) as d:
                if d.page_count:
                    page = d[0]
                    zoom = min(4.0, IMAGE_MAX_PX / max(page.rect.width, 1))
                    pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False)
        except Exception:
            return None
    if pix is None or pix.width < MIN_IMAGE_PX or pix.height < MIN_IMAGE_PX:
        return None
    if pix.colorspace is None or pix.colorspace.n not in (1, 3):
        pix = pymupdf.Pixmap(pymupdf.csRGB, pix)
    if pix.width > IMAGE_MAX_PX:
        factor = pix.width / IMAGE_MAX_PX
        pix = pymupdf.Pixmap(pix, int(pix.width / factor), int(pix.height / factor), None)
    if pix.alpha:
        return pix.tobytes("png"), "image/png"
    return pix.tobytes("jpg", jpg_quality=82), "image/jpeg"


def download_images(srcs: list[str], fetcher: Callable[[str], bytes | None] | None = None) -> dict[int, tuple[bytes, str]]:
    """Fetch and shrink images in parallel, within the size budgets."""
    def default_fetch(src: str) -> bytes | None:
        if src.startswith("data:"):
            return _decode_data_uri(src)
        try:
            # No webp/avif in Accept: MuPDF can't decode them, and image CDNs
            # (Wikimedia included) serve them to anyone who says they can.
            data, _, _ = fetch(src, limit=MAX_IMAGE_BYTES, timeout=IMAGE_TIMEOUT,
                               accept="image/png,image/jpeg,image/gif,image/svg+xml;q=0.9,*/*;q=0.5")
            return data
        except WebError:
            return None

    get = fetcher or default_fetch

    def one(src: str) -> bytes | None:
        data = get(src)
        if data is None and not src.startswith("data:"):
            time.sleep(1.5)  # image hosts sometimes turn away a burst (429); one polite retry
            data = get(src)
        return data

    results: dict[int, tuple[bytes, str]] = {}
    total = 0
    with ThreadPoolExecutor(max_workers=6) as pool:
        downloaded = list(pool.map(one, srcs))
    # Decoding stays on this thread: PyMuPDF isn't thread-safe.
    for i, data in enumerate(downloaded):
        img = prepare_image(data) if data else None
        if img is None or total + len(img[0]) > MAX_IMAGES_TOTAL:
            continue
        total += len(img[0])
        results[i] = img
    return results


def attach_images(article: Article, fetcher: Callable[[str], bytes | None] | None = None) -> None:
    """Download the article's images and point the <img> tags at local files;
    images that failed or were too small are dropped from the text."""
    got = download_images(article.image_srcs, fetcher) if article.image_srcs else {}
    names: dict[int, str] = {}
    for i in sorted(got):
        data, media = got[i]
        name = f"images/img{len(names) + 1:03d}{'.png' if media == 'image/png' else '.jpg'}"
        names[i] = name
        article.images[name] = (data, media)
    parents = {child: parent for parent in article.tree.iter() for child in parent}
    for img in list(article.tree.iter("img")):
        m = re.fullmatch(r"@img(\d+)", img.get("src", ""))
        if not m:
            continue
        i = int(m.group(1))
        if i in names:
            img.set("src", names[i])
            continue
        parent = parents[img]
        _remove_keep_tail(parent, img)
        if parent.tag == "div" and parent.get("class") == "figure" and not len(parent) \
                and not (parent.text or "").strip():
            _remove_keep_tail(parents[parent], parent)


def _remove_keep_tail(parent, el) -> None:
    tail = el.tail or ""
    index = list(parent).index(el)
    parent.remove(el)
    if tail:
        if index > 0:
            prev = parent[index - 1]
            prev.tail = (prev.tail or "") + tail
        else:
            parent.text = (parent.text or "") + tail


# ------------------------------------------------------------------ output


def _esc(s: str) -> str:
    return html.escape(_clean(s), quote=True)


def byline(article: Article) -> str:
    site = "" if article.site.lower() == article.author.lower() else article.site
    return " · ".join(p for p in (article.author, pretty_date(article.date), site) if p)


def pretty_date(value: str) -> str:
    try:
        d = datetime.strptime(value[:10], "%Y-%m-%d")
        return f"{d.day} {d.strftime('%B %Y')}"
    except ValueError:
        return value


def short_url(url: str, limit: int = 70) -> str:
    parts = urllib.parse.urlsplit(url)
    shown = parts.netloc + parts.path
    return shown if len(shown) <= limit else shown[: limit - 1] + "…"


def header_html(article: Article) -> str:
    meta = byline(article)
    return (f"<h1>{_esc(article.title)}</h1>"
            + (f'<p class="meta">{_esc(meta)}<br/><span class="source">{_esc(short_url(article.url))}</span></p>'
               if meta else f'<p class="meta"><span class="source">{_esc(short_url(article.url))}</span></p>'))


def build_epub(article: Article, target: Path) -> Path:
    """EPUB 3 (with an EPUB 2 NCX for older readers), one chapter."""
    lang = _esc(article.language or "en")
    book_id = f"urn:uuid:{uuid.uuid5(uuid.NAMESPACE_URL, article.url)}"
    modified = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    title = _esc(article.title)

    chapter = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE html>
<html xmlns="{XHTML_NS}" xmlns:epub="http://www.idpf.org/2007/ops" lang="{lang}" xml:lang="{lang}">
<head>
<meta charset="UTF-8"/>
<title>{title}</title>
<link rel="stylesheet" type="text/css" href="style.css"/>
</head>
<body>
{header_html(article)}
{article.body}
</body>
</html>
"""

    # The contents list shows the article's top heading level (h2 on most pages).
    top = min((level for _, level, _ in article.headings), default=2)
    sections = [(a, t) for a, level, t in article.headings if level == top]
    sub = "".join(f'<li><a href="article.xhtml#{a}">{_esc(t)}</a></li>' for a, t in sections)
    nav = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE html>
<html xmlns="{XHTML_NS}" xmlns:epub="http://www.idpf.org/2007/ops" lang="{lang}" xml:lang="{lang}">
<head><meta charset="UTF-8"/><title>Contents</title></head>
<body>
<nav epub:type="toc" id="toc"><h1>Contents</h1>
<ol><li><a href="article.xhtml">{title}</a>{f'<ol>{sub}</ol>' if sub else ''}</li></ol>
</nav>
</body>
</html>
"""

    points = [(None, article.title)] + sections
    nav_points = "".join(
        f'<navPoint id="p{i}" playOrder="{i}"><navLabel><text>{_esc(t)}</text></navLabel>'
        f'<content src="article.xhtml{"#" + a if a else ""}"/></navPoint>'
        for i, (a, t) in enumerate(points, 1)
    )
    ncx = f"""<?xml version="1.0" encoding="UTF-8"?>
<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">
<head><meta name="dtb:uid" content="{book_id}"/><meta name="dtb:depth" content="1"/>
<meta name="dtb:totalPageCount" content="0"/><meta name="dtb:maxPageNumber" content="0"/></head>
<docTitle><text>{title}</text></docTitle>
<navMap>{nav_points}</navMap>
</ncx>
"""

    meta = [f'<dc:identifier id="bookid">{book_id}</dc:identifier>', f"<dc:title>{title}</dc:title>",
            f"<dc:language>{lang}</dc:language>", f'<meta property="dcterms:modified">{modified}</meta>',
            f"<dc:source>{_esc(article.url)}</dc:source>"]
    if article.author:
        meta.append(f"<dc:creator>{_esc(article.author)}</dc:creator>")
    if re.fullmatch(r"\d{4}(-\d{2}(-\d{2})?)?", article.date[:10]):
        meta.append(f"<dc:date>{article.date[:10]}</dc:date>")
    if article.site:
        meta.append(f"<dc:publisher>{_esc(article.site)}</dc:publisher>")
    items = ['<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>',
             '<item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>',
             '<item id="css" href="style.css" media-type="text/css"/>',
             '<item id="article" href="article.xhtml" media-type="application/xhtml+xml"/>']
    items += [f'<item id="img{i}" href="{name}" media-type="{media}"/>'
              for i, (name, (_, media)) in enumerate(article.images.items(), 1)]
    opf = f"""<?xml version="1.0" encoding="UTF-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="bookid" xml:lang="{lang}">
<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
{chr(10).join(meta)}
</metadata>
<manifest>
{chr(10).join(items)}
</manifest>
<spine toc="ncx">
<itemref idref="article"/>
</spine>
</package>
"""

    container = """<?xml version="1.0" encoding="UTF-8"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
<rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles>
</container>
"""

    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".part")
    with zipfile.ZipFile(tmp, "w") as z:
        # OCF: "mimetype" first, stored, so readers can sniff the file type.
        z.writestr(zipfile.ZipInfo("mimetype"), "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        deflate = zipfile.ZIP_DEFLATED
        z.writestr("META-INF/container.xml", container, compress_type=deflate)
        z.writestr("OEBPS/content.opf", opf, compress_type=deflate)
        z.writestr("OEBPS/nav.xhtml", nav, compress_type=deflate)
        z.writestr("OEBPS/toc.ncx", ncx, compress_type=deflate)
        z.writestr("OEBPS/style.css", EPUB_CSS, compress_type=deflate)
        z.writestr("OEBPS/article.xhtml", chapter, compress_type=deflate)
        for name, (data, _) in article.images.items():
            z.writestr(f"OEBPS/{name}", data, compress_type=zipfile.ZIP_STORED)
    tmp.replace(target)
    return target


def build_pdf(article: Article, target: Path) -> Path:
    """Tablet-sized pages through send.html_to_pdf (PyMuPDF Story)."""
    from .send import PAGE_W, READING_CSS, html_to_pdf

    import pymupdf

    content_w = PAGE_W - 80  # html_to_pdf's 40 pt side margins
    with tempfile.TemporaryDirectory(dir=config.TMP_DIR) as tmp:
        widths = {}
        for name, (data, _) in article.images.items():
            path = Path(tmp) / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            # Story lays images out at their pixel size; give each an explicit
            # width so big ones fit the column and small ones aren't blown up.
            widths[name] = min(content_w, pymupdf.Pixmap(data).width * 72 / 110)
        for img in article.tree.iter("img"):
            if img.get("src") in widths:
                img.set("width", f"{widths[img.get('src')]:.0f}")
        body = article.body
        target.parent.mkdir(parents=True, exist_ok=True)
        html_to_pdf(header_html(article) + body, target, css=READING_CSS + PDF_CSS_EXTRA,
                    title=article.title, archive=tmp)
    return target


def find_browser() -> str | None:
    for name in BROWSERS:
        path = shutil.which(name)
        if path:
            return path
    return None


def print_page(url: str, target: Path, *, timeout: int = 120) -> Path:
    """The whole page as the browser prints it, in a throwaway profile."""
    browser = find_browser()
    if not browser:
        raise WebError("full-page mode needs Chromium or Google Chrome installed")
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=config.TMP_DIR, prefix="chromium-") as profile:
        cmd = [browser, "--headless", "--disable-gpu", "--no-pdf-header-footer", "--hide-scrollbars",
               "--no-first-run", "--no-default-browser-check", "--disable-extensions", "--mute-audio",
               "--disable-sync", "--run-all-compositor-stages-before-draw", "--virtual-time-budget=15000",
               f"--user-data-dir={profile}", f"--user-agent={USER_AGENT}", f"--print-to-pdf={target}", url]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                                  stdin=subprocess.DEVNULL)
        except subprocess.TimeoutExpired as e:
            raise WebError("the browser took too long to print the page") from e
    if not target.exists() or target.stat().st_size < 500:
        detail = (proc.stderr or "").strip().splitlines()[-1:] if proc.stderr else []
        raise WebError("the browser couldn't print the page" + (f" ({detail[0][:120]})" if detail else ""))
    return target


# ---------------------------------------------------------------- pipeline


def build(url: str, fmt: str, workdir: Path, *, full_page: bool = False) -> dict[str, Any]:
    """Make the file to send. Returns {path, title, format, mode}."""
    from .cli import progress

    url = normalize_url(url)
    host = urllib.parse.urlsplit(url).netloc
    raw = b""
    fetch_error: WebError | None = None
    if not full_page:
        progress(f"fetching {host}")
        try:
            raw, media, url = fetch(url)
        except WebError as e:
            # Some sites turn away scripts but let a real browser in.
            fetch_error = e
        else:
            if media == "application/pdf" or raw[:5] == b"%PDF-":
                target = workdir / "page.pdf"
                target.write_bytes(raw)
                title = Path(urllib.parse.unquote(urllib.parse.urlsplit(url).path)).stem or host
                return {"path": target, "title": title, "format": "pdf", "mode": "file"}
            if media == "application/epub+zip":
                target = workdir / "page.epub"
                target.write_bytes(raw)
                title = Path(urllib.parse.unquote(urllib.parse.urlsplit(url).path)).stem or host
                return {"path": target, "title": title, "format": "epub", "mode": "file"}
            if media not in ("text/html", "application/xhtml+xml", "text/plain", "application/xml", "text/xml"):
                raise WebError(f"{host} sent a {media} file, not a web page")
            progress("finding the article")
            article = extract(raw, url)
            if article is not None and article.text_chars >= MIN_ARTICLE_CHARS:
                n = len(article.image_srcs)
                if n:
                    progress(f"downloading {n} image{'s' if n != 1 else ''}")
                attach_images(article)
                progress(f"making the {fmt.upper()}")
                target = workdir / f"page.{fmt}"
                (build_epub if fmt == "epub" else build_pdf)(article, target)
                return {"path": target, "title": article.title, "format": fmt, "mode": "article",
                        "images": len(article.images), "characters": article.text_chars}
    progress("printing the whole page")
    try:
        target = print_page(url, workdir / "page.pdf")
    except WebError:
        if fetch_error:
            raise fetch_error
        raise
    title = page_title(raw) or pdf_title(target) or host
    return {"path": target, "title": title, "format": "pdf", "mode": "full-page"}


def pdf_title(path: Path) -> str:
    import pymupdf
    try:
        with pymupdf.open(path) as d:
            return (d.metadata or {}).get("title", "").strip()
    except Exception:
        return ""


def unique_name(name: str, folder: str) -> str:
    """rmapi refuses a second document with the same name in a folder."""
    from . import cloud
    lib = cloud.library()
    base = "/" + folder.strip("/") if folder.strip("/") else ""
    candidate, n = name, 2
    while lib.find_path(f"{base}/{cloud._safe_filename(candidate)}") is not None:
        candidate = f"{name} ({n})"
        n += 1
    return candidate


def send_url(url: str, *, fmt: str | None = None, full_page: bool = False, folder: str | None = None,
             name: str | None = None, out: Path | None = None, upload: bool = True) -> dict[str, Any]:
    from . import library, send

    settings = config.load_settings()
    fmt = (fmt or settings.get("webPageFormat") or "epub").lower()
    if fmt not in ("epub", "pdf"):
        fmt = "epub"
    folder = settings.get("webFolder", "") if folder is None else folder
    with tempfile.TemporaryDirectory(dir=config.TMP_DIR) as tmp:
        made = build(url, fmt, Path(tmp), full_page=full_page)
        title = name or made["title"]
        doc_name = library.safe_name(title)
        result: dict[str, Any] = {"ok": True, "url": normalize_url(url), "title": made["title"],
                                  "format": made["format"], "mode": made["mode"]}
        for k in ("images", "characters"):
            if k in made:
                result[k] = made[k]
        keep = out
        if keep is None and not upload:
            keep = config.CACHE_DIR / "web" / f"{doc_name}.{made['format']}"
        if keep is not None:
            if keep.suffix.lower() != f".{made['format']}" and keep.suffix.lower() in (".pdf", ".epub"):
                keep = keep.with_suffix(f".{made['format']}")
            keep.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(made["path"], keep)
            result["file"] = str(keep)
        if upload:
            from .cli import progress
            progress("uploading")
            doc_name = unique_name(doc_name, folder)
            sent = send.send_file(made["path"], folder, doc_name)
            library.fetch_library()
            result.update(id=sent["id"], name=sent["name"], folder=folder)
        else:
            result["name"] = doc_name
    return result


def clipboard_url() -> str:
    try:
        proc = subprocess.run(["wl-paste", "--no-newline", "--type", "text/plain"], capture_output=True,
                              text=True, timeout=5)
    except FileNotFoundError as e:
        raise WebError("wl-paste isn't installed (wl-clipboard)") from e
    except subprocess.TimeoutExpired as e:
        raise WebError("couldn't read the clipboard") from e
    text = proc.stdout.strip() if proc.returncode == 0 else ""
    m = re.search(r"https?://[^\s<>\"']+", text)
    if m:
        return m.group(0).rstrip(".,;:!?)]}")
    # A bare "example.com/page" copied from an address bar.
    if re.fullmatch(r"[\w.-]+\.[a-z]{2,}(/\S*)?", text, re.I):
        return text
    raise WebError("the clipboard doesn't hold a web address")


def cmd_web(args: argparse.Namespace) -> dict:
    from .cli import notify

    from_clipboard = args.url == "clipboard"
    wants_notify = args.notify or from_clipboard
    try:
        url = clipboard_url() if from_clipboard else args.url
        result = send_url(url, fmt=args.format, full_page=args.full_page, folder=args.folder,
                          name=args.name, out=Path(args.out).expanduser() if args.out else None,
                          upload=not args.no_upload)
    except Exception as e:
        if wants_notify:
            notify("Couldn't send the page to reMarkable", str(e), urgency="critical")
        if isinstance(e, (WebError, OSError, ValueError)):
            return {"ok": False, "error": str(e), "code": "web"}
        raise  # cloud errors: cli.main reports them with their own codes
    if wants_notify and not args.no_upload:
        notify("Sent to reMarkable", result.get("name", ""))
    return result


def register(sub) -> None:
    p = sub.add_parser("web", help="send a web page to the tablet (article as EPUB/PDF, or the whole page)",
                       description='Send a web page to the tablet. Use "clipboard" as the URL to send '
                                   "the address on the Wayland clipboard.")
    p.add_argument("url", help='page address, or "clipboard"')
    p.add_argument("--format", choices=["epub", "pdf"], help="default from settings (webPageFormat)")
    p.add_argument("--full-page", action="store_true", help="print the whole page with Chromium instead")
    p.add_argument("--folder", help='cloud folder ("" = top level); default from settings (webFolder)')
    p.add_argument("--name", help="document name on the tablet (default: the article title)")
    p.add_argument("--out", help="also save the file here")
    p.add_argument("--no-upload", action="store_true", help="only build the file")
    p.add_argument("--notify", action="store_true", help="desktop notification when done (always on for clipboard)")
    p.set_defaults(func=cmd_web)
