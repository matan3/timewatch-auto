#!/usr/bin/env python3
"""
Daily automatic hours report for TimeWatch (c.timewatch.co.il).

Logs in with your company number, employee number and password, opens this
month's attendance page, and fills every day up to today that TimeWatch marks
as "missing entry/exit" (חסרה כניסה/יציאה).

Days with an absence already reported (vacation, sick day, etc.) are not
flagged as missing by TimeWatch, so they are left untouched. Future days are
never filled.

Settings come from environment variables (GitHub Secrets / Variables):
  TW_COMPANY     company number            (required, secret)
  TW_EMPLOYEE    employee number           (required, secret)
  TW_PASSWORD    password                  (required, secret)
  START_TIME     entry time, e.g. 09:00    (default 09:00)
  END_TIME       exit time, e.g. 18:00     (optional; if empty, exit time =
                 entry time + the hours TimeWatch expects for that day)
  DEFAULT_HOURS  fallback day length if expected hours can't be read (default 9:00)
  DRY_RUN        "true" = only show what would be reported, change nothing
"""

import os
import re
import sys
import time
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

BASE = "https://c.timewatch.co.il"
LOGIN_URL = f"{BASE}/user/validate_user.php"
UPDATE_URL = f"{BASE}/punch/editwh3.php"
MISSING_TEXT = "חסרה כניסה/יציאה"
LIMITED_PUNCH = "limited punch"
TZ = ZoneInfo("Asia/Jerusalem")


class TWError(Exception):
    pass


def env(name, default=None, required=False):
    val = os.environ.get(name, "").strip()
    if not val:
        if required:
            raise TWError(f"Missing setting: {name}")
        return default
    return val


def parse_hhmm(text, name):
    m = re.fullmatch(r"(\d{1,2}):?(\d{2})", text.strip())
    if not m:
        raise TWError(f"{name} must look like 09:00 (got '{text}')")
    h, mi = int(m.group(1)), int(m.group(2))
    if h > 23 or mi > 59:
        raise TWError(f"{name} is not a valid time: '{text}'")
    return h, mi


def save_debug(name, html):
    os.makedirs("debug", exist_ok=True)
    with open(os.path.join("debug", name), "w", encoding="utf-8") as f:
        f.write(html)


class TimeWatch:
    def __init__(self, company, employee, password):
        self.company = company
        self.employee = employee
        self.password = password
        self.s = requests.Session()
        self.s.headers.update({
            "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
                          "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1",
            "Accept-Language": "he-IL,he;q=0.9,en;q=0.8",
        })
        self.punch_path = None
        self.employee_internal = None
        self.csrf = None

    def login(self):
        r = self.s.post(LOGIN_URL, data={
            "comp": self.company, "name": self.employee, "pw": self.password,
        }, timeout=30)
        r.raise_for_status()
        m = re.search(r'(/punch/editwh\.php\?[^"\']+)', r.text)
        if not m:
            save_debug("login.html", r.text)
            raise TWError("Login failed - check company number, employee number and password.")
        self.punch_path = m.group(1).replace("&amp;", "&")
        ee = re.search(r"[?&]ee=(\d+)", self.punch_path)
        if not ee:
            save_debug("login.html", r.text)
            raise TWError("Logged in, but couldn't find the internal employee id.")
        self.employee_internal = ee.group(1)
        print("Logged in.")

    def month_page(self):
        r = self.s.get(BASE + self.punch_path, timeout=30)
        r.raise_for_status()
        html = r.content.decode("utf-8", errors="replace")
        m = re.search(r'var csrf_token\s*=\s*"([^"]*)"', html)
        if not m:
            save_debug("month.html", html)
            raise TWError("Couldn't read the attendance page (no csrf token).")
        self.csrf = m.group(1)
        return html

    @staticmethod
    def missing_days(html, today, default_hours):
        """Return [(date, (hours, minutes))] for days up to today flagged as missing."""
        soup = BeautifulSoup(html, "html.parser")
        rows = soup.find_all("tr", class_="update-data") or soup.find_all("tr")
        jobs = []
        for row in rows:
            text = row.get_text(" ", strip=True)
            if MISSING_TEXT not in text:
                continue
            dm = re.search(r"(\d{2})-(\d{2})-(\d{4})", text)
            if not dm:
                continue
            d = date(int(dm.group(3)), int(dm.group(2)), int(dm.group(1)))
            if d > today:
                continue
            # Expected hours for the day: first H:MM / HH:MM value after the date.
            after = text[dm.end():]
            hm = re.search(r"(?<!\d)(\d{1,2}):(\d{2})(?!\d)", after)
            needed = (int(hm.group(1)), int(hm.group(2))) if hm else default_hours
            if needed == (0, 0):
                needed = default_hours
            jobs.append((d, needed))
        # de-duplicate, keep order
        seen, out = set(), []
        for d, n in jobs:
            if d not in seen:
                seen.add(d)
                out.append((d, n))
        return sorted(out)

    def edit_form(self, ds):
        """Read-only: load the edit form for a date (YYYY-MM-DD) and return its
        fields exactly as a browser would submit them, plus the selected task text."""
        url = (f"{BASE}/punch/editwh2.php?ie={self.company}&e={self.employee_internal}"
               f"&d={ds}&jd={ds}&tl={self.employee_internal}")
        r = self.s.get(url, headers={"Referer": BASE + self.punch_path}, timeout=30)
        r.raise_for_status()
        soup = BeautifulSoup(r.content.decode("utf-8", errors="replace"), "html.parser")
        data, task0_text = {}, ""
        for inp in soup.find_all("input"):
            name, typ = inp.get("name"), (inp.get("type") or "text").lower()
            if not name or typ in ("submit", "button", "image", "reset"):
                continue
            if typ in ("checkbox", "radio") and not inp.has_attr("checked"):
                continue
            data[name] = inp.get("value", "")
        for sel in soup.find_all("select"):
            opts = sel.find_all("option")
            chosen = next((o for o in opts if o.has_attr("selected")), opts[0] if opts else None)
            data[sel.get("name")] = chosen.get("value", "") if chosen else ""
            if sel.get("name") == "task0" and chosen:
                task0_text = chosen.get_text(strip=True)
        if "ehh0" not in data or "c" not in data:
            save_debug(f"form-{ds}.html", r.text)
            raise TWError(f"Couldn't read the edit form for {ds}.")
        return data, task0_text

    def inspect_form(self, ds):
        """Read-only. Print only what's needed: times, project code and location."""
        data, task_text = self.edit_form(ds)
        remark = data.get("remark", "")
        remark = remark if remark in ("בית", "לקוח", "") else "<other text>"
        print(f"  {ds}: {data.get('ehh0')}:{data.get('emm0')}-{data.get('xhh0')}:{data.get('xmm0')}"
              f"  task0={data.get('task0')}{' (project 350)' if '(350)' in task_text else ''}"
              f"  remark='{remark}'")

    def submit(self, d, start, end, project, location, dry=False):
        ds = d.strftime("%Y-%m-%d")
        data, _ = self.edit_form(ds)
        data.update({
            "ehh0": f"{start[0]:02d}", "emm0": f"{start[1]:02d}",
            "xhh0": f"{end[0]:02d}", "xmm0": f"{end[1]:02d}",
            "task0": project, "remark": location,
        })
        data.setdefault("csrf_token", self.csrf)
        if dry:
            return True
        headers = {
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "Origin": BASE,
            "Referer": (f"{BASE}/punch/editwh2.php?ie={self.company}&e={self.employee_internal}"
                        f"&d={ds}&jd={ds}&tl={self.employee_internal}"),
        }
        r = self.s.post(UPDATE_URL, data=data, headers=headers, timeout=30)
        r.raise_for_status()
        if r.text.strip() == LIMITED_PUNCH:
            return False
        return True


