"""agenda.py: sorting calendar items into a day, the page, sending, the daemon hook."""

from __future__ import annotations

import json
import subprocess
import time as _time
from datetime import date, datetime, timedelta

import pymupdf
import pytest

from rmbridge import agenda, send

DAY = date(2026, 10, 26)  # a Monday, the day after Europe/Berlin leaves summer time


@pytest.fixture(autouse=True)
def berlin(monkeypatch):
    monkeypatch.setenv("TZ", "Europe/Berlin")
    _time.tzset()
    yield
    monkeypatch.undo()
    _time.tzset()


def ev(title, start, end, all_day=False, location="", uid=None, account="a"):
    return {"type": "event", "title": title, "start": start, "end": end, "allDay": all_day,
            "location": location, "icalUid": uid or title, "occurrenceStart": start, "id": f"{account}:{title}"}


def task(title, due):
    return {"type": "task", "title": title, "start": due, "end": due, "allDay": True, "dueDate": due,
            "id": f"task:{title}", "icalUid": None}


ITEMS = [
    ev("Staff meeting", "2026-10-26T08:00:00+01:00", "2026-10-26T09:00:00+01:00", location="1212"),
    ev("Staff meeting", "2026-10-26T08:00:00+01:00", "2026-10-26T09:00:00+01:00", location="1212",
       account="b"),  # same event seen through a second account
    ev("Squadron lunch", "2026-10-26T13:00:00-05:00", "2026-10-26T14:00:00-05:00"),  # 19:00 Berlin
    ev("Dentist", "2026-10-26T07:30:00+01:00", "2026-10-26T08:00:00+01:00"),
    ev("Night train", "2026-10-25T22:00:00+01:00", "2026-10-26T06:15:00+01:00"),
    ev("Late show", "2026-10-26T23:00:00+01:00", "2026-10-27T01:00:00+01:00"),
    ev("Volksfest", "2026-10-20T03:00:00+02:00", "2026-10-30T04:00:00+01:00"),  # covers the whole day
    ev("Parents visiting", "2026-10-24", "2026-10-29", all_day=True),
    ev("Pay rent", "2026-10-26", "2026-10-27", all_day=True),
    ev("Yesterday", "2026-10-25", "2026-10-26", all_day=True),  # end date is exclusive
    task("Journal", "2026-10-26"),
    task("Tomorrow's task", "2026-10-27"),
]


def test_plan_day():
    d = agenda.plan_day(ITEMS, DAY)
    assert d.timed == [
        ("until 06:15", "Night train", ""),
        ("07:30–08:00", "Dentist", ""),
        ("08:00–09:00", "Staff meeting", "1212"),
        ("19:00–20:00", "Squadron lunch", ""),
        ("from 23:00", "Late show", ""),
    ]
    assert d.all_day == [("Parents visiting", "day 3 of 5"), ("Pay rent", ""), ("Volksfest", "day 7 of 11")]
    assert d.tasks == ["Journal"]


def test_plan_day_next_day():
    d = agenda.plan_day(ITEMS, DAY + timedelta(days=1))
    assert ("until 01:00", "Late show", "") in d.timed
    assert d.tasks == ["Tomorrow's task"]
    assert ("Parents visiting", "day 4 of 5") in d.all_day


def test_calendar_note():
    now = datetime(2026, 10, 26, 6, 0).astimezone()
    fresh = {"configured": True, "accounts": [{"label": "me@x", "enabled": True,
                                               "lastSync": "2026-10-26T04:30:00Z", "error": None}]}
    assert agenda.calendar_note(fresh, now) == ""
    stale = {"configured": True, "accounts": [{"label": "me@x", "enabled": True,
                                               "lastSync": "2026-09-30T21:45:29Z",
                                               "error": "Google access expired. Sign in again."}]}
    assert agenda.calendar_note(stale, now) == \
        "me@x last synced 30 Sep 23:45 — Google access expired. Sign in again."
    assert agenda.calendar_note({"configured": False}, now) == "Google Calendar isn't set up in Omarchy"
    assert agenda.calendar_note({"configured": True, "accounts": []}, now) == "No calendar account connected"


