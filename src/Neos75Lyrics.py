#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Neos75 歌词屏 v3 — 歌词实时上屏 + 屏幕固件助手。"""
import io
import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
import traceback
from tkinter import colorchooser, filedialog, messagebox

import pystray
from PIL import Image, ImageDraw, ImageTk

import logsetup
import lyrics_screen
from ui_crop import CropDialog, preview_thumb

# ------------------------------------------------------------------ design tokens
# Product design system (v4): Neos75 screen control centre. Colours are the
# spec palette; every surface, border and state colour comes from here.
BG = "#0D111B"          # main background
SIDEBAR = "#111827"      # left navigation
CARD = "#1A2231"         # card background
CARD_HI = "#222D40"      # hover / raised surface
FG = "#F4F6FC"           # primary text
MUTED = "#A0AEC0"        # secondary text
DIM = "#7A8699"          # captions / disabled text
ACCENT = "#7586FF"       # primary accent (restrained blue-violet)
ACCENT2 = "#A78BFA"      # secondary accent
ACCENT_DARK = "#5A6BE8"
MINT = "#34C99A"         # success / connected
WARN = "#F4B860"         # warning / waiting
DANGER = "#F07178"       # error / dangerous action
BORDER = "#26314A"       # hairline borders

FONT = "Microsoft YaHei UI"   # Chinese UI text
FONT_EN = "Segoe UI Variable"  # Latin / numerals
FONT_MONO = "Consolas"         # log viewer

VERSION = "0.0.2"

if getattr(sys, "frozen", False):
    BASE = os.path.dirname(sys.executable)
    RES = getattr(sys, "_MEIPASS", BASE)
else:
    BASE = os.path.dirname(os.path.abspath(__file__))
    RES = BASE
ICON_ICO = os.path.join(RES, "assets", "fish.ico")
ICON_PNG = os.path.join(RES, "assets", "fish.png")


def round_rect(canvas, x1, y1, x2, y2, r, **kw):
    points = [
        x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y2 - r, x2, y2,
        x2 - r, y2, x1 + r, y2, x1, y2, x1, y2 - r, x1, y1 + r, x1, y1,
    ]
    return canvas.create_polygon(points, smooth=True, **kw)


class Toggle(tk.Button):
    """A pill-shaped switch: shows 开 / 关 and flips on click."""

    def __init__(self, master, text, var, command=None, accent=None, **kw):
        self.var = var
        self._label = text
        self._accent = accent or accent_color()
        super().__init__(master, bd=0, relief="flat", cursor="hand2",
                         font=(FONT, 9), padx=12, pady=6, **kw)
        self.configure(command=self._click)
        self.refresh()

    def _click(self):
        self.var.set(not bool(self.var.get()))
        self.refresh()
        cb = getattr(self, "_cb", None)
        if cb:
            cb()

    def oncommand(self, cb):
        self._cb = cb

    def refresh(self):
        on = bool(self.var.get())
        self.config(text=f"{self._label}  开" if on else f"{self._label}  关",
                    bg=self._accent if on else CARD_HI,
                    fg="#FFFFFF" if on else MUTED,
                    activebackground=self._accent if on else BORDER,
                    activeforeground="#FFFFFF" if on else FG)


def accent_color():
    try:
        return ACCENT
    except Exception:
        return "#4D6BFE"


class Card(tk.Frame):
    """Rounded panel with an inner frame."""

    def __init__(self, master, bg=CARD, radius=18, padx=18, pady=16, expand=False, **kw):
        super().__init__(master, bg=master["bg"] if "bg" in master.keys() else BG, **kw)
        self._bg = bg
        self._r = radius
        self._pad = (padx, pady)
        self._expand = expand
        self.canvas = tk.Canvas(self, bg=self["bg"], highlightthickness=0, bd=0, height=60)
        self.canvas.pack(fill="both", expand=True)
        self.body = tk.Frame(self.canvas, bg=bg)
        self._win = self.canvas.create_window(padx, pady, window=self.body, anchor="nw")
        self.canvas.bind("<Configure>", self._resize)
        if not expand:
            self.body.bind("<Configure>", self._fit_height)

    def _resize(self, event):
        self.canvas.delete("bgshape")
        round_rect(self.canvas, 0, 0, event.width, event.height, self._r, fill=self._bg,
                   outline=BORDER, width=1, tags="bgshape")
        self.canvas.tag_lower("bgshape")
        if self._expand:
            self.canvas.itemconfigure(self._win, width=max(1, event.width - 2 * self._pad[0]),
                                      height=max(1, event.height - 2 * self._pad[1]))
        else:
            self.canvas.itemconfigure(self._win, width=max(1, event.width - 2 * self._pad[0]))

    def _fit_height(self, event):
        if self._expand:
            return
        need = self.body.winfo_reqheight() + 2 * self._pad[1]
        if abs(need - self.canvas.winfo_height()) > 2:
            self.canvas.configure(height=need)


class Icon(tk.Canvas):
    """Lucide-style stroke icon drawn on a canvas (24x24 grid, 2px stroke).
    tkinter has no SVG support, so icons are vector-drawn here instead of
    emoji or bitmap glyphs. One place defines every icon, so they stay
    visually consistent across the app."""

    PATHS = {
        "grid": [("rect", 3, 3, 9, 9), ("rect", 15, 3, 21, 9),
                 ("rect", 3, 15, 9, 21), ("rect", 15, 15, 21, 21)],
        "music": [("circle", 6, 18, 3), ("circle", 18, 16, 3),
                  ("line", 9, 18, 9, 5), ("line", 21, 16, 21, 3), ("line", 9, 5, 21, 3)],
        "trophy": [("poly", [(8, 4), (16, 4), (16, 10), (12, 13), (12, 13)], False),
                   ("arc", 4, 5, 8, 10, 90, 180), ("arc", 16, 5, 20, 10, 270, 180),
                   ("line", 12, 13, 12, 17), ("line", 8, 20, 16, 20), ("line", 10, 17, 14, 17)],
        "activity": [("poly", [(3, 12), (7, 12), (10, 5), (14, 19), (17, 12), (21, 12)], False)],
        "palette": [("circle", 12, 12, 9), ("dot", 8.5, 10, 1.1), ("dot", 12, 7.5, 1.1),
                    ("dot", 15.5, 10, 1.1), ("dot", 15, 15.5, 1.1)],
        "file": [("poly", [(6, 3), (14, 3), (18, 7), (18, 21), (6, 21)], True),
                 ("line", 14, 3, 14, 7), ("line", 18, 7, 14, 7),
                 ("line", 9, 12, 15, 12), ("line", 9, 16, 15, 16)],
        "cpu": [("rect", 6, 6, 18, 18), ("rect", 10, 10, 14, 14),
                ("line", 9, 3, 9, 6), ("line", 15, 3, 15, 6), ("line", 9, 18, 9, 21),
                ("line", 15, 18, 15, 21), ("line", 3, 9, 6, 9), ("line", 3, 15, 6, 15),
                ("line", 18, 9, 21, 9), ("line", 18, 15, 21, 15)],
        "play": [("poly", [(8, 5), (8, 19), (19, 12)], True)],
        "refresh": [("arc", 4, 4, 20, 20, 30, 280), ("poly", [(20, 3), (20, 8), (15, 8)], False)],
        "alert": [("poly", [(12, 4), (21, 20), (3, 20)], True),
                  ("line", 12, 10, 12, 15), ("dot", 12, 17.5, 1.0)],
        "check": [("poly", [(4, 12.5), (9.5, 18), (20, 6.5)], False)],
        "download": [("line", 12, 4, 12, 15), ("poly", [(6, 10), (12, 16), (18, 10)], False),
                     ("line", 4, 20, 20, 20)],
        "folder": [("poly", [(3, 7), (9, 7), (11, 4), (14, 4), (21, 4), (21, 19), (3, 19)], True)],
        "eye": [("arc", 2, 9, 22, 15, 300, 300), ("circle", 12, 12, 3)],
    }

    def __init__(self, master, name, size=20, color=None, bg=None):
        super().__init__(master, width=size, height=size, bg=bg or master["bg"],
                         highlightthickness=0, bd=0)
        self._color = color or MUTED
        self._size = size
        self._name = name
        self._draw()

    def _draw(self):
        self.delete("all")
        s = self._size / 24.0
        w = max(1.5, 2 * s)
        # Tk canvas only accepts -capstyle/-joinstyle on lines; rectangles,
        # ovals and arcs reject them (that crashed startup), so each item type
        # gets only the options it supports.
        c = self._color
        line_kw = dict(width=w, capstyle="round", joinstyle="round")
        poly_kw = dict(width=w, joinstyle="round")
        shape_kw = dict(width=w)
        for op in self.PATHS.get(self._name, []):
            if op[0] == "line":
                self.create_line(op[1] * s, op[2] * s, op[3] * s, op[4] * s, fill=c, **line_kw)
            elif op[0] == "poly":
                pts = [v * s for p in op[1] for v in p]
                self.create_line(*pts, fill=c, **line_kw) if not op[2] else \
                    self.create_polygon(*pts, outline=c, fill="", **poly_kw)
            elif op[0] == "rect":
                self.create_rectangle(op[1] * s, op[2] * s, op[3] * s, op[4] * s,
                                      outline=c, fill="", **shape_kw)
            elif op[0] == "circle":
                r = op[3] * s
                self.create_oval(op[1] * s - r, op[2] * s - r, op[1] * s + r, op[2] * s + r,
                                 outline=c, fill="", **shape_kw)
            elif op[0] == "dot":
                r = op[3] * s
                self.create_oval(op[1] * s - r, op[2] * s - r, op[1] * s + r, op[2] * s + r,
                                 outline=c, fill=c)
            elif op[0] == "arc":
                self.create_arc(op[1] * s, op[2] * s, op[3] * s, op[4] * s,
                                start=op[5], extent=op[6], outline=c, style="arc", **shape_kw)

    def set_color(self, color):
        self._color = color
        self._draw()


