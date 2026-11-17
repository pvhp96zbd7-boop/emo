import os, sqlite3, time, random, re
from typing import Optional
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import httpx
import yt_dlp

BASE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(BASE, "emma.db")
FRONT = os.path.join(BASE, "..", "frontend")

app = FastAPI(title="Emma")
app.add_middleware(CORSMiddleware, allow_origins=["*"],
                   allow_methods=["*"], allow_headers=["*"])

def db():
    return sqlite3.connect(DB)

def init():
    c = db().cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS tracks(
        id TEXT PRIMARY KEY, title TEXT, artist TEXT, thumbnail TEXT,
        duration INTEGER, added_at INTEGER)""")
    c.execute("""CREATE TABLE IF NOT EXISTS likes(
        id TEXT PRIMARY KEY, liked_at INTEGER)""")
    c.execute("""CREATE TABLE IF NOT EXISTS rec_cache(
        id TEXT PRIMARY KEY, title TEXT, artist TEXT, thumbnail TEXT,
        duration INTEGER, cached_at INTEGER)""")
    c.connection.commit()
init()

INFO_OPTS = {
    'quiet': True, 'no_warnings': True, 'noplaylist': True,
    'user_agent': ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                   'AppleWebKit/537.36 (KHTML, like Gecko) '
                   'Chrome/120.0.0.0 Safari/537.36'),
    'extractor_args': {'youtube': {
                'player_client': ['android_vr', 'ios']
    }},
}
LIST_OPTS = {**INFO_OPTS, 'extract_flat': 'in_playlist', 'skip_download': True, 'noplaylist': False}

def resolve_stream(vid: str) -> dict:
    url = f"https://www.youtube.com/watch?v={vid}"
    with yt_dlp.YoutubeDL(INFO_OPTS) as ydl:
        info = ydl.extract_info(url, download=False)
    fmts = [f for f in info.get('formats', []) if f.get('acodec') not in (None, 'none')]
    m4a = [f for f in fmts if f.get('ext') == 'm4a' or 'mp4' in (f.get('ext') or '')]
    key = lambda x: x.get('abr') or x.get('tbr') or 0
    best = (sorted(m4a, key=key, reverse=True)
            or sorted(fmts, key=key, reverse=True) or [{}])[0]
    return {
        'url': best.get('url'),
        'mime': 'audio/mp4' if best.get('ext') == 'm4a' else 'audio/webm',
        'title': info.get('title'),
        'artist': info.get('uploader') or info.get('channel'),
        'thumbnail': info.get('thumbnail'),
        'duration': info.get('duration'),
    }

def search_yt(q: str, n: int = 5):
    with yt_dlp.YoutubeDL({**LIST_OPTS, 'extract_flat': True}) as ydl:
        info = ydl.extract_info(f"ytsearch{n}:{q}", download=False)
    return info.get('entries', []) or []

_stream_cache: dict = {}

def get_stream(vid: str):
    now = time.time()
    hit = _stream_cache.get(vid)
    if hit and hit['expires'] > now:
        return hit
    d = resolve_stream(vid)
    entry = {**d, 'expires': now + 1800}
    _stream_cache[vid] = entry
    return entry

class AddReq(BaseModel):
    url: str

@app.post("/api/add")
async def add(req: AddReq):
    url = req.url.strip()
    if re.match(r'^[A-Za-z0-9_-]{11}$', url):
        url = f"https://www.youtube.com/watch?v={url}"
    try:
        with yt_dlp.YoutubeDL(LIST_OPTS) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as e:
        raise HTTPException(500, f"extract failed: {e}")

    entries = [e for e in info.get('entries', []) if e] if 'entries' in info else [info]
    now = int(time.time())
    conn = db(); c = conn.cursor()
    added = 0
    for e in entries:
        vid = e.get('id')
        if not vid: continue
        title = e.get('title') or 'Unknown'
        artist = e.get('uploader') or e.get('channel') or 'Unknown'
        thumb = (e.get('thumbnails') or [{}])[0].get('url') \
                or f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg"
        c.execute("INSERT OR REPLACE INTO tracks VALUES(?,?,?,?,?,?)",
                  (vid, title, artist, thumb, e.get('duration'), now))
        added += 1
    conn.commit(); conn.close()
    return {"added": added}

@app.get("/api/tracks")
async def tracks():
    c = db().cursor()
    c.execute("SELECT id,title,artist,thumbnail,duration FROM tracks ORDER BY added_at DESC")
    rows = c.fetchall()
    return [dict(zip(['id','title','artist','thumbnail','duration'], r)) for r in rows]

@app.get("/api/liked")
async def liked():
    c = db().cursor()
    c.execute("""SELECT t.id,t.title,t.artist,t.thumbnail,t.duration FROM tracks t
                 JOIN likes l ON t.id=l.id ORDER BY l.liked_at DESC""")
    rows = c.fetchall()
    return [dict(zip(['id','title','artist','thumbnail','duration'], r)) for r in rows]

@app.post("/api/like/{vid}")
async def like(vid: str):
    conn = db()
    conn.execute("INSERT OR REPLACE INTO likes VALUES(?,?)", (vid, int(time.time())))
    conn.commit(); conn.close()
    return {"ok": True}

@app.delete("/api/like/{vid}")
async def unlike(vid: str):
    conn = db()
    conn.execute("DELETE FROM likes WHERE id=?", (vid,))
    conn.commit(); conn.close()
    return {"ok": True}

@app.get("/api/wave")
async def wave():
    conn = db(); c = conn.cursor()
    c.execute("""SELECT t.id,t.title,t.artist,t.thumbnail,t.duration FROM tracks t
                 JOIN likes l ON t.id=l.id ORDER BY RANDOM() LIMIT 25""")
    liked = [dict(zip(['id','title','artist','thumbnail','duration'], r)) for r in c.fetchall()]
    c.execute("""SELECT id,title,artist,thumbnail,duration FROM rec_cache
                 WHERE cached_at > ? ORDER BY RANDOM() LIMIT 10""", (int(time.time())-3600,))
    recs = [dict(zip(['id','title','artist','thumbnail','duration'], r)) for r in c.fetchall()]
    conn.close()
    out = liked[:15] + recs[:8]
    random.shuffle(out)
    return out

@app.post("/api/refresh-wave")
async def refresh_wave():
    conn = db(); c = conn.cursor()
    c.execute("""SELECT t.artist FROM tracks t JOIN likes l ON t.id=l.id
                 GROUP BY t.artist ORDER BY COUNT(*) DESC LIMIT 2""")
    top = [r[0] for r in c.fetchall() if r[0]]
    c.execute("SELECT id FROM tracks"); existing = {r[0] for r in c.fetchall()}
    conn.close()

    now = int(time.time()); added = 0
    conn = db(); c = conn.cursor()
    for artist in top:
        try:
            for e in search_yt(f"{artist} mix", 4):
                vid = e.get('id')
                if not vid or vid in existing: continue
                c.execute("INSERT OR REPLACE INTO rec_cache VALUES(?,?,?,?,?,?)",
                          (vid, e.get('title',''), artist,
                           f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg",
                           e.get('duration'), now))
                added += 1
        except Exception:
            pass
    conn.commit(); conn.close()
    return {"added": added}

@app.get("/api/stream/{vid}")
async def stream(vid: str, request: Request):
    try:
        data = get_stream(vid)
    except Exception as e:
        raise HTTPException(500, f"resolve failed: {e}")
    if not data.get('url'):
        raise HTTPException(502, "no audio stream")

    headers = {'User-Agent': 'Mozilla/5.0', 'Referer': 'https://www.youtube.com/'}
    rng = request.headers.get('range')
    if rng: headers['Range'] = rng

    client = httpx.AsyncClient(
        timeout=httpx.Timeout(connect=15, read=None, write=None, pool=None)
    )
    upstream = await client.send(
        client.build_request('GET', data['url'], headers=headers), stream=True
    )

    async def gen():
        try:
            async for chunk in upstream.aiter_bytes(65536):
                yield chunk
        finally:
            await upstream.aclose(); await client.aclose()

    resp_headers = {
        'Accept-Ranges': 'bytes',
        'Content-Type': upstream.headers.get('content-type', data['mime']),
    }
    for h in ('content-length', 'content-range'):
        if h in upstream.headers:
            resp_headers[h.title()] = upstream.headers[h]

    return StreamingResponse(
        gen(),
        status_code=206 if rng else 200,
        headers=resp_headers,
    )

if os.path.isdir(FRONT):
    app.mount("/", StaticFiles(directory=FRONT, html=True), name="front")
