"""iPhone Duo pickup watcher, GitHub cloud copy (Jake 2026-10-22).

Runs inside a GitHub Actions job: loops for ~5h45m, checking Apple's public store-pickup
lookup every ~30 s from the runner's IP (not Jake's home Wi-Fi, which Apple throttled at 8:56 PM).
Alerts go straight to Jake's phone through ntfy (topic in the NTFY_TOPIC secret).
It never buys anything: the alert opens his pre-loaded Apple bag and he pays with Apple Pay.
"""
import json, os, random, re, subprocess, time

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
PARTS = {
    "MK1E4LL/A": "256GB Star White", "MK1F4LL/A": "256GB Night Sky",
    "MK1G4LL/A": "512GB Star White", "MK1H4LL/A": "512GB Night Sky",
    "MK1J4LL/A": "1TB Star White", "MK1L4LL/A": "1TB Night Sky",
    "MK1Q4LL/A": "2TB Star White", "MK1R4LL/A": "2TB Night Sky",
}
RANK = {p: i for i, p in enumerate(PARTS)}   # PARTS runs cheapest first, so a lower rank is the cheaper model
PREFERRED = "MK1E4LL/A"
ALERT_PARTS = {"MK1E4LL/A", "MK1F4LL/A"}   # 10/25 Jake swaps the 1TB for a 256GB before the return window: only the 256GB models alert
STORES = {"R205": "First Colony", "R058": "Houston Galleria", "R164": "Memorial City", "R437": "Highland Village", "R321": "Willowbrook", "R204": "Baybrook", "R126": "The Woodlands"}
LOCATIONS = ["77002"]   # 10/24: the Houston query alone returns all six stores; the Austin one timed out every time, which made every run hand off (the run storm)
BAG_URL = "https://www.apple.com/shop/bag"


def model_link(part):
    """The exact in-stock model's buy page (10/24 Jake lost a 512GB reconfiguring the 256GB in his bag)."""
    gb, color = PARTS[part].lower().split(" ", 1)
    return f"https://www.apple.com/shop/buy-iphone/iphone-duo/7.6-inch-display-{gb}-{color.replace(' ', '-')}-unlocked"
TOPIC = os.environ.get("NTFY_TOPIC", "")
DIAG = os.environ.get("DIAG_TOPIC", "")
RUN_SECONDS = int(os.environ.get("RUN_SECONDS", str(345 * 60)))
REALERT_S = 60
MAC_QUIET_MIN = 30   # minutes without a hold report from a Mac on LIVE before his phone hears about it


def curl(url):
    r = subprocess.run(["curl", "-sL", "--max-time", "20", "-A", UA, "-H", "Accept: application/json",
                        "-w", "\n%{http_code}", url], capture_output=True, text=True)
    body, _, code = r.stdout.rpartition("\n")
    return code, body


def push(title, msg, priority="urgent", tags="iphone,rotating_light", click=BAG_URL, topic=None):
    topic = topic or TOPIC
    if not topic:
        return
    for attempt in range(3):   # a send that fails quietly is a missed alert: check ntfy's answer and retry
        r = subprocess.run(["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}", "--max-time", "15",
                            "-H", f"Title: {title}", "-H", f"Priority: {priority}",
                            "-H", f"Tags: {tags}", "-H", f"Click: {click}", "-d", msg, f"https://ntfy.sh/{topic}"],
                           capture_output=True, text=True)
        if r.stdout.strip() == "200":
            return
        time.sleep(2)


def hand_off():
    """Apple is refusing this runner's IP: start a fresh GitHub run (new machine, new IP) and exit.
    workflow_dispatch is the one event GITHUB_TOKEN is allowed to trigger."""
    repo, token = os.environ.get("GITHUB_REPOSITORY", ""), os.environ.get("GH_TOKEN", "")
    if not (repo and token):
        return False
    r = subprocess.run(["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}", "-X", "POST",
                        "-H", f"Authorization: Bearer {token}", "-H", "Accept: application/vnd.github+json",
                        f"https://api.github.com/repos/{repo}/actions/workflows/duo-watch.yml/dispatches",
                        "-d", '{"ref":"main"}'], capture_output=True, text=True)
    return r.stdout.strip() == "204"


