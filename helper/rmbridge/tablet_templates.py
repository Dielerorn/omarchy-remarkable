"""Page templates: copy the tablet's into the render cache, add and remove custom ones.

On the tablet (Paper Pro, firmware 3.x) templates live in
/usr/share/remarkable/templates, registered in templates.json there (early
Paper Pro firmware used ferrari-templates.json; 3.15 went back to
templates.json). A notebook page stores the registry entry's "filename" as
its template value ("P Lines small", "LS Checklist", or a custom
"exercise tracker"), and the file is that name plus .svg, .png or, on newer
firmware, .template: a JSON description of paths and text drawn with
expressions over templateWidth/templateHeight. render.draw_template looks
for <value>.svg/.png in TEMPLATE_DIR, so .template files are rendered to SVG
here.
"""

from __future__ import annotations

import io
import json
import math
import re
import shutil
import tarfile
import tempfile
import time
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

from . import config
from .tablet_remote import REMOTE_BACKUP, Remote, TabletError, q
from .tablet_system import (FIRMWARE_WARNING, SCREEN_H, SCREEN_W, Change, apply, backup_originals,
                            firmware, fit_png, original, remember)

TEMPLATES_DIR = "/usr/share/remarkable/templates"
REGISTRIES = ["templates.json", "ferrari-templates.json"]
CATEGORIES = ["Creative", "Grids", "Lines", "Planners"]  # the picker's fixed tabs
# Icon glyphs from stock Paper Pro entries (templates.json stores the glyph itself).
CATEGORY_ICONS = {"Creative": "", "Grids": "", "Lines": "", "Planners": ""}
CUSTOM_STORE = f"{REMOTE_BACKUP}/custom-templates"
TEMPLATE_EXTS = (".svg", ".png", ".template")


def template_dir() -> Path:
    """Same place as render.TEMPLATE_DIR, resolved at call time."""
    return config.CACHE_DIR / "templates"


# ============================================================ expressions


class ExprError(ValueError):
    pass


_TOKEN = re.compile(r"\s*(?:(\d+\.?\d*(?:[eE][-+]?\d+)?|\.\d+)|([A-Za-z_][A-Za-z_0-9]*)|"
                    r"(&&|\|\||==|!=|<=|>=|[-+*/%()?:<>!]))")


def _tokens(text: str) -> list[str]:
    out, pos = [], 0
    text = text.strip()
    while pos < len(text):
        m = _TOKEN.match(text, pos)
        if not m or m.end() == pos:
            raise ExprError(f"can't read {text!r}")
        out.append(m.group(0).strip())
        pos = m.end()
    return out


class _Parser:
    """Recursive descent over the C-like expressions .template files use."""

    def __init__(self, text: str, names: dict[str, Any]):
        self.toks = _tokens(text)
        self.i = 0
        self.names = names

    def peek(self) -> str | None:
        return self.toks[self.i] if self.i < len(self.toks) else None

    def take(self, expected: str | None = None) -> str:
        tok = self.peek()
        if tok is None or (expected and tok != expected):
            raise ExprError(f"expected {expected or 'a value'}")
        self.i += 1
        return tok

    def parse(self) -> Any:
        value = self.ternary()
        if self.peek() is not None:
            raise ExprError(f"unexpected {self.peek()!r}")
        return value

    def ternary(self) -> Any:
        cond = self.binary(0)
        if self.peek() == "?":
            self.take("?")
            a = self.ternary()
            self.take(":")
            b = self.ternary()
            return a if cond else b
        return cond

    LEVELS = [("||",), ("&&",), ("==", "!="), ("<", "<=", ">", ">="), ("+", "-"), ("*", "/", "%")]

    def binary(self, level: int) -> Any:
        if level == len(self.LEVELS):
            return self.unary()
        left = self.binary(level + 1)
        while self.peek() in self.LEVELS[level]:
            op = self.take()
            right = self.binary(level + 1)
            left = _apply(op, left, right)
        return left

    def unary(self) -> Any:
        tok = self.peek()
        if tok in ("-", "+", "!"):
            self.take()
            v = self.unary()
            return -v if tok == "-" else (+v if tok == "+" else not v)
        return self.atom()

    def atom(self) -> Any:
        tok = self.take()
        if tok == "(":
            v = self.ternary()
            self.take(")")
            return v
        if tok[0].isdigit() or tok[0] == ".":
            return float(tok) if any(c in tok for c in ".eE") else int(tok)
        if tok in ("true", "false"):
            return tok == "true"
        if tok[0].isalpha() or tok[0] == "_":
            if tok not in self.names:
                raise ExprError(f"unknown name {tok}")
            return self.names[tok]
        raise ExprError(f"unexpected {tok!r}")


