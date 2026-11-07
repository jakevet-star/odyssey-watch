"""Zelda Switch 2 restock alerts (Jake 2026-11-06).

Runs inside a GitHub Actions job for ~5h45m, then starts the next run itself, so the watch never stops (Jake 11/07:
no gaps, like the iPhone tracker; on 11/07 GitHub's schedule skipped 4 of 7 restarts and left it off for hours).
Nintendo, Target, Walmart, Amazon and GameStop forbid scripts on their sites, so this never touches them. It reads
the public Bluesky posts of restock trackers that post every drop with a link (Wario64 posted the Nov 5 Nintendo
Store drop at 11:07 AM), through Bluesky's open public API, and pushes each console drop to Jake's phone through ntfy
with a tap-to-open link. It never buys anything.
"""
import datetime as dt, hashlib, json, os, re, subprocess, time, urllib.parse

TOPIC = os.environ.get("NTFY_TOPIC", "")
DIAG = "zdiag-" + hashlib.sha256(TOPIC.encode()).hexdigest()[:16] if TOPIC else ""   # private health/dedupe channel
RUN_SECONDS = int(os.environ.get("RUN_SECONDS") or 345 * 60)   # then start the next run (a push run: after 2 min)
LIMIT_S = 353 * 60   # GitHub kills the job at 355 min: never run past this
RETRY_S, RECHECK_S = 60, 5 * 60   # hand-off failed: try again in a minute; worked but not replaced yet: try again
POLL_S = 20
FRESH_S = 15 * 60   # a drop lasts minutes: a post older than this is history, never an alert
REPEAT_S = 10 * 60   # both trackers post the same drop minutes apart: one ping per store per drop
FEED = "https://public.api.bsky.app/xrpc/app.bsky.feed.getAuthorFeed"
TRACKERS = ["wario64.bsky.social", "nowinstockus.bsky.social"]
STORES = [   # (words in the post, store name, its product page); the store named first in the post wins
    (("nintendo store", "my nintendo", "nintendo.com"), "Nintendo Store",
     "https://www.nintendo.com/us/store/products/nintendo-switch-2-the-legend-of-zelda-40th-anniversary-edition-121642/"),
    (("best buy", "bestbuy"), "Best Buy", "https://www.bestbuy.com/site/6691841.p?skuId=6691841"),
    (("target",), "Target", "https://www.target.com/p/-/A-1013322047"),
    (("walmart",), "Walmart", "https://www.walmart.com/ip/21002656445"),
    (("amazon", "amzn"), "Amazon", "https://www.amazon.com/dp/B0HJ6F8L6V"),
    (("gamestop",), "GameStop", ""),
    (("costco",), "Costco", ""),
    (("sam's club", "sams club"), "Sam's Club", ""),
]
NOT_CONSOLE = re.compile(r"controller|\bcase\b|pin set|amiibo|ocarina|adventure pack|figure|plush|art book|soundtrack")
HEADS_UP = re.compile(r"will be|tomorrow|tonight|starting|in-store|in stores|event|out of stock|sold out")


def clean(text):
    return re.sub("[™®©]", "", text.lower())


def is_console_post(text):
    """The 40th Anniversary console, not the Zelda Pro Controller, case, pins or Ocarina of Time."""
    t = clean(text)
    if "zelda" not in t or not ("40th" in t or "anniversary" in t):
        return False
    return "console" in t or ("switch 2" in t and not NOT_CONSOLE.search(t))


def store_of(text):
    t = clean(text)
    found = [(min(t.find(w) for w in words if w in t), name, link)
             for words, name, link in STORES if any(w in t for w in words)]
    return min(found)[1:] if found else ("", "")


def post_links(post):
    rec = post["record"]
    out = [f["uri"] for facet in rec.get("facets") or [] for f in facet.get("features", []) if f.get("uri")]
    ext = ((post.get("embed") or {}).get("external") or {}).get("uri")
    return list(dict.fromkeys(out + ([ext] if ext else [])))


def post_url(post):
    return f"https://bsky.app/profile/{post['author']['handle']}/post/{post['uri'].rsplit('/', 1)[-1]}"


def push(title, msg, click, post_link):
    body = {"topic": TOPIC, "title": title, "message": msg, "priority": 5, "tags": ["video_game", "rotating_light"],
            "click": click, "actions": [{"action": "view", "label": "Open post", "url": post_link}]}
    for _ in range(3):   # a send that fails quietly is a missed drop: check ntfy's answer and retry
        r = subprocess.run(["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}", "--max-time", "15",
                            "-d", json.dumps(body), "https://ntfy.sh"], capture_output=True, text=True)
        if r.stdout.strip() == "200":
            return True
        time.sleep(2)
    return False


