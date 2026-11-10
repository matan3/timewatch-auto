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
import random
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
        """Return [(date, day_type)] for days up to today flagged as missing."""
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
            cells = [c.get_text(" ", strip=True) for c in row.find_all("td")]
            day_type = cells[2] if len(cells) > 2 else ""
            jobs.append((d, day_type))
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

    @staticmethod
    def existing_problem(data):
        """Return a reason if the day already has anything filled in, else None."""
        times = [data.get(f"{k}{i}", "").strip() for i in range(5)
                 for k in ("ehh", "emm", "xhh", "xmm")]
        if any(times):
            return "already has entry/exit times (manual report?)"
        if data.get("excuse", "0").strip() not in ("", "0"):
            return "has an absence reason (vacation/sick/etc.)"
        if data.get("atype", "0").strip() not in ("", "0"):
            return "has an absence type set"
        if data.get("remark", "").strip():
            return "already has a remark"
        return None

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


WORKDAY_TYPES = ("יום עבודה", "חמישי")


def notify(alerts):
    """Print alerts and add them to the GitHub run summary. A failed run makes
    GitHub email the owner."""
    print("ATTENTION - please check these days in TimeWatch:")
    for a in alerts:
        print(f"  ! {a}")
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    if topic:
        try:
            requests.post("https://ntfy.sh/", json={
                "topic": topic,
                "title": "TimeWatch – צריך לבדוק",
                "message": "\n".join(alerts),
                "priority": 4,
                "tags": ["warning"],
                "click": "https://github.com/matan3/timewatch-auto/actions",
            }, timeout=15).raise_for_status()
            print("Phone notification sent.")
        except requests.RequestException as e:
            print(f"Phone notification failed: {e}")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as f:
            f.write("### ⚠️ Please check these days in TimeWatch\n\n")
            f.writelines(f"- {a}\n" for a in alerts)


def add_time(start, dur):
    t = datetime(2000, 1, 1, *start) + timedelta(hours=dur[0], minutes=dur[1])
    if t.day != 1:
        raise TWError("Exit time would pass midnight - check START_TIME / hours.")
    return t.hour, t.minute


def main():
    company = env("TW_COMPANY", required=True)
    employee = env("TW_EMPLOYEE", required=True)
    password = env("TW_PASSWORD", required=True)
    base_start = parse_hhmm(env("START_TIME", "09:00"), "START_TIME")
    jitter = int(env("START_JITTER_MIN", "20"))
    day_len = parse_hhmm(env("DAY_LENGTH", "9:00"), "DAY_LENGTH")
    project = env("PROJECT_CODE", required=True)
    home_days = {x.strip().lower()[:3] for x in env("HOME_DAYS", "mon,wed").split(",")}
    default_hours = parse_hhmm(env("DEFAULT_HOURS", "9:00"), "DEFAULT_HOURS")
    dry = env("DRY_RUN", "false").lower() in ("1", "true", "yes")

    now = datetime.now(TZ)
    today = now.date()
    print(f"Today (Israel): {today}  |  dry run: {dry}")

    # Scheduled runs: start at a random minute inside the run window (Israel time).
    if env("SCHEDULED", "false").lower() == "true":
        win = env("RUN_WINDOW", "20:00-21:00")
        w0, w1 = (parse_hhmm(x, "RUN_WINDOW") for x in win.split("-"))
        w_start = now.replace(hour=w0[0], minute=w0[1], second=0, microsecond=0)
        w_end = now.replace(hour=w1[0], minute=w1[1], second=0, microsecond=0)
        if not (w_start - timedelta(minutes=45) <= now < w_end):
            print(f"Outside the {win} window ({now:%H:%M}) - skipping this trigger.")
            return 0
        target = w_start + timedelta(seconds=random.randint(0, int((w_end - w_start).total_seconds()) - 60))
        wait = (target - datetime.now(TZ)).total_seconds()
        if wait > 0:
            print(f"Waiting until {target:%H:%M} before reporting...")
            time.sleep(wait)

    tw = TimeWatch(company, employee, password)
    tw.login()

    if env("TEST_ALERT", "false").lower() == "true":
        notify(["Test alert - this is only a test, nothing was changed in TimeWatch."])
        raise TWError("Test alert (intentional failure so GitHub sends an email).")

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

    failed, alerts, handled, submitted = [], [], set(), set()
    for attempt in range(4):
        html = tw.month_page()
        jobs = [(d, t) for d, t in tw.missing_days(html, today, None)
                if d.isoformat() not in handled]
        if not jobs:
            break
        if attempt == 3:
            save_debug("month.html", html)
            raise TWError("Some days are still missing after 3 tries - please check in the app.")
        print(f"Days to fill: {len(jobs)}" if attempt == 0 else f"Retrying {len(jobs)} day(s)")
        for d, day_type in jobs:
            ds = d.isoformat()
            if ds in submitted:
                alerts.append(f"{ds} ({d:%a}): sent, but TimeWatch still shows it as missing")
                handled.add(ds)
                continue
            if d.weekday() in (4, 5):  # Friday, Saturday - never filled
                print(f"  skip {ds} ({d:%a}) - weekend")
                handled.add(ds)
                continue
            if day_type not in WORKDAY_TYPES:
                alerts.append(f"{ds} ({d:%a}): day type is '{day_type}', not a regular work day")
                handled.add(ds)
                continue
            data, _ = tw.edit_form(ds)
            problem = tw.existing_problem(data)
            if problem:
                alerts.append(f"{ds} ({d:%a}): {problem} - left untouched")
                handled.add(ds)
                continue
            rnd = random.Random(f"{ds}-{employee}")
            start = add_time(base_start, (0, rnd.randint(0, jitter)))
            end = add_time(start, day_len)
            location = "בית" if d.strftime("%a").lower() in home_days else "לקוח"
            label = (f"{ds} ({d:%a})  {start[0]:02d}:{start[1]:02d} -> "
                     f"{end[0]:02d}:{end[1]:02d}  location={location}")
            if dry:
                print(f"  [dry run] would report {label}")
                handled.add(ds)
                continue
            ok = tw.submit(d, start, end, project, location)
            submitted.add(ds)
            print(f"  {'reported' if ok else 'REJECTED'} {label}")
            if not ok:
                failed.append(ds)
                handled.add(ds)
            time.sleep(0.5)
        if dry:
            break
        time.sleep(2)

    if failed:
        alerts.append(f"TimeWatch refused: {', '.join(failed)} - please fill by hand")
    if alerts:
        notify(alerts)
        if not dry:
            raise TWError("Some days need your attention (see above).")
        return 0
    if dry:
        print("Dry run finished - nothing was sent.")
        return 0
    print("Done - nothing missing up to today.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (TWError, requests.RequestException) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
