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
from .tablet_system import (FIRMWARE_WARNING, MAX_IMPORT_BYTES, SCREEN_H, SCREEN_W, Change, apply,
                            backup_originals, firmware, fit_png, original, remember)
from .web import declared_size

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


# A .template is a tiny program: repeats of repeats can describe millions of
# shapes in a few hundred bytes, and constants can build on each other. One
# budget covers everything a render does, from the first constant on, and
# numbers are IEEE doubles like on the tablet, so no value grows with the
# arithmetic done on it.
MAX_TEMPLATE_BYTES = 1024 * 1024  # .template file to import
MAX_RENDER_STEPS = 200_000  # items and repeat tiles visited, in all
MAX_RENDER_BYTES = 8 * 1024 * 1024  # SVG produced
MAX_GROUP_DEPTH = 16  # groups inside groups
MAX_EVAL_WORK = 2_000_000  # values looked up plus expression tokens read, in all
MAX_EXPR_CHARS = 4096  # one expression
MAX_WARNINGS = 50  # kept per render; the rest are only counted
MAX_WARNING_CHARS = 200


class TemplateTooLarge(ValueError):
    """Rendering would go past the budget. Never turned into a warning."""


class _Budget:
    def __init__(self) -> None:
        self.steps = 0
        self.bytes = 0
        self.work = 0

    def step(self) -> None:
        self.steps += 1
        if self.steps > MAX_RENDER_STEPS:
            raise TemplateTooLarge(f"the template repeats too much (more than {MAX_RENDER_STEPS:,} shapes)")

    def emit(self, body: list[str], text: str) -> None:
        self.bytes += len(text) + 1
        if self.bytes > MAX_RENDER_BYTES:
            raise TemplateTooLarge(f"the template draws more than {MAX_RENDER_BYTES >> 20} MB of shapes")
        body.append(text)

    def charge(self, units: int = 1) -> None:
        self.work += units
        if self.work > MAX_EVAL_WORK:
            raise TemplateTooLarge(f"the template's expressions take too much work (more than {MAX_EVAL_WORK:,} steps)")


class ExprError(ValueError):
    pass


def _brief(value: Any, limit: int = 60) -> str:
    """A label for messages, without formatting an arbitrarily large value."""
    if isinstance(value, str):
        text = value
    elif value is None or isinstance(value, (bool, int, float)):
        text = repr(value)
    else:
        text = f"a {type(value).__name__}"
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _finite(value: Any) -> float:
    """A number as a finite double. Doubles take the same small time and
    memory whatever the arithmetic, unlike Python's unbounded integers."""
    try:
        out = float(value)
    except OverflowError:
        raise ExprError("number too large") from None
    if not math.isfinite(out):
        raise ExprError("number too large")
    return out


_TOKEN = re.compile(r"\s*(?:(\d+\.?\d*(?:[eE][-+]?\d+)?|\.\d+)|([A-Za-z_][A-Za-z_0-9]*)|"
                    r"(&&|\|\||==|!=|<=|>=|[-+*/%()?:<>!]))")


def _tokens(text: str) -> list[str]:
    out, pos = [], 0
    text = text.strip()
    while pos < len(text):
        m = _TOKEN.match(text, pos)
        if not m or m.end() == pos:
            raise ExprError(f"can't read {_brief(text)!r}")
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
            raise ExprError(f"unexpected {_brief(self.peek())!r}")
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
            return not v if tok == "!" else (-float(v) if tok == "-" else float(v))
        return self.atom()

    def atom(self) -> Any:
        tok = self.take()
        if tok == "(":
            v = self.ternary()
            self.take(")")
            return v
        if tok[0].isdigit() or tok[0] == ".":
            return float(tok)  # "1e999" is inf; evaluate() refuses it at the end
        if tok in ("true", "false"):
            return tok == "true"
        if tok[0].isalpha() or tok[0] == "_":
            if tok not in self.names:
                raise ExprError(f"unknown name {_brief(tok)}")
            return self.names[tok]
        raise ExprError(f"unexpected {_brief(tok)!r}")


_ARITHMETIC = {"+", "-", "*", "/", "%"}


def _apply(op: str, a: Any, b: Any) -> Any:
    if op in _ARITHMETIC:
        a, b = float(a), float(b)
        if op == "+":
            return a + b
        if op == "-":
            return a - b
        if op == "*":
            return a * b
        if op == "/":
            return a / b if b else 0.0
        return math.fmod(a, b) if b and math.isfinite(a) else 0.0
    if op == "&&":
        return bool(a) and bool(b)
    if op == "||":
        return bool(a) or bool(b)
    return {"==": a == b, "!=": a != b, "<": a < b, "<=": a <= b, ">": a > b, ">=": a >= b}[op]


