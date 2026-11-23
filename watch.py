"""Avengers: Doomsday Chennai show watcher -> Telegram.

Renders the District listing page in headless Chromium and sends a Telegram
message only when something is new, at ANY Chennai theatre:
  - a theatre appears with showtimes, or gets a new showtime,
  - a show that had "No tickets" becomes bookable.
PRIORITY theatres are starred and listed first.
State is kept in state.json (committed back to the repo by the workflow).

Usage:  python watch.py          normal run (exit 3 if District blocked/empty)
        python watch.py --test   send a summary only; state is not changed
"""
import html
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

URL = os.environ.get(
    "DISTRICT_URL",
    "https://www.district.in/movies/avengers-doomsday-movie-tickets-in-chennai-MV176735"
    "?frmtid=Mm5dpqTqJz",
)
HOME = "https://www.district.in/movies/"
MOVIE_PATH = "/movies/avengers-doomsday-movie-tickets-in-chennai-MV176735"
ATTEMPTS = 3

# Priority theatres: display name -> lowercase keywords in the theatre name
WATCHLIST = {
    "Kamala": ["kamala"],
    "Rakki": ["rakki"],
    "Vettri": ["vettri", "vetri"],
    "Murugan": ["murugan"],
    "Varadharaja": ["varadharaja", "varadaraja"],
}

STATE_FILE = Path(__file__).with_name("state.json")
SEND_FAILED = False
FAIL_ALERT_AFTER = 10  # consecutive empty/failed checks (~20 min) before warning you

TIME_RE = re.compile(r"^\d{1,2}:\d{2}\s?[AP]M$", re.I)
TIME_ANY_RE = re.compile(r"\b\d{1,2}:\d{2}\s?[AP]M\b", re.I)
CUTOFF = "Top Cinemas in"  # generic theatre links below the listings: ignore


def send(text: str, button: bool = True) -> None:
    token = os.environ["TELEGRAM_BOT_TOKEN"].strip()
    chat = os.environ["TELEGRAM_CHAT_ID"].strip()
    payload = {
        "chat_id": chat,
        "text": text[:4000],
        "parse_mode": "HTML",
        "disable_web_page_preview": "true",
    }
    if button:
        payload["reply_markup"] = json.dumps(
            {"inline_keyboard": [[{"text": "🎟 Book on District", "url": URL}]]}
        )
    data = urllib.parse.urlencode(payload).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage", data=data
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            r.read()
    except urllib.error.HTTPError as e:
        hint = " (bad TELEGRAM_BOT_TOKEN secret?)" if e.code == 401 else ""
        print(f"Telegram send failed: HTTP {e.code}{hint}: {e.read()[:300]!r}")
        global SEND_FAILED
        SEND_FAILED = True


def esc(t: str) -> str:
    return html.escape(t, quote=False)


def short_name(theatre: str) -> str:
    """'PVR VR Mall, Anna Nagar, Chennai' -> 'PVR VR Mall, Anna Nagar'"""
    return re.sub(r",\s*Chennai\b", "", theatre).strip(" ,")


DATE_RE = re.compile(r"\b([A-Z]{3})\n(\d{1,2})\n(Mon|Tue|Wed|Thu|Fri|Sat|Sun)\b")


def show_date(text: str) -> str:
    m = DATE_RE.search(text)
    return f"{m.group(3)}, {m.group(2)} {m.group(1).title()}" if m else ""


def nice_format(f: str) -> str:
    t = f.title()
    for a, b in (("Ms ", "MS "), ("Imax", "IMAX"), ("4Dx", "4DX"), ("Pxl", "PXL"),
                 ("Bigpix", "BIGPIX"), ("Epiq", "EPIQ"), ("Screenx", "ScreenX"), ("4K", "4K")):
        t = t.replace(a, b)
    return t


def plural(n: int) -> str:
    return f"{n} show" + ("" if n == 1 else "s")


def render_shows(items) -> list:
    """items: [(tag, show)] -> lines grouped by theatre, priority first."""
    groups = {}
    for tag, sh in items:
        groups.setdefault((not sh["priority"], sh["theatre"]), []).append((tag, sh))
    out, section = [], None
    for (not_prio, theatre), rows in sorted(groups.items(), key=lambda kv: kv[0][0]):
        sec = "⭐ <b>PRIORITY</b>" if not not_prio else "🎬 <b>Other theatres</b>"
        if sec != section:
            out += ["", sec]
            section = sec
        out.append(f"<b>{esc(short_name(theatre))}</b>")
        for tag, sh in rows:
            status = "✅ Bookable" if sh["bookable"] else "⏳ No tickets yet"
            if tag == "NOW BOOKABLE":
                status = "🔓 <b>Now bookable</b>"
            fmt = f" · {esc(nice_format(sh['format']))}" if sh["format"] else ""
            out.append(f"   🕒 <code>{esc(sh['time'])}</code>{fmt} · {status}")
    return out