def test_read_calendar(monkeypatch):
    calls = []

    def run(cmd, **kw):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps({"items": ITEMS, "context": {}}), stderr="")
    monkeypatch.setattr(agenda, "calendar_cli", lambda: "/usr/bin/omarchy-calendar")
    monkeypatch.setattr(agenda.subprocess, "run", run)
    items, ctx = agenda.read_calendar(DAY, DAY + timedelta(days=2))
    assert calls == [["/usr/bin/omarchy-calendar", "agenda", "list", "--start", "2026-10-26", "--end", "2026-10-28"]]
    assert len(items) == len(ITEMS)

    def fail(cmd, **kw):
        out = {"ok": False, "error": {"code": "daemon_unavailable", "message": "calendar service is not running"}}
        return subprocess.CompletedProcess(cmd, 1, stdout=json.dumps(out), stderr="")
    monkeypatch.setattr(agenda.subprocess, "run", fail)
    with pytest.raises(agenda.AgendaError, match="calendar service is not running"):
        agenda.read_calendar(DAY, DAY + timedelta(days=1))


def page_text(page) -> str:
    # Expand the "ff"/"fi" ligatures the layout engine uses.
    return page.get_text(flags=pymupdf.TEXTFLAGS_TEXT & ~pymupdf.TEXT_PRESERVE_LIGATURES)


def ruled_lines(page) -> list:
    return [p for p in page.get_drawings()
            if p.get("color") and all(abs(c - g) < 0.01 for c, g in zip(p["color"], agenda.RULE_GREY))]


def fake_calendar(monkeypatch, items=ITEMS, context=None):
    monkeypatch.setattr(agenda, "read_calendar", lambda start, end: (items, context or {"configured": True,
                        "accounts": [{"label": "me", "lastSync": datetime.now().astimezone().isoformat()}]}))


def test_page(monkeypatch, tmp_path):
    fake_calendar(monkeypatch)
    made = agenda.build(DAY, 2, tmp_path / "a.pdf")
    assert made["name"] == "Agenda 2026-10-26 (Monday)"
    assert made["days"][0] == {"date": "2026-10-26", "allDay": 3, "events": 5, "tasks": 1}
    with pymupdf.open(made["path"]) as d:
        assert d.page_count == 2
        page = d[0]
        assert abs(page.rect.width - send.PAGE_W) < 0.5 and abs(page.rect.height - send.PAGE_H) < 0.5
        text = page_text(page)
        for s in ("Monday 26 October", "Week 44", "ALL DAY", "SCHEDULE", "TO DO", "NOTES", "Staff meeting",
                  "08:00–09:00", "1212", "Journal", "day 3 of 5"):
            assert s in text, s
        assert "Tuesday 27 October" in page_text(d[1])
        # Ruled notes lines run to the bottom of the page, in grey.
        rules = ruled_lines(page)
        assert len(rules) >= 4
        assert max(r["rect"].y1 for r in rules) > send.PAGE_H - agenda.MARGIN_BOTTOM - agenda.LINE_GAP - 20
        # Black and grey only.
        for p in page.get_drawings():
            for c in (p.get("color"), p.get("fill")):
                if c:
                    assert max(c) - min(c) < 0.01


def test_busy_day_keeps_room_for_notes(monkeypatch, tmp_path):
    many = [ev(f"Meeting {h}", f"2026-10-26T{h:02d}:00:00+01:00", f"2026-10-26T{h:02d}:30:00+01:00")
            for h in range(6, 23)]
    fake_calendar(monkeypatch, many)
    made = agenda.build(DAY, 1, tmp_path / "busy.pdf")
    with pymupdf.open(made["path"]) as d:
        assert d.page_count == 1
        text = page_text(d[0])
        assert "more on the calendar" in text and "NOTES" in text
        rules = ruled_lines(d[0])
        assert len(rules) >= 4


