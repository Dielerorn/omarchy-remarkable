"""A morning agenda page for the tablet: the day's events and dated tasks from
the locally cached Google Calendar (omarchy-calendar), then ruled lines for
handwritten notes. Black and grey only, sized for reading on e-ink."""

from __future__ import annotations

import argparse
import html
import json
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any

from . import config

RETRY_SECONDS = 15 * 60  # after a failed morning send
STRICT_ATTEMPTS = 3  # calendar read failures before sending a page without events
STALE_HOURS = 24  # older calendar data gets a note on the page

# Layout, in points on the tablet page (send.PAGE_W x send.PAGE_H, ~509 x 679).
MARGIN_X = 36
MARGIN_TOP = 34
MARGIN_BOTTOM = 30
TIME_COL = 104
LINE_GAP = 26  # ruled lines ~9 mm apart, a comfortable handwriting size
MIN_NOTES = 5 * LINE_GAP  # paper kept for notes on a busy day: the label and four lines
MORE_LINE = 24  # room for the "+ N more on the calendar" line
GREY = (0.45, 0.45, 0.45)
RULE_GREY = (0.72, 0.72, 0.72)
BASE_CSS = ("body, p { margin: 0; padding: 0; } * { font-family: sans-serif; color: #000; line-height: 1.2; } ")


class AgendaError(RuntimeError):
    pass


@dataclass
class Day:
    day: date
    all_day: list[tuple[str, str]] = field(default_factory=list)  # (title, note)
    timed: list[tuple[str, str, str]] = field(default_factory=list)  # (time label, title, location)
    tasks: list[str] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not (self.all_day or self.timed or self.tasks)


# ------------------------------------------------------------- calendar data


def calendar_cli() -> str | None:
    found = shutil.which("omarchy-calendar")
    if found:
        return found
    local = Path.home() / ".local/bin/omarchy-calendar"
    return str(local) if local.exists() else None


def read_calendar(start: date, end: date) -> tuple[list[dict], dict]:
    """Cached events and dated tasks from start up to (not including) end."""
    cli = calendar_cli()
    if not cli:
        raise AgendaError("omarchy-calendar isn't installed, so there are no events to show")
    try:
        proc = subprocess.run([cli, "agenda", "list", "--start", start.isoformat(), "--end", end.isoformat()],
                              capture_output=True, text=True, timeout=30)
    except subprocess.TimeoutExpired as e:
        raise AgendaError("the calendar didn't answer in time") from e
    try:
        data = json.loads(proc.stdout or "{}")
    except ValueError as e:
        raise AgendaError("the calendar sent something unreadable") from e
    if proc.returncode != 0 or data.get("ok") is False:
        err = data.get("error") or {}
        message = err.get("message") if isinstance(err, dict) else str(err)
        raise AgendaError(f"couldn't read the calendar: {message or proc.stderr.strip() or 'unknown error'}")
    return list(data.get("items") or []), dict(data.get("context") or {})


def calendar_note(context: dict, now: datetime | None = None) -> str:
    """A footer line when the cached calendar may be out of date."""
    now = now or datetime.now().astimezone()
    if context.get("configured") is False:
        return "Google Calendar isn't set up in Omarchy"
    accounts = [a for a in context.get("accounts") or [] if a.get("enabled", True)]
    if not accounts:
        return "No calendar account connected"
    notes = []
    for a in accounts:
        label = a.get("label") or "Calendar"
        synced = _parse_dt(a.get("lastSync"))
        stale = synced is None or (now - synced) > timedelta(hours=STALE_HOURS)
        if a.get("error") or stale:
            when = f"last synced {synced.astimezone():%-d %b %H:%M}" if synced else "never synced"
            notes.append(f"{label} {when}" + (f" — {a['error']}" if a.get("error") else ""))
    return "; ".join(notes)


def _parse_dt(value: Any) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.astimezone()


def _parse_date(value: Any) -> date | None:
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _span_note(first: date, last: date, day: date) -> str:
    total = (last - first).days + 1
    return f"day {(day - first).days + 1} of {total}" if total > 1 else ""