def fetch_text() -> str:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(
            args=["--disable-blink-features=AutomationControlled"]
        )
        ctx = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
            ),
            locale="en-IN",
            timezone_id="Asia/Kolkata",
            geolocation={"latitude": 13.0827, "longitude": 80.2707},
            permissions=["geolocation"],
            viewport={"width": 1366, "height": 900},
            extra_http_headers={"Accept-Language": "en-IN,en;q=0.9"},
        )
        ctx.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )
        page = ctx.new_page()
        resp = None
        for attempt in range(1, ATTEMPTS + 1):
            # 1) Start at District's main page, then its movies page
            try:
                root = page.goto("https://www.district.in/", wait_until="domcontentloaded", timeout=45_000)
                print(f"Attempt {attempt}: www.district.in HTTP {root.status if root else None}, "
                      f"title={page.title()!r}")
                page.wait_for_timeout(2_000)
                home = page.goto(HOME, wait_until="domcontentloaded", timeout=45_000, referer="https://www.district.in/")
                print(f"Attempt {attempt}: homepage HTTP {home.status if home else None}, "
                      f"title={page.title()!r}")
                page.wait_for_timeout(3_000)
            except Exception as e:  # noqa: BLE001
                print(f"Attempt {attempt}: homepage error: {e}")
            # 2) Navigate inside the site to the Doomsday page (like a person would)
            how = None
            try:
                # Click the Chennai Doomsday link on the homepage (JS click, so a
                # collapsed section covering it doesn't matter)
                with page.expect_navigation(wait_until="domcontentloaded", timeout=45_000) as nav:
                    clicked = page.evaluate(
                        """() => { const a = document.querySelector('a[href*="in-chennai-MV176735"]');
                                   if (!a) return false; a.click(); return true; }"""
                    )
                    if not clicked:
                        raise RuntimeError("Chennai Doomsday link not found on homepage")
                resp = nav.value
                how = "homepage link"
            except Exception as e:  # noqa: BLE001
                print(f"Attempt {attempt}: homepage link failed: {e}")
            if "in-chennai-MV176735" not in page.url:
                how = "direct URL (homepage as referrer)"
                resp = page.goto(URL, wait_until="domcontentloaded", timeout=60_000, referer=HOME)
            print(f"Attempt {attempt}: reached Doomsday page via {how}")
            blocked = "Access Denied" in page.title() or (resp is not None and resp.status == 403)
            if not blocked:
                break
            print(f"Attempt {attempt}: District returned Access Denied")
            resp = None
            if attempt < ATTEMPTS:
                page.wait_for_timeout(5_000 * attempt)
        try:
            page.wait_for_selector(r"text=/\d{1,2}:\d{2}\s?[AP]M/", timeout=30_000)
        except Exception:
            pass
        page.wait_for_timeout(3_000)
        text = page.inner_text("body")
        if not TIME_ANY_RE.search(text):
            # Diagnostics: show what GitHub's runner actually received
            print("HTTP status:", resp.status if resp else None)
            print("Final URL:", page.url)
            print("Title:", page.title())
            print("Body text (first 800 chars):", text[:800].replace("\n", " | "))
            Path("debug").mkdir(exist_ok=True)
            Path("debug/page.html").write_text(page.content(), encoding="utf-8")
            page.screenshot(path="debug/page.png", full_page=True)
        browser.close()
    return text


def is_theatre_line(line: str) -> bool:
    return "chennai" in line.lower() and not TIME_ANY_RE.search(line) and len(line) > 12


def parse(text: str):
    """Return (all_theatres, shows).

    shows: {key: {"theatre", "time", "format", "bookable", "priority"}}
    """
    first_time = TIME_ANY_RE.search(text)
    if not first_time:
        return [], {}
    cut = text.find(CUTOFF, first_time.start())
    if cut != -1:
        text = text[:cut]

    lines = [l.strip() for l in text.splitlines() if l.strip()]
    all_theatres, shows = [], {}
    label, watched = None, None

    for i, line in enumerate(lines):
        if is_theatre_line(line):
            low = line.lower()
            watched = next(
                (n for n, keys in WATCHLIST.items() if any(k in low for k in keys)), None
            )
            label = line
            continue
        if label and TIME_RE.match(line):
            if label not in all_theatres:
                all_theatres.append(label)
            no_tickets, fmt = False, ""
            for nxt in lines[i + 1 : i + 6]:
                if TIME_RE.match(nxt) or is_theatre_line(nxt):
                    break
                nl = nxt.lower()
                if "no tickets" in nl:
                    no_tickets = True
                elif nxt.isupper() and len(nxt) > 3 and not nl.startswith("screen"):
                    fmt = fmt or nxt
            key = f"{label} | {line.upper().replace(' ', '')}"
            shows[key] = {
                "theatre": label,
                "time": line.upper(),
                "format": fmt,
                "bookable": not no_tickets,
                "priority": bool(watched),
            }
    return all_theatres, shows