def diag(msg):
    if DIAG:
        subprocess.run(["curl", "-s", "-o", "/dev/null", "--max-time", "10", "-H", "Priority: min", "-d", msg,
                        f"https://ntfy.sh/{DIAG}"], capture_output=True)


def already_sent():
    """Posts alerted by earlier runs (a new run takes over every few hours), so a drop never pings twice."""
    if not DIAG:
        return set()
    r = subprocess.run(["curl", "-s", "--max-time", "10", f"https://ntfy.sh/{DIAG}/json?poll=1&since=12h"],
                       capture_output=True, text=True)
    return set(re.findall(r'ZELDA_SENT (\S+?)"', r.stdout))


def hand_off():
    """Start the next run before this one ends. Its arrival cancels this run (one run at a time), and a post made
    in the few seconds between is still under 15 minutes old when the next run reads it, so no drop is missed.
    workflow_dispatch is the one event a run's own GITHUB_TOKEN may trigger; the schedule stays as a backup."""
    repo, token = os.environ.get("GITHUB_REPOSITORY", ""), os.environ.get("GH_TOKEN", "")
    if not (repo and token):
        return False
    r = subprocess.run(["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}", "--max-time", "15", "-X", "POST",
                        "-H", f"Authorization: Bearer {token}", "-H", "Accept: application/vnd.github+json",
                        f"https://api.github.com/repos/{repo}/actions/workflows/zelda-watch.yml/dispatches",
                        "-d", '{"ref":"main"}'], capture_output=True, text=True)
    return r.stdout.strip() == "204"


def feed(actor):
    url = FEED + "?" + urllib.parse.urlencode({"actor": actor, "limit": 15, "filter": "posts_with_replies"})
    r = subprocess.run(["curl", "-s", "--max-time", "15", url], capture_output=True, text=True)
    return [item["post"] for item in json.loads(r.stdout)["feed"]]


def check(sent, now, recent):
    """One pass over the trackers. recent maps (store, kind) to when it last pinged. Returns the alerts sent."""
    alerts = []
    for actor in TRACKERS:
        for post in feed(actor):
            text = post["record"].get("text", "")
            if post["uri"] in sent or not is_console_post(text):
                continue
            made = dt.datetime.fromisoformat(post["record"]["createdAt"].replace("Z", "+00:00")).timestamp()
            if now - made > FRESH_S:
                sent.add(post["uri"])
                continue
            store, link = store_of(text)
            links = post_links(post)
            click = link or (links[0] if links else post_url(post))
            kind = "heads-up" if HEADS_UP.search(clean(text)) else "IN STOCK"
            if store and now - recent.get((store, kind), 0) < REPEAT_S:
                sent.add(post["uri"])
                continue
            title = f"ZELDA SWITCH 2 {kind}: {store or 'see post'}"
            who = post["author"].get("displayName") or post["author"]["handle"]
            words = re.sub(r"#\w+\s*", "", text).strip()   # NowInStock opens with a row of hashtags
            msg = f"{words}\n\nvia {who}. Tap to open {store or 'the link'}, then buy on your phone."
            if push(title, msg, click, post_url(post)):   # not sent: stays unseen, so the next pass retries
                sent.add(post["uri"])
                recent[(store, kind)] = now
                diag(f"ZELDA_SENT {post['uri']}")
                alerts.append((title, click))
    return alerts


def main():
    start, loops, fails, recent = time.time(), 0, 0, {}
    next_hand_off, tries = start + RUN_SECONDS, 0
    sent = already_sent()
    diag(f"zelda run started ({os.environ.get('GITHUB_EVENT_NAME', 'local')}): trackers={','.join(TRACKERS)} "
         f"remembered={len(sent)} hand-off in {RUN_SECONDS // 60} min")
    while time.time() - start < LIMIT_S:
        loops += 1
        if time.time() >= next_hand_off:   # keep watching until the next run takes over
            tries += 1
            if tries in (1, 10, 100):
                diag(f"zelda hand-off try {tries} after loop={loops}")
            ok = hand_off()
            if not ok and tries in (1, 10, 100):
                diag(f"zelda hand-off try {tries} failed; retrying every minute, the schedule is the backup")
            next_hand_off = time.time() + (RECHECK_S if ok else RETRY_S)
        try:
            check(sent, time.time(), recent)
            fails = 0
        except Exception as e:   # never die mid-run
            fails += 1
            if fails in (3, 30):
                diag(f"zelda loop={loops} failing {fails}x: {e.__class__.__name__}")
        if loops % 180 == 0:   # about once an hour
            diag(f"zelda ok loop={loops} remembered={len(sent)}")
        time.sleep(POLL_S)


if __name__ == "__main__":
    main()