def _apply(op: str, a: Any, b: Any) -> Any:
    if op == "+":
        return a + b
    if op == "-":
        return a - b
    if op == "*":
        return a * b
    if op == "/":
        return a / b if b else 0.0
    if op == "%":
        return math.fmod(a, b) if b else 0.0
    if op == "&&":
        return bool(a) and bool(b)
    if op == "||":
        return bool(a) or bool(b)
    return {"==": a == b, "!=": a != b, "<": a < b, "<=": a <= b, ">": a > b, ">=": a >= b}[op]


def evaluate(value: Any, names: dict[str, Any]) -> Any:
    if isinstance(value, bool) or isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        return _Parser(value, names).parse()
    raise ExprError(f"can't evaluate {value!r}")


def num(value: Any, names: dict[str, Any]) -> float:
    return float(evaluate(value, names))


# ============================================================ .template -> SVG


def template_to_svg(template: dict, *, width: int = SCREEN_W, height: int = SCREEN_H) -> tuple[str, list[str]]:
    """Render a .template document to SVG at the portrait page size.

    Landscape templates are laid out on a landscape page and rotated so the
    page's top runs along the portrait right edge, matching how landscape
    notebook pages keep their strokes in portrait coordinates. (Unverified on
    a device: compare against an exported page when the tablet is reachable.)
    Returns the SVG and a list of warnings for parts that were skipped.
    """
    landscape = str(template.get("orientation", "portrait")).lower() == "landscape"
    tw, th = (height, width) if landscape else (width, height)
    names: dict[str, Any] = {"templateWidth": tw, "templateHeight": th, "paperOriginX": 0, "paperOriginY": 0}
    warnings: list[str] = []
    for const in template.get("constants") or []:
        if not isinstance(const, dict):
            continue
        for key, value in const.items():
            try:
                names[key] = evaluate(value, names)
            except (ExprError, TypeError, ZeroDivisionError) as e:
                warnings.append(f"constant {key}: {e}")
    body: list[str] = []
    for item in template.get("items") or []:
        _render_item(item, names, 0.0, 0.0, tw, th, body, warnings)
    transform = f' transform="translate({width} 0) rotate(90)"' if landscape else ""
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
           f'viewBox="0 0 {width} {height}">\n<rect width="{width}" height="{height}" fill="#ffffff"/>\n'
           f'<g{transform}>\n' + "\n".join(body) + "\n</g>\n</svg>\n")
    return svg, warnings


def _offsets(mode: Any, start: float, step: float, limit: float, names: dict) -> list[float]:
    """Tile offsets for a group's repeat value ("down"/"right", "up"/"left", "infinite", or a count)."""
    if mode in (None, "", False, 0) or step <= 0:
        return [0.0]
    cap = 2000
    if isinstance(mode, str) and mode not in ("down", "up", "right", "left", "infinite"):
        try:
            mode = int(evaluate(mode, names))
        except (ExprError, TypeError, ValueError):
            return [0.0]
    if isinstance(mode, (int, float)) and not isinstance(mode, bool):
        return [k * step for k in range(max(1, min(int(mode), cap)))]
    out: list[float] = []
    if mode in ("down", "right", "infinite"):
        k = 0
        while start + k * step < limit and k < cap:
            out.append(k * step)
            k += 1
    if mode in ("up", "left", "infinite"):
        k = -1
        while start + k * step + step > 0 and -k < cap:
            out.append(k * step)
            k -= 1
    return out or [0.0]