class ScreenPreview(tk.Frame):
    """Keyboard-screen preview: a dark device bezel holding the exact frame
    that was pushed to the keyboard (480x360, 4:3). The image always comes
    from a real render or the real pushed JPEG file — never invented data.
    Until the first push it shows an honest empty state, not a mock screen."""

    def __init__(self, master, width=440, caption="键盘屏幕预览", **kw):
        super().__init__(master, bg=BG, **kw)
        self._pw = width
        self._ph = int(width * 3 / 4)
        self._photo = None
        bezel = tk.Frame(self, bg="#05070C", padx=10, pady=10,
                         highlightbackground=BORDER, highlightthickness=1)
        bezel.pack(fill="x")
        self._label = tk.Label(bezel, bg="#000000", fg=DIM, font=(FONT, 10),
                               width=self._pw // 10, height=self._ph // 22)
        self._label.pack()
        self.set_empty("暂无推送内容")
        cap = tk.Frame(self, bg=BG)
        cap.pack(fill="x", pady=(8, 0))
        tk.Label(cap, text=caption, bg=BG, fg=MUTED, font=(FONT, 9)).pack(side="left")
        self._mode = tk.Label(cap, text="480 × 360 · JPEG", bg=BG, fg=DIM, font=(FONT_EN, 8))
        self._mode.pack(side="right")

    def set_image(self, pil_img):
        try:
            img = pil_img.convert("RGB").resize((self._pw, self._ph), Image.LANCZOS)
            self._photo = ImageTk.PhotoImage(img)
            self._label.config(image=self._photo, text="", width=self._pw, height=self._ph)
        except Exception:
            pass

    def set_empty(self, text="暂无推送内容"):
        self._photo = None
        self._label.config(image="", text=text, width=self._pw // 10, height=self._ph // 22)

    def set_mode(self, text):
        self._mode.config(text=text)


class FakeScreen:
    """A fake Neos75 screen for use without real hardware: records
    pushes and hands the JPEG bytes to the main thread through the
    app event queue, instead of talking to HID. Engine.check_device()
    accepts it because status() reports 480x360."""

    def __init__(self, events):
        self.events = events
        self.pushes = 0

    def status(self):
        return {"width": 480, "height": 360, "support_jpg": True, "space": 0}

    def push(self, jpeg, timeout=1.5):
        assert jpeg[:2] == b"\xff\xd8", "pushed data is not a JPEG"
        self.pushes += 1
        self.events.put(("simframe", (self.pushes, bytes(jpeg))))
        return {"ms": 0, "reports": 1, "bytes": len(jpeg)}

    def close(self):
        pass


PRESETS = {
    "dark": dict(bg="#0D111B", sidebar="#111827", card="#1A2231", card_hi="#222D40",
                 fg="#F4F6FC", muted="#A0AEC0", dim="#7A8699", accent="#7586FF",
                 border="#26314A"),
    "red": dict(bg="#0B0A0C", sidebar="#0F0C0E", card="#171114", card_hi="#20171B",
                fg="#F7F2F3", muted="#A08F94", dim="#6B5C60", accent="#E63C3C",
                border="#2A1D21"),
    "black": dict(bg="#000000", sidebar="#050506", card="#0C0C0E", card_hi="#141418",
                  fg="#EDEFF3", muted="#7E8493", dim="#4E5361", accent="#B8BECC",
                  border="#17171C"),
    "light": dict(bg="#F4F5F8", sidebar="#FFFFFF", card="#FFFFFF", card_hi="#ECEEF3",
                  fg="#1D1F26", muted="#5A6070", dim="#9AA1B0", accent="#E63C3C",
                  border="#E2E5EC"),
}


def apply_palette(name):
    """Switch the whole UI palette and repaint."""
    global BG, SIDEBAR, CARD, CARD_HI, FG, MUTED, DIM, ACCENT, BORDER
    preset = PRESETS.get(name) or PRESETS["dark"]
    BG, SIDEBAR, CARD, CARD_HI = preset["bg"], preset["sidebar"], preset["card"], preset["card_hi"]
    FG, MUTED, DIM = preset["fg"], preset["muted"], preset["dim"]
    ACCENT, BORDER = preset["accent"], preset["border"]


class RoundButton(tk.Canvas):
    """Fully rounded button drawn on a canvas (Tk buttons are square)."""

    def __init__(self, master, text, command, bg=None, fg="#FFFFFF", width=None,
                 height=34, radius=17, size=10, accent=None):
        host_bg = master["bg"] if "bg" in master.keys() else BG
        px = int(width * size * 0.95) + 26 if width else 110
        super().__init__(master, width=px, height=height, bg=host_bg,
                         highlightthickness=0, bd=0, cursor="hand2")
        self._text = text
        self._cmd = command
        self._bg, self._fg = bg or (accent or ACCENT), fg
        self._r, self._size = radius, size
        self._enabled = True
        self._hover = False
        self.bind("<Button-1>", self._click)
        self.bind("<Enter>", lambda e: self._set_hover(True))
        self.bind("<Leave>", lambda e: self._set_hover(False))
        self._draw()

    def _draw(self):
        self.delete("all")
        w = int(self["width"]); h = int(self["height"])
        bg = self._bg if self._enabled else CARD_HI
        if self._hover and self._enabled:
            bg = _shade(bg, 1.12)
        round_rect(self, 1, 1, w - 1, h - 1, self._r, fill=bg, outline="")
        fg = self._fg if self._enabled else DIM
        self.create_text(w // 2, h // 2 + 1, text=self._text, fill=fg,
                         font=(FONT, self._size, "bold"))

    def _set_hover(self, on):
        self._hover = on
        self._draw()

    def _click(self, _event=None):
        if self._enabled and callable(self._cmd):
            self._cmd()

    def config(self, **kw):
        if "text" in kw:
            self._text = kw.pop("text")
        if "state" in kw:
            self._enabled = kw.pop("state") != "disabled"
        if "bg" in kw:
            self._bg = kw.pop("bg")
        if "fg" in kw:
            self._fg = kw.pop("fg")
        for key in ("command", "width", "height"):
            kw.pop(key, None)
        self._draw()

    configure = config


def _shade(hex_color, factor):
    try:
        hex_color = hex_color.lstrip("#")
        r, g, b = (int(hex_color[i:i + 2], 16) for i in (0, 2, 4))
        r = max(0, min(255, int(r * factor)))
        g = max(0, min(255, int(g * factor)))
        b = max(0, min(255, int(b * factor)))
        return "#%02X%02X%02X" % (r, g, b)
    except Exception:
        return hex_color


def pill(master, text, command, kind="primary", width=12, size=10, accent=None):
    accent = accent or ACCENT
    colors = {
        "primary": (accent, "#FFFFFF"),
        "mint": (MINT, "#04241D"),
        "ghost": (CARD_HI, FG),
        "danger": (DANGER, "#2A0B0B"),
    }[kind]
    bg, fg = colors
    return RoundButton(master, text, command, bg=bg, fg=fg, width=width, size=size,
                       height=int(size * 3.2), radius=int(size * 1.6), accent=accent)


class App:
    def __init__(self, root):
        logsetup.setup()
        logsetup.install_hooks(root)
        self.root = root
        self.engine = None
        self.thread = None
        self.events = queue.Queue()
        self.cfg = lyrics_screen.load_config()
        apply_palette(self.cfg.get("palette") or "dark")
        # v3 palette: users who never picked a custom accent (still on the old
        # default blue) move to the new violet accent; custom choices are kept.
        if self.cfg.get("accent") in (None, "#4D6BFE", "#8B5CF6"):
            self.cfg["accent"] = ACCENT
        self.accent = self.cfg.get("accent") or ACCENT
        logsetup.info("app start v%s config=%s" % (
            VERSION, {k: v for k, v in self.cfg.items() if k not in ("bg_crop", "idle_crop")}))
        self.primary_btns = []
        self.last_frac = 0.0
        self.tray = None
        self.page = "lyrics"
        self.preview_img = None
        self.sim_enabled = bool(self.cfg.get("sim_screen", False))
        self.sim_window = None
        self.sim_label = None
        self.sim_status_label = None
        self.sim_photo = None
        self.previews = {}  # page key -> ScreenPreview, fed by _tick_preview
        self.skin_labels = {}
        self.idle_preview = None
        self._build()
        if self.sim_enabled:
            self._open_sim_window()
        self._refresh_thumbs()
        self.root.after(1500, self._check_aula)
        try:
            self.root.attributes("-alpha", 1.0)
        except Exception:
            pass
        self.root.after(80, self._drain)
        self.root.after(500, self._tick_preview)

    # ------------------------------------------------------------------ shell
    def _build(self):
        root = self.root
        root.title(f"Neos75 屏幕助手 v{VERSION}")  # window title stays searchable
        try:
            import ctypes
            _hwnd = ctypes.windll.user32.GetParent(root.winfo_id())
            ctypes.windll.dwmapi.DwmSetWindowAttribute(
                _hwnd, 20, ctypes.byref(ctypes.c_int(1)), ctypes.sizeof(ctypes.c_int))
        except Exception:
            pass  # dark title bar is cosmetic; fall back to the system default
        root.configure(bg=BG)
        root.geometry("1200x800")
        root.update_idletasks()
        _sw, _sh = root.winfo_screenwidth(), root.winfo_screenheight()
        root.geometry(f"1200x800+{max(0, (_sw - 1200) // 2)}+{max(0, (_sh - 800) // 2)}")
        root.minsize(1024, 640)
        try:
            if os.path.exists(ICON_ICO):
                root.iconbitmap(ICON_ICO)
        except Exception:
            pass

        self.sidebar = tk.Frame(root, bg=SIDEBAR, width=224)
        self.sidebar.pack(side="left", fill="y")
        self.sidebar.pack_propagate(False)
        self._build_sidebar()

        tk.Frame(root, bg=BORDER, width=1).pack(side="left", fill="y")

        self.right = tk.Frame(root, bg=BG)
        self.right.pack(side="left", fill="both", expand=True)
        right = self.right

        self.topbar = tk.Frame(right, bg=BG, height=64)
        self.topbar.pack(side="top", fill="x")
        self.topbar.pack_propagate(False)
        self._build_topbar()
        tk.Frame(right, bg=BORDER, height=1).pack(side="top", fill="x")

        self.statusbar = tk.Frame(right, bg=SIDEBAR, height=30)
        self.statusbar.pack(side="bottom", fill="x")
        self.statusbar.pack_propagate(False)
        self._build_statusbar()

        self.main = tk.Frame(right, bg=BG)
        self.main.pack(side="top", fill="both", expand=True)
        self.previews = {}

        self.val_var = tk.BooleanVar(value=bool(self.cfg.get("valorant", False)))
        self.pages = {
            "overview": self._page_overview(),
            "lyrics": self._page_lyrics(),
            "game": self._page_game(),
            "mon": self._page_monitor(),
            "look": self._page_look(),
            "log": self._page_log(),
            "fw": self._page_fw(),
        }
        self.show_page("overview")
        root.protocol("WM_DELETE_WINDOW", self.on_close)

    def _build_sidebar(self):
        side = self.sidebar
        brand = tk.Frame(side, bg=SIDEBAR)
        brand.pack(fill="x", padx=20, pady=(20, 6))
        try:
            img = Image.open(ICON_PNG).resize((36, 36), Image.LANCZOS)
            self.logo_img = ImageTk.PhotoImage(img)
            tk.Label(brand, image=self.logo_img, bg=SIDEBAR).pack(side="left")
        except Exception:
            tk.Label(brand, text="N75", bg=SIDEBAR, fg=FG, font=(FONT, 12, "bold")).pack(side="left")
        _wm = tk.Frame(brand, bg=SIDEBAR)
        _wm.pack(side="left", padx=(10, 0))
        tk.Label(_wm, text="Neos75 屏幕助手", bg=SIDEBAR, fg=FG, font=(FONT, 13, "bold")).pack(anchor="w")
        tk.Label(_wm, text="键盘屏幕控制中心", bg=SIDEBAR, fg=DIM, font=(FONT, 8)).pack(anchor="w")

        self.nav = {}
        for section, items in (("设备", (("overview", "设备概览", "grid"),
                                       ("lyrics", "歌词同步", "music"),
                                       ("game", "对局", "trophy"),
                                       ("mon", "系统监控", "activity"))),
                              ("系统", (("look", "外观", "palette"),
                                       ("log", "日志", "file"),
                                       ("fw", "固件", "cpu")))):
            tk.Label(side, text=section, bg=SIDEBAR, fg=DIM, font=(FONT, 8, "bold")).pack(
                anchor="w", padx=20, pady=(18, 6))
            for key, label, icon_name in items:
                row = tk.Frame(side, bg=SIDEBAR, height=40, cursor="hand2")
                row.pack(fill="x", padx=12, pady=1)
                row.pack_propagate(False)
                bar = tk.Frame(row, bg=SIDEBAR, width=3)
                bar.pack(side="left", fill="y")
                icon = Icon(row, icon_name, size=18, color=DIM)
                icon.pack(side="left", padx=(12, 0), pady=10)
                text = tk.Label(row, text=label, bg=SIDEBAR, fg=MUTED, font=(FONT, 10))
                text.pack(side="left", padx=(10, 0))
                for wdg in (row, icon, text):
                    wdg.bind("<Button-1>", lambda e, k=key: self.show_page(k))
                    wdg.bind("<Enter>", lambda e, k=key: self._nav_hover(k, True))
                    wdg.bind("<Leave>", lambda e, k=key: self._nav_hover(k, False))
                self.nav[key] = {"row": row, "bar": bar, "icon": icon, "text": text}

        bottom = tk.Frame(side, bg=SIDEBAR)
        bottom.pack(side="bottom", fill="x", padx=12, pady=14)
        dev = tk.Frame(bottom, bg=CARD, padx=12, pady=10)
        dev.pack(fill="x")
        _dr = tk.Frame(dev, bg=CARD)
        _dr.pack(fill="x")
        self.dev_dot = tk.Label(_dr, text="●", bg=CARD, fg=DIM, font=("Segoe UI", 9))
        self.dev_dot.pack(side="left")
        self.dev_text = tk.Label(_dr, text="未连接", bg=CARD, fg=DIM, font=(FONT, 9))
        self.dev_text.pack(side="left", padx=(6, 0))
        tk.Label(dev, text="USB 5542:0001 · 有线", bg=CARD, fg=DIM, font=(FONT_EN, 8)).pack(
            anchor="w", pady=(4, 0))
        self.aula_warn = tk.Label(dev, text="", bg=CARD, fg=WARN, font=(FONT, 8),
                                  wraplength=180, justify="left", anchor="w")
        self.aula_warn.pack(fill="x", pady=(6, 0))

    PAGE_META = {
        "overview": ("设备概览", "键盘、屏幕与同步服务的状态总览"),
        "lyrics": ("歌词同步", "把正在播放的歌词推送到键盘屏幕"),
        "game": ("对局", "无畏契约比分实时上屏"),
        "mon": ("系统监控", "电脑状态实时上屏"),
        "look": ("外观", "键盘屏幕的背景、配色与显示样式"),
        "log": ("日志", "运行日志与诊断信息"),
        "fw": ("固件", "屏幕版本与全屏补丁"),
    }

    def _build_topbar(self):
        bar = self.topbar
        txt = tk.Frame(bar, bg=BG)
        txt.pack(side="left", padx=28, pady=10)
        self.page_title = tk.Label(txt, text="设备概览", bg=BG, fg=FG, font=(FONT, 18, "bold"))
        self.page_title.pack(anchor="w")
        self.page_subtitle = tk.Label(txt, text="", bg=BG, fg=DIM, font=(FONT, 9))
        self.page_subtitle.pack(anchor="w")
        right = tk.Frame(bar, bg=BG)
        right.pack(side="right", padx=24, pady=16)
        self.sync_dot = tk.Label(right, text="● 未同步", bg=BG, fg=MUTED, font=(FONT, 9, "bold"))
        self.sync_dot.pack(side="left", padx=(0, 12))
        self.sync_start = pill(right, "开始同步", self.start, "primary", width=8, size=10, accent=self.accent)
        self.sync_start.pack(side="left")
        self.sync_stop = pill(right, "停止", self.stop, "danger", width=6, size=10)
        self.sync_stop.pack(side="left", padx=8)

    def _build_statusbar(self):
        bar = self.statusbar
        self.status_label = tk.Label(bar, text="未连接", bg=SIDEBAR, fg=MUTED, font=(FONT, 9), anchor="w")
        self.status_label.pack(side="left", padx=16)
        tk.Label(bar, text="v" + VERSION, bg=SIDEBAR, fg=DIM, font=(FONT_EN, 8)).pack(side="right", padx=16)
        tk.Label(bar, text="480 × 360 · JPEG", bg=SIDEBAR, fg=DIM, font=(FONT_EN, 8)).pack(side="right")

    def _nav_hover(self, key, on):
        if self.page == key:
            return
        item = self.nav[key]
        bg = CARD_HI if on else SIDEBAR
        item["row"].configure(bg=bg)
        item["icon"].config(bg=bg)
        item["icon"].set_color(FG if on else DIM)
        item["text"].configure(bg=bg, fg=FG if on else MUTED)

    def _check_aula(self):
        try:
            import screen_hid
            busy = screen_hid.official_app_running()
            self.aula_warn.config(
                text="⚠ 检测到 AULA Hub 正在运行：它会和本程序抢屏幕，刷不上时请先关掉它"
                if busy else "")
        except Exception:
            pass
        self.root.after(6000, self._check_aula)

    def show_page(self, key):
        try:
            self.apply_skin(key)
        except Exception:
            pass
        if key == "log":
            self.root.after(120, lambda: self.refresh_log_view(again=False))
        for frame in self.pages.values():
            frame.pack_forget()
        _title, _sub = self.PAGE_META.get(key, ("", ""))
        self.page_title.config(text=_title)
        self.page_subtitle.config(text=_sub)
        self.pages[key].pack(fill="both", expand=True, padx=28, pady=(16, 18))
        for k, item in self.nav.items():
            active = k == key
            bg = CARD_HI if active else SIDEBAR
            item["row"].configure(bg=bg)
            item["bar"].configure(bg=self.accent if active else SIDEBAR)
            item["icon"].config(bg=bg)
            item["icon"].set_color(self.accent if active else DIM)
            item["text"].configure(bg=bg, fg=FG if active else MUTED)
        self.page = key

    # -------------------------------------------------------------- overview
    def _page_overview(self):
        # Device overview: only facts the app can actually read — connection
        # state from the engine, the fixed USB identity of the screen the app
        # looks for, the 480x360 JPEG screen spec, sync service state, and the
        # screen content the user enabled in settings. Nothing is invented.
        frame = tk.Frame(self.main, bg=BG)
        left = tk.Frame(frame, bg=BG)
        left.pack(side="left", fill="both", expand=True)
        right = tk.Frame(frame, bg=BG)
        right.pack(side="left", fill="y", padx=(20, 0))

        dev = Card(left, bg=CARD, radius=16, padx=20, pady=18)
        dev.pack(fill="x")
        top = tk.Frame(dev.body, bg=CARD)
        top.pack(fill="x")
        self.ov_dot = tk.Label(top, text="●", bg=CARD, fg=DIM, font=(FONT_EN, 20))
        self.ov_dot.pack(side="left")
        _t = tk.Frame(top, bg=CARD)
        _t.pack(side="left", padx=(10, 0))
        self.ov_state = tk.Label(_t, text="等待键盘", bg=CARD, fg=FG, font=(FONT, 15, "bold"))
        self.ov_state.pack(anchor="w")
        self.ov_state_sub = tk.Label(_t, text="用 USB 数据线连接键盘后开始同步", bg=CARD, fg=MUTED, font=(FONT, 9))
        self.ov_state_sub.pack(anchor="w")

        rows = tk.Frame(dev.body, bg=CARD)
        rows.pack(fill="x", pady=(14, 0))
        self.ov_rows = {}
        for key, label in (("device", "设备"), ("conn", "连接"), ("screen", "屏幕"),
                           ("sync", "同步服务"), ("modes", "屏幕内容")):
            r = tk.Frame(rows, bg=CARD)
            r.pack(fill="x", pady=3)
            tk.Label(r, text=label, bg=CARD, fg=DIM, font=(FONT, 9), width=10, anchor="w").pack(side="left")
            v = tk.Label(r, text="—", bg=CARD, fg=FG, font=(FONT, 9), anchor="w")
            v.pack(side="left", fill="x", expand=True)
            self.ov_rows[key] = v

        guide = Card(left, bg=CARD, radius=16, padx=20, pady=16)
        guide.pack(fill="x", pady=(14, 0))
        tk.Label(guide.body, text="连接指南", bg=CARD, fg=FG, font=(FONT, 11, "bold")).pack(anchor="w")
        self.ov_guide = tk.Label(guide.body, text="", bg=CARD, fg=MUTED, font=(FONT, 9),
                                 justify="left", anchor="w", wraplength=560)
        self.ov_guide.pack(fill="x", pady=(8, 0))

        act = Card(right, bg=CARD, radius=16, padx=20, pady=18)
        act.pack(fill="x")
        tk.Label(act.body, text="快捷操作", bg=CARD, fg=FG, font=(FONT, 11, "bold")).pack(anchor="w")
        pill(act.body, "开始 / 停止同步", self.toggle, "primary", width=14, size=10,
             accent=self.accent).pack(anchor="w", pady=(12, 0))
        pill(act.body, "模拟屏幕", self.toggle_sim, "ghost", width=14, size=10).pack(anchor="w", pady=(8, 0))
        pill(act.body, "检查屏幕", self.check_fw, "ghost", width=14, size=10).pack(anchor="w", pady=(8, 0))
        pill(act.body, "外观设置", lambda: self.show_page("look"), "ghost", width=14, size=10).pack(anchor="w", pady=(8, 0))
        self.root.after(500, self._overview_tick)
        return frame

    def _overview_tick(self):
        try:
            running = bool(self.engine and self.engine.running)
            if self.sim_enabled:
                state, color, sub = "模拟屏幕", ACCENT2, "推送会显示在模拟窗口里，不经过真键盘"
            elif self.engine is not None and getattr(self.engine, "device_ok", False):
                state, color, sub = "已连接", MINT, "键盘屏幕已就绪，可以开始同步"
            elif running:
                state, color, sub = "等待键盘", WARN, "同步服务在运行，等待键盘接入"
            else:
                state, color, sub = "等待键盘", WARN, "用 USB 数据线连接键盘后开始同步"
            self.ov_dot.config(fg=color)
            self.ov_state.config(text=state)
            self.ov_state_sub.config(text=sub)
            self.ov_rows["device"].config(text="Neos75 键盘屏幕")
            self.ov_rows["conn"].config(
                text="USB 5542:0001 · 有线连接" if state == "已连接" else "USB 5542:0001 · 未接入")
            self.ov_rows["screen"].config(
                text="480 × 360 · JPEG 推送" if state == "已连接" else "未确认 — 连接后在固件页检查")
            self.ov_rows["sync"].config(text="运行中" if running else "已停止")
            _scenes = self.cfg.get("scenes", {}) or {}
            _modes = []
            if _scenes.get("lyrics", True):
                _modes.append("歌词")
            if _scenes.get("info", True):
                _modes.append("信息屏")
            if self.cfg.get("valorant"):
                _modes.append("对局")
            self.ov_rows["modes"].config(text="、".join(_modes) if _modes else "未启用任何内容")
            if state == "已连接":
                guide = "设备已就绪。歌词、对局、监控会按你在各页的设置推送到屏幕上；想换样式去外观页。"
            elif self.sim_enabled:
                guide = "模拟屏幕已开启：没有真键盘也能看到推送效果。关掉模拟屏幕后，下次推送会重新找真键盘。"
            else:
                guide = ("1. 用 USB 数据线把键盘连到电脑（2.4G 接收器模式下屏幕不可用）。\n"
                         "2. 点右上角「开始同步」，等状态变成已连接。\n"
                         "3. 如果一直找不到键盘，点「检查屏幕」看具体原因，或到固件页确认是否刷了全屏补丁。")
            self.ov_guide.config(text=guide)
        except Exception:
            pass
        self.root.after(2000, self._overview_tick)

    # ---------------------------------------------------------------- lyrics
    def _page_lyrics(self):
        # Lyrics sync control panel — this page controls what the keyboard
        # screen shows, it is not a music player: no play/pause/next controls.
        # Left: player + sync controls + current lyric. Right: the live screen
        # preview, which shows the exact frame last pushed to the keyboard.
        frame = tk.Frame(self.main, bg=BG)
        left = tk.Frame(frame, bg=BG)
        left.pack(side="left", fill="both", expand=True)
        right = tk.Frame(frame, bg=BG)
        right.pack(side="left", fill="y", padx=(20, 0))

        player = Card(left, bg=CARD, radius=16, padx=20, pady=16)
        player.pack(fill="x")
        tk.Label(player.body, text="正在播放", bg=CARD, fg=FG, font=(FONT, 11, "bold")).pack(anchor="w")
        _src = "系统媒体会话 (SMTC)" if self.cfg.get("smtc", True) else "桌面歌词 OCR"
        tk.Label(player.body, text=f"播放器：{_src}", bg=CARD, fg=DIM, font=(FONT, 9)).pack(anchor="w", pady=(6, 0))
        self.track_label = tk.Label(player.body, text="未检测到正在播放的歌曲", bg=CARD, fg=FG,
                                    font=(FONT, 14, "bold"), anchor="w", wraplength=460, justify="left")
        self.track_label.pack(anchor="w", fill="x", pady=(8, 0))

        ctrl = Card(left, bg=CARD, radius=16, padx=20, pady=16)
        ctrl.pack(fill="x", pady=(14, 0))
        tk.Label(ctrl.body, text="同步控制", bg=CARD, fg=FG, font=(FONT, 11, "bold")).pack(anchor="w")
        opts = tk.Frame(ctrl.body, bg=CARD)
        opts.pack(fill="x", pady=(10, 0))
        scenes = self.cfg.get("scenes", {}) or {}
        self.lyr_push = tk.BooleanVar(value=bool(scenes.get("lyrics", True)))
        t1 = Toggle(opts, "自动推送", self.lyr_push, accent=self.accent)
        t1.oncommand(self.save_cfg)
        t1.pack(side="left")
        self.lyr_cover = tk.BooleanVar(value=bool(self.cfg.get("cover_bg", True)))
        t2 = Toggle(opts, "专辑封面当背景", self.lyr_cover, accent=self.accent)
        t2.oncommand(self.save_cfg)
        t2.pack(side="left", padx=10)
        row = tk.Frame(ctrl.body, bg=CARD)
        row.pack(fill="x", pady=(12, 0))
        pill(row, "推送到屏幕", lambda: self.push_module_ui("lyrics"), "primary", width=12,
             accent=self.accent).pack(side="left")
        self.sim_btn = pill(row, "模拟屏幕", self.toggle_sim, "ghost", width=10, size=10)
        self.sim_btn.pack(side="left", padx=(10, 0))

        lyric = Card(left, bg=CARD, radius=16, padx=20, pady=16, expand=True)
        lyric.pack(fill="both", expand=True, pady=(14, 0))
        tk.Label(lyric.body, text="当前歌词", bg=CARD, fg=FG, font=(FONT, 11, "bold")).pack(anchor="w")
        self.line_label = tk.Label(lyric.body, text="♪", bg=CARD, fg=FG, wraplength=440,
                                   justify="left", font=(FONT, 20, "bold"))
        self.line_label.pack(fill="x", pady=(10, 0))
        self.next_label = tk.Label(lyric.body, text="", bg=CARD, fg=MUTED, font=(FONT, 11),
                                   wraplength=440, justify="left")
        self.next_label.pack(fill="x", pady=(6, 0))
        self.hint = tk.Label(lyric.body, text="点右上角「开始同步」后，歌词会跟着播放自动上屏",
                             bg=CARD, fg=DIM, font=(FONT, 9))
        self.hint.pack(anchor="w", pady=(10, 0))
        self.progress = tk.Canvas(lyric.body, height=6, bg=CARD, highlightthickness=0)
        self.progress.pack(fill="x", pady=(12, 0))
        self.progress.bind("<Configure>", lambda e: self._draw_progress(0.0))

        self.previews["lyrics"] = ScreenPreview(right, width=440, caption="键盘屏幕预览")
        self.previews["lyrics"].pack(fill="x")
        tk.Label(right, text="预览显示最近一次实际推送到键盘的画面，", bg=BG, fg=DIM,
                 font=(FONT, 8), wraplength=440, justify="left").pack(anchor="w", pady=(10, 0))
        tk.Label(right, text="未开始同步前这里是空的，不会用假画面代替。", bg=BG, fg=DIM,
                 font=(FONT, 8), wraplength=440, justify="left").pack(anchor="w")
        return frame

    def _draw_progress(self, frac):
        c = self.progress
        c.delete("all")
        w = c.winfo_width() or 1
        round_rect(c, 0, 1, w, 5, 2, fill=BORDER, outline="")
        self.last_frac = frac
        if frac > 0:
            round_rect(c, 0, 1, max(6, int(w * frac)), 5, 2, fill=self.accent, outline="")

    def set_palette(self, name):
        self.cfg["palette"] = name
        lyrics_screen.save_config(self.cfg)
        apply_palette(name)
        self.rebuild_ui()

    def rebuild_ui(self):
        page = self.page
        try:
            self.skin_labels.clear()
        except Exception:
            pass
        self.sidebar.destroy()
        self.right.destroy()
        self._build()
        self.show_page(page)
        self._apply_accent_to_pills()

    def _apply_accent_to_pills(self):
        try:
            self.accent = self.cfg.get("accent") or ACCENT
        except Exception:
            pass

    def skin_of(self, key):
        skins = self.cfg.get("skins") or {}
        return skins.get(key) or {}

    def apply_skin(self, key):
        frame = self.pages.get(key)
        if frame is None:
            return
        skin = self.skin_of(key)
        color = skin.get("color") or BG
        try:
            frame.configure(bg=color)
        except Exception:
            pass
        img_path = skin.get("image") or ""
        holder = self.skin_labels.get(key)
        if img_path and os.path.exists(img_path):
            try:
                img = Image.open(img_path).convert("RGB")
                img = img.resize((760, 780), Image.LANCZOS)
                photo = ImageTk.PhotoImage(img)
                if holder is None:
                    holder = tk.Label(frame, bd=0)
                    holder.place(x=0, y=0, relwidth=1, relheight=1)
                    holder.lower()
                    self.skin_labels[key] = holder
                holder.configure(image=photo)
                holder.image = photo
            except Exception as exc:
                lyrics_screen.dbg(f"skin image failed: {exc}")
        elif holder is not None:
            holder.destroy()
            self.skin_labels.pop(key, None)

    def _skin_module(self):
        return {"歌词": "lyrics", "对局": "game", "监控": "mon"}.get(
            self.skin_choice.get(), "lyrics")

    def pick_skin_color(self):
        from tkinter import colorchooser
        key = self._skin_module()
        skins = dict(self.cfg.get("skins") or {})
        cur = (skins.get(key) or {}).get("color") or BG
        rgb, hexv = colorchooser.askcolor(color=cur, title="选择页面背景色")
        if not hexv:
            return
        entry = dict(skins.get(key) or {})
        entry["color"] = hexv
        skins[key] = entry
        self.cfg["skins"] = skins
        self.save_cfg()
        self.apply_skin(key)

    def pick_skin_image(self):
        key = self._skin_module()
        path = filedialog.askopenfilename(
            title="选择页面背景图",
            filetypes=[("图片", "*.jpg *.jpeg *.png *.bmp *.webp"), ("所有文件", "*.*")])
        if not path:
            return
        skins = dict(self.cfg.get("skins") or {})
        entry = dict(skins.get(key) or {})
        entry["image"] = path
        skins[key] = entry
        self.cfg["skins"] = skins
        self.save_cfg()
        self.apply_skin(key)

    def clear_skin(self):
        key = self._skin_module()
        skins = dict(self.cfg.get("skins") or {})
        skins.pop(key, None)
        self.cfg["skins"] = skins
        self.save_cfg()
        holder = self.skin_labels.pop(key, None)
        if holder is not None:
            holder.destroy()
        frame = self.pages.get(key)
        if frame is not None:
            frame.configure(bg=BG)

    def _bg_row(self, parent, prefix, label):
        row = tk.Frame(parent, bg=CARD)
        row.pack(fill="x", pady=(8, 0))
        tk.Label(row, text=label, bg=CARD, fg=MUTED, font=(FONT, 9)).pack(side="left")
        current = self.cfg.get(prefix + "_image") or ""
        name = tk.Label(row, text=(os.path.basename(current) if current else "未设置"),
                        bg=CARD, fg=FG, font=(FONT, 9))
        name.pack(side="left", padx=8)
        pill(row, "选择", lambda: self._pick_module_bg(prefix, name), "ghost",
             width=6, size=9).pack(side="right")
        pill(row, "清除", lambda: self._clear_module_bg(prefix, name), "ghost",
             width=6, size=9).pack(side="right", padx=6)

    def _pick_module_bg(self, prefix, label):
        path = filedialog.askopenfilename(
            title="选择背景图片",
            filetypes=[("图片", "*.jpg *.jpeg *.png *.bmp *.webp"), ("所有文件", "*.*")])
        if not path:
            return
        crop = self.cfg.get(prefix + "_crop")
        try:
            dlg = CropDialog(self.root, path, crop, "调整背景图显示区域")
            self.root.wait_window(dlg)
            if dlg.result is None:
                return
            crop = dlg.result
        except Exception as exc:
            lyrics_screen.dbg(f"{prefix} crop failed: {exc}")
            crop = [0.0, 0.0, 1.0, 1.0]
        self.cfg[prefix + "_image"] = path
        self.cfg[prefix + "_crop"] = crop
        self.save_cfg()
        label.config(text=os.path.basename(path))
        self.engine_push_refresh()

    def _clear_module_bg(self, prefix, label):
        self.cfg[prefix + "_image"] = ""
        self.save_cfg()
        label.config(text="未设置")
        self.engine_push_refresh()

    def engine_push_refresh(self):
        if self.engine:
            self.engine.cfg = dict(self.cfg)
        if self.sim_enabled:
            self.engine.screen = FakeScreen(self.events)
            self.engine.pushed = None

    def _page_log(self):
        frame = tk.Frame(self.main, bg=BG)
        head = tk.Frame(frame, bg=BG)
        head.pack(fill="x")
        tk.Label(head, text="出问题时把日志发我，能直接定位", bg=BG, fg=DIM,
                 font=(FONT, 9)).pack(side="left", padx=10)

        card = Card(frame, bg=CARD, radius=20, padx=18, pady=14, expand=True)
        card.pack(fill="both", expand=True, pady=(12, 10))
        body = card.body

        row = tk.Frame(body, bg=CARD)
        row.pack(fill="x")
        pill(row, "导出日志包", self.export_diag, "primary", width=10, size=9,
             accent=self.accent).pack(side="left")
        pill(row, "打开文件夹", self.open_logs, "ghost", width=9, size=9).pack(side="left", padx=8)
        pill(row, "刷新", self.refresh_log_view, "ghost", width=6, size=9).pack(side="left")

        self.log_filter = "all"
        self.log_chips = {}
        _frow = tk.Frame(body, bg=CARD)
        _frow.pack(fill="x", pady=(10, 0))
        for _label, _key in (("全部", "all"), ("错误", "error"), ("警告", "warn"), ("信息", "info")):
            _chip = pill(_frow, _label, lambda k=_key: self._set_log_filter(k), "ghost", width=6, size=9)
            _chip.pack(side="left", padx=(0, 6))
            self.log_chips[_key] = _chip

        self.log_path_label = tk.Label(body, text="", bg=CARD, fg=DIM, font=(FONT, 8), anchor="w")
        self.log_path_label.pack(fill="x", pady=(8, 6))

        wrap = tk.Frame(body, bg=CARD_HI)
        wrap.pack(fill="both", expand=True)
        self.log_text = tk.Text(wrap, bg=CARD_HI, fg=FG, bd=0, font=(FONT_MONO, 8),
                                insertbackground=FG, wrap="char", height=18, width=80)
        sb = tk.Scrollbar(wrap, command=self.log_text.yview, bg=CARD_HI, bd=0,
                          highlightthickness=0)
        self.log_text.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.log_text.pack(side="left", fill="both", expand=True, padx=6, pady=6)
        self.log_text.tag_config("err", foreground=DANGER)
        self.log_text.tag_config("warn", foreground=WARN)

        self.root.after(300, self.refresh_log_view)
        return frame

    def _set_log_filter(self, key):
        self.log_filter = key
        for k, chip in self.log_chips.items():
            chip.config(bg=self.accent if k == key else CARD_HI,
                        fg="#FFFFFF" if k == key else MUTED)
        self.refresh_log_view(again=False)

    @staticmethod
    def _log_level(line):
        if "ERROR" in line or "CRITICAL" in line or "错误" in line or "Traceback" in line:
            return "error"
        if "WARNING" in line or "警告" in line:
            return "warn"
        return "info"

    def refresh_log_view(self, again=True):
        if self.page != "log":
            return
        try:
            import logsetup
            path = logsetup.LOG_DIR / "app.log"
            self.log_path_label.config(text=str(path))
            lines = []
            if path.exists():
                with open(path, "r", encoding="utf-8", errors="ignore") as fh:
                    lines = fh.readlines()[-300:]
            self.log_text.delete("1.0", "end")
            shown = 0
            for line in lines:
                level = self._log_level(line)
                if getattr(self, "log_filter", "all") != "all" and level != self.log_filter:
                    continue
                _tag = "err" if level == "error" else ("warn" if level == "warn" else None)
                if _tag:
                    self.log_text.insert("end", line, _tag)
                else:
                    self.log_text.insert("end", line)
                shown += 1
            if shown == 0:
                self.log_text.insert("end", "（没有符合条件的日志）")
            self.log_text.see("end")
        except Exception as exc:
            self.log_text.delete("1.0", "end")
            self.log_text.insert("end", f"读取日志失败：{exc}")
        if again:
            self.root.after(2000, lambda: self.refresh_log_view())

    # ---------------------------------------------------------------- monitor
    def _page_monitor(self):
        frame = tk.Frame(self.main, bg=BG)
        head = tk.Frame(frame, bg=BG)
        head.pack(fill="x")
        tk.Label(head, text="CPU / 内存 / 显卡 / 硬盘 / 网络", bg=BG, fg=DIM,
                 font=(FONT, 9)).pack(side="left", padx=10)

        card = Card(frame, bg=CARD, radius=20, padx=18, pady=16, expand=True)
        card.pack(fill="both", expand=True, pady=(12, 8))
        body = card.body

        top = tk.Frame(body, bg=CARD)
        top.pack(fill="x", pady=(0, 8))
        sc = self.cfg.get("scenes", {}) or {}
        self.mon_push = tk.BooleanVar(value=bool(sc.get("info", True)))
        tm = Toggle(top, "自动推送", self.mon_push, accent=self.accent)
        tm.oncommand(self.save_cfg)
        tm.pack(side="left")
        pill(top, "推送到屏幕", lambda: self.push_module_ui("mon"), "primary", width=10,
             size=9, accent=self.accent).pack(side="left", padx=10)
        self._bg_row(top, "mon", "信息屏背景")

        grid = tk.Frame(body, bg=CARD)
        grid.pack(fill="x")
        self.mon_labels = {}
        self.mon_subs = {}
        tiles = [("cpu", "CPU"), ("mem", "内存"), ("gpu", "显卡"),
                 ("disk", "硬盘 C:"), ("net", "网络"), ("up", "运行时长")]
        for idx, (key, name) in enumerate(tiles):
            row, col = divmod(idx, 3)
            box = tk.Frame(grid, bg=CARD_HI, padx=14, pady=9)
            box.grid(row=row, column=col, sticky="nsew", padx=5, pady=5)
            tk.Label(box, text=name, bg=CARD_HI, fg=MUTED, font=(FONT, 9), anchor="w").pack(fill="x")
            val = tk.Label(box, text="—", bg=CARD_HI, fg=FG, font=(FONT_EN, 15, "bold"), anchor="w")
            val.pack(fill="x")
            sub = tk.Label(box, text="", bg=CARD_HI, fg=DIM, font=(FONT, 8), anchor="w")
            sub.pack(fill="x")
            self.mon_labels[key] = val
            self.mon_subs[key] = sub
            grid.grid_columnconfigure(col, weight=1, uniform="mon")

        self.previews["mon"] = ScreenPreview(body, width=360, caption="键盘屏幕预览")
        self.previews["mon"].pack(fill="x", pady=(12, 0))
        self.previews["mon"].set_empty("采集中…")

        tk.Label(frame, text="外观页勾选「信息屏」后，这套数据会轮流显示在键盘屏上",
                 bg=BG, fg=DIM, font=(FONT, 8)).pack(anchor="w")

        self.root.after(300, self._monitor_tick)
        return frame

    def _monitor_tick(self):
        if self.page != "mon":
            self.root.after(1200, self._monitor_tick)
            return
        try:
            import monitor
            import scenes
            monitor.start_gpu()
            snap = monitor.snapshot()
            if snap.get("cpu") is not None:
                self.mon_labels["cpu"].config(text=f"{snap['cpu']:.0f}%")
                self.mon_subs["cpu"].config(text="16 线程")
            if snap.get("mem"):
                pct, used, total = snap["mem"]
                self.mon_labels["mem"].config(text=f"{pct:.0f}%")
                self.mon_subs["mem"].config(text=f"{used:.1f} / {total:.1f} GB")
            if snap.get("gpu") is not None:
                self.mon_labels["gpu"].config(text=f"{snap['gpu']:.0f}%")
                self.mon_subs["gpu"].config(text="3D 引擎占用")
            else:
                self.mon_labels["gpu"].config(text="…")
                self.mon_subs["gpu"].config(text="采样中")
            if snap.get("disk"):
                pct, used, total = snap["disk"]
                self.mon_labels["disk"].config(text=f"{pct:.0f}%")
                self.mon_subs["disk"].config(text=f"{used:.0f} / {total:.0f} GB")
            if snap.get("down") is not None:
                self.mon_labels["net"].config(text=f"↓{monitor.human_rate(snap['down'])}")
                self.mon_subs["net"].config(text=f"↑ {monitor.human_rate(snap['up'])}")
            if snap.get("uptime") is not None:
                h, m = divmod(snap["uptime"] // 60, 60)
                self.mon_labels["up"].config(text=f"{h}h {m:02d}m")
                self.mon_subs["up"].config(text="已开机")
            tmp = lyrics_screen.WORK / "mon_preview.jpg"
            scenes.render_info(self.cfg, tmp)
            img = Image.open(tmp)
            self.previews["mon"].set_image(img)
            self.previews["mon"].set_mode("实时监控画面")
        except Exception as exc:
            lyrics_screen.dbg(f"monitor tick failed: {exc}")
        self.root.after(1200, self._monitor_tick)

    # ------------------------------------------------------------------ game
    def _page_game(self):
        frame = tk.Frame(self.main, bg=BG)
        head = tk.Frame(frame, bg=BG)
        head.pack(fill="x")
        tk.Label(head, text="对局比分实时上屏", bg=BG, fg=DIM,
                 font=(FONT, 9)).pack(side="left", padx=10)
        tv = Toggle(head, "自动推送", self.val_var, accent=self.accent)
        tv.oncommand(self.save_cfg)
        tv.pack(side="right")
        pill(head, "推送到屏幕", lambda: self.push_module_ui("game"), "primary", width=10,
             size=9, accent=self.accent).pack(side="right", padx=10)

        card = Card(frame, bg=CARD, radius=20, padx=22, pady=18, expand=True)
        card.pack(fill="both", expand=True, pady=(12, 10))
        body = card.body

        left = tk.Frame(body, bg=CARD)
        left.pack(side="left", fill="both", expand=True)
        self.game_state = tk.Label(left, text="未检测", bg=CARD, fg=MUTED,
                                   font=(FONT, 11), anchor="w", justify="left")
        self.game_state.pack(fill="x")
        self.game_hint = tk.Label(left, text="进对局后自动识别顶部比分；只读屏幕，不碰游戏",
                                  bg=CARD, fg=DIM, font=(FONT, 9), anchor="w", justify="left",
                                  wraplength=330)
        self.game_hint.pack(fill="x", pady=(8, 16))
        row = tk.Frame(left, bg=CARD)
        row.pack(fill="x")
        pill(row, "测试识别", self.test_valorant, "ghost", width=8, size=9).pack(side="left")
        pill(row, "重新读取", self.refresh_game, "ghost", width=8, size=9).pack(side="left", padx=8)
        self._bg_row(left, "val", "比分卡背景")

        self.previews["game"] = ScreenPreview(left, width=380, caption="键盘屏幕预览")
        self.previews["game"].pack(fill="x", pady=(14, 0))
        tk.Label(left, text="预览由对局识别的真实比分渲染，与推送到屏幕的内容一致。",
                 bg=CARD, fg=DIM, font=(FONT, 8), wraplength=380, justify="left").pack(fill="x", pady=(8, 0))

        self.root.after(200, lambda: self._update_game(False, None))
        self.root.after(400, self.refresh_game)
        self.root.after(600, self._sync_tick)
        return frame

    def _sync_tick(self):
        running = bool(self.engine and self.engine.running)
        try:
            self.sync_dot.config(text="● 同步中" if running else "● 未同步",
                                 fg=MINT if running else MUTED)
            self.sync_start.config(state="disabled" if running else "normal",
                                   bg=CARD_HI if running else self.accent,
                                   fg=DIM if running else "#FFFFFF")
            self.sync_stop.config(state="normal" if running else "disabled",
                                  bg=DANGER if running else CARD_HI,
                                  fg="#2A0B0B" if running else DIM)
        except Exception:
            pass
        self.root.after(800, self._sync_tick)

    def refresh_game(self):
        if self.page != "game":
            self.root.after(4000, self.refresh_game)
            return
        def work():
            try:
                import valorant as V
                data = V.read(force=True)
                running = V.game_running()
            except Exception as exc:
                data, running = None, False
                lyrics_screen.dbg(f"game refresh failed: {exc}")
            self.events.put(("game", (running, data)))
        threading.Thread(target=work, daemon=True).start()
        self.root.after(4000, self.refresh_game)

    def _update_game(self, running, data):
        if not running:
            self.game_state.config(text="未检测到游戏进程", fg=MUTED)
        elif data:
            self.game_state.config(text=f"对局中   {data['ally']} : {data['enemy']}   "
                                        f"{data.get('timer') or '--:--'}", fg=MINT)
        else:
            self.game_state.config(text="游戏在运行，但当前画面没有比分（大厅/选人/结算）", fg=WARN)
        try:
            import valorant as V
            sample = data or {"ally": 0, "enemy": 0, "timer": "--:--"}
            tmp = lyrics_screen.WORK / "game_preview.jpg"
            V.render(self.cfg, sample, tmp)
            img = Image.open(tmp)
            self.previews["game"].set_image(img)
            self.previews["game"].set_mode(
                f"对局 {sample['ally']}:{sample['enemy']}" if (running and data) else "本地渲染预览")
        except Exception:
            pass

    def push_module_ui(self, key):
        if not self.engine or not self.engine.running:
            messagebox.showinfo("推送到屏幕", "先点右上角「开始同步」，再推送。")
            return
        ok, msg = self.engine.push_module(key)
        if ok:
            self.status_label.config(text=msg, fg=MINT)
        else:
            messagebox.showwarning("推送到屏幕", msg)

    def push_game(self):
        if not self.engine or not self.engine.running:
            messagebox.showinfo("推送", "先点右上角「开始同步」。")
            return
        try:
            import valorant as V
            data = V.read(force=True)
        except Exception as exc:
            messagebox.showerror("推送", f"读取失败：{exc}")
            return
        if not data:
            messagebox.showinfo("推送", "当前没有识别到比分（大厅或结算界面）。")
            return
        try:
            self.engine._tick_valorant(data)
        except Exception as exc:
            messagebox.showerror("推送", f"推送失败：{exc}")

    # ------------------------------------------------------------------ look
    def _page_look(self):
        frame = tk.Frame(self.main, bg=BG)
        tk.Label(frame, text="改的是键盘屏幕上的画面，不是这个窗口", bg=BG, fg=DIM,
                 font=(FONT, 9)).pack(anchor="w", pady=(2, 14))

        self.previews["look"] = ScreenPreview(frame, width=440, caption="当前屏幕样式预览")
        self.previews["look"].pack(anchor="w", pady=(0, 12))
        tk.Label(frame, text="显示最近一次推送的真实画面；改完背景或配色后点「推送到键盘」可刷新。",
                 bg=BG, fg=DIM, font=(FONT, 8)).pack(anchor="w", pady=(0, 12))

        theme_card = Card(frame, bg=CARD, radius=16, padx=18, pady=14)
        theme_card.pack(fill="x", pady=(0, 12))
        tr = tk.Frame(theme_card.body, bg=CARD)
        tr.pack(fill="x")
        tk.Label(tr, text="主题色", bg=CARD, fg=FG, font=(FONT, 10)).pack(side="left")
        for color in ("#4D6BFE", "#00C8A0", "#FF7A59", "#B45CFF", "#FF5C8A", "#E8C547"):
            b = tk.Label(tr, bg=color, width=3, height=1, bd=0, cursor="hand2")
            b.pack(side="left", padx=3)
            b.bind("<Button-1>", lambda e, c=color: self.apply_accent(c))
        tk.Button(tr, text="自定义…", command=self.pick_accent, bg=CARD_HI, fg=FG, bd=0,
                  relief="flat", font=(FONT, 9), cursor="hand2").pack(side="right")

        color_card = Card(frame, bg=CARD, radius=16, padx=18, pady=14)
        color_card.pack(fill="x", pady=(0, 12))
        row = tk.Frame(color_card.body, bg=CARD)
        row.pack(fill="x")
        tk.Label(row, text="背景色", bg=CARD, fg=FG, font=(FONT, 10)).pack(side="left")
        self.swatches = tk.Frame(row, bg=CARD)
        self.swatches.pack(side="left", padx=12)
        for color in ("#090A0D", "#101826", "#1B1226", "#0E1F1B", "#241318", "#F2F4F8"):
            b = tk.Label(self.swatches, bg=color, width=3, height=1, bd=0, cursor="hand2")
            b.pack(side="left", padx=3)
            b.bind("<Button-1>", lambda e, c=color: self.set_color(c))
        tk.Button(row, text="自定义…", command=self.pick_color, bg=CARD_HI, fg=FG, bd=0,
                  relief="flat", font=(FONT, 9), cursor="hand2").pack(side="right")

        image_card = Card(frame, bg=CARD, radius=16, padx=18, pady=14)
        image_card.pack(fill="x", pady=(0, 12))
        r2 = tk.Frame(image_card.body, bg=CARD)
        r2.pack(fill="x")
        pill(r2, "选择", self.pick_image, "ghost", width=5, size=9).pack(side="right", padx=(6, 0))
        pill(r2, "调整", self.adjust_bg, "ghost", width=5, size=9).pack(side="right", padx=6)
        pill(r2, "清除", self.clear_image, "ghost", width=5, size=9).pack(side="right")
        tk.Label(r2, text="背景图", bg=CARD, fg=FG, font=(FONT, 10)).pack(side="left")
        self.img_label = tk.Label(r2, text=self._name(self.cfg.get("bg_image")), bg=CARD,
                                  fg=DIM, font=(FONT, 8))
        self.img_label.pack(side="left")

        idle_card = Card(frame, bg=CARD, radius=16, padx=18, pady=14)
        idle_card.pack(fill="x", pady=(0, 12))
        r3 = tk.Frame(idle_card.body, bg=CARD)
        r3.pack(fill="x")
        pill(r3, "清除", self.clear_idle, "ghost", width=5, size=9).pack(side="right")
        pill(r3, "调整", self.adjust_idle, "ghost", width=5, size=9).pack(side="right", padx=6)
        pill(r3, "GIF", self.pick_gif, "ghost", width=4, size=9).pack(side="right", padx=6)
        pill(r3, "图片", self.pick_idle_image, "ghost", width=5, size=9).pack(side="right")
        tk.Label(r3, text="暂停 / 结束", bg=CARD, fg=FG, font=(FONT, 10)).pack(side="left")
        self.idle_label = tk.Label(r3, text=self._idle_name(), bg=CARD, fg=DIM, font=(FONT, 8))
        self.idle_label.pack(side="left")
        tk.Label(idle_card.body, text="GIF 会按约 1 秒一帧循环播放（受屏幕传输速度限制）",
                 bg=CARD, fg=DIM, font=(FONT, 8), anchor="w").pack(fill="x", pady=(8, 0))

        opt_card = Card(frame, bg=CARD, radius=16, padx=18, pady=12)
        opt_card.pack(fill="x", pady=(0, 12))
        self.smtc_var = tk.BooleanVar(value=bool(self.cfg.get("smtc", True)))
        t_smtc = Toggle(opt_card.body, "系统媒体会话（免截图）", self.smtc_var, accent=self.accent)
        t_smtc.oncommand(self.save_cfg)
        t_smtc.pack(anchor="w")
        gframe = tk.Frame(opt_card.body, bg=CARD)
        gframe.pack(fill="x", pady=(6, 0))
        self.frost_var = tk.BooleanVar(value=bool(self.cfg.get("frost", False)))
        t_frost = Toggle(gframe, "歌词背景虚化", self.frost_var, accent=self.accent)
        t_frost.oncommand(self.toggle_frost)
        t_frost.pack(side="left")
        self.blur_scale = tk.Scale(gframe, from_=1, to=16, orient="horizontal", showvalue=True,
                                   bg=CARD, fg=MUTED, troughcolor=CARD_HI, highlightthickness=0,
                                   bd=0, length=180, font=(FONT, 8), command=self._on_blur)
        self.blur_scale.set(int(self.cfg.get("frost_radius", 7)))
        self.blur_scale.pack(side="right", padx=(0, 4))
        self.autostart_var = tk.BooleanVar(value=self._autostart_enabled())
        t_start = Toggle(opt_card.body, "开机自动启动", self.autostart_var, accent=self.accent)
        t_start.oncommand(self.toggle_autostart)
        t_start.pack(anchor="w", pady=(6, 0))

        skin_card = Card(frame, bg=CARD, radius=20, padx=18, pady=14)
        skin_card.pack(fill="x", pady=(0, 12))
        srow = tk.Frame(skin_card.body, bg=CARD)
        srow.pack(fill="x")
        tk.Label(srow, text="皮肤", bg=CARD, fg=FG, font=(FONT, 9, "bold")).pack(side="left")
        self.skin_choice = tk.StringVar(value="歌词")
        pick = tk.OptionMenu(srow, self.skin_choice, "歌词", "对局", "监控")
        pick.configure(bg=CARD_HI, fg=FG, bd=0, highlightthickness=0, font=(FONT, 9),
                       activebackground=BORDER, activeforeground=FG, width=5)
        pick["menu"].configure(bg=CARD_HI, fg=FG, font=(FONT, 9))
        pick.pack(side="left", padx=10)
        pill(srow, "背景色", self.pick_skin_color, "ghost", width=7, size=9).pack(side="left")
        pill(srow, "背景图", self.pick_skin_image, "ghost", width=7, size=9).pack(side="left", padx=8)
        pill(srow, "清除", self.clear_skin, "ghost", width=5, size=9).pack(side="left")
        palette_row = tk.Frame(skin_card.body, bg=CARD)
        palette_row.pack(fill="x", pady=(10, 0))
        tk.Label(palette_row, text="整体配色", bg=CARD, fg=FG,
                 font=(FONT, 9, "bold")).pack(side="left")
        for key, label, color in (("dark", "默认", "#7586FF"), ("red", "经典红", "#E63C3C"),
                                  ("black", "酷炫黑", "#1A1A1F"), ("light", "浅色", "#FFFFFF")):
            chip = RoundButton(palette_row, label, lambda k=key: self.set_palette(k),
                               bg=color, fg=("#1D1F26" if key == "light" else "#FFFFFF"),
                               width=5, size=9, height=30, radius=15)
            chip.pack(side="left", padx=6)

        tk.Label(skin_card.body, text="只改电脑上的界面，不影响键盘屏幕；皮肤按模块单独设置",
                 bg=CARD, fg=DIM, font=(FONT, 8), anchor="w").pack(fill="x", pady=(8, 0))

        prev_card = Card(frame, bg=CARD, radius=16, padx=18, pady=12)
        prev_card.pack(fill="both", expand=True)
        self.preview = tk.Label(prev_card.body, bg=CARD, text="点下面按钮看效果", fg=DIM,
                                font=(FONT, 9))
        self.preview.pack(side="left", expand=True)
        self.idle_preview = tk.Label(prev_card.body, bg=CARD, text="待机画面：无", fg=DIM,
                                     font=(FONT, 8))
        self.idle_preview.pack(side="right", padx=(0, 10))
        pill(prev_card.body, "刷新预览", self.refresh_preview, "ghost", width=9, size=9).pack(
            side="right", padx=(10, 0))
        pill(prev_card.body, "推送到键盘", self.push_now, "ghost", width=11, size=9).pack(
            side="right", padx=(10, 0))
        return frame

    def _name(self, path):
        return os.path.basename(path) if path else "（无）"

    def _idle_name(self):
        if self.cfg.get("idle_gif"):
            return "GIF：" + self._name(self.cfg["idle_gif"])
        if self.cfg.get("idle_image"):
            return "图片：" + self._name(self.cfg["idle_image"])
        return "（无）"

    def pick_accent(self):
        color = colorchooser.askcolor(color=self.accent)[1]
        if color:
            self.apply_accent(color)

    def apply_accent(self, color):
        self.accent = color
        self.cfg["accent"] = color
        lyrics_screen.save_config(self.cfg)
        for btn in self.primary_btns:
            try:
                btn.config(bg=color, activebackground=color)
            except Exception:
                pass
        self.show_page(self.page)
        self._draw_progress(self.last_frac)

    def set_color(self, color):
        self.cfg["bg_color"] = color
        self.save_cfg()
        self.refresh_preview()

    def pick_color(self):
        color = colorchooser.askcolor(color=self.cfg.get("bg_color", "#090A0D"))[1]
        if color:
            self.set_color(color)

    def push_now(self):
        if not self.engine or not self.engine.running:
            messagebox.showinfo("推送到键盘", "先点右上角「开始同步」，再试。")
            return
        try:
            ok, msg = self.engine.force_refresh()
        except Exception as exc:
            ok, msg = False, f"推送失败：{exc}"
        lyrics_screen.dbg(f"manual push: {msg}")
        if ok:
            self.status_label.config(text="已推送到键盘（看一眼屏幕）", fg=self.accent)
        else:
            messagebox.showwarning("推送到键盘", msg)

    def pick_image(self):
        path = filedialog.askopenfilename(
            title="选择背景图片",
            filetypes=[("图片", "*.jpg *.jpeg *.png *.bmp *.webp"), ("所有文件", "*.*")])
        if not path:
            return
        crop = self.cfg.get("bg_crop")
        try:
            dlg = CropDialog(self.root, path, crop, "调整背景图显示区域")
            self.root.wait_window(dlg)
            if dlg.result is None:
                lyrics_screen.dbg("bg crop cancelled")
                return
            crop = dlg.result
        except Exception as exc:
            lyrics_screen.dbg(f"crop dialog failed: {exc}")
            crop = [0.0, 0.0, 1.0, 1.0]
        lyrics_screen.dbg(f"bg picked: {path} crop={crop}")
        self.cfg["bg_image"] = path
        self.cfg["bg_crop"] = crop
        self.img_label.config(text=self._name(path))
        self.save_cfg()
        self._refresh_thumbs()
        self.root.after(1500, self._check_aula)
        self.refresh_preview()

    def adjust_bg(self):
        path = self.cfg.get("bg_image") or ""
        if not path or not os.path.exists(path):
            messagebox.showinfo("背景图", "先选择一张背景图。")
            return
        dlg = CropDialog(self.root, path, self.cfg.get("bg_crop"), "调整背景图显示区域")
        self.root.wait_window(dlg)
        if dlg.result is None:
            return
        self.cfg["bg_crop"] = dlg.result
        self.save_cfg()
        self._refresh_thumbs()
        self.root.after(1500, self._check_aula)
        self.refresh_preview()

    def adjust_idle(self):
        path = self.cfg.get("idle_image") or ""
        if not path or not os.path.exists(path):
            messagebox.showinfo("待机图片", "待机图片才能调整区域；GIF 会整段播放。")
            return
        dlg = CropDialog(self.root, path, self.cfg.get("idle_crop"), "调整待机图片显示区域")
        self.root.wait_window(dlg)
        if dlg.result is None:
            return
        self.cfg["idle_crop"] = dlg.result
        self.cfg["idle_key"] = None
        self.save_cfg()
        self._refresh_thumbs()
        self.root.after(1500, self._check_aula)
        if self.engine:
            self.engine.idle_key = None

    def _thumb(self, path, crop):
        try:
            if not path or not os.path.exists(path):
                return None
            return ImageTk.PhotoImage(preview_thumb(path, crop, (96, 72)))
        except Exception:
            return None

    def _refresh_thumbs(self):
        # thumbnails live in the preview card, not in the rows
        try:
            path = self.cfg.get("idle_image") or self.cfg.get("idle_gif") or ""
            self.idle_thumb_img = self._thumb(path, self.cfg.get("idle_crop"))
            if getattr(self, "idle_preview", None) is not None:
                if self.idle_thumb_img:
                    self.idle_preview.config(image=self.idle_thumb_img, text="")
                else:
                    self.idle_preview.config(image="", text="待机画面：无")
        except Exception:
            pass

    @staticmethod
    def _paint_thumb(canvas, photo):
        canvas.delete("all")
        if photo:
            canvas.create_image(0, 0, anchor="nw", image=photo)

    def clear_image(self):
        self.cfg["bg_image"] = ""
        self.img_label.config(text=self._name(""))
        self.save_cfg()
        self.refresh_preview()

    def pick_idle_image(self):
        path = filedialog.askopenfilename(
            title="选择待机图片",
            filetypes=[("图片", "*.jpg *.jpeg *.png *.bmp *.webp"), ("所有文件", "*.*")])
        if not path:
            return
        crop = self.cfg.get("idle_crop")
        try:
            dlg = CropDialog(self.root, path, crop, "调整待机图片显示区域")
            self.root.wait_window(dlg)
            if dlg.result is None:
                lyrics_screen.dbg("idle crop cancelled")
                return
            crop = dlg.result
        except Exception as exc:
            lyrics_screen.dbg(f"idle crop dialog failed: {exc}")
            crop = [0.0, 0.0, 1.0, 1.0]
        lyrics_screen.dbg(f"idle picked: {path} crop={crop}")
        self.cfg["idle_image"] = path
        self.cfg["idle_gif"] = ""
        self.cfg["idle_crop"] = crop
        self.idle_label.config(text=self._idle_name())
        self.save_cfg()
        self._refresh_thumbs()
        self.root.after(1500, self._check_aula)
        if self.engine:
            self.engine.idle_key = None

    def pick_gif(self):
        path = filedialog.askopenfilename(
            title="选择待机 GIF", filetypes=[("GIF", "*.gif"), ("所有文件", "*.*")])
        if path:
            self.cfg["idle_gif"] = path
            self.cfg["idle_image"] = ""
            self.idle_label.config(text=self._idle_name())
            self.save_cfg()

    def clear_idle(self):
        self.cfg["idle_gif"] = ""
        self.cfg["idle_image"] = ""
        self.idle_label.config(text=self._idle_name())
        self.save_cfg()

    def toggle_frost(self):
        self.cfg["frost"] = bool(self.frost_var.get())
        lyrics_screen.save_config(self.cfg)
        if self.engine:
            self.engine.cfg = dict(self.cfg)
            self.engine.pushed = None
        self.refresh_preview()

    def _on_blur(self, value):
        try:
            self.cfg["frost_radius"] = int(float(value))
            lyrics_screen.save_config(self.cfg)
            if self.frost_var.get():
                if self.engine:
                    self.engine.cfg = dict(self.cfg)
                    self.engine.pushed = None
                self.refresh_preview()
        except Exception:
            pass

    def test_valorant(self):
        try:
            import valorant as V
            data = V.read(force=True)
        except Exception as exc:
            messagebox.showerror("测试识别", f"出错：{exc}")
            return
        if data:
            messagebox.showinfo("测试识别", f"识别到比分：{data['ally']} : {data['enemy']}\n"
                                             f"计时：{data.get('timer') or '—'}")
        else:
            folder = str(V.DEBUG_DIR)
            messagebox.showinfo("测试识别",
                                "没识别到比分。\n\n已截图并按 OCR 结果保存到：\n"
                                f"{folder}\n\n请把这个文件夹里的 band_*.png 和 ocr.log 发给我，"
                                "我来校准区域。")

    def apply_city(self):
        entry = getattr(self, "city_entry", None)
        if entry is None:
            return  # no city field on any page in this build
        city = (entry.get() or "").strip() or "上海"
        self.cfg["city"] = city
        lyrics_screen.save_config(self.cfg)
        if self.engine:
            self.engine.cfg = dict(self.cfg)
        try:
            import scenes
            scenes._weather_cache.update({"t": 0.0, "data": None})
            scenes._geo_cache.pop(city, None)
        except Exception:
            pass

    def save_cfg(self):
        self.cfg["smtc"] = bool(self.smtc_var.get())
        try:
            self.cfg["valorant"] = bool(self.val_var.get())
        except Exception:
            pass
        try:
            sc = dict(self.cfg.get("scenes", {}) or {})
            sc["lyrics"] = bool(self.lyr_push.get())
            sc["cover"] = True
            if hasattr(self, "mon_push"):
                sc["info"] = bool(self.mon_push.get())
            self.cfg["scenes"] = sc
            self.cfg["cover_bg"] = bool(self.lyr_cover.get())
        except Exception:
            pass
        lyrics_screen.save_config(self.cfg)
        if self.engine:
            self.engine.cfg = dict(self.cfg)

    def refresh_preview(self):
        try:
            tmp = lyrics_screen.WORK / "preview.jpg"
            lyrics_screen.render("夜空中最亮的星", "逃跑计划", "我祈祷拥有一颗透明的心灵",
                                 "和会流泪的眼睛", tmp, self.cfg, 0.42)
            img = Image.open(tmp).resize((432, 324), Image.LANCZOS)
            self.preview_img = ImageTk.PhotoImage(img)
            self.preview.config(image=self.preview_img, text="")
        except Exception as exc:
            self.preview.config(text=f"预览失败：{exc}")

    # ------------------------------------------------------------------ fw
    def _page_fw(self):
        frame = tk.Frame(self.main, bg=BG)
        head = tk.Frame(frame, bg=BG)
        head.pack(fill="x")
        tk.Label(head, text=f"当前版本 v{VERSION}", bg=BG, fg=self.accent,
                 font=(FONT, 10, "bold")).pack(side="left", padx=12)
        tk.Label(frame, text="歌词 / 对局 / 监控 都推到这块 480×360 的屏幕上", bg=BG, fg=DIM,
                 font=(FONT, 9)).pack(anchor="w", pady=(2, 14))

        card = Card(frame, bg=CARD, radius=16, padx=20, pady=18)
        card.pack(fill="x")
        self.fw_status = tk.Label(card.body, text="点「检查屏幕」查看当前状态", bg=CARD, fg=MUTED,
                                  anchor="w", justify="left", wraplength=620, font=(FONT, 10))
        self.fw_status.pack(fill="x", pady=(0, 14))
        row = tk.Frame(card.body, bg=CARD)
        row.pack(fill="x")
        pill(row, "检查屏幕", self.check_fw, "ghost", width=9, size=9).pack(side="left")
        pill(row, "下载刷机工具", self.open_tool, "primary", width=12, size=9,
             accent=self.accent).pack(side="left", padx=8)

        warn_card = Card(frame, bg=CARD, radius=16, padx=20, pady=14)
        warn_card.pack(fill="x", pady=(12, 0))
        tk.Label(warn_card.body, text="刷机有风险", bg=CARD, fg=WARN, font=(FONT, 10, "bold"),
                 anchor="w").pack(fill="x")
        tk.Label(warn_card.body, text=("刷写固件可能导致键盘无法启动。请先按下方须知备份完整固件，"
            "确认固件版本匹配后再操作；刷机工具来自第三方，与本软件无关。"),
            bg=CARD, fg=MUTED, justify="left", anchor="w", font=(FONT, 9), wraplength=620).pack(fill="x", pady=(6, 0))

        tips = Card(frame, bg=CARD, radius=16, padx=20, pady=16)
        tips.pack(fill="both", expand=True, pady=(12, 0))
        tk.Label(tips.body, text="刷机须知", bg=CARD, fg=FG, font=(FONT, 10, "bold"),
                 anchor="w").pack(fill="x")
        tk.Label(tips.body, text=(
            "1. 先备份：工具会读完整片固件（约 20 分钟，只读不写），备份文件请保管好。\n"
            "2. 再刷写：工具按固件版本匹配补丁，只支持已验证版本，未知版本会拒绝。\n"
            "3. 留有退路：刷坏时用工具的「恢复」写回备份即可回到 320×320。"),
            bg=CARD, fg=MUTED, justify="left", anchor="w", font=(FONT, 9)).pack(fill="x", pady=(8, 0))
        return frame

    def check_fw(self):
        try:
            import screen_hid
            st = screen_hid.Screen().open().status()
            if st["width"] == 480:
                self.fw_status.config(text=f"已连接，{st['width']}×{st['height']}，全屏补丁已生效", fg=MINT)
            else:
                self.fw_status.config(
                    text=f"已连接，{st['width']}×{st['height']}，还没有刷全屏补丁", fg=WARN)
        except Exception as exc:
            msg = f"没有找到键盘屏幕：{exc}"
            try:
                import screen_hid
                if screen_hid.has_dongle_only():
                    msg = "检测到 2.4G 接收器：屏幕画面只在 USB 有线模式下可用，请插上数据线。"
            except Exception:
                pass
            self.fw_status.config(text=msg, fg=WARN)

    def open_logs(self):
        import logsetup
        logsetup.LOG_DIR.mkdir(parents=True, exist_ok=True)
        os.startfile(str(logsetup.LOG_DIR))

    def export_diag(self):
        try:
            import logsetup
            out = logsetup.export_bundle()
            messagebox.showinfo("导出诊断包", f"已导出到：\n{out}\n\n把它发给我就能定位问题。")
        except Exception as exc:
            messagebox.showerror("导出诊断包", f"失败：{exc}")

    def open_tool(self):
        import webbrowser
        url = "https://github.com/hacker1232345/neos75-fullscreen/releases/latest"
        try:
            webbrowser.open(url)
        except Exception:
            messagebox.showinfo("刷机工具", f"请在浏览器打开：\n{url}")

    # ------------------------------------------------------------- autostart
    def _run_key(self):
        import winreg
        return winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run"

    def _autostart_enabled(self):
        try:
            import winreg
            key, path = self._run_key()
            with winreg.OpenKey(key, path) as k:
                winreg.QueryValueEx(k, "Neos75Lyrics")
            return True
        except Exception:
            return False

    def toggle_autostart(self):
        try:
            import winreg
            key, path = self._run_key()
            with winreg.OpenKey(key, path, 0, winreg.KEY_SET_VALUE) as k:
                if self.autostart_var.get():
                    cmd = f'"{sys.executable}"'
                    if not getattr(sys, "frozen", False):
                        cmd = f'"{sys.executable}" "{os.path.abspath(__file__)}"'
                    winreg.SetValueEx(k, "Neos75Lyrics", 0, winreg.REG_SZ, cmd)
                else:
                    try:
                        winreg.DeleteValue(k, "Neos75Lyrics")
                    except FileNotFoundError:
                        pass
        except Exception as exc:
            messagebox.showwarning("开机自启", f"设置失败：{exc}")

    # --------------------------------------------------------------- engine
    def toggle(self):
        if self.engine and self.engine.running:
            self.stop()
        else:
            self.start()

    def start(self):
        if not self.engine:
            self.engine = lyrics_screen.Engine(
                on_status=lambda m: self.events.put(("status", m)),
                on_line=lambda t, a, c, n: self.events.put(("line", (t, a, c, n))),
                on_error=lambda m: self.events.put(("error", m)),
            )
        self.engine.cfg = dict(self.cfg)
        if not (self.thread and self.thread.is_alive()):
            self.thread = threading.Thread(target=self.engine.run, daemon=True)
            self.thread.start()

    def stop(self):
        if self.engine:
            self.engine.stop()

    def _set_device(self, text, color=DIM):
        self.dev_text.config(text=text, fg=color)
        self.dev_dot.config(fg=color)

    # ------------------------------------------------------- simulated screen
    def toggle_sim(self):
        """Turn the simulated screen on/off. When on, pushes go to a
        FakeScreen and are shown in a preview window instead of the
        real keyboard; when off, the next push tries real hardware."""
        self.sim_enabled = not self.sim_enabled
        self.cfg["sim_screen"] = self.sim_enabled
        self.save_cfg()
        if self.sim_enabled:
            self._open_sim_window()
            if self.engine is not None:
                self.engine.screen = FakeScreen(self.events)
            self._set_device("模拟屏幕", MINT)
            self.status_label.config(text="模拟屏幕已开启：推送会显示在模拟窗口里", fg=MINT)
        else:
            self._close_sim_window()
            if self.engine is not None:
                self.engine.screen = None
            self._set_device("未连接", DIM)
            self.status_label.config(text="模拟屏幕已关闭", fg=MUTED)
        if getattr(self, "sim_btn", None) is not None:
            self.sim_btn.config(
                text="模拟屏幕：开" if self.sim_enabled else "模拟屏幕",
                bg=self.accent if self.sim_enabled else CARD_HI)

    def _open_sim_window(self):
        if self.sim_window is not None:
            try:
                self.sim_window.lift()
            except Exception:
                pass
            return
        win = tk.Toplevel(self.root)
        win.title("模拟 Neos75 屏幕 · 480×360")
        win.configure(bg="#000000")
        win.resizable(False, False)
        bezel = tk.Frame(win, bg="#171226", padx=10, pady=10)
        bezel.pack(padx=16, pady=(12, 8))
        self.sim_label = tk.Label(bezel, bg="#000000", text="等待第一次推送…", fg="#A49BB8")
        self.sim_label.pack()
        self.sim_status_label = tk.Label(win, text="模拟屏幕 · 等待推送", bg="#000000", fg="#A49BB8", font=(FONT, 9))
        self.sim_status_label.pack(pady=(0, 12))
        win.protocol("WM_DELETE_WINDOW", self.toggle_sim)
        self.sim_window = win
        if getattr(self, "sim_btn", None) is not None:
            self.sim_btn.config(text="模拟屏幕：开", bg=self.accent)

    def _close_sim_window(self):
        if self.sim_window is not None:
            try:
                self.sim_window.destroy()
            except Exception:
                pass
        self.sim_window = None
        self.sim_label = None
        self.sim_status_label = None
        self.sim_photo = None

    def _on_sim_frame(self, payload):
        # Runs on the main thread (via _drain), so it may touch tkinter.
        if self.sim_window is None or self.sim_label is None:
            return
        pushes, jpeg = payload
        try:
            with Image.open(io.BytesIO(jpeg)) as img:
                img = img.convert("RGB").resize((720, 540), Image.LANCZOS)
            photo = ImageTk.PhotoImage(img)
            self.sim_photo = photo  # keep a reference, or it disappears
            self.sim_label.config(image=photo, text="")
            if self.sim_status_label is not None:
                self.sim_status_label.config(text=f"模拟屏幕 · 第 {pushes} 次推送 · {len(jpeg)} 字节")
        except Exception:
            pass
        prev = self.previews.get(self.page)
        if prev is not None:
            try:
                with Image.open(io.BytesIO(jpeg)) as img:
                    prev.set_image(img)
                prev.set_mode(f"模拟推送 · 第 {pushes} 次")
            except Exception:
                pass

    def _tick_preview(self):
        # The preview shows the exact JPEG the engine last wrote to frame.jpg —
        # the same bytes _push() sends to the keyboard — for the current page.
        # Until the first push the preview stays in its empty state.
        try:
            prev = self.previews.get(self.page)
            p = lyrics_screen.FRAME
            if prev is not None and p.exists():
                with Image.open(p) as img:
                    prev.set_image(img)
                prev.set_mode("实时推送画面")
        except Exception:
            pass
        self.root.after(1200, self._tick_preview)

    def _drain(self):
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "simframe":
                    self._on_sim_frame(payload)
                elif kind == "status":
                    self.status_label.config(text=payload, fg=MUTED)
                    if "已连接" in payload:
                        self._set_device("模拟屏幕" if self.sim_enabled else "已连接", MINT)
                    elif "等待" in payload or "没有找到" in payload or "未找到" in payload:
                        self._set_device("等待键盘", WARN)
                elif kind == "line":
                    track, artist, current, nxt = payload
                    self.track_label.config(text=f"{track}  ·  {artist}" if artist else track)
                    self.line_label.config(text=current)
                    self.next_label.config(text=nxt or "")
                    if self.hint.winfo_manager():
                        self.hint.pack_forget()
                elif kind == "game":
                    self._update_game(payload[0], payload[1])
                elif kind == "error":
                    self.status_label.config(text=payload, fg=DANGER)
        except queue.Empty:
            pass
        self.root.after(80, self._drain)

    # ---------------------------------------------------------------- close
    def on_close(self):
        action = self.cfg.get("close_action", "ask")
        if action == "tray":
            self.hide_to_tray()
        elif action == "quit":
            self.quit()
        else:
            self.ask_close()

    def ask_close(self):
        dlg = tk.Toplevel(self.root)
        dlg.title("关闭 Neos75 屏幕助手")
        dlg.configure(bg=CARD)
        dlg.resizable(False, False)
        dlg.transient(self.root)
        dlg.grab_set()
        try:
            dlg.iconbitmap(ICON_ICO)
        except Exception:
            pass
        tk.Label(dlg, text="最小化到托盘，还是退出？", bg=CARD, fg=FG,
                 font=(FONT, 12, "bold")).pack(padx=28, pady=(22, 6))
        tk.Label(dlg, text="最小化后歌词继续同步，右键托盘图标可随时退出。", bg=CARD,
                 fg=MUTED, font=(FONT, 9)).pack(padx=28)
        remember = tk.BooleanVar(value=False)
        tk.Checkbutton(dlg, text="记住我的选择", variable=remember, bg=CARD, fg=MUTED,
                       selectcolor=CARD_HI, activebackground=CARD, activeforeground=FG,
                       font=(FONT, 9)).pack(pady=(12, 6))
        row = tk.Frame(dlg, bg=CARD)
        row.pack(padx=24, pady=(4, 20))

        def finish(action):
            if remember.get():
                self.cfg["close_action"] = action
                lyrics_screen.save_config(self.cfg)
            dlg.destroy()
            if action == "tray":
                self.hide_to_tray()
            elif action == "quit":
                self.quit()

        pill(row, "最小化到托盘", lambda: finish("tray"), "primary", width=12).pack(side="left", padx=4)
        pill(row, "彻底退出", lambda: finish("quit"), "danger", width=10).pack(side="left", padx=4)
        pill(row, "取消", dlg.destroy, "ghost", width=6).pack(side="left", padx=4)
        dlg.update_idletasks()
        w, h = dlg.winfo_width(), dlg.winfo_height()
        x = self.root.winfo_rootx() + (self.root.winfo_width() - w) // 2
        y = self.root.winfo_rooty() + (self.root.winfo_height() - h) // 2
        dlg.geometry(f"+{max(0, x)}+{max(0, y)}")

    def hide_to_tray(self):
        self.root.withdraw()
        self.ensure_tray()
        try:
            self.tray.notify("仍在后台运行，双击托盘图标恢复窗口", "Neos75 歌词屏")
        except Exception:
            pass

    def ensure_tray(self):
        if self.tray:
            return
        try:
            image = Image.open(ICON_PNG)
        except Exception:
            image = Image.new("RGB", (64, 64), ACCENT)
        push_menu = pystray.Menu(
            pystray.MenuItem("歌词", lambda: self.root.after(0, lambda: self.tray_push("lyrics"))),
            pystray.MenuItem("对局", lambda: self.root.after(0, lambda: self.tray_push("game"))),
            pystray.MenuItem("监控", lambda: self.root.after(0, lambda: self.tray_push("mon"))),
            pystray.MenuItem("闲置壁纸", lambda: self.root.after(0, lambda: self.tray_push("idle"))),
        )
        page_menu = pystray.Menu(
            pystray.MenuItem("歌词", lambda: self.root.after(0, lambda: self.open_page("lyrics"))),
            pystray.MenuItem("对局", lambda: self.root.after(0, lambda: self.open_page("game"))),
            pystray.MenuItem("监控", lambda: self.root.after(0, lambda: self.open_page("mon"))),
            pystray.MenuItem("外观", lambda: self.root.after(0, lambda: self.open_page("look"))),
            pystray.MenuItem("日志", lambda: self.root.after(0, lambda: self.open_page("log"))),
        )
        menu = pystray.Menu(
            pystray.MenuItem("显示主界面", lambda: self.root.after(0, self.show_window), default=True),
            pystray.MenuItem("开始同步", lambda: self.root.after(0, self.start)),
            pystray.MenuItem("停止同步", lambda: self.root.after(0, self.stop)),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("推送到屏幕", push_menu),
            pystray.MenuItem("打开页面", page_menu),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("退出", lambda: self.root.after(0, self.quit)),
        )
        self.tray = pystray.Icon("neos75-lyrics", image, "Neos75 歌词屏", menu)
        threading.Thread(target=self.tray.run, daemon=True).start()

    def tray_push(self, key):
        if key == "idle":
            if self.engine and self.engine.running:
                self.engine.cfg = dict(self.cfg)
                self.engine.show_idle_now()
            return
        if not self.engine or not self.engine.running:
            self.notify("先开始同步才能推送")
            return
        ok, msg = self.engine.push_module(key)
        self.notify(msg)

    def open_page(self, key):
        self.show_window()
        self.show_page(key)

    def notify(self, text):
        try:
            if self.tray:
                self.tray.notify(text, "Neos75 屏幕助手")
        except Exception:
            pass

    def show_window(self):
        self.root.deiconify()
        self.root.lift()
        try:
            self.root.focus_force()
        except Exception:
            pass

    def quit(self):
        self.stop()
        if self.tray:
            try:
                self.tray.stop()
            except Exception:
                pass
            self.tray = None
        if self.engine and self.engine.screen:
            try:
                self.engine.screen.close()
            except Exception:
                pass
        self.root.destroy()


def acquire_single_instance():
    """Return a mutex handle, or None when another copy is already running."""
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.CreateMutexW(None, False, "Local\\Neos75ScreenHelper")
        ERROR_ALREADY_EXISTS = 183
        if kernel32.GetLastError() == ERROR_ALREADY_EXISTS:
            return None
        return handle
    except Exception:
        return True


def focus_existing_window():
    try:
        import ctypes
        u = ctypes.windll.user32
        hwnd = u.FindWindowW(None, "Neos75 屏幕助手")
        if hwnd:
            u.ShowWindow(hwnd, 9)
            u.SetForegroundWindow(hwnd)
    except Exception:
        pass


def main():
    if acquire_single_instance() is None:
        # another copy owns the screen: bring it forward and leave quietly
        focus_existing_window()
        return
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    if "--diag" in sys.argv:
        import lyrics_screen as L
        print("windows:", L.find_windows({"DesktopLyrics", "OrpheusBrowserHost"}))
        print("audio_active:", L.audio_active())
        raise SystemExit(0)
    try:
        main()
    except Exception:
        logsetup.exception('crash')
        raise