def add_time(start, dur):
    t = datetime(2000, 1, 1, *start) + timedelta(hours=dur[0], minutes=dur[1])
    if t.day != 1:
        raise TWError("Exit time would pass midnight - check START_TIME / hours.")
    return t.hour, t.minute


def main():
    company = env("TW_COMPANY", required=True)
    employee = env("TW_EMPLOYEE", required=True)
    password = env("TW_PASSWORD", required=True)
    start = parse_hhmm(env("START_TIME", "09:00"), "START_TIME")
    end_fixed = env("END_TIME", "18:00")
    end_fixed = parse_hhmm(end_fixed, "END_TIME") if end_fixed.lower() != "auto" else None
    project = env("PROJECT_CODE", required=True)
    home_days = {x.strip().lower()[:3] for x in env("HOME_DAYS", "mon,wed").split(",")}
    default_hours = parse_hhmm(env("DEFAULT_HOURS", "9:00"), "DEFAULT_HOURS")
    dry = env("DRY_RUN", "false").lower() in ("1", "true", "yes")

    today = datetime.now(TZ).date()
    print(f"Today (Israel): {today}  |  dry run: {dry}")

    tw = TimeWatch(company, employee, password)
    tw.login()

    if dry:
        # Diagnostic: show how the attendance page was read (dates and status only).
        soup = BeautifulSoup(tw.month_page(), "html.parser")
        rows = soup.find_all("tr", class_="update-data")
        print(f"Attendance rows found: {len(rows)}")
        for row in rows:
            text = row.get_text(" ", strip=True)
            dm = re.search(r"\d{2}-\d{2}-\d{4}", text)
            if dm:
                status = "MISSING" if MISSING_TEXT in text else "ok"
                print(f"  {dm.group(0)}  {status}")
        # Read-only: show how a few past days were reported (times, project, location).
        dates = (env("INSPECT_DATES") or "").split()
        if dates:
            print("Past reports (read-only):")
        for ds in dates:
            tw.inspect_form(ds)

    failed = []
    for attempt in range(4):
        html = tw.month_page()
        jobs = [(d, n) for d, n in tw.missing_days(html, today, default_hours)
                if d.strftime("%Y-%m-%d") not in failed]
        if not jobs:
            break
        if attempt == 3:
            save_debug("month.html", html)
            raise TWError("Some days are still missing after 3 tries - please check in the app.")
        print(f"Days to fill: {len(jobs)}" if attempt == 0 else f"Retrying {len(jobs)} day(s)")
        for d, needed in jobs:
            end = end_fixed or add_time(start, needed)
            location = "בית" if d.strftime("%a").lower() in home_days else "לקוח"
            label = (f"{d} ({d.strftime('%a')})  {start[0]:02d}:{start[1]:02d} -> "
                     f"{end[0]:02d}:{end[1]:02d}  location={location}")
            if dry:
                tw.submit(d, start, end, project, location, dry=True)  # reads the form only
                print(f"  [dry run] would report {label}")
                continue
            ok = tw.submit(d, start, end, project, location)
            print(f"  {'reported' if ok else 'REJECTED'} {label}")
            if not ok:
                failed.append(d.strftime("%Y-%m-%d"))
            time.sleep(0.5)
        if dry:
            return 0
        time.sleep(2)

    if failed:
        raise TWError(f"TimeWatch refused these days, please fill them by hand: {', '.join(failed)}")
    print("Done - nothing missing up to today.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (TWError, requests.RequestException) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
