#!/usr/bin/env python3
# nitter_to_telegram_runonce.py
# Run-once version for GitHub Actions (one iteration, then exit)
# Env vars required: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, NITTER_BASE
# Optional: POLL_INTERVAL (not used here), MAX_DOWNLOAD_BYTES (bytes), ACCOUNTS_FILE (default accounts.txt)

import os, time, json, pathlib, logging
from typing import List, Optional
import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse
from requests.adapters import HTTPAdapter, Retry

# --- config ---
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
NITTER_BASE = os.getenv("NITTER_BASE", "https://nitter.net").rstrip("/")
ACCOUNTS_FILE = os.getenv("ACCOUNTS_FILE", "accounts.txt")
STATE_FILE = os.getenv("STATE_FILE", "state.json")
MAX_DOWNLOAD_BYTES = int(os.getenv("MAX_DOWNLOAD_BYTES", str(48*1024*1024)))  # 48MB

if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
    raise SystemExit("Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID env vars")

TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"
TEMP_DIR = pathlib.Path("tmp_media")
TEMP_DIR.mkdir(exist_ok=True)

# logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

# requests session with retries
session = requests.Session()
retries = Retry(total=3, backoff_factor=1, status_forcelist=[500,502,503,504])
session.mount("https://", HTTPAdapter(max_retries=retries))
session.headers.update({"User-Agent": "nitter-to-tg-runonce/1.0"})

def read_accounts() -> List[str]:
    p = pathlib.Path(ACCOUNTS_FILE)
    if not p.exists():
        logging.error("accounts file missing: %s", ACCOUNTS_FILE)
        return []
    return [l.strip() for l in p.read_text().splitlines() if l.strip() and not l.startswith("#")]

def load_state() -> dict:
    p = pathlib.Path(STATE_FILE)
    if p.exists():
        try:
            return json.loads(p.read_text())
        except Exception:
            return {}
    return {}

def save_state(state: dict):
    pathlib.Path(STATE_FILE).write_text(json.dumps(state, ensure_ascii=False))

def nitter_user_url(username: str) -> str:
    return f"{NITTER_BASE}/{username}"

def fetch_html(url: str) -> Optional[str]:
    try:
        r = session.get(url, timeout=20)
        r.raise_for_status()
        return r.text
    except Exception as e:
        logging.warning("fetch_html failed %s: %s", url, e)
        return None

def parse_tweets_from_nitter(html: str):
    soup = BeautifulSoup(html, "lxml")
    tweets = []
    for div in soup.select("div.timeline-item"):
        tweet_link = None
        for a in div.find_all("a", href=True):
            if "/status/" in a["href"]:
                tweet_link = a["href"]
                break
        if not tweet_link:
            continue
        tid = tweet_link.split("/status/")[-1].split("?")[0]
        tweet_url = urljoin(NITTER_BASE, tweet_link)
        txt_el = div.select_one("div.tweet-content")
        text = txt_el.get_text(" ", strip=True) if txt_el else ""
        media_urls = []
        for img in div.select("img"):
            src = img.get("data-src") or img.get("src")
            if src:
                media_urls.append(urljoin(NITTER_BASE, src))
        for video in div.select("video"):
            src = video.get("src")
            if src:
                media_urls.append(urljoin(NITTER_BASE, src))
            for s in video.select("source"):
                ss = s.get("src")
                if ss:
                    media_urls.append(urljoin(NITTER_BASE, ss))
        tweets.append({"id": tid, "url": tweet_url, "text": text, "media": list(dict.fromkeys(media_urls))})
    return tweets

def get_head_size(url: str) -> Optional[int]:
    try:
        r = session.head(url, allow_redirects=True, timeout=15)
        if r.status_code == 200 and "Content-Length" in r.headers:
            return int(r.headers["Content-Length"])
    except Exception:
        return None
    return None

def download_media(url: str, dest: pathlib.Path) -> bool:
    try:
        with session.get(url, stream=True, timeout=60) as r:
            r.raise_for_status()
            with open(dest, "wb") as fh:
                for chunk in r.iter_content(8192):
                    if chunk:
                        fh.write(chunk)
                        if dest.stat().st_size > MAX_DOWNLOAD_BYTES + 5*1024*1024:
                            logging.info("download exceeded size, abort: %s", url)
                            return False
        return True
    except Exception as e:
        logging.warning("download_media failed %s: %s", url, e)
        return False