def plan_day(items: list[dict], day: date) -> Day:
    """Sort one day's items into all-day, timed and tasks (local time)."""
    out = Day(day)
    day_start = datetime.combine(day, time.min).astimezone()
    day_end = datetime.combine(day + timedelta(days=1), time.min).astimezone()
    seen: set[tuple] = set()
    timed: list[tuple[datetime, str, str, str]] = []
    for it in items:
        title = " ".join(str(it.get("title") or "(no title)").split())
        # The same event shows up once per account that can see it.
        key = (it.get("icalUid") or it.get("id"), it.get("occurrenceStart") or it.get("start"), it.get("type"))
        if key in seen:
            continue
        if it.get("type") == "task":
            if _parse_date(it.get("dueDate") or it.get("start")) == day:
                seen.add(key)
                out.tasks.append(title)
            continue
        location = " ".join(str(it.get("location") or "").split())
        if it.get("allDay"):
            first = _parse_date(it.get("start"))
            end = _parse_date(it.get("end"))
            if first is None:
                continue
            last = (end - timedelta(days=1)) if end and end > first else first  # end is exclusive
            if first <= day <= last:
                seen.add(key)
                out.all_day.append((title, _span_note(first, last, day)))
            continue
        start, end = _parse_dt(it.get("start")), _parse_dt(it.get("end"))
        if start is None:
            continue
        start = start.astimezone()
        end = end.astimezone() if end else start
        if not (start < day_end and (end > day_start or start >= day_start)):
            continue
        seen.add(key)
        if start <= day_start and end >= day_end:
            # Runs through the whole day: list it with the all-day items.
            last = (end - timedelta(microseconds=1)).date()
            out.all_day.append((title, _span_note(start.date(), last, day)))
            continue
        if start < day_start:
            label = f"until {end:%H:%M}"
        elif end > day_end:
            label = f"from {start:%H:%M}"
        elif end <= start:
            label = f"{start:%H:%M}"
        else:
            label = f"{start:%H:%M}–{end:%H:%M}"
        timed.append((start, label, title, location))
    timed.sort(key=lambda t: (t[0], t[2].lower()))
    out.timed = [(label, title, loc) for _, label, title, loc in timed]
    out.all_day.sort(key=lambda t: t[0].lower())
    out.tasks.sort(key=str.lower)
    return out


# --------------------------------------------------------------------- page


def _box(page, x: float, y: float, w: float, bottom: float, text: str, css: str) -> float | None:
    """Write wrapped HTML text at (x, y); returns the height used, or None
    if it doesn't fit above `bottom` (nothing is drawn then)."""
    import pymupdf
    if bottom - y < 8:
        return None
    rect = pymupdf.Rect(x, y, x + w, bottom)
    spare, _ = page.insert_htmlbox(rect, text, css=BASE_CSS + css, scale_low=1)
    if spare < 0:
        return None
    return rect.height - spare


def _esc(s: str) -> str:
    return html.escape(s, quote=False)


