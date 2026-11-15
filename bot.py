"""
Reel -> Spotify Telegram bot (runs free on GitHub Actions).

Share an Instagram reel link (or the reel's video file) to your bot.
It finds the background song(s) with Shazam, finds songs the creator
suggests (spoken, on-screen text, caption) with Gemini, and adds them
to your Spotify playlist.
You can also just type song names, one per line, as "Title - Artist".
"""
import asyncio
import base64
import glob
import json
import os
import re
import subprocess
import tempfile
import time
import urllib.parse
from difflib import SequenceMatcher

import requests


def env(name):
    return (os.environ.get(name) or "").strip()


TG_TOKEN = env("TELEGRAM_BOT_TOKEN")
ALLOWED_CHAT_ID = env("TELEGRAM_CHAT_ID")
GEMINI_KEY = env("GEMINI_API_KEY")
GEMINI_MODEL = env("GEMINI_MODEL") or "gemini-2.5-flash"
SP_ID = env("SPOTIFY_CLIENT_ID")
SP_SECRET = env("SPOTIFY_CLIENT_SECRET")
SP_REFRESH = env("SPOTIFY_REFRESH_TOKEN")
_pl = env("SPOTIFY_PLAYLIST_ID")
_m = re.search(r"playlist[/:]([A-Za-z0-9]+)", _pl)
SP_PLAYLIST = _m.group(1) if _m else _pl
IG_COOKIES = os.environ.get("IG_COOKIES") or ""

TG = f"https://api.telegram.org/bot{TG_TOKEN}"
URL_RE = re.compile(r"https?://(?:www\.)?instagram\.com/(?:reels?|p)/[\w-]+\S*")
SPOTIFY_READY = all([SP_ID, SP_SECRET, SP_REFRESH, SP_PLAYLIST])

HELP = (
    "Send me an Instagram reel link (or the reel video itself) and I'll add its songs "
    "to your Spotify playlist.\n\n"
    "You can also type song names, one per line, like:\nKesariya - Arijit Singh"
)


# ---------------- Telegram ----------------

def tg(method, **params):
    r = requests.post(f"{TG}/{method}", json=params, timeout=60)
    r.raise_for_status()
    return r.json()["result"]


def send(chat_id, text):
    for i in range(0, len(text), 4000):
        requests.post(
            f"{TG}/sendMessage",
            json={"chat_id": chat_id, "text": text[i:i + 4000], "disable_web_page_preview": True},
            timeout=60,
        )


def tg_download(file_id, dest):
    info = tg("getFile", file_id=file_id)
    url = f"https://api.telegram.org/file/bot{TG_TOKEN}/{info['file_path']}"
    with requests.get(url, stream=True, timeout=180) as r:
        r.raise_for_status()
        with open(dest, "wb") as f:
            for chunk in r.iter_content(1 << 16):
                f.write(chunk)
    return dest


# ---------------- Instagram ----------------

def ig_download(url, workdir):
    out = os.path.join(workdir, "reel.%(ext)s")
    cmd = ["yt-dlp", "--no-playlist", "--write-info-json", "-o", out, url]
    if IG_COOKIES.strip():
        cookie_path = os.path.join(workdir, "cookies.txt")
        with open(cookie_path, "w") as f:
            f.write(IG_COOKIES)
        cmd[1:1] = ["--cookies", cookie_path]
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    videos = [
        f for f in glob.glob(os.path.join(workdir, "reel.*"))
        if not f.endswith((".json", ".part", ".txt"))
    ]
    if p.returncode != 0 or not videos:
        raise RuntimeError((p.stderr or "download failed").strip()[-400:])
    info = {}
    info_files = glob.glob(os.path.join(workdir, "*.info.json"))
    if info_files:
        with open(info_files[0]) as f:
            info = json.load(f)
    return videos[0], info


# ---------------- Song finding ----------------

def shazam_songs(video):
    """Recognise music playing in the reel, 15-second chunk by chunk."""
    from shazamio import Shazam

    seg_dir = os.path.join(os.path.dirname(video), "segs")
    os.makedirs(seg_dir, exist_ok=True)
    try:
        subprocess.run(
            ["ffmpeg", "-loglevel", "error", "-y", "-i", video, "-vn", "-ac", "1", "-ar", "44100",
             "-f", "segment", "-segment_time", "15", os.path.join(seg_dir, "s%03d.mp3")],
            check=True, timeout=120,
        )
    except Exception:
        return []  # no audio track

    async def run():
        shazam = Shazam()
        recognise = getattr(shazam, "recognize", None) or shazam.recognize_song
        found = []
        for seg in sorted(glob.glob(os.path.join(seg_dir, "*.mp3")))[:12]:
            try:
                res = await recognise(seg)
            except Exception:
                continue
            t = (res or {}).get("track") or {}
            if t.get("title"):
                found.append({"title": t["title"], "artist": t.get("subtitle", ""),
                              "source": "playing in reel"})
            await asyncio.sleep(1)
        return found

    return asyncio.run(run())


