#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Visual fake Neos75 screen: pop up a black-themed window showing what
the keyboard screen would show, without any real keyboard.

Run from the repository root on Windows:

    python tests\\show_fake_screen.py

What it does:
  * Renders lyric frames with the real lyrics_screen.render().
  * Sends each frame to a FakeScreen (no HID, no USB) through the real
    Engine._show() / Engine._push() path.
  * Shows the latest pushed frame in a black tkinter window, scaled up
    from 480x360 to 720x540. The picture itself is exactly what would
    be pushed to the keyboard.
  * Cycles: a few lyric lines (about 3s each), then a black stock page
    (about 8s), then back to lyrics, until you close the window.

Stock page: prices come from Yahoo Finance (no API key needed).
Edit STOCKS below to watch other symbols, e.g. "600519.SS" for
Kweichow Moutai, "0700.HK" for Tencent. If the network fails, the
page shows "--" instead of crashing.
Price colours follow the Chinese convention: up = red, down = green.

This window opens on YOUR computer. Muse cannot see it directly:
if you want Muse to look at the picture, take a screenshot of the
window and send the screenshot in the chat.

The script never touches your real config: it points NEOS75_LYRICS_DIR
at a temporary directory before importing lyrics_screen.
"""

import io
import json
import os
import queue
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path

# 1) Use a temporary work directory, so the real user config is untouched.
_TMPDIR = tempfile.mkdtemp(prefix="neos75_fake_screen_")
os.environ["NEOS75_LYRICS_DIR"] = _TMPDIR

# 2) This project is Windows-only (lyrics_screen uses ctypes.windll
# at import time), so fail with a clear message on other platforms
# instead of a cryptic AttributeError.
if sys.platform != "win32":
    raise SystemExit("请在 Windows 上运行这个脚本, 项目本身只支持 Windows。")

# 3) Find src/ next to this tests/ directory, no matter the working directory.
REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"
if not SRC_DIR.is_dir():
    raise SystemExit(f"cannot find src/ next to this script: expected {SRC_DIR}")
sys.path.insert(0, str(SRC_DIR))

import tkinter as tk  # noqa: E402

from PIL import Image, ImageDraw, ImageTk  # noqa: E402

import lyrics_screen  # noqa: E402

# Display size: the real frame is 480x360, shown 1.5x larger.
SHOW_W, SHOW_H = 720, 540

# --- viewer theme (black) ----------------------------------------------
BG = "#000000"
PANEL = "#0b0d12"
BEZEL = "#17191f"
FG = "#f2f3f5"
MUTED = "#9aa3b2"
ACCENT = "#00c896"
LINE = "#22262e"

LINES = [
    ("测试歌词第一行", "第二句歌词在这里"),
    ("第二句歌词在这里", "第三句，换行看看效果"),
    ("第三句，换行看看效果", "最后一句测试歌词"),
    ("最后一句测试歌词", ""),
]

# Edit this list to watch other stocks. Yahoo symbols: "AAPL" for
# Apple, "600519.SS" for Kweichow Moutai, "0700.HK" for Tencent.
STOCKS = ["AAPL", "NVDA", "MSFT", "TSLA"]


class FakeScreen:
    """A fake Neos75 screen: records pushes and hands the JPEG bytes
    to the UI thread through a queue, instead of talking to HID."""

    def __init__(self, frame_queue):
        self.frame_queue = frame_queue
        self.pushes = 0

    def status(self):
        return {"width": 480, "height": 360, "support_jpg": True, "space": 0}

    def push(self, jpeg: bytes, timeout=1.5):
        assert jpeg[:2] == b"\xff\xd8", "pushed data is not a JPEG"
        self.pushes += 1
        self.frame_queue.put((self.pushes, bytes(jpeg)))
        return {"ms": 0, "reports": 1, "bytes": len(jpeg)}

    def close(self):
        pass


def fetch_stocks(symbols):
    """Return a list of (symbol, price_text, change_text, is_up).

    Uses Yahoo Finance chart API, no key. Any failure returns "--"
    rows instead of raising, so the viewer never crashes on network.
    """
    rows = []
    for symbol in symbols:
        price, change, is_up = "--", "--", None
        try:
            url = (
                "https://query1.finance.yahoo.com/v8/finance/chart/"
                + urllib.parse.quote(symbol)
                + "?range=1d&interval=1d"
            )
            req = urllib.request.Request(
                url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
            )
            with urllib.request.urlopen(req, timeout=8) as resp:
                data = json.load(resp)
            meta = data["chart"]["result"][0]["meta"]
            now = float(meta["regularMarketPrice"])
            prev = float(meta.get("chartPreviousClose") or meta.get("previousClose"))
            if prev:
                pct = (now - prev) / prev * 100.0
                price = f"{now:,.2f}"
                change = f"{'+' if pct >= 0 else ''}{pct:.2f}%"
                is_up = pct >= 0
        except Exception:
            pass
        rows.append((symbol, price, change, is_up))
    return rows


def render_stock_frame(rows):
    """Render a black 480x360 stock page and return its JPEG bytes."""
    from PIL import Image as _Image

    img = _Image.new("RGB", (480, 360), (0, 0, 0))
    draw = ImageDraw.Draw(img)
    font = lyrics_screen._font
    f_title = font(lyrics_screen.FONT_BOLD, 22)
    f_sym = font(lyrics_screen.FONT_BOLD, 20)
    f_price = font(lyrics_screen.FONT_BOLD, 26)
    f_chg = font(lyrics_screen.FONT, 20)
    f_small = font(lyrics_screen.FONT, 14)

    draw.text((24, 18), "股票", font=f_title, fill=(242, 243, 245))
    draw.text(
        (456, 26),
        time.strftime("%H:%M"),
        font=f_small,
        fill=(154, 163, 178),
        anchor="ra",
    )
    draw.line([(24, 56), (456, 56)], fill=(34, 38, 46), width=1)

    y = 72
    for symbol, price, change, is_up in rows:
        if is_up is None:
            colour = (154, 163, 178)
        elif is_up:
            colour = (255, 77, 79)  # up = red (Chinese convention)
        else:
            colour = (0, 200, 150)  # down = green
        draw.text((24, y), symbol, font=f_sym, fill=(242, 243, 245))
        draw.text((456, y + 2), price, font=f_price, fill=(242, 243, 245), anchor="ra")
        draw.text((456, y + 38), change, font=f_chg, fill=colour, anchor="ra")
        y += 70
        if y < 340:
            draw.line([(24, y - 8), (456, y - 8)], fill=(34, 38, 46), width=1)

    draw.text(
        (24, 336),
        "数据: Yahoo Finance · 涨红跌绿",
        font=f_small,
        fill=(154, 163, 178),
    )
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=46, optimize=True, progressive=False, subsampling=2)
    return buf.getvalue()


def main():
    frame_queue = queue.Queue()
    stop_event = threading.Event()
    state = {"photo": None, "mode": "歌词"}

    root = tk.Tk()
    root.title("模拟 Neos75 屏幕 · 480×360")
    root.configure(bg=BG)
    root.resizable(False, False)

    # Top bar: green dot + title + current mode, all on black.
    top = tk.Frame(root, bg=BG)
    top.pack(fill="x", padx=16, pady=(12, 8))
    tk.Label(top, text="●", bg=BG, fg=ACCENT, font=("Segoe UI", 11)).pack(side="left")
    tk.Label(
        top, text="NEOS75 · 模拟屏幕", bg=BG, fg=FG, font=("Segoe UI", 12, "bold")
    ).pack(side="left", padx=(6, 0))
    mode_label = tk.Label(top, text="歌词", bg=BG, fg=MUTED, font=("Segoe UI", 10))
    mode_label.pack(side="right")

    # Bezel around the screen, like a keyboard screen frame.
    bezel = tk.Frame(root, bg=BEZEL, padx=10, pady=10)
    bezel.pack(padx=16)
    image_label = tk.Label(bezel, bg=BG, text="正在渲染第一帧…", fg=MUTED)
    image_label.pack()

    # Bottom status bar on a dark panel.
    bottom = tk.Frame(root, bg=PANEL)
    bottom.pack(fill="x", padx=16, pady=(10, 6))
    status_label = tk.Label(
        bottom, text="等待第一次推送…", bg=PANEL, fg=MUTED, font=("Segoe UI", 10)
    )
    status_label.pack(side="left", padx=10, pady=6)
    tk.Label(
        root,
        text="这是电脑上的模拟屏，不是真键盘。关闭窗口即停止。",
        bg=BG,
        fg=MUTED,
        font=("Segoe UI", 9),
    ).pack(pady=(0, 12))

    def poll_queue():
        # Only the main (UI) thread may touch tkinter widgets.
        try:
            while True:
                pushes, jpeg = frame_queue.get_nowait()
                with Image.open(io.BytesIO(jpeg)) as img:
                    img = img.convert("RGB").resize(
                        (SHOW_W, SHOW_H), Image.LANCZOS
                    )
                photo = ImageTk.PhotoImage(img)
                state["photo"] = photo  # keep a reference, or it disappears
                image_label.config(image=photo, text="")
                mode_label.config(text=state["mode"])
                status_label.config(
                    text=f"第 {pushes} 次推送 · {len(jpeg)} 字节 · 480×360"
                )
        except queue.Empty:
            pass
        if not stop_event.is_set():
            root.after(100, poll_queue)

    def worker():
        cfg = {
            "bg_color": "#090a0d",
            "frost": False,
            "cover_bg": False,
            "bg_image": "",
        }
        engine = lyrics_screen.Engine(
            on_status=lambda m: None,
            on_line=lambda *a: None,
            on_error=lambda m: None,
        )
        engine.cfg = dict(cfg)
        engine.track = "测试歌曲"
        engine.artist = "测试歌手"
        engine.cover_img = None
        engine.screen = FakeScreen(frame_queue)
        # Skip the engine's 2.2s minimum gap between pushes for the
        # first frame; later frames are spaced 3s+ apart anyway.
        engine.last_push_at = time.time() - 10
        index = 0
        while not stop_event.is_set():
            # Lyrics first, then one black stock page, then repeat.
            for _ in range(len(LINES)):
                if stop_event.is_set():
                    return
                state["mode"] = "歌词"
                current, nxt = LINES[index % len(LINES)]
                index += 1
                try:
                    engine._show(current, nxt)
                except Exception:
                    pass
                if stop_event.wait(3.0):
                    return
            if stop_event.is_set():
                return
            # Stock page, pushed from memory (no temp file).
            state["mode"] = "股票"
            try:
                rows = fetch_stocks(STOCKS)
                jpeg = render_stock_frame(rows)
                engine.last_push_at = time.time() - 10
                engine._push_bytes(jpeg)
            except Exception:
                pass
            if stop_event.wait(8.0):
                return

    def on_close():
        stop_event.set()
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", on_close)
    threading.Thread(target=worker, daemon=True).start()
    root.after(100, poll_queue)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
