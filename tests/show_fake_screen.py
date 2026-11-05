#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Visual fake Neos75 screen: pop up a window showing what the keyboard
screen would show, without any real keyboard.

Run from the repository root on Windows:

    python tests\\show_fake_screen.py

What it does:
  * Renders lyric frames with the real lyrics_screen.render().
  * Sends each frame to a FakeScreen (no HID, no USB) through the real
    Engine._show() / Engine._push() path.
  * Shows the latest pushed frame in a tkinter window, scaled up from
    480x360 to 720x540 so it is easy to see. The picture itself is
    exactly what would be pushed to the keyboard.
  * Cycles through a few test lyric lines, about one line every 3
    seconds, until you close the window.

This window opens on YOUR computer. Muse cannot see it directly:
if you want Muse to look at the picture, take a screenshot of the
window and send the screenshot in the chat.

The script never touches your real config: it points NEOS75_LYRICS_DIR
at a temporary directory before importing lyrics_screen.
"""

import io
import os
import queue
import sys
import tempfile
import threading
import time
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

from PIL import Image, ImageTk  # noqa: E402

import lyrics_screen  # noqa: E402

# Display size: the real frame is 480x360, shown 1.5x larger.
SHOW_W, SHOW_H = 720, 540

LINES = [
    ("测试歌词第一行", "第二句歌词在这里"),
    ("第二句歌词在这里", "第三句，换行看看效果"),
    ("第三句，换行看看效果", "最后一句测试歌词"),
    ("最后一句测试歌词", ""),
]


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


def main():
    frame_queue = queue.Queue()
    stop_event = threading.Event()

    root = tk.Tk()
    root.title("模拟 Neos75 屏幕 · 480×360")
    root.resizable(False, False)

    image_label = tk.Label(root, text="正在渲染第一帧…", width=720, height=540)
    image_label.pack()
    status_label = tk.Label(root, text="等待第一次推送…")
    status_label.pack(pady=(4, 2))
    hint_label = tk.Label(
        root,
        text="这是电脑上的模拟屏，不是真键盘。关闭窗口即停止。",
        fg="gray",
    )
    hint_label.pack(pady=(0, 8))

    state = {"photo": None}

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
        # first frame; later frames are spaced 3s apart anyway.
        engine.last_push_at = time.time() - 10
        index = 0
        while not stop_event.is_set():
            current, nxt = LINES[index % len(LINES)]
            index += 1
            try:
                engine._show(current, nxt)
            except Exception:
                # A single bad frame must not kill the viewer; the next
                # scheduled frame will try again.
                pass
            # Wait 3s (interruptible by closing the window) before the
            # next lyric line.
            stop_event.wait(3.0)

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