OPEN_MARKER = "PICKUP_OPEN_SENT"


def announce_open_once():
    """Jake 10/23: tell him once when Apple opens pickup reservations (quotes stop saying 'Check back')."""
    if not DIAG:
        return
    r = subprocess.run(["curl", "-s", "--max-time", "10", f"https://ntfy.sh/{DIAG}/json?poll=1&since=48h"],
                       capture_output=True, text=True)
    if OPEN_MARKER in r.stdout:
        return
    push("Apple pickup is LIVE", "Apple just opened iPhone Duo pickup reservations. Nothing in stock at your stores yet; you'll get the next ping the second one is.",
         priority="high", tags="iphone")
    push("diag", OPEN_MARKER, priority="min", tags="gear", topic=DIAG)


def is_available(v):
    if v.get("pickupDisplay") == "available":
        return True
    q = ((v.get("messageTypes") or {}).get("regular") or {}).get("storePickupQuote") or v.get("pickupSearchQuote") or ""
    return False   # 10/24: only Apple's real "available" flag counts; "Available" text alone was a false alarm


def check():
    q = "&".join(f"parts.{i}={p}" for i, p in enumerate(PARTS))
    seen, hits, codes, opened = set(), [], [], False
    for loc in LOCATIONS:
        code, body = curl(f"https://www.apple.com/shop/retail/pickup-message?pl=true&{q}&location={loc}")
        codes.append(code)
        if code != "200":
            continue
        for s in json.loads(body)["body"].get("stores", []):
            num = s.get("storeNumber")
            if num not in STORES or num in seen:
                continue
            seen.add(num)
            for part, v in (s.get("partsAvailability") or {}).items():
                q = ((v.get("messageTypes") or {}).get("regular") or {}).get("storePickupQuote") or ""
                if part in PARTS and q and not q.lower().startswith("check back"):
                    opened = True
                if part in ALERT_PARTS and is_available(v):
                    hits.append((STORES[num], round(float(s.get("storedistance") or 0)), part, num))
    return seen, hits, codes, opened


def mac_check():
    """Jake 10/24: tell his phone if a Mac's Chrome hold goes silent (Chrome closed, Mac asleep, Wi-Fi down).
    Each hold reports on the diag topic every 10 minutes ("hold ok device=main mode=LIVE"); this runs on GitHub,
    so it still works when the Mac itself can't. Markers on the diag topic keep overlapping runs from repeating it."""
    r = subprocess.run(["curl", "-s", "--max-time", "10", f"https://ntfy.sh/{DIAG}/json?poll=1&since=6h"],
                       capture_output=True, text=True)
    last, quiet, back = {}, {}, {}
    for line in r.stdout.splitlines():
        try:
            m = json.loads(line)
        except ValueError:
            continue
        msg, t = m.get("message") or "", m.get("time", 0)
        dev = re.search(r"device=(\w+)", msg)
        if not dev:
            continue
        if msg.startswith("hold "):
            mode = re.search(r"mode=(\w+)", msg)
            last[dev.group(1)] = (t, mode.group(1) if mode else "?")
        elif msg.startswith("MAC_QUIET "):
            quiet[dev.group(1)] = t
        elif msg.startswith("MAC_BACK "):
            back[dev.group(1)] = t
    now = time.time()
    for d, (t, mode) in last.items():
        name = "the Mac" if d == "main" else "the M1"
        if now - t > MAC_QUIET_MIN * 60:
            if mode == "LIVE" and quiet.get(d, 0) < t:   # not told about this silence yet
                push(f"Duo: {name} has gone quiet", f"No report from Chrome on {name} for {round((now - t) / 60)} minutes. "
                     "Check it: Chrome open, hold tab in checkout, charger in, Wi-Fi on.", priority="high", tags="warning")
                push("diag", f"MAC_QUIET device={d}", priority="min", tags="gear", topic=DIAG)
        elif quiet.get(d, 0) > back.get(d, 0) and quiet.get(d, 0) < t:   # it went quiet, and has reported since
            push(f"Duo: {name} is back", "Chrome on it is reporting in again.", priority="default", tags="white_check_mark")
            push("diag", f"MAC_BACK device={d}", priority="min", tags="gear", topic=DIAG)


