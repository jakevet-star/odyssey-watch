"""Zelda Switch 2 restock alerts (Jake 2026-11-06).

Runs inside a GitHub Actions job for ~5h45m. Nintendo, Target, Walmart, Amazon and GameStop forbid scripts on
their sites, so this never touches them. It reads the public Bluesky posts of restock trackers that post every
drop with a link (Wario64 posted the Nov 5 Nintendo Store drop at 11:07 AM), through Bluesky's open public API,
and pushes each console drop to Jake's phone through ntfy with a tap-to-open link. It never buys anything.
"""
import datetime as dt, hashlib, json, os, re, subprocess, time, urllib.parse

TOPIC = os.environ.get("NTFY_TOPIC", "")
DIAG = "zdiag-" + hashlib.sha256(TOPIC.encode()).hexdigest()[:16] if TOPIC else ""   # private health/dedupe channel
RUN_SECONDS = int(os.environ.get("RUN_SECONDS", str(345 * 60)))
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
    """Posts alerted by earlier runs (a new run takes over every 3 hours), so a drop never pings twice."""
    if not DIAG:
        return set()
    r = subprocess.run(["curl", "-s", "--max-time", "10", f"https://ntfy.sh/{DIAG}/json?poll=1&since=12h"],
                       capture_output=True, text=True)
    return set(re.findall(r'ZELDA_SENT (\S+?)"', r.stdout))


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
    sent = already_sent()
    diag(f"zelda run started: trackers={','.join(TRACKERS)} remembered={len(sent)}")
    while time.time() - start < RUN_SECONDS:
        loops += 1
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