def draw_day(page, d: Day, note: str = "") -> None:
    import pymupdf

    w, h = page.rect.width, page.rect.height
    left, right = MARGIN_X, w - MARGIN_X
    width = right - left
    floor = h - MARGIN_BOTTOM - (14 if note else 0)
    y = MARGIN_TOP

    used = _box(page, left, y, width, floor, f"<p>{d.day:%A} {d.day.day} {d.day:%B}</p>",
                "p { font-size: 30pt; font-weight: bold; }") or 0
    y += used + 2
    week = d.day.isocalendar().week
    used = _box(page, left, y, width, floor, f"<p>{d.day.year} · Week {week}</p>",
                "p { font-size: 12pt; color: #666; }") or 0
    y += used + 8
    page.draw_line((left, y), (right, y), color=(0, 0, 0), width=1.6)
    y += 14

    limit = floor - MIN_NOTES - MORE_LINE  # events stop here so there's always room to write

    def heading(label: str) -> bool:
        nonlocal y
        used = _box(page, left, y, width, limit, f"<p>{_esc(label.upper())}</p>",
                    "p { font-size: 10pt; font-weight: bold; color: #666; letter-spacing: 1pt; }")
        if used is None:
            return False
        y += used + 5
        return True

    hidden = 0
    item_css = "p { font-size: 15pt; } .sub { font-size: 11.5pt; color: #555; }"

    if d.all_day and heading("All day"):
        for i, (title, span) in enumerate(d.all_day):
            text = f"<p>{_esc(title)}</p>" + (f'<p class="sub">{_esc(span)}</p>' if span else "")
            used = _box(page, left + TIME_COL, y, width - TIME_COL, limit, text, item_css)
            if used is None:
                hidden += len(d.all_day) - i
                break
            mark = left + TIME_COL - 20
            page.draw_rect(pymupdf.Rect(mark, y + 5.5, mark + 8, y + 13.5), color=(0, 0, 0), fill=(0, 0, 0))
            y += used + 7
        y += 6
    elif d.all_day:
        hidden += len(d.all_day)

    if d.timed and not hidden and heading("Schedule"):
        for i, (label, title, location) in enumerate(d.timed):
            text = f"<p>{_esc(title)}</p>" + (f'<p class="sub">{_esc(location)}</p>' if location else "")
            used = _box(page, left + TIME_COL, y, width - TIME_COL, limit, text, item_css)
            if used is None:
                hidden += len(d.timed) - i
                break
            _box(page, left, y + 1.5, TIME_COL - 8, y + 40, f"<p>{_esc(label)}</p>",
                 "p { font-size: 12.5pt; font-weight: bold; }")
            y += used + 8
        y += 6
    elif d.timed:
        hidden += len(d.timed)

    if d.tasks and not hidden and heading("To do"):
        for i, title in enumerate(d.tasks):
            used = _box(page, left + TIME_COL, y, width - TIME_COL, limit, f"<p>{_esc(title)}</p>", item_css)
            if used is None:
                hidden += len(d.tasks) - i
                break
            mark = left + TIME_COL - 24
            page.draw_rect(pymupdf.Rect(mark, y + 3, mark + 14, y + 17), color=(0, 0, 0), width=1.3)
            y += used + 8
        y += 6
    elif d.tasks:
        hidden += len(d.tasks)

    if hidden:
        used = _box(page, left + TIME_COL, y, width - TIME_COL, floor,
                    f"<p>+ {hidden} more on the calendar</p>", "p { font-size: 12pt; color: #555; }")
        y += (used or 0) + 8
    if d.empty:
        used = _box(page, left, y, width, limit, "<p>Nothing on the calendar</p>",
                    "p { font-size: 15pt; color: #666; font-style: italic; }")
        y += (used or 0) + 12

    # Notes: ruled lines down to the bottom of the page.
    used = _box(page, left, y + 4, width, floor, "<p>NOTES</p>",
                "p { font-size: 10pt; font-weight: bold; color: #666; letter-spacing: 1pt; }") or 12
    line_y = y + 4 + used + LINE_GAP
    while line_y <= floor:
        page.draw_line((left, line_y), (right, line_y), color=RULE_GREY, width=0.6)
        line_y += LINE_GAP

    if note:
        _box(page, left, h - MARGIN_BOTTOM - 8, width, h - 4, f"<p>{_esc(note)}</p>",
             "p { font-size: 8pt; color: #777; }")


def render_pdf(days: list[Day], target: Path, note: str = "") -> Path:
    import pymupdf
    from .send import PAGE_H, PAGE_W

    doc = pymupdf.open()
    for d in days:
        page = doc.new_page(width=PAGE_W, height=PAGE_H)
        draw_day(page, d, note)
    first = days[0].day if days else date.today()
    doc.set_metadata({"title": document_name(first), "creator": "omarchy-remarkable"})
    target.parent.mkdir(parents=True, exist_ok=True)
    doc.save(target, garbage=3, deflate=True)
    doc.close()
    return target


def document_name(day: date) -> str:
    return f"Agenda {day.isoformat()} ({day:%A})"


def build(start: date, days: int, target: Path, *, strict: bool = False) -> dict[str, Any]:
    """Read the calendar and draw one page per day. With strict=False a
    calendar that can't be read still gives a page (with a note saying so)."""
    days = max(1, min(int(days or 1), 31))
    try:
        items, context = read_calendar(start, start + timedelta(days=days))
        note = calendar_note(context)
    except AgendaError as e:
        if strict:
            raise
        items, note = [], str(e)[:1].upper() + str(e)[1:]
    planned = [plan_day(items, start + timedelta(days=i)) for i in range(days)]
    render_pdf(planned, target, note)
    return {
        "path": str(target), "name": document_name(start), "note": note,
        "days": [{"date": d.day.isoformat(), "allDay": len(d.all_day), "events": len(d.timed),
                  "tasks": len(d.tasks)} for d in planned],
    }


def send(start: date, days: int, folder: str, *, strict: bool = False) -> dict[str, Any]:
    """Build and upload, unless a document with today's name is already there."""
    from . import cloud, library
    from . import send as sender

    name = document_name(start)
    base = "/" + folder.strip("/") if folder.strip("/") else ""
    lib = library.fetch_library()
    existing = lib.find_path(f"{base}/{cloud._safe_filename(name)}")
    if existing is not None:
        return {"ok": True, "skipped": True, "name": name, "folder": folder, "id": existing.id,
                "reason": "already on the tablet"}
    with tempfile.TemporaryDirectory(dir=config.TMP_DIR) as tmp:
        made = build(start, days, Path(tmp) / f"{name}.pdf", strict=strict)
        sent = sender.send_file(Path(made["path"]), folder, name)
    library.fetch_library()
    made.pop("path", None)
    return {"ok": True, "skipped": False, **made, "id": sent["id"], "name": sent["name"], "folder": folder}