GEMINI_PROMPT = """You are given an Instagram reel (video with audio).CAPTION_PART
List every song in it: songs the creator recommends or names (spoken aloud, shown as on-screen text, or written in the caption), plus any recognisable song that plays.
Return JSON only, in this form:
{"songs": [{"title": "...", "artist": "...", "how": "spoken | on-screen | caption | playing"}]}
Use each song's official title and main artist. Skip anything you are not sure is a real song. If there are none, return {"songs": []}."""


def gemini_songs(video, caption):
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=GEMINI_KEY)
    f = client.files.upload(file=video)
    for _ in range(60):
        f = client.files.get(name=f.name)
        state = getattr(f.state, "name", str(f.state))
        if state == "ACTIVE":
            break
        if state == "FAILED":
            raise RuntimeError("Gemini could not process the video")
        time.sleep(3)
    caption_part = f'\nCaption: """{caption[:3000]}"""' if caption else ""
    prompt = GEMINI_PROMPT.replace("CAPTION_PART", caption_part)
    resp = client.models.generate_content(
        model=GEMINI_MODEL,
        contents=[f, prompt],
        config=types.GenerateContentConfig(response_mime_type="application/json", temperature=0),
    )
    try:
        client.files.delete(name=f.name)
    except Exception:
        pass
    data = json.loads(resp.text or "{}")
    songs = []
    for s in data.get("songs", []):
        if (s.get("title") or "").strip():
            songs.append({"title": s["title"].strip(), "artist": (s.get("artist") or "").strip(),
                          "source": s.get("how") or "suggested"})
    return songs


def clean(s):
    s = re.sub(r"[\(\[].*?[\)\]]", " ", (s or "").lower())
    s = re.sub(r"\bfeat\.?.*$", " ", s)
    return " ".join(re.sub(r"[^\w\s]", " ", s).split())


def dedupe(songs):
    seen, out = set(), []
    for s in songs:
        key = clean(s["title"])
        if key and key not in seen:
            seen.add(key)
            out.append(s)
    return out


# ---------------- Spotify ----------------

def sp_token():
    auth = base64.b64encode(f"{SP_ID}:{SP_SECRET}".encode()).decode()
    r = requests.post(
        "https://accounts.spotify.com/api/token",
        data={"grant_type": "refresh_token", "refresh_token": SP_REFRESH},
        headers={"Authorization": f"Basic {auth}"},
        timeout=30,
    )
    r.raise_for_status()
    return r.json()["access_token"]


VARIANT_WORDS = ("lofi", "lo fi", "lo-fi", "flip", "remix", "slowed", "reverb", "sped up",
                 "cover", "karaoke", "instrumental", "mashup", "8d", "unplugged")


def score(it, title, artist):
    """Higher is better: title match + artist match, minus lofi/remix versions not asked for."""
    name = it["name"].lower()
    s = SequenceMatcher(None, clean(title), clean(it["name"])).ratio()
    artist_words = clean(artist).split()
    if artist_words:
        artists = " ".join(a["name"].lower() for a in it.get("artists", []))
        if artist_words[0] in artists:
            s += 0.5
    asked = (title + " " + artist).lower()
    if any(w in name and w not in asked for w in VARIANT_WORDS):
        s -= 0.6
    return s


def sp_search(tok, title, artist):
    h = {"Authorization": f"Bearer {tok}"}
    queries = []
    if artist:
        queries.append(f'track:"{title}" artist:"{artist}"')
    queries.append(f"{title} {artist}".strip())
    candidates = []
    for q in queries:
        r = requests.get("https://api.spotify.com/v1/search",
                         params={"q": q, "type": "track", "limit": 10}, headers=h, timeout=30)
        if r.status_code == 200:
            candidates += (r.json().get("tracks") or {}).get("items") or []
    if not candidates:
        return None
    best = max(candidates, key=lambda it: score(it, title, artist))
    if SequenceMatcher(None, clean(title), clean(best["name"])).ratio() >= 0.5:
        return best
    return None


def sp_playlist_uris(tok):
    h = {"Authorization": f"Bearer {tok}"}
    uris, url = set(), f"https://api.spotify.com/v1/playlists/{SP_PLAYLIST}/items?limit=50"
    while url:
        r = requests.get(url, headers=h, timeout=30)
        if r.status_code != 200:
            break
        d = r.json()
        for it in d.get("items", []):
            obj = it.get("item") or it.get("track") or {}
            if obj.get("uri"):
                uris.add(obj["uri"])
        url = d.get("next")
    return uris


