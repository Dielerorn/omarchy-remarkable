"""A stand-in for the tablet's USB web interface, with its known quirks.

Modelled on remarkable.guide's endpoint list and the measured behaviour in
laithalsaadoon/remarkable-spec (Paper Pro, firmware 3.27.3.0):
- listings carry both VisibleName and the misspelled VissibleName;
- JSON is labelled ISO-8859-1 but encoded as UTF-8;
- /documents/<anything that isn't a folder> returns the root listing;
- /download responses send Content-Length *and* chunked encoding;
- uploads land in the folder listed last;
- errors are {"error": "..."} with 400/500.
"""

from __future__ import annotations

import json
import threading
import uuid
import zipfile
import io
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class Tablet:
    def __init__(self):
        self.entries: dict[str, dict] = {}
        self.last_listed = ""
        self.uploads: list[dict] = []
        self.downloads: list[str] = []
        self.legacy = False  # older firmware: only VissibleName, sizeInBytes as a string

    def add(self, name: str, parent: str = "", folder: bool = False, file_type: str = "notebook",
            modified: str = "2026-10-01T10:00:00.000Z", doc_id: str | None = None) -> str:
        doc_id = doc_id or str(uuid.uuid4())
        self.entries[doc_id] = {"ID": doc_id, "name": name, "parent": parent, "folder": folder,
                                "fileType": file_type, "modified": modified}
        return doc_id

    def listing(self, parent: str) -> list[dict]:
        out = []
        for e in self.entries.values():
            if e["parent"] != parent:
                continue
            item = {"ID": e["ID"], "ModifiedClient": e["modified"], "Parent": e["parent"],
                    "Type": "CollectionType" if e["folder"] else "DocumentType", "VissibleName": e["name"]}
            if not self.legacy:
                item["VisibleName"] = e["name"]
                item["Bookmarked"] = False
            if not e["folder"]:
                item["fileType"] = e["fileType"]
                item["CurrentPage"] = 0
                if self.legacy:
                    item["sizeInBytes"] = "12345"
                    item["pageCount"] = 3
            out.append(item)
        return out

    def rmdoc(self, doc_id: str) -> bytes:
        e = self.entries[doc_id]
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr(f"{doc_id}.metadata", json.dumps({"visibleName": e["name"], "parent": e["parent"]}))
            z.writestr(f"{doc_id}.content", json.dumps({"fileType": e["fileType"], "cPages": {"pages": []}}))
        return buf.getvalue()


def make_handler(tablet: Tablet):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass

        def _send(self, status: int, body: bytes, ctype: str, extra: dict | None = None, chunked: bool = False):
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            if chunked:
                self.send_header("Content-Length", str(len(body)))  # the firmware sends both
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                for i in range(0, len(body), 4096):
                    part = body[i:i + 4096]
                    self.wfile.write(f"{len(part):x}\r\n".encode() + part + b"\r\n")
                self.wfile.write(b"0\r\n\r\n")
            else:
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            self.close_connection = True

        def _error(self, status: int, message: str):
            self._send(status, json.dumps({"error": message}).encode(), "application/json; charset=ISO-8859-1")

        def do_GET(self):
            path = self.path.split("?", 1)[0]
            if path == "/documents/" or path.startswith("/documents/"):
                if not path.startswith("/documents/"):
                    return self._error(500, "Unknown file")
                folder = path[len("/documents/"):]
                e = tablet.entries.get(folder)
                if not (e and e["folder"]):
                    folder = ""  # silent fallback to the root
                tablet.last_listed = folder
                body = json.dumps(tablet.listing(folder), ensure_ascii=False, indent=4).encode("utf-8")
                return self._send(200, body, "application/json; charset=ISO-8859-1", chunked=True)
            if path.startswith("/download/"):
                parts = path.split("/")
                if len(parts) < 4 or not parts[3]:
                    return self._error(400, "Malformed URL")
                doc_id, fmt = parts[2], parts[3]
                if fmt not in ("pdf", "rmdoc"):
                    return self._error(400, "Filetype not supported")
                e = tablet.entries.get(doc_id)
                if e is None:
                    return self._error(400, "No such entry")
                if e["folder"]:
                    return self._error(400, "Can only download documents")
                tablet.downloads.append(f"{doc_id}/{fmt}")
                body = (b"%PDF-1.7\n% fake " + e["name"].encode() + b"\n%%EOF\n") if fmt == "pdf" else tablet.rmdoc(doc_id)
                ctype = "application/pdf" if fmt == "pdf" else "application/zip"
                name = f'{e["name"]}.{fmt}'.encode("utf-8").decode("latin-1")
                return self._send(200, body, ctype, {"Content-Disposition": f'attachment; filename="{name}"'},
                                  chunked=True)
            if path == "/log.txt":
                return self._send(200, b"journal\n", "text/plain")
            return self._error(500, "Unknown file")

        do_HEAD = do_GET

        def do_POST(self):
            path = self.path.split("?", 1)[0]
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length)
            if path.startswith("/documents/"):
                return self.do_GET()
            if path != "/upload":
                return self._error(500, "Unknown file")
            ctype = self.headers.get("Content-Type", "")
            if "boundary=" not in ctype:
                return self._error(400, "No file sent")
            boundary = ctype.split("boundary=", 1)[1].encode()
            for part in body.split(b"--" + boundary):
                head, _, data = part.partition(b"\r\n\r\n")
                if b'name="file"' not in head:
                    continue
                filename = head.split(b'filename="', 1)[1].split(b'"', 1)[0].decode("utf-8")
                data = data.rsplit(b"\r\n", 1)[0]
                tablet.uploads.append({"filename": filename, "folder": tablet.last_listed, "data": data,
                                       "head": head.decode("utf-8", "replace")})
                tablet.add(filename, parent=tablet.last_listed,
                           file_type="pdf" if filename.lower().endswith(".pdf") else "notebook")
                return self._send(201, b'{"status":"Upload successful"}', "application/json; charset=ISO-8859-1")
            return self._error(400, "No file sent")

    return Handler


class FakeUsbServer:
    def __init__(self):
        self.tablet = Tablet()
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.tablet))
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def __enter__(self) -> "FakeUsbServer":
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.httpd.shutdown()
        self.httpd.server_close()