def load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {"seen": {}, "fail": 0}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main() -> None:
    test = "--test" in sys.argv
    state = load_state()

    try:
        text = fetch_text()
        date = show_date(text)
        all_theatres, shows = parse(text)
        error = None if all_theatres else "page loaded but no showtimes were found"
    except Exception as e:  # noqa: BLE001
        all_theatres, shows, error, date = [], {}, f"{type(e).__name__}: {e}", ""

    if error:
        print("Problem:", error)
        if test:
            send("⚠️ <b>Doomsday watch — test</b>\n\n"
                 f"District didn't return showtimes: <i>{esc(error)}</i>\n"
                 "It may be blocking this GitHub server or changed its page.")
        else:
            state["fail"] = state.get("fail", 0) + 1
            if state["fail"] == FAIL_ALERT_AFTER:
                send(
                    "⚠️ <b>Doomsday watch — not getting showtimes</b>\n\n"
                    f"{state['fail']} checks in a row failed: <i>{esc(error)}</i>\n"
                    "District may be blocking GitHub or changed its page. "
                    "Checks continue; you'll get alerts as soon as it works again."
                )
            save_state(state)
        sys.exit(1 if SEND_FAILED else 3)

    seen = dict(state.get("seen", {}))
    # First run after enabling all-theatre alerts: record the non-priority
    # shows already listed silently, so you are not flooded with old shows.
    seeding = state.get("v") != 2
    new_items = []
    for key, s in shows.items():
        if key not in seen:
            if not (seeding and not s["priority"]):
                new_items.append(("NEW", s))
        elif s["bookable"] and not seen[key]:
            new_items.append(("NOW BOOKABLE", s))
        seen[key] = s["bookable"]
    new_items.sort(key=lambda t: not t[1]["priority"])  # priority first

    print(f"{len(all_theatres)} theatres listed, {len(shows)} shows, "
          f"{len(new_items)} new" + (" (seeded others silently)" if seeding else ""))

    if test:
        by_theatre = {}
        for sh in shows.values():
            by_theatre.setdefault(sh["theatre"], []).append(sh)
        prio = [t for t in all_theatres if any(sh["priority"] for sh in by_theatre.get(t, []))]
        others = [t for t in all_theatres if t not in prio]

        def block(t):
            rows = [f"<b>{esc(short_name(t))}</b>"]
            for sh in by_theatre.get(t, []):
                status = "✅" if sh["bookable"] else "⏳"
                fmt = f" · {esc(nice_format(sh['format']))}" if sh["format"] else ""
                rows.append(f"   🕒 <code>{esc(sh['time'])}</code>{fmt} {status}")
            return rows

        lines = ["✅ <b>Doomsday watch — all shows now</b>",
                 (f"📅 {esc(date)} · " if date else "")
                 + f"{len(all_theatres)} theatres · {len(shows)} shows",
                 "", "⭐ <b>PRIORITY</b>"]
        for t in prio:
            lines += block(t)
        if not prio:
            lines.append("<i>No shows at your priority theatres yet</i>")
        lines += ["", "🎬 <b>Other theatres</b>"]
        for t in others:
            lines += block(t)
        lines += ["", "✅ bookable · ⏳ no tickets yet"]
        send("\n".join(lines))
        sys.exit(1 if SEND_FAILED else 0)

    if new_items:
        prio_n = sum(1 for _, sh in new_items if sh["priority"])
        head = ("🚨 <b>Avengers: Doomsday — PRIORITY theatre update!</b>" if prio_n
                else "🎬 <b>Avengers: Doomsday — new shows in Chennai</b>")
        lines = [head]
        if date:
            lines.append(f"📅 {esc(date)}")
        lines += render_shows(new_items)
        send("\n".join(lines))

    if not SEND_FAILED:  # keep items unseen if Telegram failed, so they retry
        state["seen"] = seen
        state["v"] = 2
    state["fail"] = 0
    save_state(state)
    if SEND_FAILED:
        sys.exit(1)


if __name__ == "__main__":
    main()