def main():
    start = time.time()
    alerted, fails, loops, found_total = {}, 0, 0, 0
    # 10/23 Jake: start-up pings were noise (35 in a day, one per machine switch). Status goes to the diag topic only;
    # his phone hears from this script ONLY when a phone is in stock (or when switching machines keeps failing).
    if DIAG:
        push("diag", f"run started: {len(STORES)} stores x {len(PARTS)} models", priority="min", tags="eyes", topic=DIAG)
    while time.time() - start < RUN_SECONDS:
        loops += 1
        try:
            seen, hits, codes, opened = check()
        except Exception as e:  # never die mid-run
            seen, hits, codes, opened = set(), [], [f"err:{e.__class__.__name__}"], False
        if False:   # 10/24: pickup opened 7:08 AM; this repeat check spammed "pickup is LIVE" every 30 s
            announce_open_once()
        ok = len(seen) >= len(STORES) - 1 and all(c == "200" for c in codes)
        fails = 0 if ok else fails + 1
        now = time.time()
        fresh = [h for h in hits if now - alerted.get(h[3] + h[2], 0) > REALERT_S]
        if fresh:
            found_total += 1
            # 10/24 Jake: "lowest gb variant preferred". The cheapest model in stock wins; among its stores, First Colony, then nearest.
            t = sorted(fresh, key=lambda h: (RANK[h[2]], h[0] != "First Colony", h[1]))[0]
            by_model = {}
            for h in sorted(fresh, key=lambda h: (RANK[h[2]], h[1])):
                by_model.setdefault(PARTS[h[2]], []).append(h[0])
            others = "; ".join(f"{m} at {', '.join(dict.fromkeys(s))}" for m, s in by_model.items())
            # 10/24: every alert opens the exact in-stock model; his Safari ADD bookmark puts it in the bag.
            title = f"iPhone Duo {'256GB STAR WHITE' if t[2] == PREFERRED else PARTS[t[2]]} IN STOCK: {t[0]}"
            msg = (f"Tap -> opens the {PARTS[t[2]]}. Tap your ADD bookmark (or No AppleCare + Add to Bag), "
                   f"Pickup at {t[0]}, Apple Pay. All stock: {others}")
            push(title, msg, click=model_link(t[2]))
            for h in fresh:
                alerted[h[3] + h[2]] = now
        if DIAG and loops % 10 == 0:   # about every 5 minutes
            try:
                mac_check()
            except Exception:
                pass
        if DIAG and (loops == 1 or loops % 20 == 0 or fresh or fails in (3, 10)):
            push("diag", f"topic_set={bool(TOPIC)} loop={loops} ok={ok} codes={codes} stores_seen={len(seen)} hits={len(hits)} fails={fails}",
                 priority="min", tags="gear", topic=DIAG)
        if fails == 8:    # hand-off failing and Apple still refusing
            if DIAG:   # 10/23 Jake: phone gets only "pickup is live" and "in stock"
                push("diag", "blocked 8 checks in a row and hand-off failing", priority="min", tags="warning", topic=DIAG)
        # 10/23 Jake: "Don't slow them, just switch the GitHub." Full speed; when Apple blocks this runner's IP,
        # hand off to a brand-new runner (new IP) and exit instead of waiting it out.
        if fails >= 2:
            young = time.time() - start < 120          # blocked straight away: pause so a bad streak can't loop fast
            if young:
                time.sleep(60)
            ok_dispatch = hand_off()
            if DIAG:
                push("diag", f"blocked after loop={loops}; handed off to a new runner: {ok_dispatch}", priority="min", tags="gear", topic=DIAG)
            if ok_dispatch:
                return
            time.sleep(120)                            # couldn't hand off: keep going here
            continue
        wait = 30
        time.sleep(wait + random.uniform(-4, 4))


if __name__ == "__main__":
    main()