def test_page_without_calendar(monkeypatch, tmp_path):
    monkeypatch.setattr(agenda, "calendar_cli", lambda: None)
    made = agenda.build(DAY, 1, tmp_path / "x.pdf")
    assert "isn't installed" in made["note"]
    with pymupdf.open(made["path"]) as d:
        text = page_text(d[0])
        assert "Nothing on the calendar" in text and "isn't installed" in text
    with pytest.raises(agenda.AgendaError):
        agenda.build(DAY, 1, tmp_path / "y.pdf", strict=True)


def test_send_and_skip(monkeypatch, fake_cloud):
    fake_calendar(monkeypatch)
    result = agenda.send(DAY, 1, "Daily")
    assert result["ok"] and result["skipped"] is False and result["name"] == "Agenda 2026-10-26 (Monday)"
    assert fake_cloud.uploads[0]["folder"] == "Daily" and fake_cloud.uploads[0]["bytes"][:5] == b"%PDF-"
    again = agenda.send(DAY, 1, "Daily")
    assert again["ok"] and again["skipped"] is True and again["name"] == "Agenda 2026-10-26 (Monday)"
    assert len(fake_cloud.uploads) == 1
    agenda.send(DAY, 1, "Omarchy Plugin Test")  # another folder doesn't count
    assert len(fake_cloud.uploads) == 2


SETTINGS = {"agendaEnabled": True, "agendaTime": "06:00", "agendaDays": 1, "agendaFolder": "Daily"}


def test_maybe_run(monkeypatch, fake_cloud):
    fake_calendar(monkeypatch)
    state: dict = {}
    assert agenda.maybe_run({**SETTINGS, "agendaEnabled": False}, state, datetime(2026, 10, 26, 7, 0)) is None
    assert agenda.maybe_run(SETTINGS, state, datetime(2026, 10, 26, 5, 59)) is None
    assert fake_cloud.uploads == []
    result = agenda.maybe_run(SETTINGS, state, datetime(2026, 10, 26, 6, 0))
    assert result and result["name"] == "Agenda 2026-10-26 (Monday)" and result["ok"]
    assert state["agendaLastDate"] == "2026-10-26"
    assert agenda.maybe_run(SETTINGS, state, datetime(2026, 10, 26, 9, 0)) is None
    assert len(fake_cloud.uploads) == 1
    # Next morning; and a page someone already sent counts as done.
    fake_cloud.add("Daily/Agenda 2026-10-27 (Tuesday)")
    assert agenda.maybe_run(SETTINGS, state, datetime(2026, 10, 27, 6, 30)) is None
    assert state["agendaLastDate"] == "2026-10-27" and len(fake_cloud.uploads) == 1


def test_maybe_run_failure_backs_off(monkeypatch, fake_cloud):
    calls = []

    def broken(start, end):
        calls.append(start)
        raise agenda.AgendaError("couldn't read the calendar: calendar service is not running")
    monkeypatch.setattr(agenda, "read_calendar", broken)
    state: dict = {}
    t0 = datetime(2026, 10, 26, 6, 0)
    assert agenda.maybe_run(SETTINGS, state, t0) is None
    assert "calendar service is not running" in state["agendaError"] and "agendaLastDate" not in state
    assert agenda.maybe_run(SETTINGS, state, t0 + timedelta(minutes=5)) is None  # backing off
    assert len(calls) == 1
    for i in range(1, agenda.STRICT_ATTEMPTS):
        agenda.maybe_run(SETTINGS, state, t0 + timedelta(minutes=16 * i))
    assert fake_cloud.uploads == []
    # After a few tries the page goes out anyway, with the reason printed on it.
    result = agenda.maybe_run(SETTINGS, state, t0 + timedelta(hours=2))
    assert result and "calendar service is not running" in result["note"]
    assert state["agendaLastDate"] == "2026-10-26" and "agendaError" not in state
    assert len(fake_cloud.uploads) == 1


def test_maybe_run_never_raises(monkeypatch):
    monkeypatch.setattr(agenda, "send", lambda *a, **k: 1 / 0)
    state: dict = {}
    assert agenda.maybe_run(SETTINGS, state, datetime(2026, 10, 26, 6, 0)) is None
    assert state["agendaError"] == "division by zero"
