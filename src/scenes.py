#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Extra scenes for the Neos75 screen: album cover, clock/weather/system info."""
import functools
import json
import time
import urllib.parse
import urllib.request
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

import lyrics_screen as L

W, H = 480, 360
FONT = r"C:\Windows\Fonts\msyh.ttc"
FONT_BOLD = r"C:\Windows\Fonts\msyhbd.ttc"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"

_weather_cache = {"t": 0.0, "data": None}
_cover_cache = {}
_stats_last = {"t": 0.0, "net": None}
_geo_cache = {}
try:
    import psutil as _ps
    _ps.cpu_percent(interval=None)   # prime the first sample
except Exception:
    pass


# ------------------------------------------------------------------ helpers
def _get_json(url, timeout=10):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.load(resp)


def _fit(img, size=(W, H)):
    scale = max(size[0] / img.width, size[1] / img.height)
    img = img.resize((max(1, int(img.width * scale)), max(1, int(img.height * scale))), Image.LANCZOS)
    left = (img.width - size[0]) // 2
    top = (img.height - size[1]) // 2
    return img.crop((left, top, left + size[0], top + size[1]))


@functools.lru_cache(maxsize=32)
def _font(path, size):
    try:
        return ImageFont.truetype(path, size)
    except Exception:
        return ImageFont.load_default()


# -------------------------------------------------------------------- cover
def song_meta(title, artist):
    """Return {'id', 'cover'} for the song, cached on disk."""
    key = "meta_" + L.re.sub(r"[^\w\u4e00-\u9fff]+", "_", f"{title}_{artist}")[:110]
    path = L.CACHE / f"{key}.json"
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            pass
    try:
        q = urllib.parse.quote(f"{title} {artist.split('/')[0].strip() or artist}")
        search = _get_json(f"https://music.163.com/api/search/get/web?s={q}&type=1&limit=5")
        songs = (search.get("result") or {}).get("songs") or []
        if not songs:
            return {}
        song = songs[0]
        detail = _get_json(f"https://music.163.com/api/song/detail?ids=[{song['id']}]")
        rows = detail.get("songs") or []
        cover = ""
        if rows:
            cover = ((rows[0].get("album") or {}).get("picUrl") or "")
        meta = {"id": song["id"], "cover": cover}
        path.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
        return meta
    except Exception:
        return {}


def cover_image(url, size=(W, H)):
    """Download (once) and return the cover as a 480x360 image."""
    if not url:
        return None
    if url in _cover_cache:
        return _cover_cache[url]
    name = L.CACHE / ("cover_" + str(abs(hash(url))) + ".jpg")
    try:
        if name.exists():
            img = Image.open(name).convert("RGB")
        else:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=15) as resp:
                raw = resp.read()
            name.write_bytes(raw)
            img = Image.open(name).convert("RGB")
        img = _fit(img, size)
        _cover_cache[url] = img
        return img
    except Exception:
        return None


# ------------------------------------------------------------------ weather
WMO = {
    0: "晴", 1: "晴间多云", 2: "多云", 3: "阴",
    45: "雾", 48: "霜雾",
    51: "毛毛雨", 53: "毛毛雨", 55: "毛毛雨",
    56: "冻雨", 57: "冻雨",
    61: "小雨", 63: "中雨", 65: "大雨",
    66: "冻雨", 67: "冻雨",
    71: "小雪", 73: "中雪", 75: "大雪", 77: "雪粒",
    80: "阵雨", 81: "阵雨", 82: "强阵雨",
    85: "阵雪", 86: "阵雪",
    95: "雷阵雨", 96: "雷阵雨伴冰雹", 99: "雷阵雨伴冰雹",
}


def _geocode(city):
    if city in _geo_cache:
        return _geo_cache[city]
    try:
        data = _get_json("https://geocoding-api.open-meteo.com/v1/search?name=%s&count=1&language=zh"
                         % urllib.parse.quote(city))
        rows = data.get("results") or []
        if rows:
            out = (rows[0]["latitude"], rows[0]["longitude"], rows[0].get("name") or city)
            _geo_cache[city] = out
            return out
    except Exception:
        pass
    return None


