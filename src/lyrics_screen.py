#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Real-time lyrics for the AULA Neos75 keyboard screen.

Two lyric sources, chosen automatically:
  * SMTC  — any player that publishes a Windows media session (Spotify, QQ音乐,
            foobar2000, Apple Music ...): title/artist/position are exact, the
            line is picked from the LRC by timestamp. No screen capture.
  * OCR   — NetEase desktop-lyrics window captured + Windows OCR + LRC match.

Rendering, background options and HID push are shared by both.
"""
import argparse
import asyncio
import bisect
import ctypes
import ctypes.wintypes as wt
import difflib
import functools
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageFilter, ImageSequence

import screen_hid

WORK = Path(os.environ.get("NEOS75_LYRICS_DIR", str(Path.home() / ".neos75_lyrics")))
WORK.mkdir(parents=True, exist_ok=True)
CACHE = WORK / "lrc"
CACHE.mkdir(parents=True, exist_ok=True)
FRAME = WORK / "frame.jpg"
CONFIG_PATH = WORK / "config.json"

FONT_BOLD = r"C:\Windows\Fonts\msyhbd.ttc"
FONT = r"C:\Windows\Fonts\msyh.ttc"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"

DEFAULT_CONFIG = {
    "bg_color": "#090a0d",
    "bg_image": "",
    "smtc": True,
    "idle_image": "",
    "idle_gif": "",
    "bg_crop": None,
    "idle_crop": None,
    "frost": False,
    "frost_radius": 7,
    "accent": "#4D6BFE",
    "scenes": {"lyrics": True, "info": True},
    "city": "上海",
    "cover_bg": True,
    "valorant": False,
    "val_image": "",
    "val_crop": [0, 0, 1, 1],
    "mon_image": "",
    "mon_crop": [0, 0, 1, 1],
    "skins": {},
}

# players that are safe to trust for SMTC (a video tab in Edge must not win)
MUSIC_APPS = (
    "spotify", "cloudmusic", "qqmusic", "foobar", "applemusic", "music.ui",
    "kugou", "kuwo", "aimp", "musicbee", "deezer", "tidal", "amazon music",
    "netease",
)


_audio_cache = {"t": 0.0, "v": None}


def audio_active():
    """True/False if a NetEase audio session is active, None if unknown."""
    now = time.time()
    if now - _audio_cache["t"] < 2.0:
        return _audio_cache["v"]
    val = None
    try:
        from pycaw.pycaw import AudioUtilities
        for sess in AudioUtilities.GetAllSessions():
            proc = sess.Process
            if proc and "cloudmusic" in proc.name().lower():
                val = sess.State == 1
                break
    except Exception:
        val = None
    _audio_cache.update({"t": now, "v": val})
    return val


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    try:
        if CONFIG_PATH.exists():
            cfg.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
    except Exception:
        pass
    # drop keys that older builds wrote but 0.0.1 no longer uses
    for stale in ("glass", "glass_alpha", "rotate", "rotate_sec", "mode", "cover_scene"):
        cfg.pop(stale, None)
    scenes = cfg.get("scenes")
    if isinstance(scenes, dict):
        scenes.pop("cover", None)
        cfg["scenes"] = {"lyrics": bool(scenes.get("lyrics", True)),
                         "info": bool(scenes.get("info", True))}
    return cfg


def save_config(cfg):
    # Atomic write: write a temp file in the same directory, then
    # replace the config, so a crash mid-write cannot leave a
    # half-written config.json behind.
    try:
        tmp = CONFIG_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, CONFIG_PATH)
    except Exception:
        pass


# ---------------------------------------------------------------- win32 utils

u32 = ctypes.windll.user32
g32 = ctypes.windll.gdi32
EnumWindows = u32.EnumWindows
WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)


def dbg(msg):
    try:
        import logsetup
        logsetup.dbg(msg)
    except Exception:
        pass
    try:
        p = WORK / "app.log"
        if p.stat().st_size > 512 * 1024:
            p.unlink()
    except Exception:
        pass


def find_windows(class_names):
    hits = []

    def cb(hwnd, _):
        buf = ctypes.create_unicode_buffer(256)
        u32.GetClassNameW(hwnd, buf, 256)
        if buf.value in class_names:
            n = u32.GetWindowTextLengthW(hwnd)
            t = ctypes.create_unicode_buffer(n + 1)
            u32.GetWindowTextW(hwnd, t, n + 1)
            hits.append((hwnd, buf.value, t.value, bool(u32.IsWindowVisible(hwnd))))
        return True

    EnumWindows(WNDENUMPROC(cb), 0)
    return hits


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wt.DWORD), ("biWidth", wt.LONG), ("biHeight", wt.LONG),
        ("biPlanes", wt.WORD), ("biBitCount", wt.WORD), ("biCompression", wt.DWORD),
        ("biSizeImage", wt.DWORD), ("biXPelsPerMeter", wt.LONG), ("biYPelsPerMeter", wt.LONG),
        ("biClrUsed", wt.DWORD), ("biClrImportant", wt.DWORD),
    ]


_last_capture_error = ""


def capture_window(hwnd):
    global _last_capture_error
    err = ctypes.WinDLL("user32", use_last_error=True)
    rect = wt.RECT()
    u32.GetWindowRect(hwnd, ctypes.byref(rect))
    w, h = rect.right - rect.left, rect.bottom - rect.top
    if w <= 0 or h <= 0:
        _last_capture_error = f"bad rect {w}x{h}"
        return None
    hdc = u32.GetWindowDC(hwnd)
    mdc = g32.CreateCompatibleDC(hdc)
    bmp = g32.CreateCompatibleBitmap(hdc, w, h)
    g32.SelectObject(mdc, bmp)
    try:
        if not err.PrintWindow(wt.HWND(hwnd), mdc, 2):
            _last_capture_error = f"PrintWindow rc=0 err={ctypes.get_last_error()}"
        bi = BITMAPINFOHEADER()
        bi.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bi.biWidth, bi.biHeight = w, -h
        bi.biPlanes, bi.biBitCount, bi.biCompression = 1, 32, 0
        buf = ctypes.create_string_buffer(w * h * 4)
        if not g32.GetDIBits(mdc, bmp, 0, h, buf, ctypes.byref(bi), 0):
            _last_capture_error = "GetDIBits failed"
            return None
        img = Image.frombuffer("RGBA", (w, h), buf, "raw", "BGRA", 0, 1).convert("RGB")
        if img.getextrema() == ((0, 0), (0, 0), (0, 0)):
            _last_capture_error += " (all black)"
            return None
        return img
    finally:
        g32.DeleteObject(bmp)
        g32.DeleteDC(mdc)
        u32.ReleaseDC(hwnd, hdc)


# ---------------------------------------------------------------------- OCR

_ocr_engine = None


def ocr_engine():
    global _ocr_engine
    if _ocr_engine is None:
        from winsdk.windows.media.ocr import OcrEngine
        from winsdk.windows.globalization import Language
        _ocr_engine = (
            OcrEngine.try_create_from_language(Language("zh-Hans-CN"))
            or OcrEngine.try_create_from_user_profile_languages()
        )
    return _ocr_engine


async def _ocr_async(img):
    from winsdk.windows.graphics.imaging import BitmapDecoder
    from winsdk.windows.storage.streams import InMemoryRandomAccessStream, DataWriter

    buf = io.BytesIO()
    img.save(buf, "PNG")
    stream = InMemoryRandomAccessStream()
    writer = DataWriter(stream.get_output_stream_at(0))
    writer.write_bytes(buf.getvalue())
    await writer.store_async()
    await writer.flush_async()
    stream.seek(0)
    decoder = await BitmapDecoder.create_async(stream)
    bitmap = await decoder.get_software_bitmap_async()
    result = await ocr_engine().recognize_async(bitmap)
    return [line.text for line in result.lines if line.text.strip()]


def ocr_lines(img):
    try:
        return asyncio.run(_ocr_async(img))
    except Exception:
        return []


# --------------------------------------------------------------------- SMTC

async def _smtc_async():
    from winsdk.windows.media.control import (
        GlobalSystemMediaTransportControlsSessionManager as Manager,
        GlobalSystemMediaTransportControlsSessionPlaybackStatus as Status,
    )
    mgr = await Manager.request_async()
    sessions = list(mgr.get_sessions())
    if not sessions:
        return None

    def is_music(s):
        app = (s.source_app_user_model_id or "").lower()
        return any(k in app for k in MUSIC_APPS)

    music = [s for s in sessions if is_music(s)]
    playing = [s for s in music if s.get_playback_info().playback_status == Status.PLAYING]
    pick = playing[0] if playing else (music[0] if music else None)
    if pick is None:
        return None
    props = await pick.try_get_media_properties_async()
    tl = pick.get_timeline_properties()
    info = pick.get_playback_info()
    return {
        "app": pick.source_app_user_model_id,
        "title": props.title or "",
        "artist": props.artist or "",
        "pos": max(0.0, tl.position.total_seconds()),
        "dur": max(0.0, tl.end_time.total_seconds()),
        "playing": info.playback_status == Status.PLAYING,
    }


_smtc_busy = threading.Event()
_smtc_block_until = 0.0
_smtc_cache = {"t": 0.0, "data": None}


def smtc_state(timeout=3.0):
    """WinRT media queries can block forever when a media app hangs; cap them."""
    global _smtc_block_until
    if time.time() < _smtc_block_until:
        return _smtc_cache["data"]
    if _smtc_busy.is_set():
        return _smtc_cache["data"]
    if time.time() - _smtc_cache["t"] < 2.0:
        return _smtc_cache["data"]
    _smtc_busy.set()
    box = {}

    def worker():
        try:
            box["v"] = asyncio.run(_smtc_async())
        except Exception as exc:
            dbg(f"smtc error {exc}")
            box["v"] = None
        finally:
            _smtc_busy.clear()

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        dbg("smtc timeout")
        _smtc_block_until = time.time() + 20
        _smtc_cache.update({"t": time.time(), "data": None})
        return None
    data = box.get("v")
    _smtc_cache.update({"t": time.time(), "data": data})
    return data


# --------------------------------------------------------------------- lyric

def http_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Referer": "https://music.163.com/"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.load(resp)


def norm(text):
    text = re.sub(r"\s+", "", text or "")
    text = re.sub(r"[^\w\u4e00-\u9fff]", "", text)
    return text.lower()


def _parse_lyric(data):
    raw = (data.get("lrc") or {}).get("lyric") or ""
    trans = {}
    for row in ((data.get("tlyric") or {}).get("lyric") or "").splitlines():
        m = re.match(r"\[(\d+):(\d+(?:\.\d+)?)\](.*)", row.strip())
        if m and m.group(3).strip():
            trans[(int(m.group(1)), round(float(m.group(2)), 2))] = m.group(3).strip()
    lines = []
    for row in raw.splitlines():
        m = re.match(r"\[(\d+):(\d+(?:\.\d+)?)\](.*)", row.strip())
        if not m:
            continue
        text = m.group(3).strip()
        if not text:
            continue
        k = (int(m.group(1)), round(float(m.group(2)), 2))
        lines.append({"t": int(m.group(1)) * 60 + float(m.group(2)), "text": text,
                      "tr": trans.get(k, "")})
    lines.sort(key=lambda x: x["t"])
    return lines


CREDIT_HINTS = ("作词", "作曲", "编曲", "制作", "混音", "母带", "录音", "出品", "监制", "统筹")


def _usable_lines(lines):
    return sum(1 for row in lines if not row["text"].startswith(CREDIT_HINTS))


def fetch_lrc(title, artist, retry=True):
    """Find a usable LRC for the track; skips search hits without real lyrics."""
    key = "v4_" + re.sub(r"[^\w\u4e00-\u9fff]+", "_", f"{title}_{artist}")[:116]
    cached = CACHE / f"{key}.json"
    if cached.exists():
        try:
            payload = json.loads(cached.read_text(encoding="utf-8"))
            rows = payload.get("lrc") if isinstance(payload, dict) else payload
            if rows and _usable_lines(rows) >= 4:
                return rows
        except Exception:
            pass

    query = urllib.parse.quote(f"{title} {artist.split('/')[0].strip() or artist}")
    try:
        search = http_json(f"https://music.163.com/api/search/get/web?s={query}&type=1&limit=8")
    except Exception as exc:
        dbg(f"lrc search failed: {exc}")
        return []
    songs = (search.get("result") or {}).get("songs") or []
    if not songs:
        dbg("lrc search: no hits")
        return []

    def score(song):
        name = norm(song.get("name", ""))
        artists = norm("".join(a.get("name", "") for a in song.get("artists", []) or []))
        return (difflib.SequenceMatcher(None, norm(title), name).ratio()
                + 0.6 * difflib.SequenceMatcher(None, norm(artist), artists).ratio())

    fallback = None
    for song in sorted(songs, key=score, reverse=True)[:8]:
        try:
            data = http_json(f"https://music.163.com/api/song/lyric?id={song['id']}&lv=1&kv=1&tv=1")
        except Exception:
            continue
        lines = _parse_lyric(data)
        title_sim = difflib.SequenceMatcher(None, norm(title), norm(song.get("name", ""))).ratio()
        if len(lines) >= 5 and _usable_lines(lines) >= 4:
            dbg(f"lrc ok: {song.get('name')} / {song['id']} -> {len(lines)} lines")
            try:
                cached.write_text(json.dumps({"id": song["id"], "name": song.get("name"),
                                              "lrc": lines}, ensure_ascii=False), encoding="utf-8")
            except Exception:
                pass
            return lines
        # credit-only stub: remember a same-title hit with more text, keep looking
        if title_sim >= 0.85 and _usable_lines(lines) >= 4 and fallback is None:
            fallback = (lines, song)
        dbg(f"lrc skip stub: {song.get('name')} -> {len(lines)} lines")

    if fallback is not None:
        lines, song = fallback
        dbg(f"lrc fallback: {song.get('name')} -> {len(lines)} lines")
        try:
            cached.write_text(json.dumps({"id": song["id"], "name": song.get("name"),
                                          "lrc": lines}, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass
        return lines
    dbg("lrc: no usable lyrics found")
    if retry:
        # remixes often have credit-only lyrics; retry with the bare song name
        bare = re.sub(r"[\(（\[【].*?[\)）\]】]", "", title).strip() or title
        if bare != title:
            dbg(f"lrc retry with bare title: {bare}")
            return fetch_lrc(bare, artist, retry=False)
    return []


def index_at(lines, pos):
    if not lines:
        return None
    times = [row["t"] for row in lines]
    i = bisect.bisect_right(times, pos) - 1
    return max(0, min(i, len(lines) - 1))


def pick_current(ocr_texts, lines, prev, title, artist):
    tnorm = norm(f"{title}{artist}")
    cands = []
    for text in ocr_texts:
        t = norm(text)
        if len(t) < 2:
            continue
        if tnorm and difflib.SequenceMatcher(None, t, tnorm).ratio() > 0.72:
            continue
        cands.append(t)
    if not cands or not lines:
        return None, 0.0

    def sim(text, line):
        best = difflib.SequenceMatcher(None, text, norm(line["text"])).ratio()
        tr = line.get("tr") or ""
        if tr:
            best = max(best, difflib.SequenceMatcher(None, text, norm(tr)).ratio())
        return best

    def score_at(i):
        return max(sim(c, lines[i]) for c in cands)

    lo = prev if prev is not None else 0
    hi = min(len(lines) - 1, prev + 6) if prev is not None else len(lines) - 1
    best_i, best_s = None, 0.0
    for i in range(lo, hi + 1):
        s = score_at(i)
        if s > best_s:
            best_i, best_s = i, s

    if (best_i is None or best_s < 0.60) and prev is not None:
        for i in range(hi + 1, min(len(lines), prev + 60)):
            s = score_at(i)
            if s > best_s:
                best_i, best_s = i, s

    if best_s < 0.60 and prev is not None:
        for i in range(0, max(0, lo)):
            s = score_at(i)
            if s > best_s:
                best_i, best_s = i, s
        if best_s < 0.90:
            return None, best_s

    if best_i is None or best_s < 0.60:
        return None, best_s
    return best_i, best_s


# -------------------------------------------------------------------- render

@functools.lru_cache(maxsize=32)
def _font(path, size):
    try:
        return ImageFont.truetype(path, size)
    except Exception:
        return ImageFont.load_default()


def wrap(text, font, draw, max_width):
    if draw.textbbox((0, 0), text, font=font)[2] <= max_width:
        return [text]
    out, cur = [], ""
    for ch in text:
        trial = cur + ch
        if draw.textbbox((0, 0), trial, font=font)[2] > max_width and cur:
            out.append(cur)
            cur = ch
        else:
            cur = trial
    if cur:
        out.append(cur)
    return out


def fit_lines(text, draw, max_width, max_lines=2):
    """Prefer one full line; otherwise split into two balanced lines."""
    for size in (46, 42, 38, 34, 30, 26, 22):
        font = _font(FONT_BOLD, size)
        if len(wrap(text, font, draw, max_width)) == 1:
            return font, [text]

    rows = [text]
    for size in (46, 42, 38, 34, 30, 26, 22):
        font = _font(FONT_BOLD, size)
        rows = wrap(text, font, draw, max_width)
        if len(rows) > 2:
            continue
        best_i, best_diff = None, None
        for i in range(1, len(text)):
            left, right = text[:i], text[i:]
            lw = draw.textbbox((0, 0), left, font=font)[2]
            rw = draw.textbbox((0, 0), right, font=font)[2]
            if lw > max_width or rw > max_width:
                continue
            diff = abs(lw - rw)
            if best_diff is None or diff < best_diff:
                best_i, best_diff = i, diff
        if best_i:
            return font, [text[:best_i], text[best_i:]]
        return font, rows
    return font, rows[:max_lines]


def ellipsize(text, font, draw, max_width):
    if draw.textbbox((0, 0), text, font=font)[2] <= max_width:
        return text
    while text and draw.textbbox((0, 0), text + "…", font=font)[2] > max_width:
        text = text[:-1]
    return text + "…"


def _crop_from(img, crop, size=(480, 360)):
    """Apply a normalized crop [x0,y0,x1,y1] then resize."""
    if crop:
        try:
            x0, y0, x1, y1 = crop
            box = (int(x0 * img.width), int(y0 * img.height),
                   max(1, int(x1 * img.width)), max(1, int(y1 * img.height)))
            if box[2] - box[0] > 4 and box[3] - box[1] > 4:
                return img.crop(box).resize(size, Image.LANCZOS)
        except Exception:
            pass
    return None


def _cover(img, size=(480, 360)):
    scale = max(size[0] / img.width, size[1] / img.height)
    img = img.resize((max(1, int(img.width * scale)), max(1, int(img.height * scale))), Image.LANCZOS)
    left = (img.width - size[0]) // 2
    top = (img.height - size[1]) // 2
    return img.crop((left, top, left + size[0], top + size[1]))


def load_idle_frames(gif_path, image_path, limit=48, crop=None):
    """Return a list of 480x360 JPEG byte strings for the idle screen."""
    frames = []
    try:
        if gif_path and Path(gif_path).exists():
            src = Image.open(gif_path)
            for i, frame in enumerate(ImageSequence.Iterator(src)):
                if i >= limit:
                    break
                rgb = frame.convert("RGB")
                buf = io.BytesIO()
                _cover(rgb).save(buf, "JPEG", quality=45, optimize=True, subsampling=2)
                frames.append(buf.getvalue())
            if frames:
                return frames
        if image_path and Path(image_path).exists():
            rgb = Image.open(image_path).convert("RGB")
            cropped = _crop_from(rgb, crop)
            if cropped is None:
                cropped = _cover(rgb)
            buf = io.BytesIO()
            cropped.save(buf, "JPEG", quality=48, optimize=True, subsampling=2)
            frames.append(buf.getvalue())
    except Exception as exc:
        dbg(f"idle load failed: {exc}")
    return frames


def _background(cfg, size=(480, 360)):
    img = _background_raw(cfg, size)
    if cfg.get("frost"):
        try:
            radius = float(cfg.get("frost_radius", 7))
            if radius > 0:
                img = img.filter(ImageFilter.GaussianBlur(radius))
                img = Image.blend(img, Image.new("RGB", size, (0, 0, 0)), 0.22)
        except Exception:
            pass
    return img


def module_bg(cfg, prefix, size=(480, 360), veil=0.45):
    """Optional picture background for the VALORANT / monitor cards."""
    path = cfg.get(prefix + "_image") or ""
    if not path or not Path(path).exists():
        return None
    try:
        img = Image.open(path).convert("RGB")
        crop = cfg.get(prefix + "_crop")
        if crop:
            img = _crop_from(img, crop, size)
        if img.size != size:
            img = img.resize(size, Image.LANCZOS)
        if veil:
            img = Image.blend(img, Image.new("RGB", size, (0, 0, 0)), veil)
        return img
    except Exception as exc:
        dbg(f"module_bg {prefix} failed: {exc}")
        return None


def _background_raw(cfg, size=(480, 360)):
    img = Image.new("RGB", size, cfg.get("bg_color") or "#090a0d")
    path = cfg.get("bg_image") or ""
    if path and Path(path).exists():
        try:
            src = Image.open(path).convert("RGB")
            cropped = _crop_from(src, cfg.get("bg_crop"), size)
            if cropped is not None:
                veil = Image.new("RGB", size, (0, 0, 0))
                return Image.blend(cropped, veil, 0.35)
            scale = max(size[0] / src.width, size[1] / src.height)
            src = src.resize((max(1, int(src.width * scale)), max(1, int(src.height * scale))), Image.LANCZOS)
            left = (src.width - size[0]) // 2
            top = (src.height - size[1]) // 2
            img = src.crop((left, top, left + size[0], top + size[1]))
            veil = Image.new("RGB", size, (0, 0, 0))
            img = Image.blend(img, veil, 0.55)
        except Exception:
            pass
    return img


def render(track, artist, current, nxt, out_path, cfg=None, progress=None, cover=None):
    cfg = cfg or load_config()
    if cover is not None:
        img = cover.copy()
        if cfg.get("frost"):
            radius = float(cfg.get("frost_radius", 7))
            if radius > 0:
                img = img.filter(ImageFilter.GaussianBlur(radius))
        veil = Image.new("RGB", img.size, (0, 0, 0))
        img = Image.blend(img, veil, 0.62 if cfg.get("frost") else 0.55)
    else:
        img = _background(cfg)
    draw = ImageDraw.Draw(img)

    f_head = _font(FONT, 19)
    head = ellipsize(f"{track}  ·  {artist}" if artist else track, f_head, draw, 400)
    draw.text((240, 22), head, font=f_head, fill=(150, 158, 172), anchor="mt")
    draw.ellipse([236, 52, 244, 60], fill=(0, 200, 160))

    f_cur, rows = fit_lines(current or "…", draw, 436, 2)
    line_h = f_cur.size + 14
    y = 180 - (line_h * len(rows)) // 2
    for row in rows:
        draw.text((241, y + 2), row, font=f_cur, fill=(0, 0, 0), anchor="mt")
        draw.text((240, y), row, font=f_cur, fill=(246, 247, 250), anchor="mt")
        y += line_h

    if nxt:
        f_next = _font(FONT, 24)
        draw.text((240, 300), ellipsize(nxt, f_next, draw, 420), font=f_next,
                  fill=(176, 182, 196), anchor="mt")

    if progress is not None:
        frac = max(0.0, min(1.0, progress))
        draw.rectangle([40, 336, 440, 337], fill=(70, 74, 84))
        draw.rectangle([40, 336, 40 + int(400 * frac), 337], fill=(0, 200, 160))
    else:
        draw.rectangle([40, 336, 440, 337], fill=(70, 74, 84))

    img.save(out_path, "JPEG", quality=46, optimize=True, progressive=False, subsampling=2)
    if out_path.stat().st_size > 9000:
        img.save(out_path, "JPEG", quality=38, optimize=True, progressive=False, subsampling=2)
    return out_path.stat().st_size


# -------------------------------------------------------------------- engine

def track_info():
    for _h, _c, title, _v in find_windows({"OrpheusBrowserHost"}):
        if title and " - " in title:
            name, _, artist = title.rpartition(" - ")
            return name.strip(), artist.strip()
    for _h, _c, title, _v in find_windows({"OrpheusBrowserHost"}):
        if title:
            return title, ""
    return None, None


def lyrics_window():
    for hwnd, _c, _t, vis in find_windows({"DesktopLyrics"}):
        if vis:
            return hwnd
    return None


class Engine:
    """Capture/OCR or SMTC -> LRC -> render -> HID push, until stop()."""

    def __init__(self, on_status=None, on_line=None, on_error=None):
        self.on_status = on_status or (lambda msg: None)
        self.on_line = on_line or (lambda *a: None)
        self.on_error = on_error or (lambda msg: None)
        self.running = False
        self.screen = None
        self.cfg = load_config()
        self.track = self.artist = None
        self.lines = []
        self.prev = None
        self.pushed = None
        self.last_hash = None
        self.last_push_at = 0.0
        self.mode = None
        self.smtc_misses = 0
        self.scene = "lyrics"
        self.scene_at = time.time()
        self.info_at = 0.0
        self.cover_img = None
        self.cover_url = ""
        self._val_score = None
        self._val_at = 0.0
        self._val_pending = None
        self._val_lost_at = 0.0
        self.manual_until = 0.0
        self.last_shown = (None, None, None)
        self.idle_key = None
        self.idle_frames = []
        self.idle_index = 0
        self.idle_last = 0.0
        self.idle_pushed = False

    # -------------------------------------------------------------- device
    def check_device(self):
        try:
            if not self.screen:
                self.screen = screen_hid.Screen().open()
            st = self.screen.status()
            if st["width"] != 480 or st["height"] != 360:
                self.device_ok = False
                return False, f"屏幕当前 {st['width']}x{st['height']}，需要先在「固件」页刷全屏补丁"
            self.device_ok = True
            return True, f"屏幕已连接（{st['width']}x{st['height']}）"
        except Exception as exc:
            self.device_ok = False
            try:
                if screen_hid.has_dongle_only():
                    return False, "检测到 2.4G 接收器：屏幕画面只在 USB 有线模式下可用"
            except Exception:
                pass
            return False, f"未找到键盘屏幕：{exc}"

    def _ensure_screen(self):
        if self.screen is None:
            self.screen = screen_hid.Screen().open()
        return self.screen

    def _push_data(self, data):
        # Push JPEG bytes directly, without writing/reading a file.
        # Same 2.2s minimum gap and retry behaviour as _push().
        for attempt in range(4):
            try:
                wait = 2.2 - (time.time() - self.last_push_at)
                if wait > 0:
                    time.sleep(wait)
                res = self._ensure_screen().push(bytes(data))
                self.last_push_at = time.time()
                return res
            except Exception as exc:
                dbg(f"push failed (attempt {attempt}): {exc}")
                self._drop_screen()
                time.sleep(1.2 + attempt * 0.8)
        return None

    def _push(self, path):
        return self._push_data(Path(path).read_bytes())

    def _push_bytes(self, data):
        # No longer writes idle_frame.jpg to disk first.
        return self._push_data(data)

    # ---------------------------------------------------------------- loop
    def stop(self):
        self.running = False

    def run(self):
        self.running = True
        while self.running:
            if self.screen is None:
                ok, msg = self.check_device()
                self.on_status(msg)
                if not ok:
                    time.sleep(1.5)     # keep waiting: the keyboard may be on BT/2.4G
                    continue
            try:
                self._tick()
            except Exception as exc:
                dbg(f"tick error: {exc}")
                self._drop_screen()
                self.on_status("连接中断，正在等待键盘重新接入…")
                time.sleep(1.0)
            time.sleep(0.25)

    def _drop_screen(self):
        try:
            if self.screen:
                self.screen.close()
        except Exception:
            pass
        self.screen = None

    # ------------------------------------------------------------- sources
    def _set_track(self, name, artist):
        if (name, artist) == (self.track, self.artist):
            return
        self.track, self.artist = name, artist
        self.lines = fetch_lrc(name, artist) if name else []
        self.cover_img, self.cover_url = None, ""
        if name:
            try:
                import scenes
                meta = scenes.song_meta(name, artist)
                url = (meta or {}).get("cover") or ""
                if url and self.cfg.get("cover_bg", True):
                    self.cover_url = url
                    self.cover_img = scenes.cover_image(url)
            except Exception as exc:
                dbg(f"cover fetch failed: {exc}")
        self.prev = None
        self.pushed = None
        self.on_status(f"正在播放：{name} - {artist}" if artist else f"正在播放：{name}")

    def _show(self, current, nxt, progress=None):
        self.last_shown = (current, nxt, progress)
        cover = self.cover_img if self.cfg.get("cover_bg", True) else None
        size = render(self.track or "", self.artist or "", current, nxt, FRAME, self.cfg, progress,
                      cover=cover)
        res = self._push(FRAME) if self.screen else None
        if res:
            self.on_status(f"{current}   ({size}B / {res['ms']}ms)")
        return res

    def _tick(self):
        if time.time() < getattr(self, "manual_until", 0):
            return

        # 1) VALORANT takes the screen while a match is running
        if self.cfg.get("valorant"):
            try:
                import valorant as V
                data = V.read()
            except Exception:
                data = None
            if data:
                self._val_lost_at = 0.0
                self._tick_valorant(data)
                return
            if self._val_score is not None:
                if not self._val_lost_at:
                    self._val_lost_at = time.time()
                elif time.time() - self._val_lost_at > 8:
                    self._val_score = None
                    self._val_pending = None
                    self._val_lost_at = 0.0
                    self.pushed = None
                    dbg("valorant: match over")

        # 2) lyrics: media session first, screen OCR as the fallback
        if self.cfg.get("scenes", {}).get("lyrics", True):
            state = smtc_state() if self.cfg.get("smtc", True) else None
            if state and state["playing"]:
                self._tick_smtc(state)
                return
            if self._tick_ocr():
                return
            if state and state["title"] and not state["playing"]:
                self._show_idle_or_info()
                return

        # 3) nothing playing: idle wallpaper or the monitor screen
        self._show_idle_or_info()

    def force_refresh(self):
        """Re-render and push the current frame (used by the UI preview button)."""
        self.pushed = None
        self.last_hash = None
        self.idle_pushed = False
        self.scene = "lyrics"
        self.scene_at = time.time()
        self.info_at = 0.0
        self.cover_img = None
        self.cover_url = ""
        self._val_score = None
        self._val_at = 0.0
        self._val_pending = None
        self.idle_key = None
        if self.screen is None:
            ok, msg = self.check_device()
            if not ok:
                return False, msg
        self._tick()
        return True, "已推送到键盘"

    def _show_idle_or_info(self):
        if (self.cfg.get("idle_gif") or "") or (self.cfg.get("idle_image") or ""):
            self._show_idle()
        else:
            self._tick_info(force=True)

    def _tick_valorant(self, data):
        import valorant as V
        now = time.time()
        score = (data["ally"], data["enemy"])
        prev = self._val_score
        dbg(f"valorant read {score} votes={data.get('votes')} prev={prev}")

        # a score only ever goes up by one side at a time; anything else needs
        # to be seen repeatedly before it is trusted
        implausible = False
        if prev is not None:
            if score[0] < prev[0] or score[1] < prev[1]:
                implausible = True
            elif abs(score[0] - prev[0]) + abs(score[1] - prev[1]) > 1:
                implausible = True
        if implausible:
            pending = getattr(self, "_val_pending", None)
            if pending and pending[0] == score:
                self._val_pending = (score, pending[1] + 1)
            else:
                self._val_pending = (score, 1)
            if self._val_pending[1] < 3:
                dbg(f"valorant hold implausible {score} (x{self._val_pending[1]})")
                return
        self._val_pending = None

        same = score == prev
        last = getattr(self, "_val_at", 0.0)
        if same and now - last < 15:
            return
        size = V.render(self.cfg, data, FRAME)
        res = self._push(FRAME) if self.screen else None
        if res:
            self._val_score = score
            self._val_at = now
            self.on_status(f"无畏契约 {score[0]}:{score[1]}   ({size}B / {res['ms']}ms)")

    def _tick_info(self, force=False):
        now = time.time()
        if not force and now - self.info_at < 6:
            return
        try:
            import scenes
            size = scenes.render_info(self.cfg, FRAME)
        except Exception as exc:
            self.on_error(f"信息屏渲染失败：{exc}")
            return
        res = self._push(FRAME) if self.screen else None
        if res:
            self.info_at = now
            self.on_status(f"信息屏   ({size}B / {res['ms']}ms)")

    def _show_idle(self):
        gif = self.cfg.get("idle_gif") or ""
        img = self.cfg.get("idle_image") or ""
        if not gif and not img:
            return
        key = (gif, img)
        if key != self.idle_key:
            self.idle_key = key
            self.idle_frames = load_idle_frames(gif, img, crop=self.cfg.get("idle_crop"))
            self.idle_index = 0
            self.idle_last = 0.0
            self.idle_pushed = False
        if not self.idle_frames:
            return
        now = time.time()
        if self.idle_pushed and (len(self.idle_frames) == 1 or now - self.idle_last < 1.2):
            return
        data = self.idle_frames[self.idle_index % len(self.idle_frames)]
        if self._push_bytes(data):
            self.idle_index += 1
            self.idle_last = now
            self.idle_pushed = True

    def _tick_smtc(self, st):
        self._set_track(st["title"], st["artist"])
        if not self.lines:
            if self.pushed != ("none", st["title"]):
                self._show("（没有找到歌词）", "")
                self.pushed = ("none", st["title"])
            return
        idx = index_at(self.lines, st["pos"])
        if idx is None:
            return
        progress = (st["pos"] / st["dur"]) if st["dur"] > 5 else None
        if self.pushed == (self.track, idx):
            return
        row = self.lines[idx]
        nxt = row.get("tr") or (self.lines[idx + 1]["text"] if idx + 1 < len(self.lines) else "")
        self.on_line(self.track or "", self.artist or "", row["text"], nxt)
        if self._show(row["text"], nxt, progress):
            self.pushed = (self.track, idx)

    def _tick_ocr(self):
        """Read the NetEase desktop-lyrics window. True when a frame was pushed."""
        name, art = track_info()
        if name:
            self._set_track(name, art)
        hwnd = lyrics_window()
        if not hwnd:
            dbg("ocr: no desktop-lyrics window")
            self.on_status("没找到网易云桌面歌词：请打开「桌面歌词」并切换到横排")
            return False
        img = capture_window(hwnd)
        if img is None:
            dbg(f"ocr: capture failed hwnd={hwnd} err={_last_capture_error}")
            return False
        digest = hashlib.md5(img.tobytes()).hexdigest()
        if digest == self.last_hash and self.pushed:
            return False
        self.last_hash = digest
        time.sleep(0.12)
        img = capture_window(hwnd) or img

        texts = []
        for _ in range(3):
            texts = ocr_lines(img)
            if texts:
                break
            time.sleep(0.15)
            img = capture_window(hwnd) or img
        if texts:
            dbg(f"ocr: {texts}")

        idx, _score = pick_current(texts, self.lines, self.prev, self.track or "", self.artist or "")
        if idx is None:
            dbg("ocr: no matching lyric line")
            return False
        self.prev = idx
        if self.pushed == (self.track, idx):
            return False
        row = self.lines[idx]
        nxt = row.get("tr") or (self.lines[idx + 1]["text"] if idx + 1 < len(self.lines) else "")
        self.on_line(self.track or "", self.artist or "", row["text"], nxt)
        if self._show(row["text"], nxt):
            self.pushed = (self.track, idx)
            return True
        return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    args = ap.parse_args()
    eng = Engine(
        on_status=lambda m: print(time.strftime("[%H:%M:%S]"), m, flush=True),
        on_line=lambda t, a, c, n: print(time.strftime("[%H:%M:%S]"), f"{c}  |  {n}", flush=True),
        on_error=lambda m: print(time.strftime("[%H:%M:%S]"), "ERR", m, flush=True),
    )
    try:
        if args.once:
            ok, msg = eng.check_device()
            print(msg)
            eng._tick()
        else:
            eng.run()
    except KeyboardInterrupt:
        pass
    finally:
        if eng.screen:
            eng.screen.close()


if __name__ == "__main__":
    main()