def _render_item(item: Any, names: dict, dx: float, dy: float, tw: float, th: float,
                 body: list[str], warnings: list[str]) -> None:
    if not isinstance(item, dict):
        return
    kind = item.get("type")
    label = item.get("id") or kind
    try:
        if kind == "group":
            box = item.get("boundingBox") or {}
            x = num(box.get("x", 0), names) + dx
            y = num(box.get("y", 0), names) + dy
            w = num(box.get("width", tw), names)
            h = num(box.get("height", th), names)
            repeat = item.get("repeat") or {}
            for oy in _offsets(repeat.get("rows"), y, h, th, names):
                for ox in _offsets(repeat.get("columns"), x, w, tw, names):
                    for child in item.get("children") or []:
                        _render_item(child, names, x + ox, y + oy, tw, th, body, warnings)
        elif kind == "path":
            d = _path_data(item.get("data") or [], names, dx, dy)
            if d:
                stroke = item.get("strokeColor", "#000000")
                fill = item.get("fillColor", "none")
                width = num(item.get("strokeWidth", 1), names)
                body.append(f'<path d="{d}" fill="{escape(str(fill))}" stroke="{escape(str(stroke))}" '
                            f'stroke-width="{width:g}"/>')
        elif kind == "text":
            pos = item.get("position") or {}
            size = num(item.get("fontSize", 24), names)
            x = num(pos.get("x", 0), names) + dx
            # Positions are the top of the text; SVG wants the baseline.
            y = num(pos.get("y", 0), names) + dy + size * 0.8
            color = item.get("color") or item.get("fillColor") or "#000000"
            body.append(f'<text x="{x:g}" y="{y:g}" font-family="sans-serif" font-size="{size:g}" '
                        f'fill="{escape(str(color))}">{escape(str(item.get("text", "")))}</text>')
        else:
            warnings.append(f"{label}: unsupported item type {kind!r}")
    except (ExprError, TypeError, ValueError, ZeroDivisionError) as e:
        warnings.append(f"{label}: {e}")


_ARITY = {"M": 2, "L": 2, "C": 6, "Q": 4, "H": 1, "V": 1, "Z": 0}


def _path_data(data: list, names: dict, dx: float, dy: float) -> str:
    out: list[str] = []
    i = 0
    while i < len(data):
        cmd = data[i]
        if not isinstance(cmd, str) or cmd.upper() not in _ARITY:
            raise ExprError(f"unknown path command {cmd!r}")
        n = _ARITY[cmd.upper()]
        args = [num(v, names) for v in data[i + 1:i + 1 + n]]
        if len(args) < n:
            raise ExprError(f"path command {cmd} is missing values")
        i += 1 + n
        if cmd.isupper():
            if cmd == "H":
                args = [args[0] + dx]
            elif cmd == "V":
                args = [args[0] + dy]
            else:
                args = [a + (dx if k % 2 == 0 else dy) for k, a in enumerate(args)]
        out.append(cmd + " ".join(f"{a:g}" for a in args))
    return " ".join(out)


# ============================================================ registry


def read_registry(remote: Remote) -> tuple[str, dict]:
    """The live registry: (path, parsed JSON)."""
    for name in REGISTRIES:
        path = f"{TEMPLATES_DIR}/{name}"
        if remote.exists(path):
            raw = remote.get(path)
            try:
                data = json.loads(raw)
            except ValueError as e:
                raise TabletError(f"The tablet's {name} isn't valid JSON, so nothing was changed.") from e
            if not isinstance(data, dict) or not isinstance(data.get("templates"), list):
                raise TabletError(f"The tablet's {name} has an unexpected layout, so nothing was changed.")
            return path, data
    raise TabletError("Couldn't find templates.json on the tablet.")


def entry_files(filename: str, present: set[str]) -> list[str]:
    return [filename + ext for ext in TEMPLATE_EXTS if filename + ext in present]


def list_remote_files(remote: Remote) -> set[str]:
    out = remote.run(f"ls -1 {q(TEMPLATES_DIR)}", check=False).text
    return {l.strip() for l in out.splitlines() if l.strip()}


def stock_filenames(remote: Remote | None, fw: str, registry_path: str) -> set[str] | None:
    """Filenames in the registry as the firmware shipped it, if we kept a copy."""
    raw = original(remote, fw, registry_path)
    if raw is None:
        return None
    try:
        return {str(e.get("filename", "")) for e in json.loads(raw).get("templates", []) if isinstance(e, dict)}
    except (ValueError, AttributeError):
        return None