def weather(cfg, ttl=900):
    now = time.time()
    if _weather_cache["data"] and now - _weather_cache["t"] < ttl:
        return _weather_cache["data"]
    city = (cfg.get("city") or "上海").strip()
    geo = _geocode(city)
    if not geo:
        return _weather_cache["data"]
    lat, lon, name = geo
    try:
        data = _get_json(
            f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}"
            f"&current=temperature_2m,weather_code&timezone=auto")
        cur = data.get("current") or {}
        code = int(cur.get("weather_code") or 0)
        out = {"city": name, "temp": cur.get("temperature_2m"), "code": code,
               "desc": WMO.get(code, "—")}
        _weather_cache.update({"t": now, "data": out})
        return out
    except Exception:
        return _weather_cache["data"]


# -------------------------------------------------------------------- stats
def stats():
    out = {"cpu": None, "mem": None, "down": None, "up": None, "bat": None}
    try:
        import psutil
        out["cpu"] = psutil.cpu_percent(interval=None)
        out["mem"] = psutil.virtual_memory().percent
        try:
            batt = psutil.sensors_battery()
            if batt:
                out["bat"] = (int(batt.percent), bool(batt.power_plugged))
        except Exception:
            pass
        now = time.time()
        counters = psutil.net_io_counters()
        last = _stats_last["net"]
        if last and now - _stats_last["t"] > 0.5:
            dt = now - _stats_last["t"]
            out["down"] = max(0.0, (counters.bytes_recv - last.bytes_recv) / dt)
            out["up"] = max(0.0, (counters.bytes_sent - last.bytes_sent) / dt)
        _stats_last.update({"t": now, "net": counters})
    except Exception:
        pass
    return out


def _rate(bps):
    if bps is None:
        return "—"
    for unit in ("B/s", "KB/s", "MB/s"):
        if bps < 1024 or unit == "MB/s":
            return f"{bps:.1f}{unit}" if unit != "B/s" else f"{bps:.0f}B/s"
        bps /= 1024
    return f"{bps:.1f}MB/s"


# ------------------------------------------------------------------- render
WEEK = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")


def render_info(cfg, out_path):
    base = L.module_bg(cfg, "mon", (W, H), veil=0.55)
    img = base if base is not None else Image.new("RGB", (W, H), cfg.get("bg_color") or "#090a0d")
    draw = ImageDraw.Draw(img)
    accent = cfg.get("accent") or "#4D6BFE"
    now = time.localtime()

    # clock
    f_clock = _font(FONT_BOLD, 92)
    draw.text((W // 2, 96), time.strftime("%H:%M", now), font=f_clock, fill=(246, 247, 250), anchor="mm")
    # seconds + date
    f_sec = _font(FONT, 22)
    draw.text((W // 2 + 150, 66), time.strftime("%S"), font=f_sec, fill=(140, 148, 162), anchor="mm")
    f_date = _font(FONT, 22)
    date_str = time.strftime("%m月%d日", now) + "  " + WEEK[now.tm_wday]
    draw.text((W // 2, 158), date_str, font=f_date, fill=(170, 176, 190), anchor="mm")

    # weather
    w = weather(cfg)
    if w and w.get("temp") is not None:
        f_w = _font(FONT, 24)
        draw.text((W // 2, 200), f"{w['city']}  {w['temp']:.0f}°C  {w['desc']}",
                  font=f_w, fill=(200, 206, 220), anchor="mm")

    # system stats (cpu / mem / gpu / disk / net)
    try:
        import monitor
        snap = monitor.snapshot()
    except Exception:
        snap = {}
    f_s = _font(FONT, 17)
    parts = []
    if snap.get("cpu") is not None:
        parts.append(f"CPU {snap['cpu']:.0f}%")
    if snap.get("mem"):
        parts.append(f"MEM {snap['mem'][0]:.0f}%")
    if snap.get("gpu") is not None:
        parts.append(f"GPU {snap['gpu']:.0f}%")
    if snap.get("disk"):
        parts.append(f"SSD {snap['disk'][0]:.0f}%")
    if parts:
        draw.text((W // 2, 258), "   ".join(parts), font=f_s, fill=(150, 158, 172), anchor="mm")
    if snap.get("down") is not None:
        try:
            rates = f"↓{monitor.human_rate(snap['down'])}   ↑{monitor.human_rate(snap['up'])}"
        except Exception:
            rates = ""
        if rates:
            draw.text((W // 2, 288), rates, font=_font(FONT, 15), fill=(120, 128, 142), anchor="mm")

    draw.rectangle([60, 312, 420, 313], fill=(60, 64, 76))
    draw.rectangle([60, 312, 60 + 90, 313], fill=accent)
    img.save(out_path, "JPEG", quality=46, optimize=True, progressive=False, subsampling=2)
    return out_path.stat().st_size
