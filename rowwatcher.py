#!/usr/bin/env python3
"""
rowwatcher.py — BookMyShow per-seat availability watcher (rows A–G by default).

Verified pipeline (2026-10-30, BMS production):
  1. Load /movies/<city>/<event-slug>/buytickets/<EVENT>/<DATE> in a HEADFUL
     browser (Cloudflare blocks headless and the app's own /v5 dynamic API is
     not reproducible; but the SSR'd DOM has every venue + venueCode, and the
     GET  /api/movies-data/seatlayout/v1/primary?eventCode&dateCode&regionCode&venueCode
     returns JSON with all sessionIds + showtimes + categories for that venue.
  2. For each session open
     /movies/<regionSlug>/seat-layout/<EVENT>/<VENUECODE>/<sessionId>/<DATE>
     wait for the Konva canvas seat map, then walk the Konva node tree:
     every seat Group carries attrs.seatObj {rowId, seatNumber, seatStatus,...}
     — seatStatus 1 = Available, 2 = Booked/Held.
  3. Count status==1 seats whose rowId is in the watched rows; compare with the
     state file; email alerts when seats newly appear.

Email config via env (same keys as checker.py):
  BMS_SMTP_HOST (default smtp.gmail.com), BMS_SMTP_PORT (587),
  BMS_SMTP_USER, BMS_SMTP_PASS, BMS_TO_EMAIL, BMS_FROM_EMAIL
"""
import argparse
import json
import os
import smtplib
import ssl
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

IST = timezone(timedelta(hours=5, minutes=30))


def now_ist() -> str:
    return datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S IST")


# ------------------------------------------------------------------ konva
KONVA_WALK_JS = r"""
() => {
  const K = window.Konva;
  if (!K || !K.stages) return { seats: [], ok: false, reason: 'no Konva global' };
  const seats = [];
  const walk = (node, depth) => {
    if (depth > 8 || seats.length > 4000) return;
    let at = null;
    try { at = node.getAttrs(); } catch (e) { at = null; }
    if (at && at.seatObj) {
      let o = at.seatObj;
      if (typeof o === 'string') { try { o = JSON.parse(o); } catch (e) { o = null; } }
      if (o && o.rowId) {
        seats.push({
          row: o.rowId, num: o.seatNumber, status: o.seatStatus, type: o.seatType,
          area: o.areaCode, price: o.curPrice,
        });
      }
    }
    let kids = null;
    try { if (typeof node.getChildren === 'function') kids = node.getChildren(); } catch (e) { kids = null; }
    if (kids) for (const c of kids) walk(c, depth + 1);
  };
  for (const st of K.stages) for (const layer of st.getLayers() || []) walk(layer, 0);
  return { seats, ok: true };
}
"""

KONVA_READY_JS = r"""
() => {
  const K = window.Konva;
  if (!K || !K.stages || !K.stages.length) return 0;
  let n = 0;
  const walk = (node, d) => {
    if (d > 7 || n > 3) return;
    try { if (node.getAttrs && node.getAttrs().seatObj) { n += 1; return; } } catch (e) {}
    let kids = null;
    try { if (typeof node.getChildren === 'function') kids = node.getChildren(); } catch (e) { kids = null; }
    if (kids) for (const c of kids) walk(c, d + 1);
  };
  for (const st of K.stages) for (const ln of st.getLayers() || []) walk(ln, 0);
  return n;
}
"""

VENUE_LIST_JS = r"""
() => {
  const out = [];
  const seen = new Set();
  for (const a of document.querySelectorAll('a[href]')) {
    const href = a.getAttribute('href') || '';
    const m = href.match(/\/buytickets\/([A-Z0-9]{4})(?:\/(\d{8}))?/);
    if (!m) continue;
    const code = m[1];
    if (seen.has(code)) continue;
    // venue slug from cinema-page style URLs only (exclude the movie page itself)
    const cinema = href.match(/\/cinemas\/[^/]+\/([^/]+)\/buytickets\//);
    if (!cinema) continue;
    seen.add(code);
    out.push({ code, slug: cinema[1] });
  }
  return out;
}
"""


def rows_wanted(spec: str):
    out = []
    for part in spec.replace(" ", "").split(","):
        if "-" in part and len(part) == 3:
            out += [chr(c) for c in range(ord(part[0].upper()), ord(part[2].upper()) + 1)]
        elif part:
            out.append(part.upper())
    return out