def sp_add(tok, uris):
    r = requests.post(f"https://api.spotify.com/v1/playlists/{SP_PLAYLIST}/items",
                      json={"uris": uris}, headers={"Authorization": f"Bearer {tok}"}, timeout=30)
    r.raise_for_status()


def search_link(s):
    return "https://open.spotify.com/search/" + urllib.parse.quote(f"{s['title']} {s['artist']}".strip())


def label(s):
    return f"{s['title']} - {s['artist']}" if s["artist"] else s["title"]


def report(songs):
    if not songs:
        return "I couldn't find any songs in that one."
    if not SPOTIFY_READY:
        lines = [f"Found {len(songs)} song(s). Tap to open in Spotify:"]
        for s in songs:
            lines.append(f"\n{label(s)} ({s['source']})\n{search_link(s)}")
        return "\n".join(lines)

    tok = sp_token()
    existing = sp_playlist_uris(tok)
    added, already, missing, to_add = [], [], [], []
    for s in songs:
        t = sp_search(tok, s["title"], s["artist"])
        if not t:
            missing.append(s)
            continue
        name = f"{t['name']} - {', '.join(a['name'] for a in t.get('artists', []))}"
        link = (t.get("external_urls") or {}).get("spotify", "")
        if t["uri"] in existing or t["uri"] in to_add:
            already.append(name)
        else:
            to_add.append(t["uri"])
            added.append(f"{name} ({s['source']})\n{link}")
    if to_add:
        sp_add(tok, to_add)

    lines = []
    if added:
        lines.append("Added to your playlist:\n" + "\n\n".join(added))
    if already:
        lines.append("Already in your playlist:\n" + "\n".join(already))
    if missing:
        lines.append("Not found on Spotify (tap to search):\n" +
                     "\n".join(f"{label(s)}\n{search_link(s)}" for s in missing))
    return "\n\n".join(lines)


# ---------------- Message handling ----------------

def process(msg):
    chat_id = str(msg["chat"]["id"])
    if not ALLOWED_CHAT_ID or chat_id != ALLOWED_CHAT_ID:
        send(chat_id, f"This is a private bot. Your chat ID is {chat_id}")
        return

    text = (msg.get("text") or msg.get("caption") or "").strip()
    video = msg.get("video")
    doc = msg.get("document")
    if not video and doc and (doc.get("mime_type") or "").startswith("video"):
        video = doc
    url_match = URL_RE.search(text)

    if text.startswith("/") and not video:
        send(chat_id, HELP)
        return

    # Typed song names
    if not video and not url_match:
        songs = []
        for line in text.splitlines():
            line = line.strip(" -•\t")
            if line:
                parts = re.split(r"\s+[-–—]\s*|\s*[-–—]\s+", line, maxsplit=1)
                title = parts[0]
                artist = parts[1] if len(parts) > 1 else ""
                songs.append({"title": title.strip(), "artist": artist.strip(), "source": "typed"})
        send(chat_id, report(dedupe(songs)) if songs else HELP)
        return

    send(chat_id, "Got it, finding the songs. This takes a minute or two.")
    with tempfile.TemporaryDirectory() as work:
        caption, songs = "", []
        if video:
            if (video.get("file_size") or 0) > 20 * 1024 * 1024:
                send(chat_id, "That video is over 20 MB, which Telegram bots can't download. "
                              "Try a shorter clip or send the link instead.")
                return
            path = tg_download(video["file_id"], os.path.join(work, "reel.mp4"))
        else:
            try:
                path, info = ig_download(url_match.group(0), work)
            except Exception as e:
                send(chat_id, "Instagram blocked the download for this link.\n"
                              "Please send me the reel video itself instead (screen-record it, or use "
                              "Instagram's download option if it has one).\n\n"
                              f"Details: {str(e)[-200:]}")
                return
            caption = info.get("description") or ""
            if info.get("track"):
                songs.append({"title": info["track"], "artist": info.get("artist") or "",
                              "source": "reel audio label"})

        try:
            songs += shazam_songs(path)
        except Exception as e:
            send(chat_id, f"(Shazam step failed: {str(e)[:150]})")
        if GEMINI_KEY:
            try:
                songs += gemini_songs(path, caption)
            except Exception as e:
                send(chat_id, f"(Gemini step failed: {str(e)[:150]})")

    send(chat_id, report(dedupe(songs)))


def main():
    updates = tg("getUpdates", timeout=0, allowed_updates=["message"])
    if not updates:
        return
    for u in updates:
        msg = u.get("message")
        if not msg:
            continue
        try:
            process(msg)
        except Exception as e:
            send(msg["chat"]["id"], f"Something went wrong: {str(e)[:300]}")
    # Tell Telegram these messages are handled
    tg("getUpdates", offset=updates[-1]["update_id"] + 1, timeout=0)


if __name__ == "__main__":
    main()