def evaluate(value: Any, names: dict[str, Any], budget: _Budget | None = None) -> Any:
    """A number, true/false, or an expression over names, as a finite double
    or a bool. With a budget, each token read is charged to it."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return _finite(value)
    if not isinstance(value, str):
        raise ExprError(f"can't evaluate {_brief(value)}")
    if len(value) > MAX_EXPR_CHARS:
        raise ExprError(f"expression longer than {MAX_EXPR_CHARS:,} characters")
    parser = _Parser(value, names)
    if budget is not None:
        budget.charge(len(parser.toks))
    try:
        result = parser.parse()
    except RecursionError:
        raise ExprError("expression nested too deeply") from None
    return result if isinstance(result, bool) else _finite(result)


class _Scope:
    """What one render evaluates against: the names, the budget and the
    warnings. Once the constants are set the names stay fixed, so each
    distinct expression is worked out once and remembered."""

    def __init__(self, names: dict[str, Any]) -> None:
        self.names = names
        self.budget = _Budget()
        self.warnings: list[str] = []
        self.unlisted = 0
        self.known: dict[str, tuple[Any, str | None]] | None = None

    def freeze(self) -> None:
        self.known = {}

    def value(self, value: Any) -> Any:
        self.budget.charge()
        if self.known is None or not isinstance(value, str):
            return evaluate(value, self.names, self.budget)
        if value not in self.known:
            try:
                self.known[value] = (evaluate(value, self.names, self.budget), None)
            except ExprError as e:
                self.known[value] = (None, str(e)[:MAX_WARNING_CHARS])
        result, error = self.known[value]
        if error is not None:
            raise ExprError(error)
        return result

    def num(self, value: Any) -> float:
        return float(self.value(value))

    def warn(self, text: str) -> None:
        if len(self.warnings) >= MAX_WARNINGS:
            self.unlisted += 1
        else:
            self.warnings.append(text if len(text) <= MAX_WARNING_CHARS else text[:MAX_WARNING_CHARS - 1] + "…")


def _dict(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _list(value: Any) -> list:
    return value if isinstance(value, list) else []


# ============================================================ .template -> SVG


def template_to_svg(template: dict, *, width: int = SCREEN_W, height: int = SCREEN_H) -> tuple[str, list[str]]:
    """Render a .template document to SVG at the portrait page size.

    Landscape templates are laid out on a landscape page and rotated so the
    page's top runs along the portrait right edge, matching how landscape
    notebook pages keep their strokes in portrait coordinates. (Unverified on
    a device: compare against an exported page when the tablet is reachable.)
    Returns the SVG and a list of warnings for parts that were skipped;
    raises TemplateTooLarge before going past the budget, which starts with
    the constants.
    """
    landscape = _brief(template.get("orientation", "portrait")).lower() == "landscape"
    tw, th = (height, width) if landscape else (width, height)
    scope = _Scope({"templateWidth": float(tw), "templateHeight": float(th),
                    "paperOriginX": 0.0, "paperOriginY": 0.0})
    for const in _list(template.get("constants")):
        for key, value in _dict(const).items():
            try:
                scope.names[key] = scope.value(value)
            except ExprError as e:
                scope.warn(f"constant {_brief(key)}: {e}")
    scope.freeze()
    body: list[str] = []
    for item in _list(template.get("items")):
        _render_item(item, scope, 0.0, 0.0, tw, th, body, 0)
    if scope.unlisted:
        scope.warnings.append(f"and {scope.unlisted:,} more")
    transform = f' transform="translate({width} 0) rotate(90)"' if landscape else ""
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
           f'viewBox="0 0 {width} {height}">\n<rect width="{width}" height="{height}" fill="#ffffff"/>\n'
           f'<g{transform}>\n' + "\n".join(body) + "\n</g>\n</svg>\n")
    return svg, scope.warnings


DIRECTIONS = ("down", "up", "right", "left", "infinite")


def _offsets(mode: Any, start: float, step: float, limit: float, scope: _Scope) -> list[float]:
    """Tile offsets for a group's repeat value ("down"/"right", "up"/"left", "infinite", or a count)."""
    if mode in (None, "", False, 0) or step <= 0:
        return [0.0]
    cap = 2000
    if not (isinstance(mode, str) and mode in DIRECTIONS):
        try:
            count = scope.num(mode)
        except ExprError:
            return [0.0]
        return [k * step for k in range(max(1, min(int(count), cap)))]
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