def describe(entry: dict, present: set[str], stock: set[str] | None) -> dict:
    filename = str(entry.get("filename", ""))
    return {
        "name": entry.get("name", filename), "filename": filename,
        "categories": entry.get("categories", []), "landscape": bool(entry.get("landscape", False)),
        "files": entry_files(filename, present),
        "custom": (filename not in stock) if stock is not None else False,
    }


# ============================================================ fetch / list


def fetch(remote: Remote) -> dict:
    """Snapshot the tablet's templates into TEMPLATE_DIR (rendering .template to SVG)."""
    fw = firmware(remote)
    proc = remote.run(f"cd {q(TEMPLATES_DIR)} && tar -chf - .", timeout=300)
    target = template_dir()
    target.parent.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(dir=target.parent, prefix=".templates-"))
    skipped: list[dict] = []
    rendered: list[str] = []
    warnings: dict[str, list[str]] = {}
    try:
        with tarfile.open(fileobj=io.BytesIO(proc.out), mode="r:") as tar:
            for member in tar.getmembers():
                name = member.name.lstrip("./")
                if not member.isfile() or not name or "/" in name or name.startswith("."):
                    continue
                src = tar.extractfile(member)
                if src is None:
                    continue
                (work / name).write_bytes(src.read())
        files = {p.name for p in work.iterdir()}
        for tpl in sorted(work.glob("*.template")):
            svg = tpl.with_suffix(".svg")
            if svg.name in files:
                continue
            try:
                doc = json.loads(tpl.read_text(encoding="utf-8"))
                text, warn = template_to_svg(doc)
            except (ValueError, UnicodeDecodeError) as e:
                skipped.append({"file": tpl.name, "reason": f"not a readable template ({e})"})
                continue
            svg.write_text(text, encoding="utf-8")
            rendered.append(svg.name)
            if warn:
                warnings[tpl.name] = warn[:10]
        registry: dict = {}
        for name in REGISTRIES:
            if (work / name).exists():
                try:
                    registry = json.loads((work / name).read_text(encoding="utf-8"))
                    break
                except ValueError:
                    skipped.append({"file": name, "reason": "registry isn't valid JSON"})
        files = {p.name for p in work.iterdir()}
        entries = [e for e in registry.get("templates", []) if isinstance(e, dict)]
        missing = sorted({str(e.get("filename")) for e in entries
                          if not any(str(e.get("filename")) + ext in files for ext in (".svg", ".png"))})
        index = {
            "fetched": time.time(), "firmware": fw, "host": remote.host,
            "templates": [describe(e, files, None) for e in entries],
            "rendered": rendered, "skipped": skipped, "missing": missing, "warnings": warnings,
        }
        (work / "index.json").write_text(json.dumps(index, indent=2, ensure_ascii=False))
        old = target.with_name(".templates-old")
        shutil.rmtree(old, ignore_errors=True)
        if target.exists():
            target.rename(old)
        work.rename(target)
        shutil.rmtree(old, ignore_errors=True)
    except BaseException:
        shutil.rmtree(work, ignore_errors=True)
        raise
    return {"dir": str(target), "firmware": fw, "files": len(files), "templates": len(entries),
            "rendered": rendered, "skipped": skipped, "missing": missing, "warnings": warnings}


def cached_index() -> dict | None:
    try:
        return json.loads((template_dir() / "index.json").read_text())
    except (OSError, ValueError):
        return None


def list_live(remote: Remote) -> dict:
    fw = firmware(remote)
    path, registry = read_registry(remote)
    present = list_remote_files(remote)
    stock = stock_filenames(remote, fw, path)
    entries = [describe(e, present, stock) for e in registry["templates"] if isinstance(e, dict)]
    return {"firmware": fw, "registry": path, "templates": entries, "stockKnown": stock is not None}


# ============================================================ install / remove


def safe_filename(name: str) -> str:
    clean = re.sub(r"[^\w .()+-]", "", name, flags=re.UNICODE).strip(" .")
    clean = re.sub(r"\s+", " ", clean)
    if not clean:
        raise ValueError("the template name needs at least one letter or number")
    return clean[:80]