def tg_send_text(chat_id: str, text: str) -> bool:
    try:
        r = session.post(f"{TELEGRAM_API}/sendMessage", json={"chat_id": chat_id, "text": text, "disable_web_page_preview": False}, timeout=20)
        return r.ok
    except Exception as e:
        logging.warning("tg_send_text error: %s", e)
        return False

def tg_send_photo(chat_id: str, path: str, caption: Optional[str]=None) -> bool:
    try:
        files = {"photo": open(path, "rb")}
        data = {"chat_id": chat_id}
        if caption:
            data["caption"] = caption
        r = session.post(f"{TELEGRAM_API}/sendPhoto", data=data, files=files, timeout=60)
        return r.ok
    except Exception as e:
        logging.warning("tg_send_photo error: %s", e)
        return False

def tg_send_video(chat_id: str, path: str, caption: Optional[str]=None) -> bool:
    try:
        files = {"video": open(path, "rb")}
        data = {"chat_id": chat_id}
        if caption:
            data["caption"] = caption
        r = session.post(f"{TELEGRAM_API}/sendVideo", data=data, files=files, timeout=120)
        return r.ok
    except Exception as e:
        logging.warning("tg_send_video error: %s", e)
        return False

def handle_account(username: str, state: dict):
    logging.info("Checking %s", username)
    html = fetch_html(nitter_user_url(username))
    if not html:
        logging.warning("no html for %s", username)
        return
    tweets = parse_tweets_from_nitter(html)
    if not tweets:
        logging.info("no tweets parsed for %s", username)
        return
    tweets_sorted = sorted(tweets, key=lambda t: int(t["id"]))
    last_seen = state.get(username)
    to_send = [t for t in tweets_sorted if last_seen is None or int(t["id"]) > int(last_seen)]
    if not to_send:
        logging.info("no new tweets for %s", username)
        return
    for t in to_send:
        caption = f"{username} — {t['url']}\n\n{t['text']}"[:1024]
        sent_any = False
        if t['media']:
            for murl in t['media']:
                if murl.startswith("/"):
                    murl = urljoin(NITTER_BASE, murl)
                size = get_head_size(murl)
                if size and size > MAX_DOWNLOAD_BYTES:
                    logging.info("media too big %s -> sending link", murl)
                    tg_send_text(TELEGRAM_CHAT_ID, f"{caption}\n\nMedia too large to upload: {murl}")
                    sent_any = True
                    continue
                ext = pathlib.Path(urlparse(murl).path).suffix or ".bin"
                fname = TEMP_DIR / f"{username}_{t['id']}_{abs(hash(murl))}{ext}"
                ok = download_media(murl, fname)
                if not ok or not fname.exists():
                    logging.warning("failed download %s", murl)
                    tg_send_text(TELEGRAM_CHAT_ID, f"{caption}\n\n(Couldn't download media: {murl})")
                    continue
                if ext.lower() in [".jpg", ".jpeg", ".png", ".webp"]:
                    ok2 = tg_send_photo(TELEGRAM_CHAT_ID, str(fname), caption=None if sent_any else caption)
                elif ext.lower() in [".mp4", ".mov", ".webm", ".gif"]:
                    ok2 = tg_send_video(TELEGRAM_CHAT_ID, str(fname), caption=None if sent_any else caption)
                else:
                    ok2 = tg_send_text(TELEGRAM_CHAT_ID, f"{caption}\n\nMedia: {murl}")
                if ok2:
                    sent_any = True
                try:
                    fname.unlink()
                except Exception:
                    pass
        if not t['media']:
            tg_send_text(TELEGRAM_CHAT_ID, caption)
            sent_any = True
        if not sent_any:
            tg_send_text(TELEGRAM_CHAT_ID, f"{caption}\n\n(All media failed to send)")
        state[username] = t['id']
        time.sleep(1.0)

def main():
    accounts = read_accounts()
    if not accounts:
        logging.error("no accounts")
        return
    state = load_state()
    for acc in accounts:
        try:
            handle_account(acc, state)
        except Exception as e:
            logging.exception("error handling %s: %s", acc, e)
    save_state(state)
    logging.info("done")

if __name__ == "__main__":
    main()