def _render_item(item: Any, scope: _Scope, dx: float, dy: float, tw: float, th: float,
                 body: list[str], depth: int) -> None:
    budget = scope.budget
    budget.step()
    if not isinstance(item, dict):
        return
    if depth > MAX_GROUP_DEPTH:
        raise TemplateTooLarge(f"groups are nested more than {MAX_GROUP_DEPTH} deep")
    kind = item.get("type")
    label = _brief(item.get("id") or kind)
    try:
        if kind == "group":
            box = _dict(item.get("boundingBox"))
            x = scope.num(box.get("x", 0)) + dx
            y = scope.num(box.get("y", 0)) + dy
            w = scope.num(box.get("width", tw))
            h = scope.num(box.get("height", th))
            repeat = _dict(item.get("repeat"))
            children = _list(item.get("children"))
            columns = _offsets(repeat.get("columns"), x, w, tw, scope)
            for oy in _offsets(repeat.get("rows"), y, h, th, scope):
                for ox in columns:
                    budget.step()
                    for child in children:
                        _render_item(child, scope, x + ox, y + oy, tw, th, body, depth + 1)
        elif kind == "path":
            d = _path_data(_list(item.get("data")), scope, dx, dy)
            if d:
                stroke = item.get("strokeColor", "#000000")
                fill = item.get("fillColor", "none")
                width = scope.num(item.get("strokeWidth", 1))
                budget.emit(body, f'<path d="{d}" fill="{escape(_brief(fill))}" stroke="{escape(_brief(stroke))}" '
                                  f'stroke-width="{width:g}"/>')
        elif kind == "text":
            pos = _dict(item.get("position"))
            size = scope.num(item.get("fontSize", 24))
            x = scope.num(pos.get("x", 0)) + dx
            # Positions are the top of the text; SVG wants the baseline.
            y = scope.num(pos.get("y", 0)) + dy + size * 0.8
            color = item.get("color") or item.get("fillColor") or "#000000"
            text = item.get("text", "")
            budget.emit(body, f'<text x="{x:g}" y="{y:g}" font-family="sans-serif" font-size="{size:g}" '
                              f'fill="{escape(_brief(color))}">'
                              f'{escape(text if isinstance(text, str) else _brief(text))}</text>')
        else:
            scope.warn(f"{label}: unsupported item type {_brief(kind)!r}")
    except TemplateTooLarge:
        raise
    except (ExprError, TypeError, ValueError, ZeroDivisionError) as e:
        scope.warn(f"{label}: {e}")


_ARITY = {"M": 2, "L": 2, "C": 6, "Q": 4, "H": 1, "V": 1, "Z": 0}


def _path_data(data: list, scope: _Scope, dx: float, dy: float) -> str:
    out: list[str] = []
    i = 0
    while i < len(data):
        cmd = data[i]
        scope.budget.charge()
        if not isinstance(cmd, str) or len(cmd) != 1 or cmd.upper() not in _ARITY:
            raise ExprError(f"unknown path command {_brief(cmd)!r}")
        n = _ARITY[cmd.upper()]
        args = [scope.num(v) for v in data[i + 1:i + 1 + n]]
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


def read_limited(path: Path, limit: int, what: str) -> bytes:
    """A file's bytes, refusing it if it's over limit, without reading more than that."""
    with open(path, "rb") as f:
        raw = f.read(limit + 1)
    if len(raw) > limit:
        raise ValueError(f"{path.name} is larger than {limit >> 20} MB; that isn't {what}")
    return raw


def parse_template(raw: bytes, name: str) -> dict:
    """A .template file's JSON, refusing one too large or too nested to be a page template."""
    if len(raw) > MAX_TEMPLATE_BYTES:
        raise ValueError(f"{name} is larger than {MAX_TEMPLATE_BYTES >> 20} MB; that isn't a page template")
    try:
        doc = json.loads(raw.decode("utf-8"))
    except RecursionError:
        raise ValueError(f"{name} is nested too deeply to be a page template") from None
    if not isinstance(doc, dict):
        raise ValueError(f"{name} isn't a page template")
    return doc


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
                raw = read_limited(tpl, MAX_TEMPLATE_BYTES, "a page template")
                text, warn = template_to_svg(parse_template(raw, tpl.name))
            except ValueError as e:  # includes UnicodeDecodeError and TemplateTooLarge
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
        raw = read_limited(src, MAX_TEMPLATE_BYTES, "a page template")
        doc = parse_template(raw, src.name)  # the bytes checked are the bytes installed
        is_land = _brief(doc.get("orientation", "portrait")).lower() == "landscape" if landscape is None else landscape
        files[f"{filename}.template"] = raw
        svg, warn = template_to_svg(doc)
        preview_svg = svg
        notes += warn[:5]
    elif suffix == ".svg":
        text = read_limited(src, MAX_IMPORT_BYTES, "a page template").decode("utf-8", errors="replace")
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
        # The header gives the orientation; fit_png checks the size and decodes it in a limited process.
        with open(src, "rb") as f:
            declared = declared_size(f.read(64))
        if declared is None:
            raise ValueError(f"{src.name} isn't a PNG")
        is_land = (declared[0] > declared[1]) if landscape is None else landscape
        w, h = (SCREEN_H, SCREEN_W) if is_land else (SCREEN_W, SCREEN_H)
        files[f"{filename}.png"], info = fit_png(src, w, h)
        if info.get("converted"):
            notes.append(f"resized the PNG from {declared[0]}×{declared[1]} to {w}×{h}")
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
