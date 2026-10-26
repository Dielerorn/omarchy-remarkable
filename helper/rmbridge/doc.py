"""A downloaded document (.rmdoc zip) as pages you can render or read."""

from __future__ import annotations

import io
import json
import zipfile
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Page:
    index: int  # position in the document as the tablet shows it
    id: str
    template: str = ""
    pdf_page: int | None = None  # page of the backing PDF, if any
    rm: bytes | None = None  # stroke data; None for an untouched page

    @property
    def annotated(self) -> bool:
        return bool(self.rm)


@dataclass
class Document:
    id: str
    name: str
    file_type: str  # notebook | pdf | epub
    pages: list[Page] = field(default_factory=list)
    pdf: bytes | None = None
    epub: bytes | None = None
    content: dict = field(default_factory=dict)
    metadata: dict = field(default_factory=dict)

    @property
    def tags(self) -> list[str]:
        return [t.get("name", "") for t in (self.content.get("tags") or []) if isinstance(t, dict)]


def _value(x, default=None):
    """cPages fields are {"timestamp": .., "value": ..} pairs."""
    if isinstance(x, dict) and "value" in x:
        return x["value"]
    return default if x is None else x


def load(path: Path | str) -> Document:
    with zipfile.ZipFile(path) as z:
        names = z.namelist()
        content_name = next((n for n in names if n.endswith(".content") and "/" not in n), None)
        if content_name is None:
            raise ValueError(f"{path} has no .content file")
        doc_id = content_name[: -len(".content")]
        content = json.loads(z.read(content_name))
        meta_name = f"{doc_id}.metadata"
        metadata = json.loads(z.read(meta_name)) if meta_name in names else {}
        rm_files = {n.split("/")[-1][: -len(".rm")]: n for n in names if n.startswith(doc_id + "/") and n.endswith(".rm")}

        file_type = content.get("fileType") or "notebook"
        pdf = z.read(f"{doc_id}.pdf") if f"{doc_id}.pdf" in names else None
        epub = z.read(f"{doc_id}.epub") if f"{doc_id}.epub" in names else None

        pages: list[Page] = []
        cpages = (content.get("cPages") or {}).get("pages")
        if cpages:
            ordered = sorted(
                (p for p in cpages if not _value(p.get("deleted"), 0)),
                key=lambda p: str(_value(p.get("idx"), "")),
            )
            for i, p in enumerate(ordered):
                pid = p.get("id", "")
                redir = _value(p.get("redir"))
                pages.append(Page(
                    index=i, id=pid, template=str(_value(p.get("template"), "") or ""),
                    pdf_page=int(redir) if redir is not None and int(redir) >= 0 else None,
                    rm=z.read(rm_files[pid]) if pid in rm_files else None,
                ))
        else:
            # Older format: a flat list of page ids, PDF pages in order.
            for i, pid in enumerate(content.get("pages") or []):
                pages.append(Page(index=i, id=pid, pdf_page=i if pdf else None,
                                  rm=z.read(rm_files[pid]) if pid in rm_files else None))

        if epub and (pdf is None or not pages):
            # The tablet adds its own PDF rendering of an EPUB the first time
            # it's opened there; until then, lay the EPUB out at tablet size.
            pdf = epub_to_pdf(epub)
            pages = []

        if pdf and not pages:
            import pymupdf
            with pymupdf.open(stream=pdf, filetype="pdf") as src:
                pages = [Page(index=i, id=f"pdf-{i}", pdf_page=i) for i in range(src.page_count)]

    return Document(
        id=doc_id, name=metadata.get("visibleName", doc_id), file_type=file_type,
        pages=pages, pdf=pdf, epub=epub, content=content, metadata=metadata,
    )


def epub_to_pdf(epub: bytes) -> bytes:
    import pymupdf
    with pymupdf.open(stream=epub, filetype="epub") as book:
        book.layout(width=1620 * 72 / 229, height=2160 * 72 / 229, fontsize=11)
        return book.convert_to_pdf()


def rm_stream(page: Page) -> io.BytesIO:
    return io.BytesIO(page.rm or b"")