def seat_hits(seats, wanted_rows):
    wanted = set(wanted_rows)
    hits = [s for s in seats
            if str(s.get("status")) == "1" and str(s.get("row", "")).upper() in wanted]
    hits.sort(key=lambda s: (s.get("row", ""), int(s.get("num", 0)) if str(s.get("num", "")).isdigit() else 10**6))
    return hits


def by_row(hits):
    d = {}
    for s in hits:
        d.setdefault(str(s.get("row", "")).upper(), []).append(str(s.get("num")))
    return dict(sorted(d.items()))


def format_rows(d) -> str:
    return "; ".join(f"Row {r}: {', '.join(nums[:12])}{'…' if len(nums) > 12 else ''}" for r, nums in d.items())


def read_state(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}


def write_state(path: Path, state: dict) -> None:
    path.write_text(json.dumps(state, indent=1, sort_keys=True))


def tidy_name(slug: str) -> str:
    keep_lower = {"of", "the", "at", "and", "in"}
    words = slug.replace("-", " ").split()
    return " ".join(w if i == 0 or w.lower() not in keep_lower else w.lower()
                    for i, w in enumerate(words))


class Watcher:
    def __init__(self, args):
        self.a = args
        self.state = read_state(args.state)
        self.first_run = not args.state.exists()
        self.summary_lines = []
        self.alerts = []

    def log(self, msg):
        print(f"[{now_ist()}] {msg}", flush=True)

    # -------------------------------------------------------------- main
    def run(self):
        from playwright.sync_api import sync_playwright

        t_start = time.time()
        with sync_playwright() as pw:
            launch_kwargs = {
                "headless": self.a.headless,
                "args": ["--disable-blink-features=AutomationControlled", "--disable-infobars"],
            }
            browser, flavor = None, "bundled"
            for channel in [os.getenv("BMS_BROWSER_CHANNEL") or "chrome", None]:
                try:
                    kw = dict(launch_kwargs)
                    if channel:
                        kw["channel"] = channel
                    browser = pw.chromium.launch(**kw)
                    flavor = channel or "bundled"
                    break
                except Exception:
                    browser = None
            if browser is None:
                raise RuntimeError("no browser could be launched")
            self.log(f"browser: {flavor} headless={self.a.headless}")

            ctx = browser.new_context(
                user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
                viewport={"width": 1500, "height": 950},
                locale="en-IN",
                timezone_id="Asia/Kolkata",
            )
            page = ctx.new_page()
            buy_url = (
                f"https://in.bookmyshow.com/movies/{self.a.city}/{self.a.event_slug}"
                f"/buytickets/{self.a.event}/{self.a.date}?etCodes={self.a.event}"
                f"&language={self.a.language}&refEventCode={self.a.event}"
            )
            self.log(f"opening {buy_url}")
            page.goto(buy_url, wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(6000)

            content = (page.content() or "").lower()
            if "attention required" in content or "cf-challenge" in content:
                self.log("CLOUDFLARE CHALLENGE — aborting cycle")
                return self.finish("blocked")
            if self.a.debug_dump:
                self.a.debug_dump.write_text(page.content())

            venues_js = page.evaluate(VENUE_LIST_JS)
            wanted = rows_wanted(self.a.rows)
            self.summary_lines.append(
                f"configure: event={self.a.event} date={self.a.date} rows={''.join(wanted)}")

            if not venues_js:
                self.log("no venues on page (shows may not be open yet)")
                self.summary_lines.append("- no sessions found for that date yet")
                return self.finish("no-sessions")

            for vi, v in enumerate(venues_js):
                if time.time() - t_start > self.a.budget:
                    self.log("budget reached — stopping")
                    break
                code = v["code"]
                name = tidy_name(v["slug"]) if not v["slug"].startswith("buytickets") else code
                sessions = self.sessions_for_venue(page, code, self.a.date)
                self.summary_lines.append(f"\n**{name}** (`{code}`)")
                if not sessions:
                    self.summary_lines.append("- no sessions (sold out / not open)")
                    continue
                venue_broken = 0
                for si, sess in enumerate(sessions):
                    if time.time() - t_start > self.a.budget:
                        self.log("budget reached inside venue")
                        break
                    if venue_broken >= 2:
                        self.log(f"  venue {code}: skipping remaining sessions (BMS seat map down)")
                        self.summary_lines.append(f"  - (remaining sessions skipped — BMS seat map unreachable)")
                        break
                    before = len(self.summary_lines)
                    key = f"{code}_{sess['sessionId']}"
                    self.check_session(page, name, code, sess, key, wanted)
                    tail = self.summary_lines[before:]
                    if any("⚠️" in t for t in tail):
                        venue_broken += 1

            try:
                browser.close()
            except Exception:
                pass
        return self.finish("ok")

    # ------------------------------------------------------ per venue/session
    def sessions_for_venue(self, page, venue_code: str, date_code: str):
        api = (
            f"https://in.bookmyshow.com/api/movies-data/seatlayout/v1/primary"
            f"?eventCode={self.a.event}&dateCode={date_code}&regionCode={self.a.region}"
            f"&venueCode={venue_code}"
        )
        try:
            raw = page.evaluate(
                """async (u) => { const r = await fetch(u, {credentials:'include'}); return {s: r.status, t: await r.text()} }""",
                api,
            )
        except Exception as e:
            self.log(f"  venue {venue_code}: fetch failed {e}")
            return []
        if raw.get("s") != 200:
            self.log(f"  venue {venue_code}: api status {raw.get('s')}")
            return None
        try:
            j = json.loads(raw["t"])
        except Exception:
            self.log(f"  venue {venue_code}: bad json")
            return []
        sessions = []
        for st in (j.get("data", {}) or {}).get("showTimes", []) or []:
            if str(st.get("availStatus")) != "1":
                continue
            cats = st.get("categories") or []
            cat = next(
                (c for c in cats if str(c.get("availStatus")) == "1"), (cats or [{}])[0])
            sessions.append({
                "sessionId": st.get("sessionId"),
                "showTime": st.get("showTime"),
                "areaCode": (cat or {}).get("areaCatCode"),
                "areaName": (cat or {}).get("priceDesc"),
                "price": (cat or {}).get("curPrice"),
            })
        sessions = [s for s in sessions if s.get("sessionId")]
        return sessions

    def check_session(self, page, venue_name, venue_code, sess, key, wanted_rows):
        url = (
            f"https://in.bookmyshow.com/movies/{self.a.slug}/seat-layout/"
            f"{self.a.event}/{venue_code}/{sess['sessionId']}/{self.a.date}"
        )
        self.log(f"checking {key} {sess.get('showTime','?')} …")
        page.goto(url, wait_until="domcontentloaded", timeout=45000)
        count = 0
        is_error = False
        deadline = time.time() + 30
        while time.time() < deadline:
            try:
                count = page.evaluate(KONVA_READY_JS)
            except Exception:
                count = 0
            if count > 0:
                break
            if "attention required" in (page.content() or "").lower():
                break
            page.wait_for_timeout(700)
        if count == 0:
            # BMS sometimes bounces the seatmap: "Sorry! Something is not
            # right... connectivity issue with the cinema (#5)". Click their
            # Refresh and try again; if it persists, mark UNKNOWN and never
            # touch state.
            is_error = "something is not right" in (page.content() or "").lower()
            for retry in range(2):
                if not is_error:
                    break
                self.log("  BMS error screen; pressing their Refresh")
                clicked = False
                try:
                    page.locator('div:has-text("Refresh")').filter(has_text="Refresh").first.click(timeout=4000)
                    clicked = True
                except Exception:
                    pass
                if not clicked:
                    try:
                        page.reload(wait_until="domcontentloaded", timeout=45000)
                    except Exception:
                        pass
                deadline = time.time() + 20
                while time.time() < deadline:
                    try:
                        count = page.evaluate(KONVA_READY_JS)
                    except Exception:
                        count = 0
                    if count > 0:
                        break
                    page.wait_for_timeout(700)
                if count > 0:
                    break
                is_error = "something is not right" in (page.content() or "").lower()
        seats = []
        if count:
            try:
                data = page.evaluate(KONVA_WALK_JS)
                seats = data.get("seats", [])
            except Exception as e:
                self.log(f"  walk failed: {e}")
        if not seats:
            reason = "BMS connectivity error" if is_error else "seat map never loaded"
            self.log(f"  UNKNOWN for {key}: {reason} (state untouched)")
            self.summary_lines.append(f"- `{key}` {sess.get('showTime','')}: ⚠️ {reason} (reported as unknown)")
            return
        hits = seat_hits(seats, wanted_rows)
        self.log(f"  seats={len(seats)} A–G-available={len(hits)}")
        self.push_result(key, venue_name, sess, hits, url, len(seats))

    # ------------------------------------------------------------ results
    def push_result(self, key, venue_name, sess, hits, url, total_seats):
        rowsmap = by_row(hits)
        prev = self.state.get(key)
        prev_n = int(prev) if isinstance(prev, (int, float)) else None
        self.state[key] = len(hits)
        if hits:
            self.state[f"{key}_detail"] = format_rows(rowsmap)[:400]

        rel = ""
        if prev_n is not None:
            rel = "  (NEW)" if len(hits) > prev_n else ("  (decreased)" if len(hits) < prev_n else "")
        self.summary_lines.append(
            f"- `{key}` {sess.get('showTime','')} · {sess.get('areaName','')} {sess.get('areaCode','')} "
            f"₹{sess.get('price','')} · A–G open: **{len(hits)}**{rel}"
        )
        if hits:
            self.summary_lines.append(f"  - {format_rows(rowsmap)}")

        if self.first_run:
            return
        if prev_n is None or len(hits) > prev_n:
            self.alerts.append({
                "key": key, "venue": venue_name, "time": sess.get("showTime", ""),
                "rows": rowsmap, "url": url, "area": sess.get("areaName"),
            })
            self.state[f"{key}_alerted_n"] = len(hits)
            self.state[f"{key}_alerted_at"] = now_ist()

    def finish(self, status: str):
        summary = "\n".join(self.summary_lines or ["(nothing)"])
        if self.a.summary:
            try:
                with open(self.a.summary, "a") as f:
                    f.write(f"\n### Row watcher {now_ist()} ({status})\n{summary}\n")
            except Exception as e:
                self.log(f"summary write failed: {e}")
        self.log(f"--- summary ({status}) ---\n{summary}")

        if self.alerts and not self.first_run:
            self.send_alerts()
        elif self.alerts:
            self.log("first run: baseline recorded, alerts suppressed")
        write_state(self.a.state, self.state)
        self.log(f"state saved → {self.a.state}")
        return 0

    def send_alerts(self):
        host = os.getenv("BMS_SMTP_HOST", "smtp.gmail.com")
        port = int(os.getenv("BMS_SMTP_PORT", "587"))
        user, pw = os.getenv("BMS_SMTP_USER", ""), os.getenv("BMS_SMTP_PASS", "")
        to = os.getenv("BMS_TO_EMAIL", "")
        frm = os.getenv("BMS_FROM_EMAIL") or user
        if not (user and pw and to):
            for al in self.alerts:
                self.log(f"ALERT (no email cfg): {al['venue']} {al['time']} → {format_rows(al['rows'])}")
            return
        ok_tail = (":465" in f"{host}:{port}")
        for al in self.alerts:
            msg = email_for(al, self.a)
            try:
                ctx = ssl.create_default_context()
                if port == 465:
                    server = smtplib.SMTP_SSL(host, port, context=ctx, timeout=40)
                else:
                    server = smtplib.SMTP(host, port, timeout=40)
                with server:
                    if port != 465:
                        server.starttls(context=ctx)
                    server.login(user, pw)
                    server.sendmail(frm, [to], msg.as_string())
                self.log(f"EMAIL SENT → {to} [rows A–G: {format_rows(al['rows'])}]")
                self.macos_notify(f"Rows A–G open! {al['venue']} {al['time']}")
            except Exception as e:
                self.log(f"email failed [{al['key']}]: {e}")

    def macos_notify(self, text):
        if not self.a.mac_notify:
            return
        try:
            subprocess.run(["osascript", "-e",
                            f'display notification "{text}" with title "BMS rows watch" sound name "Glass"'],
                           capture_output=True, timeout=6)
        except Exception:
            pass


def email_for(al, a) -> MIMEMultipart:
    rows = format_rows(al["rows"])
    buy = (f"https://in.bookmyshow.com/movies/{a.city}/{a.event_slug}/buytickets/{a.event}/{a.date}")
    plain = (f"Rows A–G just opened!\n\n{al['venue']} — {al['time']}\nSeats: {rows}\n"
             f"Open seat layout: {al['url']}\nBuy: {buy}\n(seat holds expire in ~2 minutes)\n")
    html = f"""<html><body style="font-family:-apple-system,Segoe UI,Arial,sans-serif">
  <h2 style="color:#d32f2f;margin:0 0 6px">🚨 Rows A–G open at {al['venue']} — {al['time']}</h2>
  <p style="margin:0 0 8px;color:#555">{a.event} · {a.date} · {al['area'] or ''}</p>
  <p style="font-size:16px;margin:0 0 10px"><b>Seats:</b> {rows}</p>
  <p style="margin:0 0 4px"><a href="{al['url']}" style="font-size:14px">Open the seat layout directly</a></p>
  <p style="margin:0 0 14px"><a href="{buy}" style="font-size:14px">Open Ticket page</a></p>
  <p style="color:#b71c1c"><b>Seats can vanish in seconds — go book now.</b></p></body></html>"""
    m = MIMEMultipart("alternative")
    m["Subject"] = f"[BMS] Rows A–G open: {al['venue']} — {al['time']} ({a.event} {a.date})"
    m["From"] = os.getenv("BMS_FROM_EMAIL") or os.getenv("BMS_SMTP_USER", "")
    m["To"] = os.getenv("BMS_TO_EMAIL", "")
    m.attach(MIMEText(plain, "plain"))
    m.attach(MIMEText(html, "html"))
    return m


def selftest(dump_path: str) -> int:
    data = json.loads(Path(dump_path).read_text())
    seats = []
    for s in data.get("seats", []):
        for anc in s.get("ancestor", []):
            attrs = anc.get("attrs") if isinstance(anc, dict) else None
            if isinstance(attrs, str) and "seatObj=" in attrs:
                try:
                    obj = json.loads(attrs.split("seatObj=", 1)[1])
                    seats.append({"row": obj.get("rowId"), "num": obj.get("seatNumber"),
                                  "status": str(obj.get("seatStatus"))})
                    break
                except Exception:
                    pass
    print(f"selftest: parsed {len(seats)} seats")
    hits = seat_hits(seats, rows_wanted("A-G"))
    print(f"A–G open: {len(hits)} → {hits[:8]}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Watch BookMyShow for rows A–G reopening.")
    ap.add_argument("--event", default=os.getenv("BMS_EVENT_CODE", "ET00523158"))
    ap.add_argument("--event-slug", default=os.getenv("BMS_EVENT_SLUG", "namit-malhotras-ramayana"))
    ap.add_argument("--city", default=os.getenv("BMS_CITY", "chennai"))
    ap.add_argument("--slug", default=os.getenv("BMS_SLUG", "chen"), help="region slug in seat-layout URLs")
    ap.add_argument("--region", default=os.getenv("BMS_REGION", "CHEN"))
    ap.add_argument("--language", default=os.getenv("BMS_LANGUAGE", "hindi"))
    ap.add_argument("--date", default=os.getenv("BMS_DATE", "20261105"))
    ap.add_argument("--rows", default=os.getenv("BMS_ROWS", "A-G"))
    ap.add_argument("--budget", type=float, default=float(os.getenv("BMS_BUDGET", "300")))
    ap.add_argument("--interval", type=int, default=int(os.getenv("BMS_INTERVAL", "0")),
                    help="loop mode: seconds between cycles")
    ap.add_argument("--state", default=os.getenv("BMS_STATE_FILE", "row_state.json"), type=Path)
    ap.add_argument("--summary", default=os.getenv("GITHUB_STEP_SUMMARY", ""))
    ap.add_argument("--debug-dump", default="", help="write page DOM to this path for debugging")
    ap.add_argument("--headless", dest="headless", action="store_true",
                    default=os.getenv("BMS_HEADLESS") == "1")
    ap.add_argument("--mac-notify", action="store_true", default=os.getenv("BMS_MAC_NOTIFY") == "1")
    ap.add_argument("--selftest", default="", help="test parsing against a konva-deep.json capture")
    args = ap.parse_args()

    if args.debug_dump:
        args.debug_dump = Path(args.debug_dump)
    args.budget = args.budget or 300.0

    if args.selftest:
        return selftest(args.selftest)

    while True:
        try:
            Watcher(args).run()
        except KeyboardInterrupt:
            return 0
        except Exception as e:
            print(f"[{now_ist()}] cycle crashed: {e}", flush=True)
        if args.interval <= 0:
            return 0
        time.sleep(args.interval)


if __name__ == "__main__":
    sys.exit(main())
