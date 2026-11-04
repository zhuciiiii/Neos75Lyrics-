#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""No-hardware benchmark + fake-screen test for Neos75Lyrics.

Run from the repository root on Windows, no Neos75 keyboard needed:

    python tests\\bench_no_hardware.py

What it checks (all offline, no SMTC / OCR / network):
  1. render() can draw a 480x360 lyric frame, and how long it takes.
  2. How many times ImageFont.truetype() is called while rendering.
     This is the number the font-cache optimisation should bring down.
  3. A FakeScreen (a fake keyboard) receives exactly the JPEG bytes
     that render() produced, without any real HID device.

How to judge an optimisation:
  * Run this script BEFORE the change and write down the numbers.
  * Run it again AFTER the change, with the same machine and input.
  * Success = all checks still pass, the picture is still 480x360,
    and the time / truetype-call count went down noticeably.
  * This script does NOT prove anything about the real keyboard:
    HID rejection rate and the on-screen result still need real hardware.

The script never touches your real config: it points NEOS75_LYRICS_DIR
at a temporary directory before importing lyrics_screen.
"""

import io
import os
import sys
import tempfile
import time
from pathlib import Path

# 1) Use a temporary work directory, so the real user config is untouched.
_TMPDIR = tempfile.mkdtemp(prefix="neos75_bench_")
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

from PIL import Image, ImageFont  # noqa: E402

import lyrics_screen  # noqa: E402


class FakeScreen:
    """A fake Neos75 screen: records pushes instead of talking to HID."""

    def __init__(self):
        self.pushes = 0
        self.last_bytes = b""
        self.last_ms = 0

    def status(self):
        return {"width": 480, "height": 360, "support_jpg": True, "space": 0}

    def push(self, jpeg: bytes, timeout=1.5):
        assert jpeg[:2] == b"\xff\xd8", "pushed data is not a JPEG"
        self.pushes += 1
        self.last_bytes = bytes(jpeg)
        return {"ms": 0, "reports": 1, "bytes": len(jpeg)}

    def close(self):
        pass


def main():
    # --- count font loads -------------------------------------------------
    counter = {"truetype_calls": 0}
    original_truetype = ImageFont.truetype

    def counting_truetype(path, size, *args, **kwargs):
        counter["truetype_calls"] += 1
        return original_truetype(path, size, *args, **kwargs)

    ImageFont.truetype = counting_truetype

    cfg = {"bg_color": "#090a0d", "frost": False, "cover_bg": False, "bg_image": ""}
    tmp = Path(_TMPDIR)
    out1 = tmp / "frame1.jpg"
    out2 = tmp / "frame2.jpg"

    # --- benchmark render() ----------------------------------------------
    renders = 20
    start = time.perf_counter()
    for _ in range(renders):
        size = lyrics_screen.render(
            "测试歌曲", "测试歌手", "测试歌词第一行", "下一句歌词", out1, cfg=cfg
        )
    elapsed = time.perf_counter() - start
    avg_ms = elapsed / renders * 1000.0
    calls_per_render = counter["truetype_calls"] / renders

    assert out1.exists() and size > 0, "render() did not produce a file"
    assert out1.read_bytes()[:2] == b"\xff\xd8", "render() output is not a JPEG"
    with Image.open(out1) as img:
        assert img.size == (480, 360), f"unexpected frame size: {img.size}"

    # Same input again: the picture should be the same size.
    lyrics_screen.render(
        "测试歌曲", "测试歌手", "测试歌词第一行", "下一句歌词", out2, cfg=cfg
    )
    with Image.open(out2) as img:
        assert img.size == (480, 360), f"unexpected frame size: {img.size}"

    # --- fake-screen push --------------------------------------------------
    engine = lyrics_screen.Engine(
        on_status=lambda m: None,
        on_line=lambda *a: None,
        on_error=lambda m: None,
    )
    engine.cfg = dict(cfg)
    engine.cover_img = None
    fake = FakeScreen()
    engine.screen = fake

    # Skip the engine's 2.2s minimum gap between pushes for this test.
    engine.last_push_at = time.time() - 10
    engine._show("测试歌词第一行", "下一句歌词")
    assert fake.pushes == 1, f"expected 1 push from _show(), got {fake.pushes}"
    with Image.open(io.BytesIO(fake.last_bytes)) as img:
        assert img.size == (480, 360), f"pushed frame size: {img.size}"

    # _push() must deliver exactly the bytes from the file, unchanged.
    data = out1.read_bytes()
    engine.last_push_at = time.time() - 10
    engine._push(out1)
    assert fake.pushes == 2, f"expected 2 pushes, got {fake.pushes}"
    assert fake.last_bytes == data, "_push() changed the JPEG bytes"

    # Indicator only (not a pass/fail check): does _push_bytes() still
    # write a temporary file to disk? Before the in-memory optimisation
    # the answer is yes; after it, it should be no.
    idle_path = lyrics_screen.WORK / "idle_frame.jpg"
    before_exists = idle_path.exists()
    before_mtime = idle_path.stat().st_mtime if before_exists else None
    engine.last_push_at = time.time() - 10
    engine._push_bytes(data)
    assert fake.pushes == 3 and fake.last_bytes == data
    disk_written = (not before_exists and idle_path.exists()) or (
        before_exists and idle_path.stat().st_mtime != before_mtime
    )

    print("全部检查通过 (all checks passed)")
    print(f"渲染次数: {renders}")
    print(f"渲染总耗时: {elapsed:.2f} 秒, 平均: {avg_ms:.1f} 毫秒/张")
    print(f"字体加载总次数: {counter['truetype_calls']}, "
          f"平均: {calls_per_render:.1f} 次/张")
    print(f"渲染输出大小: {out1.stat().st_size} 字节, 尺寸: 480x360")
    print(f"假键盘收到推送: {fake.pushes} 次, 最后一次: {len(fake.last_bytes)} 字节")
    print(f"_push_bytes() 是否写了临时文件: {disk_written} "
          f"(优化前为 True, 内存推送优化后应为 False)")
    print("请把上面的数字记下来, 优化后再跑一次对比。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