def _agenda_time(value: Any) -> time:
    try:
        hh, mm = str(value).strip().split(":")[:2]
        return time(int(hh), int(mm))
    except (ValueError, TypeError):
        return time(6, 0)


def maybe_run(settings: dict, state: dict, now: datetime) -> dict | None:
    """Daemon hook: send today's agenda once, after settings["agendaTime"].

    Returns the send result (with "name") when a page went up, else None:
    not time yet, already done today, already on the tablet, or a failure.
    Updates `state` in place for the caller to save: agendaLastDate once
    done; agendaError/agendaFailedAt after a failure, retried every 15 min.
    Never raises, so a failure can't make the daemon retry every tick."""
    if not settings.get("agendaEnabled"):
        return None
    if now.time().replace(tzinfo=None) < _agenda_time(settings.get("agendaTime", "06:00")):
        return None
    today = now.date().isoformat()
    if state.get("agendaLastDate") == today:
        return None
    failed_at = state.get("agendaFailedAt")
    if failed_at and now.timestamp() - float(failed_at) < RETRY_SECONDS:
        return None
    failures = int(state.get("agendaFailures") or 0) if state.get("agendaFailedDate") == today else 0
    try:
        # Early tries wait for the calendar (it may still be starting);
        # after a few, send the page anyway with a note on it.
        result = send(now.date(), int(settings.get("agendaDays") or 1), str(settings.get("agendaFolder", "")),
                      strict=failures < STRICT_ATTEMPTS)
    except Exception as e:  # noqa: BLE001 - see docstring
        state.update(agendaFailedAt=now.timestamp(), agendaFailedDate=today, agendaFailures=failures + 1,
                     agendaError=str(e) or type(e).__name__)
        return None
    state["agendaLastDate"] = today
    for k in ("agendaFailedAt", "agendaFailedDate", "agendaFailures", "agendaError"):
        state.pop(k, None)
    if result.get("skipped"):
        return None  # someone already put today's page there
    return {**result, "feature": "agenda"}


# ----------------------------------------------------------------- commands


def _start(args: argparse.Namespace) -> date:
    if not args.date:
        return date.today()
    try:
        return date.fromisoformat(args.date)
    except ValueError as e:
        raise AgendaError(f"{args.date} isn't a date like 2026-10-26") from e


def _days(args: argparse.Namespace) -> int:
    return args.days if args.days else int(config.load_settings().get("agendaDays") or 1)


def cmd_build(args: argparse.Namespace) -> dict:
    try:
        start = _start(args)
        target = Path(args.out).expanduser() if args.out else config.CACHE_DIR / "agenda" / f"{document_name(start)}.pdf"
        made = build(start, _days(args), target)
    except AgendaError as e:
        return {"ok": False, "error": str(e), "code": "agenda"}
    if args.open:
        from .cli import open_path
        open_path(target)
    return {"ok": True, **made}


def cmd_send(args: argparse.Namespace) -> dict:
    from .cli import notify
    folder = config.load_settings().get("agendaFolder", "") if args.folder is None else args.folder
    try:
        result = send(_start(args), _days(args), folder)
    except AgendaError as e:
        if args.notify:
            notify("Couldn't send the agenda", str(e), urgency="critical")
        return {"ok": False, "error": str(e), "code": "agenda"}
    if args.notify:
        notify("Agenda on your reMarkable" if not result.get("skipped") else "Agenda already on your reMarkable",
               result["name"])
    return result


def register(sub) -> None:
    p = sub.add_parser("agenda", help="a daily agenda page from the calendar, with room for notes")
    asub = p.add_subparsers(dest="action", required=True)
    for name, func, text in (("build", cmd_build, "make the PDF on this computer"),
                             ("send", cmd_send, "make it and put it on the tablet (skips if already there)")):
        x = asub.add_parser(name, help=text)
        x.add_argument("--date", help="first day, YYYY-MM-DD (default today)")
        x.add_argument("--days", type=int, help="pages, one per day (default from settings: agendaDays)")
        if name == "build":
            x.add_argument("--out", help="where to save the PDF")
            x.add_argument("--open", action="store_true", help="open it afterwards")
        else:
            x.add_argument("--folder", help='cloud folder ("" = top level); default from settings (agendaFolder)')
            x.add_argument("--notify", action="store_true", help="desktop notification when done")
        x.set_defaults(func=func)
