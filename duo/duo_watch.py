"""iPhone Duo pickup watcher, GitHub cloud copy (Jake 2026-10-22).

Runs inside a GitHub Actions job: loops for ~5h45m, checking Apple's public store-pickup
lookup every ~30 s from the runner's IP (not Jake's home Wi-Fi, which Apple throttled at 8:56 PM).
Alerts go straight to Jake's phone through ntfy (topic in the NTFY_TOPIC secret).
It never buys anything: the alert opens his pre-loaded Apple bag and he pays with Apple Pay.
"""
import json, os, random, subprocess, time

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
PARTS = {
    "MK1E4LL/A": "256GB Star White", "MK1F4LL/A": "256GB Night Sky",
    "MK1G4LL/A": "512GB Star White", "MK1H4LL/A": "512GB Night Sky",
    "MK1J4LL/A": "1TB Star White", "MK1L4LL/A": "1TB Night Sky",
    "MK1Q4LL/A": "2TB Star White", "MK1R4LL/A": "2TB Night Sky",
}
PREFERRED = "MK1E4LL/A"
STORES = {"R205": "First Colony", "R058": "Houston Galleria", "R164": "Memorial City",
          "R437": "Highland Village", "R216": "Domain Northside (Austin)", "R085": "Barton Creek (Austin)"}
LOCATIONS = ["77002", "78701"]          # downtown Houston + downtown Austin; together they cover all six stores
BAG_URL = "https://www.apple.com/shop/bag"
TOPIC = os.environ.get("NTFY_TOPIC", "")
DIAG = os.environ.get("DIAG_TOPIC", "")
RUN_SECONDS = int(os.environ.get("RUN_SECONDS", str(345 * 60)))
REALERT_S = 60


def curl(url):
    r = subprocess.run(["curl", "-sL", "--max-time", "20", "-A", UA, "-H", "Accept: application/json",
                        "-w", "\n%{http_code}", url], capture_output=True, text=True)
    body, _, code = r.stdout.rpartition("\n")
    return code, body


def push(title, msg, priority="urgent", tags="iphone,rotating_light", click=BAG_URL, topic=None):
    topic = topic or TOPIC
    if not topic:
        return
    subprocess.run(["curl", "-s", "--max-time", "15", "-H", f"Title: {title}", "-H", f"Priority: {priority}",
                    "-H", f"Tags: {tags}", "-H", f"Click: {click}", "-d", msg, f"https://ntfy.sh/{topic}"],
                   capture_output=True)


def is_available(v):
    if v.get("pickupDisplay") == "available":
        return True
    q = ((v.get("messageTypes") or {}).get("regular") or {}).get("storePickupQuote") or v.get("pickupSearchQuote") or ""
    return q.lower().startswith(("available", "today at", "pick up today", "pickup today"))


def check():
    q = "&".join(f"parts.{i}={p}" for i, p in enumerate(PARTS))
    seen, hits, codes = set(), [], []
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
                if part in PARTS and is_available(v):
                    hits.append((STORES[num], round(float(s.get("storedistance") or 0)), part, num))
    return seen, hits, codes


def main():
    start = time.time()
    alerted, fails, loops, found_total = {}, 0, 0, 0
    push("Cloud watcher on (GitHub)", f"Checking {len(STORES)} stores x {len(PARTS)} models about every 30 s for the next ~6 h.",
         priority="min", tags="eyes")
    while time.time() - start < RUN_SECONDS:
        loops += 1
        try:
            seen, hits, codes = check()
        except Exception as e:  # never die mid-run
            seen, hits, codes = set(), [], [f"err:{e.__class__.__name__}"]
        ok = len(seen) >= len(STORES) - 1 and all(c == "200" for c in codes)
        fails = 0 if ok else fails + 1
        now = time.time()
        fresh = [h for h in hits if now - alerted.get(h[3] + h[2], 0) > REALERT_S]
        if fresh:
            found_total += 1
            pref = [h for h in fresh if h[2] == PREFERRED]
            t = sorted(pref or fresh, key=lambda h: (h[0] != "First Colony", h[1]))[0]
            others = "; ".join(sorted({f"{h[0]}: {PARTS[h[2]]}" for h in fresh}))
            if t[2] == PREFERRED:
                title = f"iPhone Duo 256GB STAR WHITE IN STOCK: {t[0]}"
                msg = f"It's the one in your bag. Tap, choose Pickup at {t[0]}, then Apple Pay. All stock: {others}"
            else:
                title = f"iPhone Duo {PARTS[t[2]]} IN STOCK: {t[0]}"
                msg = f"Tap to open your bag. Keep only the {PARTS[t[2]]}, choose Pickup at {t[0]}, then Apple Pay. All stock: {others}"
            push(title, msg)
            for h in fresh:
                alerted[h[3] + h[2]] = now
        if DIAG and (loops == 1 or loops % 20 == 0 or fresh or fails in (3, 10)):
            push("diag", f"topic_set={bool(TOPIC)} loop={loops} ok={ok} codes={codes} stores_seen={len(seen)} hits={len(hits)} fails={fails}",
                 priority="min", tags="gear", topic=DIAG)
        if fails == 20:   # ~10+ minutes of Apple refusing this runner
            push("Cloud watcher blocked", "Apple is refusing this GitHub machine. The next scheduled run starts on a fresh one within 3 hours.",
                 priority="high", tags="warning")
        wait = 30 if fails < 3 else 120
        time.sleep(wait + random.uniform(-4, 4))


if __name__ == "__main__":
    main()
