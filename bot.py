#!/usr/bin/env python3
"""
Reddit -> Telegram channel bot.
Fetches new posts from a subreddit (r/GamingLeaksAndRumours by default)
and publishes them to a Telegram channel.

Two ways to fetch posts:
  1. RSS (default) - no keys needed, works out of the box.
  2. Reddit API (OAuth) - used when REDDIT_CLIENT_ID and REDDIT_CLIENT_SECRET are set.
     More reliable, provides flairs (Leak / Rumour / Verified ...) and image previews.

Python 3.8+ standard library only.
"""

import base64
import html
import json
import logging
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path


# ---------------------------------------------------------------- config ----

def load_dotenv(path: Path) -> None:
    """Minimal .env loader (KEY=VALUE) so python-dotenv is not required."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TG_CHANNEL = os.environ.get("TELEGRAM_CHANNEL_ID", "")          # @mychannel or -100123...
SUBREDDIT = os.environ.get("SUBREDDIT", "GamingLeaksAndRumours")
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "120"))      # seconds
FIRST_RUN_POST = int(os.environ.get("FIRST_RUN_POST", "0"))      # how many posts to send on the very first run
ALLOWED_FLAIRS = [f.strip().lower() for f in os.environ.get("ALLOWED_FLAIRS", "").split(",") if f.strip()]
MIN_SCORE = int(os.environ.get("MIN_SCORE", "0"))                # API mode only
REDDIT_CLIENT_ID = os.environ.get("REDDIT_CLIENT_ID", "")
REDDIT_CLIENT_SECRET = os.environ.get("REDDIT_CLIENT_SECRET", "")
USER_AGENT = os.environ.get("USER_AGENT", "python:glr-telegram-bot:v1.0 (by /u/your_username)")
STATE_FILE = Path(os.environ.get("STATE_FILE", BASE_DIR / "seen.json"))
SNIPPET_LEN = int(os.environ.get("SNIPPET_LEN", "350"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("glr-bot")


# ------------------------------------------------------------------ http ----

def http_request(url, data=None, headers=None, method=None, timeout=30):
    headers = {"User-Agent": USER_AGENT, **(headers or {})}
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


# ----------------------------------------------------------------- state ----

class SeenStore:
    """Stores IDs of already processed posts to avoid duplicates."""

    MAX = 2000

    def __init__(self, path: Path):
        self.path = path
        self.ids = []
        self.first_run = not path.exists()
        if not self.first_run:
            try:
                self.ids = json.loads(path.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                log.warning("Could not read %s, starting from scratch", path)
                self.ids = []
        self._set = set(self.ids)

    def __contains__(self, post_id):
        return post_id in self._set

    def add(self, post_id):
        if post_id in self._set:
            return
        self.ids.append(post_id)
        self._set.add(post_id)
        if len(self.ids) > self.MAX:
            removed = self.ids[: len(self.ids) - self.MAX]
            self.ids = self.ids[-self.MAX:]
            self._set.difference_update(removed)

    def save(self):
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.ids), encoding="utf-8")
        tmp.replace(self.path)


# ---------------------------------------------------------------- reddit ----

class Post:
    def __init__(self, id, title, permalink, url=None, author=None, flair=None,
                 text="", score=None, image=None, created=0.0):
        self.id = id
        self.title = title
        self.permalink = permalink
        self.url = url              # external link (source), if any
        self.author = author
        self.flair = flair
        self.text = text
        self.score = score
        self.image = image
        self.created = created

    def __repr__(self):
        return f"Post({self.id!r}, {self.title[:40]!r})"


class RedditRSS:
    """Keyless mode: reads the /new/.rss Atom feed."""

    NS = {"a": "http://www.w3.org/2005/Atom", "media": "http://search.yahoo.com/mrss/"}

    def __init__(self, subreddit):
        self.url = f"https://www.reddit.com/r/{subreddit}/new/.rss?limit=50"

    def fetch(self):
        raw = http_request(self.url)
        return self.parse(raw)

    @classmethod
    def parse(cls, raw):
        root = ET.fromstring(raw)
        posts = []
        for e in root.findall("a:entry", cls.NS):
            pid = (e.findtext("a:id", "", cls.NS) or "").strip()
            title = html.unescape(e.findtext("a:title", "", cls.NS) or "").strip()
            link_el = e.find("a:link", cls.NS)
            permalink = link_el.get("href") if link_el is not None else ""
            author = (e.findtext("a:author/a:name", "", cls.NS) or "").replace("/u/", "")
            content = e.findtext("a:content", "", cls.NS) or ""
            published = e.findtext("a:published", "", cls.NS) or e.findtext("a:updated", "", cls.NS) or ""
            thumb = e.find("media:thumbnail", cls.NS)

            # content holds HTML: post body + [link] and [comments] anchors
            ext_url = None
            m = re.search(r'<a href="([^"]+)">\[link\]</a>', content)
            if m:
                candidate = html.unescape(m.group(1))
                if "/comments/" not in candidate or "reddit.com" not in candidate:
                    ext_url = candidate
            text = cls._html_to_text(content)

            posts.append(Post(
                id=pid,
                title=title,
                permalink=permalink,
                url=ext_url,
                author=author or None,
                text=text,
                image=thumb.get("url") if thumb is not None else None,
                created=_parse_iso(published),
            ))
        return posts

    @staticmethod
    def _html_to_text(content):
        # strip the "submitted by ... [link] [comments]" footer
        content = re.sub(r"&#32;|\s*submitted by.*$", " ", content, flags=re.S)
        content = re.sub(r"<br\s*/?>|</p>|</li>", "\n", content)
        content = re.sub(r"<[^>]+>", "", content)
        content = html.unescape(content)
        content = re.sub(r"[ \t]+", " ", content)
        content = re.sub(r"\n\s*\n+", "\n\n", content)
        return content.strip()


class RedditAPI:
    """Keyed mode: official API via app-only OAuth."""

    def __init__(self, subreddit, client_id, client_secret):
        self.subreddit = subreddit
        self.client_id = client_id
        self.client_secret = client_secret
        self.token = None
        self.token_expires = 0

    def _auth(self):
        if self.token and time.time() < self.token_expires - 60:
            return
        creds = base64.b64encode(f"{self.client_id}:{self.client_secret}".encode()).decode()
        raw = http_request(
            "https://www.reddit.com/api/v1/access_token",
            data=urllib.parse.urlencode({"grant_type": "client_credentials"}).encode(),
            headers={"Authorization": f"Basic {creds}"},
            method="POST",
        )
        data = json.loads(raw)
        if "access_token" not in data:
            raise RuntimeError(f"Reddit OAuth error: {data}")
        self.token = data["access_token"]
        self.token_expires = time.time() + int(data.get("expires_in", 3600))
        log.info("Obtained Reddit OAuth token")

    def fetch(self):
        self._auth()
        url = f"https://oauth.reddit.com/r/{self.subreddit}/new?limit=50&raw_json=1"
        try:
            raw = http_request(url, headers={"Authorization": f"bearer {self.token}"})
        except urllib.error.HTTPError as err:
            if err.code == 401:          # token expired - refresh on next call
                self.token = None
            raise
        return self.parse(raw)

    @staticmethod
    def parse(raw):
        data = json.loads(raw)
        posts = []
        for child in data.get("data", {}).get("children", []):
            d = child.get("data", {})
            if d.get("stickied"):
                continue
            ext_url = None
            if not d.get("is_self") and d.get("url"):
                u = d["url"]
                if "reddit.com" not in u and "redd.it" not in u:
                    ext_url = u
            image = None
            try:
                image = d["preview"]["images"][0]["source"]["url"]
            except (KeyError, IndexError, TypeError):
                if str(d.get("url", "")).lower().endswith((".jpg", ".jpeg", ".png", ".gif", ".webp")):
                    image = d["url"]
            posts.append(Post(
                id=d.get("name", ""),
                title=d.get("title", "").strip(),
                permalink="https://www.reddit.com" + d.get("permalink", ""),
                url=ext_url,
                author=d.get("author"),
                flair=d.get("link_flair_text"),
                text=(d.get("selftext") or "").strip(),
                score=d.get("score"),
                image=image,
                created=float(d.get("created_utc") or 0),
            ))
        return posts


def _parse_iso(s):
    from datetime import datetime
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except (ValueError, AttributeError):
        return 0.0


# -------------------------------------------------------------- telegram ----

class Telegram:
    def __init__(self, token, chat_id):
        self.base = f"https://api.telegram.org/bot{token}/"
        self.chat_id = chat_id

    def call(self, method, payload):
        body = json.dumps(payload).encode()
        for attempt in range(5):
            try:
                raw = http_request(self.base + method, data=body,
                                   headers={"Content-Type": "application/json"})
                return json.loads(raw)
            except urllib.error.HTTPError as err:
                info = {}
                try:
                    info = json.loads(err.read())
                except ValueError:
                    pass
                retry = (info.get("parameters") or {}).get("retry_after")
                if err.code == 429 and retry:
                    log.warning("Telegram flood limit, waiting %s s", retry)
                    time.sleep(int(retry) + 1)
                    continue
                raise RuntimeError(f"Telegram {method} failed: {err.code} {info or err}") from err
            except urllib.error.URLError as err:
                log.warning("Network error (%s), retry %d/5", err, attempt + 1)
                time.sleep(5)
        raise RuntimeError(f"Telegram {method}: too many retries")

    def check(self):
        me = self.call("getMe", {})
        log.info("Bot: @%s", me["result"]["username"])

    def send_post(self, post: Post):
        text = format_post(post)
        preview_url = post.url or post.image or post.permalink
        self.call("sendMessage", {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": "HTML",
            "link_preview_options": {
                "url": preview_url,
                "prefer_large_media": True,
            },
        })


def esc(s):
    return html.escape(s or "", quote=False)


FLAIR_EMOJI = {
    "leak": "🚨",
    "rumour": "👀",
    "rumor": "👀",
    "confirmed": "✅",
    "verified": "✅",
    "datamine": "⛏",
    "removed": "🗑",
    "speculation": "🤔",
    "opinion": "💭",
}


def format_post(p: Post) -> str:
    emoji = "🎮"
    head = ""
    if p.flair:
        key = p.flair.lower()
        emoji = next((v for k, v in FLAIR_EMOJI.items() if k in key), emoji)
        head = f"[{esc(p.flair)}] "

    parts = [f"{emoji} <b>{head}{esc(p.title)}</b>"]

    if p.text:
        snippet = p.text if len(p.text) <= SNIPPET_LEN else p.text[:SNIPPET_LEN].rsplit(" ", 1)[0] + "…"
        parts.append(esc(snippet))

    links = []
    if p.url:
        links.append(f'🔗 <a href="{esc(p.url)}">Source</a>')
    links.append(f'💬 <a href="{esc(p.permalink)}">Reddit thread</a>')
    parts.append(" | ".join(links))

    text = "\n\n".join(parts)
    return text[:4096]


# ------------------------------------------------------------------ main ----

def passes_filters(p: Post) -> bool:
    if ALLOWED_FLAIRS:
        if not p.flair or not any(f in p.flair.lower() for f in ALLOWED_FLAIRS):
            return False
    if MIN_SCORE and p.score is not None and p.score < MIN_SCORE:
        return False
    return True


def run_once(source, tg: Telegram, seen: SeenStore):
    posts = source.fetch()
    posts.sort(key=lambda p: p.created)          # oldest -> newest

    if seen.first_run:
        to_send = posts[-FIRST_RUN_POST:] if FIRST_RUN_POST > 0 else []
        for p in posts:
            if p not in to_send:
                seen.add(p.id)
        log.info("First run: %d posts marked as seen, %d will be sent",
                 len(posts) - len(to_send), len(to_send))
        seen.first_run = False
        seen.save()
        posts = to_send

    sent = 0
    for p in posts:
        if not p.id or p.id in seen:
            continue
        if passes_filters(p):
            try:
                tg.send_post(p)
                sent += 1
                log.info("Sent: %s", p.title[:80])
                time.sleep(3)                     # stay under channel rate limits
            except RuntimeError as err:
                log.error("Failed to send %s: %s", p.id, err)
                continue                          # retry on the next cycle
        seen.add(p.id)
        seen.save()
    if sent == 0:
        log.info("No new posts")


def main():
    if not TG_TOKEN or not TG_CHANNEL:
        sys.exit("Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHANNEL_ID (in .env or environment)")

    if REDDIT_CLIENT_ID and REDDIT_CLIENT_SECRET:
        source = RedditAPI(SUBREDDIT, REDDIT_CLIENT_ID, REDDIT_CLIENT_SECRET)
        log.info("Mode: Reddit API (OAuth)")
    else:
        source = RedditRSS(SUBREDDIT)
        log.info("Mode: RSS (no keys)")
        if ALLOWED_FLAIRS:
            log.warning("ALLOWED_FLAIRS only works in API mode - RSS has no flairs, filter disabled")
            ALLOWED_FLAIRS.clear()

    tg = Telegram(TG_TOKEN, TG_CHANNEL)
    tg.check()
    seen = SeenStore(STATE_FILE)
    log.info("Watching r/%s every %d s", SUBREDDIT, POLL_INTERVAL)

    once = "--once" in sys.argv
    while True:
        try:
            run_once(source, tg, seen)
        except urllib.error.HTTPError as err:
            log.error("Reddit returned %s %s", err.code, err.reason)
            if err.code == 429:
                time.sleep(POLL_INTERVAL)
        except Exception as err:                  # keep the bot alive
            log.exception("Loop error: %s", err)
        if once:
            break
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log.info("Stopped")
