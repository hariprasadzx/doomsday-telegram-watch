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


def send(text: str) -> None:
    token = os.environ["TELEGRAM_BOT_TOKEN"].strip()
    chat = os.environ["TELEGRAM_CHAT_ID"].strip()
    data = urllib.parse.urlencode(
        {"chat_id": chat, "text": text[:4000], "disable_web_page_preview": "true"}
    ).encode()
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
            # Open District first, reload, then go to the movie page
            try:
                page.goto(HOME, wait_until="domcontentloaded", timeout=45_000)
                page.wait_for_timeout(2_000)
                page.reload(wait_until="domcontentloaded", timeout=45_000)
                page.wait_for_timeout(2_000)
            except Exception as e:  # noqa: BLE001
                print(f"Attempt {attempt}: home page error: {e}")
            resp = page.goto(URL, wait_until="domcontentloaded", timeout=60_000)
            status = resp.status if resp else None
            if status != 403 and "Access Denied" not in page.title():
                break
            print(f"Attempt {attempt}: District returned {status} (Access Denied)")
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
            Path("debug/page.html").write_text(page.content())
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
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {"seen": {}, "fail": 0}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")


def fmt_item(tag: str, s: dict) -> str:
    status = "bookable" if s["bookable"] else "no tickets yet"
    fmt = f" · {s['format']}" if s["format"] else ""
    star = "⭐ " if s["priority"] else ""
    return f"• {star}[{tag}] {s['theatre']} — {s['time']}{fmt} ({status})"


def main() -> None:
    test = "--test" in sys.argv
    state = load_state()

    try:
        text = fetch_text()
        all_theatres, shows = parse(text)
        error = None if all_theatres else "page loaded but no showtimes were found"
    except Exception as e:  # noqa: BLE001
        all_theatres, shows, error = [], {}, f"{type(e).__name__}: {e}"

    if error:
        print("Problem:", error)
        if test:
            send(f"⚠️ Doomsday watch test: {error}. District may be blocking this "
                 f"GitHub server or changed its page.\n{URL}")
        else:
            state["fail"] = state.get("fail", 0) + 1
            if state["fail"] == FAIL_ALERT_AFTER:
                send(
                    f"⚠️ Doomsday watch: {error} ({state['fail']} checks in a row). "
                    f"District may be blocking GitHub or changed its page.\n{URL}"
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
        prio = sorted({s["theatre"] for s in shows.values() if s["priority"]}) or ["none yet"]
        others = [t for t in all_theatres if t not in prio]
        msg = ("✅ Doomsday watch test run\n⭐ Priority theatres with shows:\n- "
               + "\n- ".join(prio)
               + f"\n\nOther theatres ({len(others)}):\n- " + "\n- ".join(others or ["none"]))
        if new_items:
            msg += "\n\nA normal run would alert:\n" + "\n".join(fmt_item(t, s) for t, s in new_items)
        send(msg)
        sys.exit(1 if SEND_FAILED else 0)

    if new_items:
        prio_n = sum(1 for _, s in new_items if s["priority"])
        head = ("🚨⭐ Avengers: Doomsday — PRIORITY theatre update" if prio_n
                else "🎬 Avengers: Doomsday — new shows in Chennai")
        lines = [head] + [fmt_item(t, s) for t, s in new_items]
        lines.append(f"\nBook: {URL}")
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