def svg_size(text: str) -> tuple[float, float] | None:
    head = re.search(r"<svg\b[^>]*>", text, re.S)
    if not head:
        return None
    tag = head.group(0)

    def attr(name: str) -> str | None:
        m = re.search(rf'\s{name}\s*=\s*["\']([^"\']*)["\']', tag)
        return m.group(1) if m else None

    vb = attr("viewBox")
    w, h = attr("width"), attr("height")
    try:
        if w and h and not w.endswith("%") and not h.endswith("%"):
            return float(re.match(r"[\d.]+", w).group()), float(re.match(r"[\d.]+", h).group())  # type: ignore[union-attr]
        if vb:
            parts = re.split(r"[\s,]+", vb.strip())
            return float(parts[2]), float(parts[3])
    except (AttributeError, ValueError, IndexError):
        pass
    return None


def normalize_svg(text: str, width: int, height: int) -> str:
    """Pin the SVG's size to the page, keeping its drawing via viewBox."""
    head = re.search(r"<svg\b[^>]*>", text, re.S)
    if not head:
        raise ValueError("that file isn't an SVG")
    tag = head.group(0)
    size = svg_size(text)
    if not re.search(r"\sviewBox\s*=", tag) and size:
        tag = tag[:-1].rstrip("/") + f' viewBox="0 0 {size[0]:g} {size[1]:g}"' + (" />" if tag.endswith("/>") else ">")
    tag = re.sub(r'\s(width|height)\s*=\s*["\'][^"\']*["\']', "", tag)
    tag = tag.replace("<svg", f'<svg width="{width}" height="{height}"', 1)
    return text[:head.start()] + tag + text[head.end():]


def prepare_install(src: Path, name: str, category: str, landscape: bool | None) -> dict:
    """Work out exactly what an install writes, without touching the tablet."""
    if not src.exists():
        raise ValueError(f"{src} doesn't exist")
    suffix = src.suffix.lower()
    filename = safe_filename(name)
    files: dict[str, bytes] = {}
    notes: list[str] = []
    if suffix == ".template":
        doc = json.loads(src.read_text(encoding="utf-8"))
        is_land = str(doc.get("orientation", "portrait")).lower() == "landscape" if landscape is None else landscape
        files[f"{filename}.template"] = src.read_bytes()
        svg, warn = template_to_svg(doc)
        preview_svg = svg
        notes += warn[:5]
    elif suffix == ".svg":
        text = src.read_text(encoding="utf-8", errors="replace")
        size = svg_size(text)
        is_land = (size[0] > size[1]) if (landscape is None and size) else bool(landscape)
        w, h = (SCREEN_H, SCREEN_W) if is_land else (SCREEN_W, SCREEN_H)
        if not size or (round(size[0]), round(size[1])) != (w, h):
            notes.append(f"scaled the SVG to {w}×{h}" + (f" (it was {size[0]:g}×{size[1]:g})" if size else ""))
        text = normalize_svg(text, w, h)
        files[f"{filename}.svg"] = text.encode()
        preview_svg = text
        # A raster copy too, for firmware that draws the PNG.
        with tempfile.NamedTemporaryFile(suffix=".svg", dir=config.CACHE_DIR, delete=False) as tmp:
            tmp.write(text.encode())
        try:
            files[f"{filename}.png"], _ = fit_png(Path(tmp.name), w, h)
        finally:
            Path(tmp.name).unlink(missing_ok=True)
    elif suffix == ".png":
        import pymupdf
        pix = pymupdf.Pixmap(str(src))
        is_land = (pix.width > pix.height) if landscape is None else landscape
        w, h = (SCREEN_H, SCREEN_W) if is_land else (SCREEN_W, SCREEN_H)
        files[f"{filename}.png"], info = fit_png(src, w, h)
        if info.get("converted"):
            notes.append(f"resized the PNG from {pix.width}×{pix.height} to {w}×{h}")
        preview_svg = None
    else:
        raise ValueError("templates must be .svg, .png or .template files")
    if category not in CATEGORIES:
        raise ValueError(f"category must be one of {', '.join(CATEGORIES)} (the tablet only shows those tabs)")
    entry: dict[str, Any] = {"name": name.strip(), "filename": filename,
                             "iconCode": CATEGORY_ICONS[category], "categories": [category]}
    if is_land:
        entry["landscape"] = True
    return {"filename": filename, "entry": entry, "files": files, "notes": notes, "previewSvg": preview_svg}


def install(remote: Remote, prepared: dict, *, restart: bool) -> dict:
    fw = firmware(remote)
    reg_path, registry = read_registry(remote)
    present = list_remote_files(remote)
    stock = stock_filenames(remote, fw, reg_path)
    filename = prepared["filename"]
    if stock is None:
        stock = {str(e.get("filename", "")) for e in registry["templates"] if isinstance(e, dict)}
    if filename in stock:
        raise TabletError(f"“{filename}” is one of the tablet's own templates; pick another name.")
    clash = [f for f in entry_files(filename, present) if f not in prepared["files"]]
    backup_originals(remote, fw, [reg_path])
    templates = [e for e in registry["templates"]
                 if not (isinstance(e, dict) and e.get("filename") == filename
                         and bool(e.get("landscape", False)) == bool(prepared["entry"].get("landscape", False)))]
    blanks = [i for i, e in enumerate(templates) if isinstance(e, dict) and e.get("filename") == "Blank"]
    templates.insert(max(blanks) + 1 if blanks else len(templates), prepared["entry"])
    registry["templates"] = templates
    new_registry = json.dumps(registry, indent=2, ensure_ascii=False).encode()
    changes = [Change(f"{TEMPLATES_DIR}/{n}", data) for n, data in prepared["files"].items()]
    changes += [Change(f"{TEMPLATES_DIR}/{n}", None) for n in clash]
    changes.append(Change(reg_path, new_registry))
    # Keep the source on /home so it can be put back after a firmware update.
    for n, data in prepared["files"].items():
        remote.put(data, f"{CUSTOM_STORE}/{n}")
    remote.put(json.dumps(prepared["entry"], ensure_ascii=False).encode(), f"{CUSTOM_STORE}/{filename}.entry.json")
    result = apply(remote, changes, restart=restart)
    local = template_dir()
    local.mkdir(parents=True, exist_ok=True)
    for n, data in prepared["files"].items():
        (local / n).write_bytes(data)
    if prepared.get("previewSvg") and not (local / f"{filename}.svg").exists():
        (local / f"{filename}.svg").write_text(prepared["previewSvg"], encoding="utf-8")
    remember("templates", filename, {"name": prepared["entry"]["name"], "files": sorted(prepared["files"]),
                                     "firmware": fw})
    return {"firmware": fw, "registry": reg_path, "entry": prepared["entry"],
            "files": sorted(prepared["files"]), **result, "warning": FIRMWARE_WARNING}


def find_entry(registry: dict, name: str) -> list[dict]:
    return [e for e in registry["templates"] if isinstance(e, dict)
            and (e.get("name") == name or e.get("filename") == name)]


def remove(remote: Remote, name: str, *, restart: bool) -> dict:
    fw = firmware(remote)
    reg_path, registry = read_registry(remote)
    matches = find_entry(registry, name)
    if not matches:
        raise TabletError(f"No template called “{name}” on the tablet.")
    stock = stock_filenames(remote, fw, reg_path)
    if stock is None:
        raise TabletError("This template came with the tablet (nothing was installed from here on this "
                          "firmware), so it was left alone.")
    filenames = {str(e.get("filename")) for e in matches}
    if filenames & stock:
        raise TabletError(f"“{name}” came with the tablet, so it was left alone. Only templates added "
                          "afterwards can be removed.")
    present = list_remote_files(remote)
    registry["templates"] = [e for e in registry["templates"] if e not in matches]
    still_used = {str(e.get("filename")) for e in registry["templates"] if isinstance(e, dict)}
    doomed = [f for fn in filenames if fn not in still_used for f in entry_files(fn, present)]
    changes = [Change(f"{TEMPLATES_DIR}/{f}", None) for f in doomed]
    changes.append(Change(reg_path, json.dumps(registry, indent=2, ensure_ascii=False).encode()))
    result = apply(remote, changes, restart=restart)
    stored = [f"{CUSTOM_STORE}/{fn}{ext}" for fn in filenames for ext in (*TEMPLATE_EXTS, ".entry.json")]
    remote.run("rm -f " + " ".join(q(p) for p in stored), check=False)
    for fn in filenames:
        remember("templates", fn, None)
    return {"firmware": fw, "removed": sorted(filenames), "files": doomed, **result}
