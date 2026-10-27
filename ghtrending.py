# -*- coding: utf-8 -*-
"""
GitHub Trending EXE — 每周 Star 增长 Top10 排行
================================================
数据源: GitHub Search API (官方, 无需登录, 60次/小时)
  - 检索近 7 天创建或更新的仓库, 按 stargazers_count 降序取 Top 10
  - 单仓库查询数 (stars/forks/issues/语言) 缓存 1 小时, 同步保底
  - OSSInsight API 可选加速, 失败自动降级

分类方法: 先按 "科技/生活" 粗分, 再按关键词细分主题
  - 科技: 人工智能/大模型, 前端/Web, 后端/系统, 移动开发, 游戏/引擎, 工具/开发,
          硬件/嵌入式, 科学/算法, 安全/隐私, 云原生/运维, 数据/数据库, 编程语言/编译器
  - 生活: 媒体/影音, 效率/笔记, 设计, 网络工具, 学习/资源, 金融/理财, 音乐/音频,
          智能家居, 生活/健康, 阅读/文档

外观: 设置里可换 10 套预设主题或自定义背景色/两个主色, 明暗自动适配
背景图: 设置里可选一张本地图片当壁纸 (等比铺满 + 可调压暗), 卡片自动取背后那块
        照片的平均色做毛玻璃底, 内容区间隙能看到图片本身
翻译: 底部「一键翻译」+ 语言菜单 (简繁中/英/日/韩/俄/法/德/西/葡/阿/印地)

交互: 双击 / 右键菜单 / 回车 / 底部按钮 → 打开浏览器跳转项目页
打包: python -m PyInstaller --onefile --windowed --name "GitHub周榜Top10" github_trending.py
"""
import concurrent.futures
import ctypes
import hashlib
import json
import math
import os
import queue
import re
import sys
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
import urllib.error
import urllib.parse
import urllib.request
import base64
import webbrowser
from datetime import datetime, timedelta, timezone
from tkinter import colorchooser, filedialog

# 窗帘动画的"透明色键": 这个颜色的像素在分层窗口里等于全透明。
# 面板位图是纯矩形, 边上没有半透明像素, 所以色键不会啃出毛边。
_CURTAIN_KEY = "#ff00ff"
_KEYREF = 0x00FF00FF          # ctypes 用的 0x00BBGGRR


def _layer_key(hwnd, alpha=255):
    """把窗口变成"色键透明 + 整体不透明度"的分层窗口 (Tk 自己做不到)。"""
    try:
        u = ctypes.windll.user32
        ex = u.GetWindowLongW(hwnd, -20)
        u.SetWindowLongW(hwnd, -20, ex | 0x00080000)          # WS_EX_LAYERED
        return bool(u.SetLayeredWindowAttributes(hwnd, _KEYREF, max(0, min(255, alpha)), 0x1 | 0x2))
    except Exception:
        return False


def _layer_alpha(hwnd, alpha):
    """只改整体不透明度 (色键保持), 一帧 ~0.1ms。"""
    try:
        ctypes.windll.user32.SetLayeredWindowAttributes(
            hwnd, _KEYREF, max(0, min(255, int(alpha))), 0x1 | 0x2)
    except Exception:
        pass


def _timer_hi_res():
    """把 Windows 定时器精度从 15.6ms 提到 1ms。

    默认精度下 after(8) 实测交付间隔 15.5ms (≈64fps, 还会抖); 提到 1ms 后
    after(8) 是 8.2ms (≈122fps, p90 8.8ms) —— 这是"卡不卡"最关键的开关。
    """
    try:
        return bool(ctypes.windll.winmm.timeBeginPeriod(1))
    except Exception:
        return False



def _dbg(msg):
    """程序目录存在 .debug 文件时, 把交互写进 debug.log (真机排查用)。"""
    try:
        d = os.path.dirname(os.path.abspath(CACHE_FILE))
        if not os.path.isfile(os.path.join(d, ".debug")):
            return
        with open(os.path.join(d, "debug.log"), "a", encoding="utf-8") as f:
            f.write("%.3f %s\n" % (time.time() % 10000, msg))
    except Exception:
        pass


def _install_exc_logging():
    """把未捕获异常写进 debug.log (EXE 没控制台, 不打日志什么都看不到)。"""
    import traceback as _tb

    def hook(t, v, tb):
        _dbg("EXC " + "".join(_tb.format_exception(t, v, tb)).replace("\n", " | "))

    sys.excepthook = hook
    try:
        tk.Tk.report_callback_exception = lambda self, t, v, tb: hook(t, v, tb)
    except Exception:
        pass


def _enable_dpi_awareness():
    """尽早声明 DPI 感知, 避免高分屏下字体发虚、窗口被系统缩放。"""
    for fn in (lambda: ctypes.windll.shcore.SetProcessDpiAwareness(2),
               lambda: ctypes.windll.shcore.SetProcessDpiAwareness(1),
               lambda: ctypes.windll.user32.SetProcessDPIAware()):
        try:
            fn()
            return
        except Exception:
            continue


if sys.platform == "win32":
    _enable_dpi_awareness()
    _timer_hi_res()                 # after(8) 才真能 8ms 交付 (不设是 15.6ms)
_install_exc_logging()

# ---------------------------------------------------------------- 常量
APP_TITLE = "GitHub 每周 Star 榜 Top10"
CACHE_FILE = os.path.join(os.path.dirname(os.path.abspath(sys.argv[0])), "trending_cache.json")
SETTINGS_FILE = os.path.join(os.path.dirname(os.path.abspath(sys.argv[0])), "settings.json")
CAT_FRESH_SEC = 20 * 3600  # 分类结果 20 小时内直接复用, 不重复打 API (一天一轮)
MAX_TOPIC_PER_RUN = 4      # 一次刷新最多补抓多少个分类 (越快越好: 其余留后台安静补)
CAT_STORE_KEY = "cats"   # cache 里按分类持久化的抓取结果
CACHE_TTL = 3600  # 秒
_ICON_B64 = (
    "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"
)


def set_window_icon(win):
    """把内置图标挂到窗口上 (任务栏/标题栏用)。"""
    try:
        img = tk.PhotoImage(data=base64.b64decode(_ICON_B64))
        win.iconphoto(True, img)
        win._icon_img = img
    except Exception:
        pass


_SHADOW = "#05070c"   # 壁纸模式下文字投影色
_WHEEL_PX = 66        # 滚轮一格滚动的像素 (约 3 行)
_INERTIA_MAX = 150.0  # 惯性滑行最大追加像素
_INERTIA_DECAY = 0.86  # 惯性每帧衰减
UA = {"User-Agent": "GitHubTrendingEXE/1.0 (github-trending-viewer)"}


def _gh_token():
    """可选的 GitHub Token: 有它就 30 次/分钟(未认证只有 10 次/分钟)。"""
    try:
        t = (SETTINGS.get("token") or "").strip()
    except Exception:
        t = ""
    if not t:
        t = (os.environ.get("GITHUB_TOKEN") or "").strip()
    return t


def _auth_headers(url):
    """只给 api.github.com 加 Token, 别把 token 发给第三方站点。"""
    h = dict(UA)
    if _host_of(url) == "api.github.com":
        t = _gh_token()
        if t:
            h["Authorization"] = "Bearer " + t
            h["Accept"] = "application/vnd.github+json"
    return h


# ---------------------------------------------------------------- 配色/设置
def _hex2rgb(h):
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def _rgb2hex(t):
    return "#%02x%02x%02x" % tuple(max(0, min(255, int(round(v)))) for v in t)


def _mix(c1, c2, t):
    """把 c1 向 c2 混合 t (0~1)。"""
    a, b = _hex2rgb(c1), _hex2rgb(c2)
    return _rgb2hex([x + (y - x) * t for x, y in zip(a, b)])


def _luma(h):
    r, g, b = [v / 255.0 for v in _hex2rgb(h)]
    return 0.299 * r + 0.587 * g + 0.114 * b


def _shadow_of(ink):
    """文字影子色: 亮字给同色暗影, 暗字给同色亮影。

    用纯黑/纯白会把笔画边缘"糊"成第二份字, 看着像重影; 用同色相明度差
    只在笔画外圈压一圈, 照片再花也不抢字。
    """
    try:
        return _mix(ink, "#000000", 0.66) if _luma(ink) > 0.5 else _mix(ink, "#ffffff", 0.66)
    except Exception:
        return _SHADOW


# ---------------------------------------------------------------- 过渡动画
def _ease_out_cubic(t):
    t = 0.0 if t < 0 else (1.0 if t > 1 else t)
    return 1 - (1 - t) ** 3


def _ease_out_quint(t):
    t = 0.0 if t < 0 else (1.0 if t > 1 else t)
    return 1 - (1 - t) ** 5


def _ease_in_out(t):
    """iOS 那种先慢后快再收住的手感。"""
    t = 0.0 if t < 0 else (1.0 if t > 1 else t)
    return t * t * (3 - 2 * t)


class _Ticker:
    """单循环 60fps 补间: 颜色/数值都走这里, 每个 key 只保留最新一条。

    用法: tick.add("row12", 0.22, lambda t: ..., delay=0.04)
    t 已经过缓动 (默认 ease-out-cubic), 回调里只管把 t 用到属性上。
    """

    def __init__(self, app, fps=120):
        self.app = app
        self.items = {}
        self.job = None
        self.dt = 1.0 / max(30, fps)
        self.alive = True

    def add(self, key, dur, apply, delay=0.0, ease=_ease_out_cubic):
        self.items[key] = {
            "t0": time.time(), "dur": max(0.01, float(dur)),
            "d": max(0.0, float(delay)), "fn": apply, "ease": ease,
        }
        if self.job is None:
            self.job = self.app.after(int(self.dt * 1000), self._tick)

    def cancel(self, key):
        self.items.pop(key, None)

    def stop(self):
        self.alive = False
        if self.job is not None:
            try:
                self.app.after_cancel(self.job)
            except Exception:
                pass
            self.job = None

    def _tick(self):
        self.job = None
        if not self.alive or not self.items:
            return
        t_start = time.perf_counter()
        now = time.time()
        dead = []
        for k, it in list(self.items.items()):
            el = now - it["t0"] - it["d"]
            if el < 0:
                continue
            t = el / it["dur"]
            if t >= 1.0:
                t = 1.0
                dead.append(k)
            try:
                it["fn"](it["ease"](t))
            except tk.TclError:
                dead.append(k)
            except Exception:
                dead.append(k)
        for k in dead:
            self.items.pop(k, None)
        if self.items:
            # 扣掉本帧已花掉的时间再排下一帧: 否则 8ms 间隔 + 7ms 干活 = 15ms/帧
            spent = (time.perf_counter() - t_start) * 1000
            delay = max(1, int(round(self.dt * 1000 - spent)))
            self.job = self.app.after(delay, self._tick)


DEFAULT_SETTINGS = {
    "bg": "#0d1017",       # 背景
    "tech": "#5b9dff",     # 科技主色
    "life": "#c98bff",     # 生活主色
    "lang": "off",         # 翻译目标语言, off = 原文
    "details": True,       # 详情行默认展开
    "bgimg": "",           # 自定义背景图 (本地图片路径, 空 = 纯色)
    "bgdim": 0.42,         # 背景图压暗程度 0~0.85 (越大文字越清楚)
    "cardop": 0.86,        # 卡片不透明度 0.45~1.0 (越小越透, 越能看见背景图)
    "wallmode": True,      # 壁纸模式: 文字直接压在图上, 不画卡片色块
    "glass": 0.55,         # 毛玻璃强度 0~1 (0 = 关, 越大越磨砂)
    "token": "",           # 可选 GitHub Token: 抓取提速 3 倍 (30次/分钟 + 4路并发)
}


# ---------------------------------------------------------------- 背景图
_BG_CACHE = {}


def _pil():
    """惰性引入 Pillow, 没装也不影响主功能。"""
    try:
        import PIL.Image as _img
        return _img
    except Exception:
        return None


def _bg_open(path):
    """打开并缓存原图 (RGB); 失败返回 None。"""
    if not path:
        return None
    key = os.path.abspath(path)
    if key in _BG_CACHE:
        return _BG_CACHE[key]
    Image = _pil()
    im = None
    if Image is not None:
        try:
            im = Image.open(key)
            try:
                from PIL import ImageOps
                im = ImageOps.exif_transpose(im)
            except Exception:
                pass
            im = im.convert("RGB")
        except Exception:
            im = None
    _BG_CACHE[key] = im
    return im


def _bg_avg(im, box=None):
    """取一块区域的平均色 (缩到 24×24 再平均, 很快)。"""
    if im is None:
        return None
    try:
        src = im if box is None else im.crop(box)
        src = src.resize((24, 24))
        px = list(src.getdata())
        n = max(1, len(px))
        return _rgb2hex((sum(p[0] for p in px) / n,
                         sum(p[1] for p in px) / n,
                         sum(p[2] for p in px) / n))
    except Exception:
        return None


def _bg_cover(im, w, h):
    """等比放大后居中裁剪成 w×h (铺满不留白)。"""
    Image = _pil()
    if im is None or Image is None or w < 2 or h < 2:
        return None
    try:
        sw, sh = im.size
        scale = max(w / float(sw), h / float(sh))
        nw, nh = max(1, int(sw * scale + 0.5)), max(1, int(sh * scale + 0.5))
        im2 = im.resize((nw, nh), Image.LANCZOS)
        left, top = (nw - w) // 2, (nh - h) // 2
        return im2.crop((left, top, left + w, top + h))
    except Exception:
        return None


def _bg_dim(im, dim, base_hex):
    """把图片向底色压暗, 保证文字可读。"""
    Image = _pil()
    if im is None or Image is None:
        return im
    try:
        dim = max(0.0, min(0.9, float(dim or 0)))
        if dim <= 0.001:
            return im
        base = Image.new("RGB", im.size, _hex2rgb(base_hex))
        return Image.blend(im, base, dim)
    except Exception:
        return im


_BGVIEW_MEM = {}


def _bg_view_cached(path, w, h, dim, base_hex):
    """整窗壁纸 (cover + 压暗 + 明暗融合) 按参数缓存。

    开场/改尺寸时会被连着算好几次, 每次都要解码 13MB 原图再缩放 ~0.4s,
    缓存之后第二次开始就是取现成的, 不会再卡动画。
    """
    Image = _pil()
    if Image is None:
        return None
    try:
        mt = os.path.getmtime(path)
    except OSError:
        mt = 0
    key = (path, round(float(mt), 1), int(w), int(h),
           round(float(dim or 0), 3), str(base_hex))
    got = _BGVIEW_MEM.get(key)
    if got is not None:
        return got
    im = _bg_open(path)
    if im is None:
        return None
    view = _bg_dim(_bg_cover(im, w, h), dim, base_hex)
    if view is None:
        return None
    try:                       # 照片明暗和主题冲突时再融一点底色, 否则文字看不清
        av = _bg_avg(view)
        if av:
            gap = _luma(base_hex) - _luma(av)
            if abs(gap) > 0.18:
                extra = min(0.45, abs(gap) * 0.55)
                view = Image.blend(view, Image.new("RGB", view.size,
                                                   _hex2rgb(base_hex)), extra)
    except Exception:
        pass
    if len(_BGVIEW_MEM) > 2:
        _BGVIEW_MEM.clear()
    _BGVIEW_MEM[key] = view
    return view


def _blur_fast(im, radius=16, shrink=4):
    """毛玻璃的"模糊底": 缩到 1/4 做高斯再放大回来。

    全尺寸 1460×940 上做 radius=16 的高斯要 40ms 以上, 降 4 倍再升回来
    只要 3ms 左右, 肉眼看不出区别 (反正要的就是"糊")。
    """
    Image = _pil()
    if im is None or Image is None:
        return None
    try:
        from PIL import ImageFilter as _IF
        w, h = im.size
        f = max(1, int(shrink))
        r = max(0.5, float(radius)) / f
        small = im.resize((max(2, w // f), max(2, h // f)), Image.BILINEAR)
        small = small.filter(_IF.GaussianBlur(r))
        return small.resize((w, h), Image.BILINEAR)
    except Exception:
        return im


def _glass_plate(sharp, blur, box, rad=13, tint="#0d1017", amount=0.22,
                 strength=1.0, edge=None, mask_cache=None):
    """一块"磨砂玻璃"贴片, 圆角内 = 模糊+染色, 圆角外 = 原像素。

    Tk 的 canvas 图元没法跟**底下的图元**做 alpha 混合 (RGBA 只会跟画布
    底色混), 所以这里用 PIL 先把"原图 + 圆角玻璃"合成成一张不透明图再贴,
    贴回去与壁纸严丝合缝, 没有半透明接缝。
    """
    Image = _pil()
    if sharp is None or blur is None or Image is None:
        return None
    try:
        from PIL import ImageDraw as _ID
        box = tuple(int(v) for v in box)
        if box[2] - box[0] < 8 or box[3] - box[1] < 8:
            return None
        base = sharp.crop(box)
        frosted = blur.crop(box)
        a = max(0.0, min(0.92, float(amount) * max(0.0, min(1.0, float(strength)))))
        if a > 0.004:
            frosted = Image.blend(frosted,
                                  Image.new("RGB", frosted.size, _hex2rgb(tint)), a)
        r = int(max(0, min(rad, (base.size[1] - 1) // 2, (base.size[0] - 1) // 2)))
        key = (base.size, r)
        mask = mask_cache.get(key) if mask_cache is not None else None
        if mask is None:
            mask = Image.new("L", base.size, 0)
            _ID.Draw(mask).rounded_rectangle(
                [0, 0, base.size[0] - 1, base.size[1] - 1], radius=r, fill=255)
            if mask_cache is not None:
                if len(mask_cache) > 48:
                    mask_cache.clear()
                mask_cache[key] = mask
        out = Image.composite(frosted, base, mask)
        if edge:                    # 1px 玻璃边: 让"这是一层面板"读得出来
            try:
                _ID.Draw(out).rounded_rectangle(
                    [0, 0, base.size[0] - 1, base.size[1] - 1], radius=r,
                    outline=_hex2rgb(edge), width=1)
            except Exception:
                pass
            # 顶边再压一道更亮的高光: 玻璃"接光"的那一面
            try:
                hi = _ID.Draw(out)
                hi.rounded_rectangle([1, 1, base.size[0] - 2, base.size[1] - 2],
                                     radius=max(0, r - 1),
                                     outline=_hex2rgb(_mix(edge, "#ffffff", 0.55)),
                                     width=1)
            except Exception:
                pass
        return out
    except Exception:
        return None


def _bg_tile_vertical(base, w, h):
    """把单屏图按宽度缩放后竖向镜像重复, 铺满整块内容区 (接缝不可见)。"""
    Image = _pil()
    if base is None or Image is None or w < 2 or h < 2:
        return None
    try:
        bw, bh = base.size
        th = max(1, int(bh * (w / float(bw)) + 0.5))
        tile = base.resize((w, th), Image.LANCZOS)
        flip = tile.transpose(Image.FLIP_TOP_BOTTOM)
        out = Image.new("RGB", (w, h))
        y, use_flip = 0, False
        while y < h:
            out.paste(flip if use_flip else tile, (0, y))
            y += th
            use_flip = not use_flip
        return out
    except Exception:
        return None

# 预设主题: (名称, 背景, 科技色, 生活色)
THEME_PRESETS = [
    ("午夜蓝",  "#0d1017", "#5b9dff", "#c98bff"),
    ("石墨黑",  "#101114", "#7c9cff", "#d0a2ff"),
    ("深空紫",  "#0f0a19", "#8b7bff", "#ff7bd5"),
    ("曜石黑",  "#000000", "#4f8cff", "#c07bff"),
    ("黛蓝",    "#0b1622", "#3fa9f5", "#7bd3b0"),
    ("松林绿",  "#0b1410", "#3ddc97", "#ffd166"),
    ("暖褐",    "#14100c", "#ffab5e", "#ff7e9d"),
    ("夜樱",    "#1a1017", "#ff9ec4", "#c4a2ff"),
    ("藏青",    "#0a1020", "#00d4ff", "#ffb86b"),
    ("日间白",  "#f4f6fb", "#2f6bff", "#8b5cf6"),
]

# 翻译目标语言
TRANS_LANGS = [
    ("原文（不翻译）", "off"),
    ("中文（简体）",   "zh-CN"),
    ("中文（繁體）",   "zh-TW"),
    ("English",       "en"),
    ("日本語",        "ja"),
    ("한국어",         "ko"),
    ("Русский",       "ru"),
    ("Français",      "fr"),
    ("Deutsch",       "de"),
    ("Español",       "es"),
    ("Português",     "pt"),
    ("العربية",        "ar"),
    ("हिन्दी",           "hi"),
]


def load_settings():
    s = dict(DEFAULT_SETTINGS)
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
            s.update(json.load(f) or {})
    except Exception:
        pass
    return s


def save_settings(s):
    try:
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(s, f, ensure_ascii=False, indent=1)
    except Exception:
        pass


def build_palette(bg, tech, life):
    """由背景色 + 两个主色派生出整套配色 (自动适配明暗)。"""
    light = _luma(bg) > 0.62
    ink = "#000000" if light else "#ffffff"
    text = _mix(bg, ink, 0.88)
    sub = _mix(bg, ink, 0.66 if light else 0.55)
    muted = _mix(bg, ink, 0.50 if light else 0.38)
    hl = "#b96a00" if light else "#ffc857"

    def fam(accent):
        return {
            "bg": bg,
            "card": _mix(bg, accent, 0.12),
            "hover": _mix(bg, accent, 0.22),
            "accent": accent,
            "text": text,
            "sub": sub,
            "hl": hl,
            "badge": _mix(bg, accent, 0.28),
        }

    btn = _mix(bg, tech, 0.90 if light else 0.34)
    ui = {
        "bg": bg,
        "title": text,
        "text": text,
        "sub": sub,
        "muted": muted,
        "btn": btn,
        "btn_hover": _mix(bg, tech, 1.0 if light else 0.52),
        "btn_soft": _mix(bg, ink, 0.10 if light else 0.13),
        "btn_soft_hover": _mix(bg, ink, 0.19 if light else 0.22),
        "btn_edge": _mix(bg, ink, 0.26),
        "btn_fg": "#ffffff" if _luma(btn) < 0.58 else "#141414",
        "ok": "#2f7d4f" if light else "#7fd18b",
        "warn": "#a8700a" if light else "#e8c46b",
        "err": "#b23b3b" if light else "#ff8a8a",
        "scroll": _mix(bg, tech, 0.24),
        "scroll_hover": _mix(bg, tech, 0.42),
        "entry": _mix(bg, ink, 0.10),
    }

    return {"科技": fam(tech), "生活": fam(life), "ui": ui}

# 分类→GitHub Search 限定词 (每类 1 次请求, 未登录限 10 次/分钟, 故全局节流)
TECH_QUERIES = [
    ("🤖 人工智能/大模型", "topic:llm"),
    ("🖥️ 前端/Web",       "topic:frontend"),
    ("⚙️ 后端/系统",       "language:rust"),
    ("📱 移动开发",         "topic:android"),
    ("🎮 游戏/引擎",       "topic:game"),
    ("🛠️ 工具/开发",      "topic:developer-tools"),
    ("📡 硬件/嵌入式",      "topic:raspberry-pi"),
    ("🔬 科学/算法",       "topic:science"),
    ("🔐 安全/隐私",       "topic:security"),
    ("☁️ 云原生/运维",     "topic:kubernetes"),
    ("🗄️ 数据/数据库",     "topic:database"),
    ("🧩 编程语言/编译器",  "topic:compilers"),
]
LIFE_QUERIES = [
    ("🎬 媒体/影音",       "topic:ffmpeg"),
    ("📝 效率/笔记",       "topic:productivity"),
    ("🎨 设计",           "topic:icons"),
    ("🌐 网络工具",        "topic:network"),
    ("📚 学习/资源",       "topic:awesome-list"),
    ("💰 金融/理财",       "topic:finance"),
    ("🎵 音乐/音频",       "topic:music"),
    ("🏠 智能家居",        "topic:home-automation"),
    ("🍳 生活/健康",       "topic:health"),
    ("📖 阅读/文档",       "topic:documentation"),
]


# ---------------------------------------------------------------- 分类
def categorize(full_name, description, language, topics):
    """返回 (大类, 小类)。优先级: 强生活topic > AI信号 > 弱生活topic > 其他科技 > 语言。"""
    blob = f"{full_name or ''} {description or ''} {' '.join(topics or [])}".lower()
    lang = (language or "").lower()
    t = set(topics or [])

    # ── 强生活信号: 明确的生活类 topic, 优先于一切 ──────────
    if t & {"media-server", "jellyfin", "plex", "iptv", "emby", "danmu", "subtitle"}:
        return "生活", "媒体/影音"
    if t & {"note-taking", "journal", "diary", "knowledge-base"}:
        return "生活", "效率/笔记"
    if t & {"figma", "icons", "wallpaper", "font", "ui-kit"}:
        return "生活", "设计"
    if t & {"clash", "shadowsocks", "trojan", "vpn", "proxy"}:
        return "生活", "网络工具"
    if t & {"awesome-list", "awesome", "cheatsheet", "roadmap", "interview"}:
        return "生活", "学习/资源"
    if t & {"finance", "fintech", "trading", "quant", "stock", "investment", "accounting"}:
        return "生活", "金融/理财"
    if t & {"music", "spotify", "audio-player", "mp3", "podcast", "sound"}:
        return "生活", "音乐/音频"
    if t & {"home-assistant", "home-automation", "smart-home", "domotics"}:
        return "生活", "智能家居"
    if t & {"health", "fitness", "workout", "diet", "sleep", "medical"}:
        return "生活", "生活/健康"
    if t & {"documentation", "ebook", "epub", "reader", "wiki", "docs"}:
        return "生活", "阅读/文档"

    # ── AI 最优先的科技信号 (描述含 llm 等也算) ─────────────
    if t & {"llm", "machine-learning", "deep-learning", "ai-agent", "artificial-intelligence",
            "chatbot", "rag", "diffusion", "transformers", "nlp", "computer-vision", "speech",
            "stable-diffusion", "langchain", "generative-ai"} \
       or any(k in blob for k in ("大模型", "人工智能", "ai agent", "ai-agent", " llm", "llm-", "chatgpt",
                                  "gpt-", "deepseek", "推理框架", "智能体", "language model", "machine learning",
                                  "deep learning", "neural network")):
        return "科技", "人工智能/大模型"

    # ── 弱生活信号 (video/audio/productivity 等泛 topic) ────
    if t & {"video", "audio", "streaming", "movie", "anime", "rss", "podcast"} \
       or any(k in blob for k in ("音乐播放", "media player", "追剧", "影视", "小说", "阅读器", "音乐")):
        return "生活", "媒体/影音"
    if t & {"notes", "productivity", "markdown", "todo", "obsidian", "anki"} \
       or any(k in blob for k in ("笔记", "待办", "日程", "记账", "习惯", "日记", "清单", "todo", "note app", "note-taking", "productiv")):
        return "生活", "效率/笔记"
    if t & {"design", "design-tools", "icons-pack", "emoji", "theme"}:
        return "生活", "设计"

    # ── 科技新增细分 ────────────────────────────────────────
    if t & {"security", "penetration-testing", "ctf", "malware", "cryptography", "cybersecurity",
            "privacy", "hacking", "reverse-engineering", "exploit"} \
       or any(k in blob for k in ("安全", "渗透", "漏洞", "加密", "隐私")):
        return "科技", "安全/隐私"
    if t & {"kubernetes", "docker", "devops", "terraform", "ansible", "infrastructure",
            "containers", "cloud", "serverless", "observability"}:
        return "科技", "云原生/运维"
    if t & {"database", "postgresql", "sql", "mysql", "redis", "sqlite", "etl", "data-pipeline",
            "data-engineering", "mongodb", "clickhouse"}:
        return "科技", "数据/数据库"
    if t & {"compiler", "interpreter", "programming-language", "wasm", "llvm", "parser", "virtual-machine"}:
        return "科技", "编程语言/编译器"

    # ── 科技类其余 ──────────────────────────────────────────
    if t & {"frontend", "react", "vue", "svelte", "nextjs", "tailwind", "css", "webapp"} or lang == "typescript":
        return "科技", "前端/Web"
    if t & {"android", "ios", "flutter", "swiftui", "kotlin", "jetpack-compose", "react-native"} \
       or any(k in blob for k in ("安卓", "手机", "android tv", "apple tv", "debloat")):
        return "科技", "移动开发"
    if t & {"game", "game-engine", "gamedev", "godot", "unity", "unreal"} or "game" in blob:
        return "科技", "游戏/引擎"
    if t & {"hardware", "embedded", "raspberry-pi", "esp32", "arduino", "firmware", "iot", "robot"} \
       or any(k in blob for k in ("硬件", "单片机", "嵌入式", "固件")):
        return "科技", "硬件/嵌入式"
    if t & {"science", "algorithm", "mathematics", "physics", "bioinformatics", "quantum"} \
       or any(k in blob for k in ("算法", "科学计算")):
        return "科技", "科学/算法"
    if t & {"developer-tools", "cli", "terminal", "shell", "editor", "ide", "git", "zsh"}:
        return "科技", "工具/开发"
    if t & {"network", "firewall", "dns", "tunnel"} or "proxy" in blob:
        return "生活", "网络工具"
    if lang in ("rust", "go", "c", "c++", "zig"):
        return "科技", "后端/系统"
    if lang in ("python", "java", "javascript", "typescript", "c#", "php", "ruby", "swift", "kotlin", "dart", "lua", "elixir", "haskell", "ocaml", "scala"):
        return "科技", "工具/开发"
    return "科技", "其他"


# 分类元数据: 小类名 -> (emoji, 大类)  用于顶部筛选 chip 的排序/配色
def _build_cat_meta():
    out = {}
    for queries, family in ((TECH_QUERIES, "科技"), (LIFE_QUERIES, "生活")):
        for label, _q in queries:
            m = re.search(r"[\u4e00-\u9fff]", label)
            if not m:
                out[label] = ("", family)
            else:
                i = m.start()
                out[label[i:]] = (label[:i].strip(), family)
    return out


_CAT_META = _build_cat_meta()


# ---------------------------------------------------------------- 网络层
_OPENER_DIRECT = urllib.request.build_opener(urllib.request.ProxyHandler({}))   # 直连
_OPENER_SYSPROXY = urllib.request.build_opener()                                # 系统代理
_NET_PREF = {}   # host -> "direct" / "proxy", 记住每个域名上次成功的通路
_NET_LOCK = threading.Lock()
_QSTAT_LOCK = threading.Lock()   # 查询健康度表(空结果的查询当天不再重试)的锁
_NET_BOOTED = [False]


def _netpref_boot():
    """首次使用时从缓存载入上次成功的通路, 避免每次启动都重试死路。"""
    if _NET_BOOTED[0]:
        return
    _NET_BOOTED[0] = True
    try:
        p = (load_cache() or {}).get("netpref") or {}
        if isinstance(p, dict):
            for k, v in p.items():
                if v in ("direct", "proxy"):
                    _NET_PREF[k] = v
    except Exception:
        pass


def _netpref_save():
    try:
        c = load_cache()
        c["netpref"] = dict(_NET_PREF)
        save_cache(c)
    except Exception:
        pass


def _host_of(url):
    try:
        return urllib.parse.urlsplit(url).netloc.lower()
    except Exception:
        return ""


def _opener_chain(url):
    """按上次成功的通路优先返回 opener 顺序。"""
    _netpref_boot()
    pref = _NET_PREF.get(_host_of(url))
    if pref == "proxy":
        return (_OPENER_SYSPROXY, _OPENER_DIRECT)
    return (_OPENER_DIRECT, _OPENER_SYSPROXY)


def _mark_net(url, opener):
    with _NET_LOCK:
        _NET_PREF[_host_of(url)] = "direct" if opener is _OPENER_DIRECT else "proxy"
    _netpref_save()


def _http_json(url, timeout=12):
    """GET 并解析 JSON。直连/代理自动择优 + 记忆, 非 200 抛 RuntimeError。"""
    last_err = None
    for opener in _opener_chain(url):
        req = urllib.request.Request(url, headers=_auth_headers(url))
        try:
            with opener.open(req, timeout=timeout) as resp:
                if getattr(resp, "status", 200) != 200:
                    raise RuntimeError(f"HTTP {resp.status}")
                data = json.loads(resp.read().decode("utf-8", "replace"))
                _mark_net(url, opener)
                return data
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"HTTP {e.code}") from None
        except Exception as e:
            last_err = str(getattr(e, "reason", e))
    raise RuntimeError(last_err or "connection failed")


def load_cache():
    for p in (CACHE_FILE, CACHE_FILE + ".bak"):
        try:
            with open(p, "r", encoding="utf-8") as f:
                d = json.load(f)
            if isinstance(d, dict):
                return d
        except Exception:
            continue
    return {}


def save_cache(cache):
    """合并写盘 + 原子替换: 进程/线程同时写也不会互相抹掉别的键。"""
    if not cache:
        return
    try:
        disk = load_cache()
        if isinstance(disk, dict):
            for k, v in disk.items():
                if k not in cache:
                    cache[k] = v
        tmp = CACHE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False, indent=1)
        try:
            os.replace(CACHE_FILE, CACHE_FILE + ".bak")
        except Exception:
            pass
        os.replace(tmp, CACHE_FILE)
    except Exception:
        pass


# ---------------------------------------------------------------- 翻译
CJK_RE = re.compile(r"[\u3400-\u9fff\u3040-\u30ff\uac00-\ud7af]")
_TRANS_MEM = None          # 内存里的翻译表 {key: 译文}
_TRANS_LOCK = threading.Lock()


def _http_text(url, timeout=9):
    """GET 纯文本, 同样按域名记忆直连/代理择优。"""
    last_err = None
    for opener in _opener_chain(url):
        req = urllib.request.Request(url, headers=UA)
        try:
            with opener.open(req, timeout=timeout) as resp:
                txt = resp.read().decode("utf-8", "replace")
                _mark_net(url, opener)
                return txt
        except Exception as e:
            last_err = str(getattr(e, "reason", e))
    raise RuntimeError(last_err or "connection failed")


def _translate_remote(text, target):
    """在线翻译: Google 免费端点优先, MyMemory 兜底。"""
    q = urllib.parse.quote(text[:900])
    try:
        raw = _http_text("https://translate.googleapis.com/translate_a/single"
                         f"?client=gtx&sl=auto&tl={target}&dt=t&q={q}", timeout=6)
        data = json.loads(raw)
        segs = data[0] if data and isinstance(data[0], list) else []
        out = "".join(s[0] for s in segs if isinstance(s, list) and s and s[0])
        if out.strip():
            return out.strip()
    except Exception:
        pass
    src = "zh-CN" if CJK_RE.search(text) else "en"
    raw = _http_text("https://api.mymemory.translated.net/get"
                     f"?q={q}&langpair={src}%7C{target}", timeout=10)
    data = json.loads(raw)
    out = ((data.get("responseData") or {}).get("translatedText") or "").strip()
    if out and "MYMEMORY WARNING" not in out.upper() and "QUERY LENGTH" not in out.upper():
        return out
    raise RuntimeError("翻译服务无响应")


def _trans_cache():
    global _TRANS_MEM
    if _TRANS_MEM is None:
        c = load_cache()
        _TRANS_MEM = c.get("trans") or {}
    return _TRANS_MEM


def translate_cached(text, target):
    """带磁盘缓存的翻译。返回译文或 None。"""
    if not text:
        return None
    key = f"{target}:{hashlib.md5(text.encode('utf-8')).hexdigest()[:20]}"
    mem = _trans_cache()
    if key in mem:
        return mem[key]
    try:
        out = _translate_remote(text, target)
    except Exception:
        return None
    with _TRANS_LOCK:
        mem[key] = out
    return out


def trans_flush():
    """把翻译表写回缓存文件。"""
    mem = _trans_cache()
    if not mem:
        return
    try:
        c = load_cache()
        c["trans"] = mem
        if len(mem) > 4000:                      # 防止无限膨胀
            c["trans"] = dict(list(mem.items())[-3000:])
        save_cache(c)
    except Exception:
        pass


def translate_batch(repos, target, ui_cb):
    """后台批量翻译仓库简介 (4 线程并发), 边翻边回传进度。"""
    items = [r for r in repos.values() if (r.get("desc") or "").strip()]
    total = len(items)
    if not total:
        ui_cb("trans_done", target)
        return
    done = [0]
    lock = threading.Lock()

    def worker(r):
        tr = translate_cached(r["desc"], target)
        if tr:
            r["desc_tr"] = tr
        with lock:
            done[0] += 1
            n = done[0]
        if n % 3 == 0 or n == total:
            ui_cb("trans_progress", (n, total))

    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(worker, items))
    except Exception:
        pass
    trans_flush()
    ui_cb("trans_done", target)


SNAPSHOT_KEY = "snapshot:last"


def snapshot_save(repos, dates):
    """把完整榜单快照存盘, 供下次启动秒开。"""
    try:
        c = load_cache()
        c[SNAPSHOT_KEY] = {"ts": time.time(), "dates": dates, "repos": repos}
        save_cache(c)
    except Exception:
        pass


def snapshot_load():
    """返回 (repos, dates, age_sec) 或 None。"""
    try:
        ent = load_cache().get(SNAPSHOT_KEY)
        if not ent:
            return None
        repos = ent.get("repos") or {}
        if not repos:
            return None
        return repos, ent.get("dates") or {}, max(0, time.time() - ent.get("ts", 0))
    except Exception:
        return None

def _today_str():
    return datetime.now().strftime("%Y-%m-%d")


def last_snapshot_ts():
    """上次成功抓取数据的时间戳 (0 表示没有)。"""
    try:
        return float((load_cache().get(SNAPSHOT_KEY) or {}).get("ts") or 0)
    except Exception:
        return 0.0


def daily_due():
    """今天还没成功刷新过 → 该刷了 (每天只自动刷一次)。"""
    ts = last_snapshot_ts()
    if not ts:
        return True
    try:
        return datetime.fromtimestamp(ts).strftime("%Y-%m-%d") != _today_str()
    except Exception:
        return True



def fetch_ossinsight(period="past_7_days", limit=10):
    """OSSInsight 周星榜; 失败抛 RuntimeError。返回 {full_name: [stars, forks, issues]}"""
    url = f"https://api.ossinsight.io/v1/repos/gained-stars/?period={period}&limit={limit}"
    data = _http_json(url, timeout=18)  # 该站直连较慢, 放宽; 超时后走官方 API
    rows = (data.get("data") or {}).get("rows") or []
    out = {}
    for r in rows[:limit]:
        name = r.get("full_name") or r.get("repo_name")
        if name:
            out[name] = [int(r.get("stars", 0) or 0), int(r.get("forks", 0) or 0), int(r.get("issues", 0) or 0)]
    if not out:
        raise RuntimeError("OSSInsight 返回为空")
    return out


def fetch_repo_detail(full_name, cache, persist=False):
    """单仓库详情, 优先缓存(1小时); 失败返回 None。"""
    key = f"repo:{full_name}"
    ent = cache.get(key)
    if ent and time.time() - ent.get("ts", 0) < CACHE_TTL:
        v = ent.get("v")
        return [v[0], v[1], v[2], v[3], v[4], v[5]] if v else None
    try:
        d = _http_json(f"https://api.github.com/repos/{full_name}")
        v = [
            int(d.get("stargazers_count") or 0), int(d.get("forks_count") or 0),
            int(d.get("open_issues_count") or 0), d.get("language") or "",
            d.get("description") or "", d.get("topics") or [],
        ]
        cache[key] = {"ts": time.time(), "v": v}
        if persist:
            save_cache(cache)
        return v
    except Exception:
        return None


def _rate_limit_status():
    """查询 GitHub Search 配额 (该请求本身不计配额)。返回 (remaining, reset_ts) 或 (None, 0)。"""
    try:
        d = _http_json("https://api.github.com/rate_limit")
        s = (d.get("resources") or {}).get("search") or {}
        return int(s.get("remaining") or 0), int(s.get("reset") or 0)
    except Exception:
        return None, 0


class _SearchThrottle:
    """Search API 节流。未认证 10次/分钟(取 9), 有 Token 30次/分钟(取 25)。"""
    _stamps = []
    _lock = None

    @classmethod
    def limit(cls):
        return 25 if _gh_token() else 9

    @classmethod
    def wait(cls):
        if cls._lock is None:
            import threading
            cls._lock = threading.Lock()
        with cls._lock:
            lim = cls.limit()
            now = time.time()
            cls._stamps = [t for t in cls._stamps if now - t < 60]
            if len(cls._stamps) >= lim:
                sleep_s = 60 - (now - cls._stamps[0]) + 1.2
                if sleep_s > 0:
                    time.sleep(sleep_s)
            cls._stamps.append(time.time())


def _search_one(label, q, since, per_page=50):
    """单分类搜索。返回 items 列表。"""
    _SearchThrottle.wait()
    params = {
        "q": f"{q} pushed:>={since}",
        "sort": "stars", "order": "desc",
        "per_page": str(per_page), "page": "1",
    }
    url = "https://api.github.com/search/repositories?" + urllib.parse.urlencode(params)
    return _http_json(url).get("items") or []


# 有些 topic 在 GitHub 上根本不存在(返回 0), 给它准备几个备选查询
QUERY_FALLBACKS = {
    "硬件/嵌入式": ["topic:embedded", "topic:arduino", "topic:esp32"],
    "安全/隐私": ["topic:cybersecurity", "topic:cryptography", "topic:privacy"],
    "学习/资源": ["topic:awesome", "topic:learning", "topic:roadmap"],
    "媒体/影音": ["topic:video-player", "topic:subtitle", "topic:media"],
    "编程语言/编译器": ["topic:interpreter", "language:zig"],
    "科学/算法": ["topic:algorithm", "topic:data-science"],
    "设计": ["topic:design-system", "topic:ui"],
    "效率/笔记": ["topic:notes", "topic:todo"],
    "网络工具": ["topic:proxy", "topic:vpn"],
    "阅读/文档": ["topic:docs", "topic:ebook"],
    "金融/理财": ["topic:trading", "topic:stock", "topic:cryptocurrency"],
    "音乐/音频": ["topic:audio", "topic:spotify", "topic:music-player"],
    "智能家居": ["topic:home-assistant", "topic:iot", "topic:smart-home"],
    "生活/健康": ["topic:fitness", "topic:wellness"],
    "人工智能/大模型": ["topic:llm-inference", "topic:machine-learning"],
    "移动开发": ["topic:ios", "topic:react-native"],
    "云原生/运维": ["topic:devops", "topic:docker"],
    "数据/数据库": ["topic:database", "topic:analytics"],
    "前端/Web": ["topic:react", "topic:web"],
}


def _search_generic(since, page=1, per_page=50):
    """通用高星搜索(不分 topic): 一次 50 个仓库, 用来秒出榜单。

    注意: per_page=100 会被中间链路截断(http.client.IncompleteRead, 白等 20~30s),
    50 是实测稳定的上限。
    """
    _SearchThrottle.wait()
    params = {
        "q": f"pushed:>={since} stars:>50",
        "sort": "stars", "order": "desc",
        "per_page": str(per_page), "page": str(page),
    }
    url = "https://api.github.com/search/repositories?" + urllib.parse.urlencode(params)
    return _http_json(url).get("items") or []


def _fetch_with_fallback(sub, q, since, ui_cb, qstat=None):
    """主查询 + 1 个备选查询, 有结果即止; 上次返回 0 的备选当天不再浪费配额。"""
    import threading
    chain = [q] + [x for x in QUERY_FALLBACKS.get(sub, []) if x != q][:1]
    now = time.time()
    for step, qq in enumerate(chain):
        if step > 0 and qstat is not None:
            st = qstat.get(qq) or {}
            if st.get("n", 1) == 0 and now - float(st.get("ts") or 0) < 86400:
                continue                    # 上次空手而归, 今天跳过
        got = []
        try:
            got = _search_one(sub, qq, since)
        except RuntimeError as e:
            if "403" in str(e):
                rem, reset = _rate_limit_status()
                if rem == 0 and reset:
                    wait = max(2, min(int(reset - time.time()) + 3, 75))
                    ui_cb("status", f"搜索配额耗尽, 等待 {wait}s 后重试 {sub} …")
                    time.sleep(wait)
                    try:
                        got = _search_one(sub, qq, since)
                    except RuntimeError:
                        got = []
        if qstat is not None:
            with _QSTAT_LOCK:
                qstat[qq] = {"n": len(got), "ts": time.time()}
        if got:
            return got, qq
        if step == 0 and len(chain) > 1:
            ui_cb("status", f"{sub}: 主查询无结果, 换备选查询 …")
    return [], None


def _subs(repo):
    """仓库归属的所有分类 (老快照没有 subs 字段时回退到 sub)。"""
    lst = repo.get("subs")
    if lst:
        return lst
    one = repo.get("sub")
    return [one] if one else []


def search_all(dates, cache, ui_cb):
    """官方 Search API 聚合所有分类(每分类1页)。返回 (repos, note)。

    并行抓取 + 边抓边推 UI: 缓存命中的分类立刻渲染, 剩下的用 2~4 路并发补齐,
    每完成一个分类就 ui_cb("partial", ...) 让界面先长出来。
    """
    import threading
    from collections import Counter
    repos = {}
    cat_cnt = Counter()
    queries = TECH_QUERIES + LIFE_QUERIES
    life_labels = {l for l, _ in LIFE_QUERIES}
    total = len(queries)
    store = cache.get(CAT_STORE_KEY) or {}
    qstat = cache.setdefault("qstat", {})
    lock = threading.Lock()
    done = [0]
    fresh_fns = set()      # 本轮真正抓到/刷新过的仓库 (区分"上次快照的旧数据")
    t0 = time.time()

    def merge(sub, cat, fresh):
        with lock:
            for r in fresh:                # 合并进总池, 命中多分类则追加归属
                fn = r["full_name"]
                fresh_fns.add(fn)
                old = repos.get(fn)
                if old is not None:
                    box = old.setdefault("subs", [old.get("sub")])
                    if sub not in box:
                        box.append(sub)
                    for k in ("desc", "stars", "forks", "issues", "lang", "url"):
                        if k in r and r[k] not in (None, ""):
                            old[k] = r[k]
                    continue
                r = dict(r)
                r["subs"] = [sub]
                r["category"] = cat
                r["sub"] = sub
                repos[fn] = r

    def snapshot():
        with lock:
            return {k: dict(v, subs=list(v.get("subs") or [])) for k, v in repos.items()}

    def push_partial():
        try:
            ui_cb("partial", {"repos": snapshot(), "dates": dates,
                              "done": done[0], "total": total,
                              "secs": int(time.time() - t0)})
        except Exception:
            pass

    # ── 快速通道: 3 页通用高星搜索(3 次请求 ≈ 300 个仓库) → 秒出榜单 ──
    # ── 先铺上次的快照: 点刷新的一瞬间就有内容, 后面全是"原地更新" ──
    try:
        prev = snapshot_load()
        if prev and prev[0]:
            n0 = 0
            for fn, r0 in prev[0].items():
                if isinstance(r0, dict) and r0.get("full_name"):
                    repos[fn] = dict(r0, subs=list(_subs(r0)))
                    n0 += 1
            if n0:
                push_partial()
                ui_cb("status", f"沿用上次 {n0} 个仓库 · 正在后台更新 …")
    except Exception:
        pass

    fast_n = 0

    def ingest(items):
        """把搜索结果转成仓库字典并合进总池, 返回本次新增数。"""
        add = 0
        for it in items:
            fn = it.get("full_name")
            if not fn:
                continue
            fresh_fns.add(fn)
            desc = it.get("description") or ""
            lang = it.get("language") or ""
            _c, _s = categorize(fn, desc, lang, it.get("topics") or [])
            rec = {
                "full_name": fn, "url": it.get("html_url") or f"https://github.com/{fn}",
                "desc": desc,
                "stars": int(it.get("stargazers_count") or 0),
                "forks": int(it.get("forks_count") or 0),
                "issues": int(it.get("open_issues_count") or 0),
                "lang": lang, "category": _c, "sub": _s, "subs": [_s],
            }
            old = repos.get(fn)
            if old is None:
                repos[fn] = rec
                add += 1
            else:                          # 已在上次快照里: 用新数据覆盖数字, 追加分类
                box = old.setdefault("subs", [old.get("sub")])
                if _s not in box:
                    box.append(_s)
                for k in ("desc", "stars", "forks", "issues", "lang", "url"):
                    if rec[k] not in (None, ""):
                        old[k] = rec[k]
        return add

    fast_ent = cache.get("fast") or {}
    fast_age = time.time() - float(fast_ent.get("ts") or 0)
    fast_items = []
    if fast_ent.get("items") and fast_age < CAT_FRESH_SEC:
        fast_items = [dict(x) for x in fast_ent["items"]]     # 3 小时内复用, 0 请求
    else:
        for page in (1,):
            try:
                items = _search_generic(dates["since"], page)
            except RuntimeError as e:
                if page == 1:
                    ui_cb("status", f"快速通道不可用({e}), 改用分类查询 …")
                break
            fast_items.extend(items)
            fast_n += ingest(items)
            push_partial()                 # 每拿到一页就先让界面长出来
            if page == 1:
                ui_cb("status", f"首屏 {fast_n} 个仓库已出 · 继续补齐其余分类 …")
        if fast_items:
            cache["fast"] = {"ts": time.time(), "items": [
                {"full_name": it.get("full_name"), "html_url": it.get("html_url"),
                 "description": it.get("description"), "stargazers_count": it.get("stargazers_count"),
                 "forks_count": it.get("forks_count"), "open_issues_count": it.get("open_issues_count"),
                 "language": it.get("language"), "topics": it.get("topics")}
                for it in fast_items[:300] if it.get("full_name")]}
    if fast_items and not fast_n:
        fast_n += ingest(fast_items)
    if fast_n:
        push_partial()
        ui_cb("status", f"快速结果已出: {fast_n} 个仓库 · 只补稀疏的分类 …")

    # ── 缓存命中 + 稀疏分类判断: 只有条数不够的分类才真的去打 API ──
    def have(n):
        """本轮真正刷到过、且属于分类 n 的仓库数 (不算上次快照的旧数据)。"""
        return sum(1 for fn in list(fresh_fns)
                   if n in _subs(repos.get(fn) or {}))

    todo = []
    cand = []
    for label, q in queries:
        cat = "生活" if label in life_labels else "科技"
        sub = label.split(" ", 1)[1]
        ent = store.get(sub) or {}
        age = time.time() - float(ent.get("ts") or 0)
        if ent.get("items") and age < CAT_FRESH_SEC:
            fresh = [dict(x) for x in ent["items"]]
            merge(sub, cat, fresh)
            cat_cnt[(cat, sub)] = len(fresh)
            done[0] += 1
        elif have(sub) >= 10:
            done[0] += 1
            cat_cnt[(cat, sub)] = have(sub)
        else:
            cand.append((float(ent.get("ts") or 0), cat, sub, q))
    # 未认证 Search API 只有 10 次/分钟: 一轮最多刷 MAX_TOPIC_PER_RUN 个最旧的分类,
    # 其余留到下一次 (界面此时已经有内容了, 不会空等)
    cand.sort()
    todo = [(c, s, q) for _ts, c, s, q in cand[:MAX_TOPIC_PER_RUN]]
    left = len(cand) - len(todo)
    if repos:
        more = f" · 另有 {left} 个下轮再刷" if left else ""
        ui_cb("status", f"已有 {done[0]}/{total} 个分类可用, 补抓 {len(todo)} 个{more} …")
        push_partial()

    nthread = 4 if _gh_token() else 2

    # 首屏 + 缓存已经齐了: 先告诉 UI "刷新完成"(停转圈), 剩下的分类安静地补
    try:
        ui_cb("phase", ("fast", done[0], total))
    except Exception:
        pass

    def work(item):
        cat, sub, q = item
        fresh = []
        try:
            items, _used = _fetch_with_fallback(sub, q, dates["since"], ui_cb, qstat)
        except Exception:
            items = []
        for it in items:
            fn = it.get("full_name")
            if not fn:
                continue
            fresh.append({
                "full_name": fn, "url": it.get("html_url") or f"https://github.com/{fn}",
                "desc": it.get("description") or "",
                "stars": int(it.get("stargazers_count") or 0),
                "forks": int(it.get("forks_count") or 0),
                "issues": int(it.get("open_issues_count") or 0),
                "lang": it.get("language") or "",
                "category": cat, "sub": sub, "subs": [sub],
            })
        with lock:
            if fresh:
                store[sub] = {"ts": time.time(), "items": fresh[:120]}
            else:
                cat_cnt[(cat, sub, "失败")] = 0
            cat_cnt[(cat, sub)] = len(fresh)
            done[0] += 1
            n = done[0]
        merge(sub, cat, fresh)
        ui_cb("status", f"官方 API ({n}/{total}): {sub} · {len(fresh)} 个 · "
                        f"{int(time.time() - t0)}s")
        push_partial()

    if todo:
        ui_cb("status", f"官方 API: {nthread} 路并发抓 {len(todo)} 个分类 "
                        f"(已用缓存 {done[0]}/{total}) …")
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=nthread) as ex:
            list(ex.map(work, todo))
    # ── 补量: 有些分类本身搜索结果就少, 用已抓到的仓库按内容二次归类补齐 ──
    try:
        sparse = [n for n in _CAT_META
                  if sum(1 for r in repos.values() if n in _subs(r)) < 10]
        if sparse:
            want = set(sparse)
            filled = {}
            for r in repos.values():
                _c2, _s2 = categorize(r["full_name"], r["desc"], r["lang"], [])
                if _s2 in want:
                    box = r.setdefault("subs", [r.get("sub")])
                    if _s2 not in box:
                        box.append(_s2)
                        filled[_s2] = filled.get(_s2, 0) + 1
    except Exception:
        pass
    # ── 合并上次快照: 只增不减, 刷新后分类不会突然变少 ──
    try:
        prev_all = snapshot_load()
        if prev_all:
            for fn, r0 in (prev_all[0] or {}).items():
                if not isinstance(r0, dict) or "full_name" not in r0:
                    continue
                cur = repos.get(fn)
                if cur is None:
                    repos[fn] = dict(r0)
                else:
                    box = cur.setdefault("subs", [cur.get("sub")])
                    for n in _subs(r0):
                        if n not in box:
                            box.append(n)
    except Exception:
        pass
    # ── 补量②: 还偏少的分类, 用上次快照里的项目补齐 (跨天累积, 长期不会空) ──
    try:
        prev = snapshot_load()
        if prev:
            prev_repos = prev[0] or {}
            for n in list(_CAT_META):
                have = sum(1 for r in repos.values() if n in _subs(r))
                if have >= 10:
                    continue
                cand = sorted([r for r in prev_repos.values() if n in _subs(r)],
                              key=lambda r: -r.get("stars", 0))
                for r in cand:
                    if have >= 12:
                        break
                    cur = repos.get(r["full_name"])
                    if cur is None:
                        repos[r["full_name"]] = dict(r)
                        have += 1
                    else:
                        box = cur.setdefault("subs", [cur.get("sub")])
                        if n not in box:
                            box.append(n)
                            have += 1
    except Exception:
        pass
    # ── 补量③: 仍偏少的分类, 用持久化的历史结果补齐 (7 天内, 跨次累积) ──
    try:
        now_ts = time.time()
        for sub in list(_CAT_META):
            have = sum(1 for r in repos.values() if sub in _subs(r))
            if have >= 10:
                continue
            ent = store.get(sub) or {}
            if not ent.get("items") or now_ts - float(ent.get("ts") or 0) > 7 * 86400:
                continue
            for r in sorted(ent["items"], key=lambda x: -x.get("stars", 0)):
                if have >= 40:
                    break
                cur = repos.get(r["full_name"])
                if cur is None:
                    repos[r["full_name"]] = dict(r)
                    have += 1
                else:
                    box = cur.setdefault("subs", [cur.get("sub")])
                    if sub not in box:
                        box.append(sub)
                        have += 1
    except Exception:
        pass
    cache[CAT_STORE_KEY] = store
    note = " · ".join(f"{k[0]}/{k[1]}:{v}" for k, v in sorted(cat_cnt.items(), key=lambda x: -x[1])[:6])
    return repos, note


def run_pipeline(ui_cb):
    """采集→分类→排序。ui_cb(kind, payload) 向 UI 线程投递。"""
    try:
        today = datetime.now(timezone.utc)
        dates = {"since": (today - timedelta(days=7)).strftime("%Y-%m-%d"),
                 "until": (today + timedelta(days=1)).strftime("%Y-%m-%d")}
        cache = load_cache()

        # ── 1) OSSInsight 快速通道 (失败过的 6 小时内直接跳过, 不再白等超时) ──
        dead_until = float(cache.get("oi_dead_until") or 0)
        if time.time() < dead_until:
            ui_cb("status", "OSSInsight 上次不可用, 跳过 · 直接走官方 API …")
        else:
          try:
            ui_cb("status", "优先通道: OSSInsight 周星榜 …")
            oi = fetch_ossinsight()
            ui_cb("status", f"OSSInsight Top{len(oi)} 获取成功, 补充仓库详情 …")
            details = {}
            for i, fn in enumerate(list(oi.keys()), 1):
                d = fetch_repo_detail(fn, cache, persist=(i % 3 == 0))
                if d:
                    details[fn] = d
            save_cache(cache)
            if len(details) >= 3:
                repos = {}
                for fn, d in details.items():
                    _c, _s = categorize(fn, d[4], d[3], d[5])
                    repos[fn] = {
                        "full_name": fn, "url": f"https://github.com/{fn}",
                        "desc": d[4], "stars": d[0], "forks": d[1], "issues": d[2],
                        "lang": d[3], "category": _c, "sub": _s,
                    }
                snapshot_save(repos, dates)
                ui_cb("done", (repos, dates, "数据源: OSSInsight 周星榜 + GitHub 补充详情"))
                return
            cache["oi_dead_until"] = time.time() + 6 * 3600
            ui_cb("status", f"OSSInsight 详情不足({len(details)}个), 转官方 Search API …")
          except RuntimeError as e:
            cache["oi_dead_until"] = time.time() + 6 * 3600
            save_cache(cache)
            ui_cb("status", f"OSSInsight 不可用({e}), 6 小时内不再尝试 · 用官方 Search API …")

        # ── 2) 官方 Search API 通道 ─────────────────────────
        ui_cb("status", "官方 API: 逐分类搜索近7日高星仓库 …")
        repos, note = search_all(dates, cache, ui_cb)
        save_cache(cache)
        if not repos:
            ui_cb("error", "所有数据源均失败, 请检查网络或稍后重试")
            return
        snapshot_save(repos, dates)
        ui_cb("done", (repos, dates, "数据源: GitHub Search API · 按总星数排序 · " + note))
    except Exception as e:
        ui_cb("error", f"{type(e).__name__}: {e}")


# ---------------------------------------------------------------- UI
STAR_ICON = "★"

# 主题状态: 由 settings.json 驱动, 运行时可改
SETTINGS = load_settings()
BG_DARK = SETTINGS["bg"]
PALETTE = build_palette(SETTINGS["bg"], SETTINGS["tech"], SETTINGS["life"])


def apply_theme(settings):
    """把设置里的配色写入模块级 BG_DARK / PALETTE。"""
    global BG_DARK, PALETTE
    bg = settings.get("bg") or DEFAULT_SETTINGS["bg"]
    img = (settings.get("bgimg") or "").strip()
    if img:
        # 有背景图: 底色向照片的平均色靠拢, 让整窗色调和照片一致
        avg = _bg_avg(_bg_open(img))
        if avg:
            bg = _mix(avg, bg, 0.84)
    BG_DARK = bg
    PALETTE = build_palette(bg, settings["tech"], settings["life"])


def _autowrap(label, min_width=240):
    """让 Label 的 wraplength 跟随实际分配宽度, 避免文字被右侧列截断。"""
    def _fit(event):
        try:
            target = max(min_width, event.width - 6)
            if abs(int(label.cget("wraplength")) - target) > 8:
                label.configure(wraplength=target)
        except tk.TclError:
            pass
    label.bind("<Configure>", _fit, add="+")

LANG_ICON = {
    "Python": "🐍", "JavaScript": "🟨", "TypeScript": "🔷", "Java": "☕", "Go": "🐹",
    "Rust": "🦀", "C": "🅒", "C++": "🅒", "C#": "🎯", "PHP": "🐘", "Swift": "🍎",
    "Kotlin": "🟪", "Dart": "🎯", "Ruby": "💎", "Shell": "🐚", "HTML": "🌐",
    "CSS": "🎨", "Vue": "🟩", "Zig": "⚡", "Lua": "🌙", "Jupyter Notebook": "📓",
}


class _FlatItem:
    """画在画布条上的文字项: 没有方块底, 只有文字 (可选点击/悬停)。"""

    def __init__(self, app, bar, text="", size=11, bold=False, fill=None, cmd=None,
                 align="left", y=8, halo=True, hover=None):
        self.app = app
        self.bar = bar
        self.text = text
        self.size = size
        self.bold = bold
        self.fg = fill
        self.cmd = cmd
        self.align = align
        self.y = y
        self.halo = halo
        self.hover = hover
        self.ids = []
        self.box = (0, 0, 0, 0)

    # —— 兼容 tk.Label / tk.Button 的少量接口 ——
    def configure(self, **kw):
        if "text" in kw:
            self.text = kw["text"]
        if "fg" in kw:
            self.fg = kw["fg"]
        self.app._paint_bar(self.bar)

    config = configure

    def cget(self, key):
        if key == "text":
            return self.text
        if key == "fg":
            return self.fg
        return ""

    def winfo_rootx(self):
        return self.bar.cv.winfo_rootx() + self.box[0]

    def winfo_rooty(self):
        return self.bar.cv.winfo_rooty() + self.box[1]

    def winfo_width(self):
        return max(0, self.box[2] - self.box[0])

    def pack(self, *a, **k):
        return None

    place = pack

    def destroy(self):
        for i in self.ids:
            try:
                self.bar.cv.delete(i)
            except tk.TclError:
                pass
        self.ids = []


class _FlatBar:
    """一条横向文字栏 (本身就是画布), 背景在壁纸模式下贴对应位置的壁纸。"""

    def __init__(self, app, parent, height=40, pad=22, gap=18, width=0):
        self.app = app
        kw = {"bg": BG_DARK, "highlightthickness": 0, "bd": 0, "height": height}
        if width:
            kw["width"] = width
        self.cv = tk.Canvas(parent, **kw)
        self.cv.app = app
        self.height = height
        self.pad = pad
        self.gap = gap
        self.items = []
        self.img_id = None
        self._img = None
        self.cv.bind("<Button-1>", self._click)
        self.cv.bind("<Motion>", self._motion)
        self._hot = None

    def _pick(self, e):
        """命中判定: 与画出来的那圈边框 (box ±11, 条高上下各留 4px) 完全对齐,
        点在文字上方/下方都算按到。"""
        try:
            ch = self.cv.winfo_height()
        except tk.TclError:
            ch = self.height
        for it in self.items:
            b = getattr(it, "box", None)
            if not b or not it.cmd:
                continue
            if b[0] - 11 <= e.x <= b[2] + 11 and -4 <= e.y <= ch + 4:
                return it
        return None

    def _click(self, e):
        it = self._pick(e)
        _dbg("bar click x=%d y=%d hit=%r n=%d" % (e.x, e.y, it and it.text, len(self.items)))
        if it is None:
            return
        try:
            self.app._flash_item(self.cv, it.ids[-1], it.fg or PALETTE["ui"]["text"],
                                 it.hover or PALETTE["ui"]["text"])
        except Exception:
            pass
        it.cmd()
        return "break"

    def _motion(self, e):
        it = self._pick(e)
        if it is self._hot:
            return
        old, self._hot = self._hot, it
        for o, on in ((old, False), (it, True)):
            if o is None or not o.ids:
                continue
            try:
                self.cv.itemconfigure(o.ids[-1],
                                      fill=(o.hover or PALETTE["ui"]["text"]) if on
                                      else (o.fg or PALETTE["ui"]["text"]))
                rg = getattr(o, "ring", None)
                if rg:
                    self.cv.itemconfigure(
                        rg, outline=(PALETTE["科技"]["accent"] if on
                                     else getattr(o, "ring_edge", PALETTE["ui"]["muted"])))
            except tk.TclError:
                pass
        try:
            self.cv.configure(cursor="hand2" if it else "")
        except tk.TclError:
            pass

    def add(self, **kw):
        it = _FlatItem(self.app, self, **kw)
        self.items.append(it)
        return it

    def pack(self, **kw):
        self.cv.pack(**kw)

    def clear(self):
        self.cv.delete("all")
        self.items = []
        self.img_id = None
        self._img = None


_GEO_RE = re.compile(r"^(\d{3,5})x(\d{3,5})([+-]\d{1,5})([+-]\d{1,5})$")


def _load_win_geo(sw, sh):
    """上次退出时的窗口大小/位置 (clamp 到当前屏幕), 没有就返回 None。"""
    raw = str(SETTINGS.get("win") or "").strip()
    m = _GEO_RE.match(raw)
    if not m:
        return None
    w, h, x, y = (int(m.group(i)) for i in (1, 2, 3, 4))
    w = max(880, min(w, sw - 40))
    h = max(600, min(h, sh - 60))
    x = max(-20, min(x, sw - w - 20))
    y = max(0, min(y, sh - h - 40))
    return f"{w}x{h}+{x}+{y}"


class TrendingApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_TITLE)
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        self.minsize(880, 600)
        geo = _load_win_geo(sw, sh)
        if geo:
            self.geometry(geo)
            m = _GEO_RE.match(geo)
            self._final_geo = tuple(int(v) for v in m.groups())
        else:
            w = max(1000, min(1460, int(sw * 0.58)))
            h = max(680, min(940, int(sh * 0.60)))
            x = max(0, (sw - w) // 2)
            y = max(0, (sh - h) // 2 - 26)
            self.geometry(f"{w}x{h}+{x}+{y}")
            self._final_geo = (w, h, x, y)
        self._details_on = bool(SETTINGS.get("details", True))
        self._lang = SETTINGS.get("lang", "off")
        self._rows = []
        self._open_count = 0
        self._last_repos = None
        self._cat = "全部"          # 分类筛选: "全部" 或某个小类名
        self._filter_inner = None
        self._filter_canvas = None
        self._filter_pos = 0.0
        self._translating = False
        self._q = queue.Queue()
        self._bg_photo = None
        self._bg_view = None
        self._bg_tile_photo = None
        self._bg_item = None
        self._bg_lbl = None
        self._inner_bg = None
        self._resize_job = None
        self._fonts = {}
        self._mcache = {}
        self._row_bands = []
        self._hover_repo = None
        self._wheel_acc = 0.0
        self._tick = _Ticker(self)
        self._sv_vel = 0.0
        self._sv_frac = 0.0
        self._sv_on = False
        self._build_ui()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.bind("<Configure>", self._on_configure)
        self.after(100, self._drain)
        # boot() 会渲染整份缓存 (较慢), 放到开场动画之后, 否则动画会被卡住
        self._booted = False
        self.after(900, self._boot_once)
        # 开场动画: 一开始透明, 等窗口真的显示出来再"从小放大"(不然会先闪一下全尺寸)
        try:
            self.attributes("-alpha", 0.0)
        except tk.TclError:
            pass
        self._anim_started = False
        self._anim_tries = 0
        self.bind("<Map>", self._on_first_map, add="+")
        self.after(120, self._open_anim)
        # 调试钩子: 设了 GHTOP_AUTO_SETTINGS 就自动打开设置面板(平时无副作用)
        if os.environ.get("GHTOP_AUTO_TOGGLE"):
            self.after(6000, self.toggle_details)   # 调试钩子: 自动点一次收起/展开
        if os.environ.get("GHTOP_AUTO_SETTINGS"):
            self.after(3000, self.open_settings)
        if os.environ.get("GHTOP_HITTEST"):
            self.after(4200, self._hittest_dump)   # 调试钩子: 打印每个按钮的可点区域

    def _hittest_dump(self):
        """把每条栏的按钮矩形和"哪些 y 会命中"打出来 (排查点击错位)。"""
        for bi, bar in enumerate(getattr(self, "_bars", [])):
            cv = bar.cv
            try:
                cw, ch = cv.winfo_width(), cv.winfo_height()
            except tk.TclError:
                continue
            _dbg("HIT bar%d canvas=%dx%d root=(%d,%d) items=%d" % (
                bi, cw, ch, cv.winfo_rootx() - self.winfo_rootx(),
                cv.winfo_rooty() - self.winfo_rooty(), len(bar.items)))
            for it in bar.items:
                _dbg("HIT    %-16r y=%s box=%s cmd=%s" % (
                    (it.text or "")[:14], it.y, getattr(it, "box", None), bool(it.cmd)))
            for it in bar.items:
                if not it.cmd or not getattr(it, "box", None):
                    continue
                cx = (it.box[0] + it.box[2]) // 2
                hot = [y for y in range(0, ch + 40, 2)
                       if (bar._pick(type("E", (), {"x": cx, "y": y})()) is it)]
                _dbg("HIT    -> %r x=%d hotY=%s" % ((it.text or "")[:12], cx,
                                                    (hot[:1] + hot[-1:]) if hot else "none"))

    def _boot_once(self):
        if getattr(self, "_booted", False):
            return
        if getattr(self, "_win_anim", False):     # 开场动画还没走完, 别在这儿渲染整份榜单
            self.after(120, self._boot_once)
            return
        self._booted = True
        if getattr(self, "_bg_view", None) is None:
            try:                  # 开场动画被跳过时 (窗口太小/异常) 兜底建壁纸
                self._layers_build()
            except Exception:
                pass
        self.boot()
        self.after(2600, self._warm_settings)

    def _warm_settings(self):
        """预热: alpha=0 把设置面板画一遍并抓住位图, 这样第一次点开也是"瞬间出图"。

        只画不显示 —— warm=True 时 __init__ 不排任何动画/校准任务, 抓完就销毁。
        """
        self._warm_done = True
        if _SNAP_MEM or getattr(self, "_dlg", None) is not None:
            return
        try:
            dlg = SettingsDialog(self, origin=(0, 0, 1, 1), warm=True)
            dlg.destroy()
        except Exception:
            pass

    def _on_first_map(self, _e=None):
        if getattr(self, "_anim_started", False):
            return
        self.after(20, self._open_anim)

    def _open_anim(self):
        """启动: 从 0.86 倍大小 + 透明, 0.52s 长到正常大小, 边长大边淡入。

        步长刻意压小 (1460*0.14/39 ≈ 5px/帧), 透明度只走 6 档 —— 整数像素的
        窗口尺寸动画在 tk 里一帧跳 40px 会看出台阶, 跳 5px 才像丝滑。
        """
        if getattr(self, "_anim_started", False):
            return
        self._anim_started = True
        try:
            self.update_idletasks()
        except tk.TclError:
            return
        w, h, x, y = self._final_geo          # 别读 self.geometry(): 窗口还没 map 时是 1x1
        _dbg("open_anim geo=%s,%s,%s,%s" % (w, h, x, y))
        if w < 400 or h < 300:
            return
        try:
            self.attributes("-alpha", 0.0)
            self.lift()
            self.attributes("-topmost", True)   # 长出来的过程中别被别的窗口压住
        except tk.TclError as exc:
            _dbg("open_anim alpha err %s" % exc)
            return
        _dbg("open_anim alpha ok")
        self._win_anim = True
        self._splash_off = False
        self._alpha_step = -1
        try:                    # 13MB 壁纸解码+压暗+裁剪 ~0.4s, 趁闪屏还盖着做完,
            if getattr(self, "_bg_view", None) is None:   # 否则会卡在动画中间
                self._layers_build()
                self.update_idletasks()
        except Exception:
            pass
        k0, span = 0.86, 0.14
        alpha_ladder = (0.0, 0.34, 0.60, 0.80, 0.93, 1.0)

        def step(t):
            k = k0 + span * t
            ww, hh = int(round(w * k)), int(round(h * k))
            try:
                self.geometry("%dx%d+%d+%d" % (ww, hh, x + (w - ww) // 2,
                                              y + (h - hh) // 2))
                idx = min(len(alpha_ladder) - 1, int(t * len(alpha_ladder)))
                if idx != self._alpha_step:      # 透明度频繁改会拖累分层窗口合成
                    self._alpha_step = idx
                    self.attributes("-alpha", alpha_ladder[idx])
            except tk.TclError:
                pass
            if t >= 0.24 and not self._splash_off:
                self._splash_off = True        # 窗口已有 95% 大小时再收闪屏, 换场不突兀
                try:
                    import pyi_splash          # type: ignore
                    pyi_splash.close()
                except Exception:
                    pass

        def done():
            self._win_anim = False
            try:
                self.geometry("%dx%d+%d+%d" % (w, h, x, y))
                self.attributes("-alpha", 1.0)
                self.attributes("-topmost", False)
                self.focus_force()
            except tk.TclError:
                pass
            if getattr(self, "_layers_pending", False):
                self._layers_build()       # 动画期间推迟的壁纸, 现在一次性算好
            try:                       # 打包版: 窗口彻底显形了再收掉启动闪屏
                import pyi_splash      # type: ignore
                pyi_splash.close()
            except Exception:
                pass
            if getattr(self, "_bars_dirty", False):
                self._bars_dirty = False
                self._paint_all_bars()
            self._after_resize()       # 动画期间的 Configure 被跳过了, 这里补一次
            self.after(20, self._boot_once)

        self._tick.add("winopen", 0.52, step, ease=_ease_in_out)
        self.after(560, done)

    def _on_close(self):
        """退出前记住窗口大小和位置, 下次原样打开。"""
        try:
            self.update_idletasks()
            geo = self.geometry()
            m = _GEO_RE.match(geo)
            if m:
                SETTINGS["win"] = geo
                save_settings(SETTINGS)
        except Exception:
            pass
        try:
            self.destroy()
        except Exception:
            pass

    # ── 界面骨架 (换主题时整体重建) ─────────────────────────
    def _build_ui(self):
        _dbg("build_ui screen=%sx%s win=%s" % (self.winfo_screenwidth(), self.winfo_screenheight(),
                                              self.geometry()))
        ui = PALETTE["ui"]
        for w in list(self.winfo_children()):
            w.destroy()
        self.configure(bg=BG_DARK)
        set_window_icon(self)
        self.bind_all("<Button-1>", lambda e: _dbg(
            "click widget=%s(%s) xy=%s,%s" % (e.widget, e.widget.winfo_class(),
                                              e.x, e.y)), add="+")

        # ── 背景图层 (最先生成, 位于所有控件之下) ──
        self._bg_lbl = tk.Label(self, bd=0, highlightthickness=0, bg=BG_DARK, anchor="nw")
        self._bg_lbl.place(x=0, y=0, relwidth=1, relheight=1)

        # ── 顶部标题栏 (纯文字, 无方块底) ──
        self._bars = []
        bar_top = _FlatBar(self, self, height=60, pad=22, gap=18)
        bar_top.is_top = True
        bar_top.reserve = 30
        self._bar_top = bar_top
        bar_top.pack(fill="x", pady=(12, 0))
        bar_top.add(text="GitHub 每周 Star 榜", size=22, bold=True, fill=ui["title"], y=30)
        bar_top.add(text="TOP 10", size=15, bold=True, fill=PALETTE["科技"]["accent"], y=30)
        bar_top.add(text="⚙  设 置", size=11, fill=ui["sub"], cmd=self.open_settings,
                    align="right", y=30, hover=ui["text"])
        bar_top.add(text="⟳  刷 新", size=11, bold=True, fill=PALETTE["科技"]["accent"],
                    cmd=self.refresh, align="right", y=30,
                    hover=_mix(PALETTE["科技"]["accent"], "#ffffff", 0.35))
        self._bars.append(bar_top)

        bar_info = _FlatBar(self, self, height=58, pad=24, gap=12)
        bar_info.pack(fill="x", pady=(0, 0))
        self.note_lbl = bar_info.add(text="", size=10, fill=ui["muted"], y=15)
        self.status = bar_info.add(text="点击「刷新」拉取本周 Top 10 …", size=11,
                                   fill=ui["ok"], y=41)
        self._bars.append(bar_info)

        # ── 分类筛选条 ──
        self._build_filter_bar()

        # ── 底部操作条 (先占住底部, 画布再吃剩下的空间) ──
        bar_bottom = _FlatBar(self, self, height=46, pad=22, gap=16)
        bar_bottom.pack(side="bottom", fill="x", pady=(4, 10))
        self.lbl_total = bar_bottom.add(text="", size=10, bold=True, fill=ui["sub"], y=23)
        bar_bottom.add(text="🌐  一键翻译", size=11, fill=ui["text"], cmd=self.translate_now,
                       align="right", y=23, hover=PALETTE["科技"]["accent"])
        self.lang_btn = bar_bottom.add(text="🌐 %s ▾" % self._lang_name(), size=11,
                                       fill=ui["text"], cmd=self._lang_menu,
                                       align="right", y=23, hover=PALETTE["科技"]["accent"])
        self.detail_btn = bar_bottom.add(
            text="▾  收起详情" if self._details_on else "▸  展开详情", size=11,
            fill=ui["text"], cmd=self.toggle_details, align="right", y=23,
            hover=PALETTE["科技"]["accent"])
        bar_bottom.add(text="⚡  全部打开", size=11, bold=True,
                       fill=PALETTE["科技"]["accent"], cmd=self.open_all,
                       align="right", y=23,
                       hover=_mix(PALETTE["科技"]["accent"], "#ffffff", 0.35))
        self.bottom = bar_bottom.cv
        self._bars.append(bar_bottom)

        # ── 内容画布 ──
        holder = tk.Frame(self, bg=BG_DARK)
        holder.pack(fill="both", expand=True, padx=(22, 6))
        self.canvas = tk.Canvas(holder, bg=BG_DARK, highlightthickness=0, bd=0,
                                yscrollincrement=1, yscrollcommand=self._on_yscroll)
        self.vsb = tk.Scrollbar(holder, orient="vertical", command=self.canvas.yview,
                                width=13, bg=_mix(BG_DARK, ui["text"], 0.18),
                                troughcolor=BG_DARK, relief="flat", bd=0,
                                highlightthickness=0,
                                activebackground=_mix(BG_DARK, ui["text"], 0.32))
        self.vsb.pack(side="right", fill="y")
        if self._wall_mode():
            self.vsb.pack_forget()          # 壁纸模式下不要灰条挡住照片
        self.canvas.pack(side="left", fill="both", expand=True)
        self.inner = tk.Frame(self.canvas, bg=BG_DARK)
        self._inner_win = self.canvas.create_window((0, 0), window=self.inner,
                                                    anchor="nw", tags="content")

        def _conf(_e=None):
            try:
                self.canvas.configure(scrollregion=self.canvas.bbox("all"))
                self.canvas.itemconfigure(self._inner_win, width=self.canvas.winfo_width())
            except tk.TclError:
                pass
        self.inner.bind("<Configure>", _conf)

        def _canc(_e=None):
            if not self._wall_mode():
                _conf()
            self._bg_follow()
        self.canvas.bind("<Configure>", _canc)
        # 壁纸模式: 行是直接画在画布上的, 没有控件, 必须自己绑鼠标事件
        self.canvas.bind("<Motion>", self._wall_motion, add="+")
        self.canvas.bind("<Button-1>", self._wall_click, add="+")
        self.canvas.bind("<Button-3>", self._wall_menu, add="+")

        for seq in ("<MouseWheel>", "<Shift-MouseWheel>", "<Button-4>", "<Button-5>"):
            self.canvas.bind(seq, self._wheel)
            self.inner.bind(seq, self._wheel)
            self.bind(seq, self._wheel)

        self.after(10, self._install_hover_fx)
        self.after(120, self._paint_all_bars)

        # ── 分类筛选条 (自绘 Canvas: 选中块会滑到另一个框框里) ────
    def _build_filter_bar(self):
        """一行可横向滚动的分类 chip, 选中高亮块位置带动画滑动。"""
        ui = PALETTE["ui"]
        wrap = tk.Frame(self, bg=BG_DARK)
        wrap.pack(fill="x", padx=(24, 22), pady=(0, 8))
        self._filter_wrap = wrap
        self._chips = {}
        self._chip_order = []
        self._puck = None
        self._hover_chip = None
        self._sel_acc = PALETTE["科技"]["accent"]
        self._sel_fg = "#f2f5ff"

        def _fwheel(e):
            cv = getattr(self, "_chip_cv", None)
            if cv is None:
                return
            step = -60 if (getattr(e, "num", None) == 4 or (getattr(e, "delta", 0) or 0) > 0) else 60
            self._filter_canvas.xview_scroll(step, "units")
            self._filter_pos = self._filter_canvas.xview()[0]
            self._chips_bg_sync()
            return "break"
        self._fwheel = _fwheel

        def _nav(side, txt, d):
            fbar = _FlatBar(self, wrap, height=36, pad=0, gap=6, width=22)
            fbar.pack(side=side, padx=(0, 6) if side == "left" else (6, 0))
            self._bars.append(fbar)

            def step(_e=None, dd=d):
                iw = max(1, self._chip_cv.winfo_reqwidth())
                try:
                    frac = self._filter_canvas.xview()[0]
                except Exception:
                    return "break"
                df = (150.0 / iw) * dd

                def apply(t):
                    try:
                        self._filter_canvas.xview_moveto(max(0.0, min(1.0, frac + df * t)))
                    except tk.TclError:
                        pass

                self._tick.add("fnav", 0.22, apply, ease=_ease_in_out)
                return "break"

            fbar.add(text=txt, size=12, bold=True, fill=ui["sub"], y=18,
                     cmd=lambda: step(None, d), hover=ui["text"])
            for seq in ("<MouseWheel>", "<Shift-MouseWheel>", "<Button-4>", "<Button-5>"):
                fbar.cv.bind(seq, _fwheel)
            return fbar
        _nav("left", "◀", -1)
        _nav("right", "▶", 1)

        self._filter_canvas = tk.Canvas(wrap, bg=BG_DARK, height=36, highlightthickness=0, bd=0,
                                        xscrollincrement=1)
        self._filter_canvas.pack(side="left", fill="x", expand=True)
        self._chip_cv = tk.Canvas(self._filter_canvas, bg=BG_DARK, height=36,
                                  highlightthickness=0, bd=0, cursor="hand2")
        self._filter_inner = self._chip_cv          # 兼容旧引用
        self._filter_win = self._filter_canvas.create_window(
            (0, 0), window=self._chip_cv, anchor="nw")

        def _sync(*_):
            bb = self._filter_canvas.bbox("all")
            if bb:
                self._filter_canvas.configure(scrollregion=bb)
            self._chips_bg_sync()
        self._chip_cv.bind("<Configure>", _sync)
        self._chip_cv.bind("<Button-1>", self._chip_click)
        self._chip_cv.bind("<Motion>", self._chip_motion)
        self._chip_cv.bind("<Leave>", lambda e: self._chip_hover(None))

        for seq in ("<MouseWheel>", "<Shift-MouseWheel>", "<Button-4>", "<Button-5>"):
            self._filter_canvas.bind(seq, _fwheel)
            self._chip_cv.bind(seq, _fwheel)
            wrap.bind(seq, _fwheel)

    def _rrect(self, cv, x0, y0, x1, y1, r, **kw):
        """圆角矩形 (Canvas 没有原生圆角, 用 smooth 多边形近似)。"""
        pts = [x0 + r, y0, x1 - r, y0, x1, y0, x1, y0 + r,
               x1, y1 - r, x1, y1, x1 - r, y1, x0 + r, y1,
               x0, y1, x0, y1 - r, x0, y0 + r, x0, y0]
        return cv.create_polygon(pts, smooth=True, **kw)

    def _draw_flat(self, cv, it, x, y, anchor="w"):
        """画一个文字项 (有阴影更稳), 返回画布 bbox。"""
        for i in it.ids:
            try:
                cv.delete(i)
            except tk.TclError:
                pass
        it.ids = []
        font = self._font(it.size, "bold" if it.bold else "normal")
        fill = it.fg or PALETTE["ui"]["text"]
        halo_col = _shadow_of(fill)
        ids = []
        if it.halo:
            ids.append(cv.create_text(x + 1, y + 1, text=it.text, font=font,
                                      fill=halo_col, anchor=anchor))
        main = cv.create_text(x, y, text=it.text, font=font, fill=fill, anchor=anchor)
        ids.append(main)
        it.ids = ids
        box = cv.bbox(main) or (x, y, x, y)
        it.box = box
        if it.cmd:
            # 给可点击项画一圈可见的细边框: 让人一眼看出"整块都能点",
            # 判定区和这圈线完全一致 (见 _FlatBar._pick)。
            try:
                ch = cv.winfo_height()
            except tk.TclError:
                ch = 0
            edge = _mix(PALETTE["ui"]["text"], BG_DARK, 0.50)
            if ch >= 24 and box[2] > box[0]:
                ring = self._rrect(cv, box[0] - 11, 4, box[2] + 11, ch - 4, 8,
                                   fill="", outline=edge, width=1)
                it.ring = ring
                it.ring_edge = edge
                cv.tag_lower(ring, main)
            # 点击/悬停统一交给 _FlatBar._click / _motion (按边框命中),
            # 不要再给文字项单独 tag_bind: 两条路径会同时触发 → 一次点击执行两遍。
            try:
                cv.configure(cursor="hand2")
            except tk.TclError:
                pass
        return box

    def _paint_bar(self, bar):
        """重画一条文字栏: 壁纸模式下贴该位置的壁纸, 再左右对齐画文字。"""
        cv = bar.cv
        try:
            nw = cv.winfo_width()          # 真实宽度: ◀ ▶ 箭头条只有 22px
            w = max(160, nw)
        except tk.TclError:
            return
        cv.delete("all")
        bar.img_id = None
        bar._img = None
        if self._wall_mode() and getattr(self, "_bg_pil", None) is not None:
            try:
                y0 = cv.winfo_rooty() - self.winfo_rooty()
                xoff = max(0, cv.winfo_rootx() - self.winfo_rootx())
                glass = max(0.0, min(1.0, float(SETTINGS.get("glass", 0.55) or 0)))
                bl = getattr(self, "_glass_pil", None)
                use_glass = glass > 0.02 and bl is not None
                pil = bl if use_glass else self._bg_pil
                bw, bh = pil.size
                y1 = min(bh, max(0, y0) + bar.height)
                if nw < 90:
                    # 22px 宽的 ◀ ▶ 箭头条: 贴一整张图容易和分类条错位 (看着像深色块),
                    # 直接取同一位置的玻璃平均色做平涂, 视觉上是同一条带子
                    avg = _bg_avg(pil, (xoff, max(0, y0), min(bw, xoff + max(24, nw)), y1))
                    if avg:
                        if use_glass:
                            avg = _mix(avg, BG_DARK, min(0.16, 0.10 * glass))
                        try:
                            cv.configure(bg=avg)
                        except tk.TclError:
                            pass
                    bar._img = None
                else:
                    crop = pil.crop((xoff, max(0, y0), min(bw, xoff + w), y1))
                    if use_glass and crop.size[0] > 4 and crop.size[1] > 4:
                        try:      # 三条文字栏整体做成一条磨砂玻璃带 (照片还看得见)
                            from PIL import Image as _Im
                            # 只给一点点底色: 压太狠就变成"深色横带", 壁纸被挡住
                            crop = _Im.blend(crop, _Im.new("RGB", crop.size,
                                                           _hex2rgb(BG_DARK)),
                                             min(0.16, 0.10 * glass))
                        except Exception:
                            pass
                    # 壁纸比这条栏窄/矮时 (窗口刚改尺寸、还没重建背景) 直接拉伸铺满,
                    # 否则右侧/底部会露出纯色条, 看着像"这里没有背景图"
                    if crop.size != (int(w), int(bar.height)):
                        try:
                            from PIL import Image as _Im
                            crop = crop.resize((max(2, int(w)), max(2, int(bar.height))),
                                               _Im.LANCZOS)
                        except Exception:
                            pass
                    from PIL import ImageTk
                    bar._img = ImageTk.PhotoImage(crop)
                    bar.img_id = cv.create_image(0, 0, anchor="nw", image=bar._img)
            except Exception:
                bar._img = None
        _dbg("paint_bar w=%s h=%s items=%d wall=%s pil=%r crop=%r" % (
            w, bar.height, len(bar.items), self._wall_mode(),
            getattr(getattr(self, "_bg_pil", None), "size", None),
            getattr(getattr(bar, "_img", None), "width", None)))
        lefts = [i for i in bar.items if i.align == "left"]
        rights = [i for i in bar.items if i.align == "right"]
        # 同一个 y 算一行: 换行时 x 回到左边距
        x = bar.pad
        row_y = None
        for it in lefts:
            if row_y is None or it.y != row_y:
                x = bar.pad
                row_y = it.y
            it.box = self._draw_flat(cv, it, x, it.y, anchor="w")
            x = it.box[2] + bar.gap
        x = w - bar.pad - getattr(bar, "reserve", 0)
        row_y = None
        for it in reversed(rights):
            if row_y is None or it.y != row_y:
                x = w - bar.pad - getattr(bar, "reserve", 0)
                row_y = it.y
            box = self._draw_flat(cv, it, x, it.y, anchor="e")
            it.box = box
            x = box[0] - bar.gap
        if getattr(bar, "is_top", False) and getattr(self, "_fetching", False):
            try:
                cx = w - bar.pad - 11
                r = 9
                self._spin_id = cv.create_arc(cx - r, 30 - r, cx + r, 30 + r,
                                              start=getattr(self, "_spin_angle", 0),
                                              extent=270, style="arc", width=3,
                                              outline=PALETTE["科技"]["accent"])
            except tk.TclError:
                pass

    def _flash_item(self, cv, item, base, hot):
        """点一下: 先闪亮再回落 (0.26s), 让每次点击都有回应。"""
        def step(t):
            try:
                cv.itemconfigure(item, fill=_mix(hot, base, t))
            except tk.TclError:
                pass

        self._tick.add("flash%d" % item, 0.26, step, ease=_ease_out_quint)

    def _spin_start(self):
        if getattr(self, "_spin_on", False):
            return
        self._spin_on = True
        self._spin_angle = 0
        bar = getattr(self, "_bar_top", None)
        if bar is not None:
            self._paint_bar(bar)
        self._spin_tick()

    def _spin_tick(self):
        if not getattr(self, "_fetching", False):
            self._spin_on = False
            bar = getattr(self, "_bar_top", None)
            if bar is not None:
                self._paint_bar(bar)
            return
        self._spin_angle = (getattr(self, "_spin_angle", 0) + 28) % 360
        cv = getattr(self, "_bar_top", None)
        try:
            cv = cv.cv
            cv.itemconfigure(self._spin_id, start=self._spin_angle)
        except Exception:
            pass
        self.after(55, self._spin_tick)

    def _paint_all_bars(self):
        if getattr(self, "_painting", False):
            return
        self._painting = True
        try:
            for bar in getattr(self, "_bars", []):
                try:
                    bar.cv.update_idletasks()
                    self._paint_bar(bar)
                except Exception:
                    pass
            self._chips_bg_sync()
        finally:
            self._painting = False

    def _chips_bg_sync(self):
        """分类条背景也贴上壁纸 (跟随横向滚动, 所以按视口位置实时对位)。"""
        cv = getattr(self, "_chip_cv", None)
        if cv is None:
            return
        if not (self._wall_mode() and getattr(self, "_bg_pil", None) is not None):
            if getattr(self, "_chips_img", None) is not None:
                try:
                    cv.delete(self._chips_img_id)
                except Exception:
                    pass
                self._chips_img = None
                self._chips_img_id = None
                self._chips_bg_key = None
                try:
                    cv.configure(bg=BG_DARK)
                except tk.TclError:
                    pass
            return
        try:
            vw = max(60, self._filter_canvas.winfo_width())
            cwid = max(vw, self._chip_cv.winfo_reqwidth(), self._chip_cv.winfo_width())
            x0 = self._filter_canvas.winfo_rootx() - self.winfo_rootx()
            y0 = self._chip_cv.winfo_rooty() - self.winfo_rooty()
            off = self._filter_canvas.canvasx(0)
            bw, bh = self._bg_pil.size
            chh = max(20, self._chip_cv.winfo_height())
        except Exception:
            return
        key = (cwid, x0, y0, chh)
        try:
            if getattr(self, "_chips_bg_key", None) != key:
                glass = self._glass_strength()
                bl = getattr(self, "_glass_pil", None)
                use_glass = glass > 0.02 and bl is not None
                src = bl if use_glass else self._bg_pil
                # 整条内容宽度都贴图 (不只视口), 否则横向滚到右边会露出纯色底
                crop = src.crop((max(0, x0), max(0, y0),
                                 min(min(bw, src.size[0]), max(0, x0) + cwid),
                                 min(bh, max(0, y0) + chh)))
                if use_glass and crop.size[0] > 4 and crop.size[1] > 4:
                    try:      # 与三条文字栏同一层玻璃, 整条 chrome 才是一块料
                        from PIL import Image as _Im
                        crop = _Im.blend(crop, _Im.new("RGB", crop.size, _hex2rgb(BG_DARK)),
                                         min(0.16, 0.10 * glass))
                    except Exception:
                        pass
                from PIL import ImageTk
                self._chips_img = ImageTk.PhotoImage(crop)
                self._chips_bg_key = key
                if getattr(self, "_chips_img_id", None) is not None:
                    try:
                        cv.delete(self._chips_img_id)
                    except Exception:
                        pass
                self._chips_img_id = cv.create_image(0, 0, anchor="nw",
                                                     image=self._chips_img)
            else:
                cv.coords(self._chips_img_id, 0, 0)
            cv.tag_lower(self._chips_img_id)
        except Exception:
            pass

    def _chip_at(self, x):
        for name, c in self._chips.items():
            if c["x"] <= x <= c["x"] + c["w"]:
                return name
        return None

    def _chip_click(self, e):
        name = self._chip_at(self._chip_cv.canvasx(e.x))
        if name:
            self._set_cat(name)
        return "break"

    def _chip_motion(self, e):
        self._chip_hover(self._chip_at(self._chip_cv.canvasx(e.x)))

    def _chip_tween(self, name, c0, c1, dur=0.14):
        c = self._chips.get(name)
        if not c:
            return
        cv = self._chip_cv

        def apply(t):
            try:
                cv.itemconfigure(c["text"], fill=_mix(c0, c1, t))
            except tk.TclError:
                pass

        self._tick.add("chipfill%d" % id(c), dur, apply, ease=_ease_out_quint)

    def _chip_hover(self, name):
        if name == self._hover_chip:
            return
        prev = self._hover_chip
        self._hover_chip = name
        if prev and prev != self._cat:
            c = self._chips.get(prev)
            if c:
                self._chip_tween(prev, _mix(c["base"], c["fg"], 0.45), c["base"])
        if name and name != self._cat:
            c = self._chips.get(name)
            if c:
                self._chip_tween(name, c["base"], _mix(c["base"], c["fg"], 0.45))

    def _sync_filter(self, repos):
        """按当前数据重建 chip: 全部 + 出现过的分类, 选中块铺在新位置上。"""
        cv = getattr(self, "_chip_cv", None)
        if cv is None:
            return
        ui = PALETTE["ui"]

        counts = {}
        for r in repos.values():
            for one in _subs(r):
                counts[one] = counts.get(one, 0) + 1
        total = len(repos)

        meta = _CAT_META
        order = {name: i for i, name in enumerate(meta.keys())}
        names = sorted(counts.keys(), key=lambda n: (order.get(n, 999), -counts[n]))

        # 选中项若已不存在 (换数据了) → 回到全部
        if self._cat != "全部" and self._cat not in counts:
            self._cat = "全部"

        # chip 集合没变就别重建 —— 否则切分类时选中块的滑动会被打断
        sig = tuple((n, counts[n]) for n in names) + (total,)
        if getattr(self, "_chip_sig", None) == sig and self._chips:
            return
        self._chip_sig = sig

        cv.delete("all")
        self._chips = {}
        self._puck = None

        items = [("全部", "全部  %d" % total, None)]
        for n in names:
            emoji, fam_name = meta.get(n, ("", None))
            items.append((n, ("%s %s  %d" % (emoji, n, counts[n])).strip(), fam_name))

        font = self._font(10, "bold")
        # 分类条现在压在磨砂玻璃条上(亮度已归一), 再叠一层描边就会出现"重影字"
        halo_col = None
        x = 4
        self._chip_order = []
        text_items = []
        for name, label, fam in items:
            fam_p = PALETTE[fam] if fam else None
            hot = fam_p["accent"] if fam_p else PALETTE["科技"]["accent"]
            base = _mix(PALETTE["ui"]["text"], BG_DARK, 0.20)
            w = font.measure(label) + 20
            sh = None
            sh2 = None
            t = cv.create_text(x + w // 2, 17, text=label, fill=base, font=font)
            self._chips[name] = {"x": x, "w": w, "rect": None, "text": t,
                                 "shadow": sh, "shadow2": sh2,
                                 "label": label, "base": base, "fg": hot, "fam": fam}
            self._chip_order.append(name)
            text_items.append(t)
            x += w + 8

        total_w = max(24, x)
        cv.configure(width=total_w, height=36)

        sel = self._chips.get(self._cat) or self._chips.get("全部")
        if sel:
            self._cat = next(n for n, c in self._chips.items() if c is sel)
            acc = (PALETTE[sel["fam"]]["accent"] if sel["fam"] else PALETTE["科技"]["accent"])
            self._sel_acc = acc
            self._sel_fg = acc
            self._puck = cv.create_rectangle(sel["x"] + 7, 30, sel["x"] + sel["w"] - 7, 33,
                                             fill=acc, outline="")
            for _sk in ("shadow", "shadow2"):
                for c in self._chips.values():
                    if c.get(_sk) is not None:
                        cv.tag_raise(c[_sk])
            for t in text_items:
                cv.tag_raise(t)
            cv.tag_raise(self._puck)
            cv.itemconfigure(sel["text"], fill=acc)
        self._chips_img_id = None
        self._chips_bg_key = None
        self._chips_bg_sync()
        try:
            self._filter_canvas.configure(scrollregion=self._filter_canvas.bbox("all"))
        except tk.TclError:
            pass

    def _ensure_chip_visible(self, name):
        """把滑到的 chip 带进可视区 (分段控件会自动跟随)。"""
        c = self._chips.get(name)
        out = getattr(self, "_filter_canvas", None)
        if not c or out is None:
            return
        try:
            total = float(self._chip_cv.winfo_reqwidth() or 1)
            view = float(out.winfo_width() or 1)
            f0 = out.xview()[0]
            vf = min(1.0, view / total)
            left = c["x"] / total
            right = (c["x"] + c["w"]) / total
            if left < f0 + 0.004:
                target = max(0.0, left - 0.01)
            elif right > f0 + vf - 0.004:
                target = min(1.0 - vf, right - vf + 0.01)
            else:
                return
        except Exception:
            return

        def apply(t):
            try:
                out.xview_moveto(f0 + (target - f0) * t)
                self._chips_bg_sync()
            except tk.TclError:
                pass

        self._tick.add("fview", 0.26, apply, ease=_ease_in_out)

    def _slide_puck(self, old, new):
        """选中下划线从旧 chip 滑到新 chip, 文字颜色同步交叉渐变。"""
        cv = getattr(self, "_chip_cv", None)
        if cv is None or not self._chips:
            return
        b = self._chips.get(new)
        if not b:
            return
        a = self._chips.get(old) if old != new else None
        acc_new = PALETTE[b["fam"]]["accent"] if b["fam"] else PALETTE["科技"]["accent"]
        acc_old = self._sel_acc

        if self._puck is None:
            self._puck = cv.create_rectangle(b["x"] + 7, 30, b["x"] + b["w"] - 7, 33,
                                             fill=acc_new, outline="")
        for c in self._chips.values():
            for _sk in ("shadow", "shadow2"):
                if c.get(_sk) is not None:
                    cv.tag_raise(c[_sk])
            cv.tag_raise(c["text"])
        cv.tag_raise(self._puck)

        x0, w0 = (a["x"], a["w"]) if a else (b["x"], b["w"])
        x1, w1 = b["x"], b["w"]

        def move(t):
            xx = x0 + (x1 - x0) * t
            ww = w0 + (w1 - w0) * t
            try:
                cv.coords(self._puck, xx + 7, 30, xx + ww - 7, 33)
                cv.itemconfigure(self._puck, fill=_mix(acc_old, acc_new, t))
            except tk.TclError:
                pass

        self._tick.add("puck", 0.32, move, ease=_ease_in_out)

        if a:
            def fade_old(t):
                try:
                    cv.itemconfigure(a["text"], fill=_mix(acc_old, a["base"], t))
                except tk.TclError:
                    pass
            self._tick.add("ctext_old", 0.26, fade_old, ease=_ease_out_quint)

        def fade_new(t):
            try:
                cv.itemconfigure(b["text"], fill=_mix(b["base"], acc_new, t))
            except tk.TclError:
                pass
        self._tick.add("ctext_new", 0.26, fade_new, ease=_ease_out_quint)

        self._sel_acc = acc_new
        self._sel_fg = acc_new
        self.after(40, lambda: self._ensure_chip_visible(new))

    def _set_cat(self, name):
        if name == self._cat:
            return
        old = self._cat
        self._cat = name
        # 先让选中块自己滑过去 (这条动画全程 50fps), 列表等它快落地时再重建,
        # 否则重建的布局开销会把滑动卡成四五帧
        self._no_anim = True
        self._slide_puck(old, name)
        try:
            if getattr(self, "_cat_job", None):
                self.after_cancel(self._cat_job)
        except Exception:
            pass
        self._cat_job = self.after(270, self._commit_cat)
        self.after(330, lambda: setattr(self, "_no_anim", False))

    def _commit_cat(self):
        self._cat_job = None
        if not self._last_repos:
            return
        self._no_anim = True
        self.render(self._last_repos, first=8)
        self._glide_top()
        self._stream_rows(8)
        self.after(150, lambda: setattr(self, "_no_anim", False))

    def _stream_rows(self, done):
        """把剩下的条目一小批一小批补上, 别一次性卡住主线程。"""
        repos = self._last_repos or {}
        if self._cat == "全部" or self._wall_mode():
            return
        items = sorted([r for r in repos.values() if self._cat in _subs(r)],
                       key=lambda r: -r["stars"])[:50]
        if done >= len(items):
            return
        meta = _CAT_META.get(self._cat)
        fam = meta[1] if meta else "科技"
        for i, r in enumerate(items[done:done + 6], done + 1):
            try:
                self._row(self.inner, fam, i, r)
            except Exception:
                return
        self.after(16, lambda: self._stream_rows(done + 6))

    # ── 网络/事件 (子线程 → 主线程队列) ─────────────────────
    def _ui(self, kind, payload):
        try:
            self._q.put((kind, payload))
        except Exception:
            pass

    def _drain(self):
        try:
            while True:
                kind, payload = self._q.get_nowait()
                try:
                    self._on_event(kind, payload)
                except Exception:
                    pass
        except queue.Empty:
            pass
        except Exception:
            pass
        try:
            self.after(100, self._drain)
        except Exception:
            pass

    def _on_event(self, kind, payload):
        ui = PALETTE["ui"]
        if kind == "status":
            self.status.configure(text=payload, fg=ui["warn"])
        elif kind == "phase":
            # 首屏已就绪: 停止转圈, 剩下的分类在后台安静补
            try:
                _k, d, tot = payload
                self._fetching = False
                self._enriching = True
                self._paint_all_bars()
                if not getattr(self, "_quieting", False):
                    self.status.configure(
                        text=f"✅ 已更新 {d}/{tot} 个分类 · 其余后台补 (可正常使用)",
                        fg=ui["ok"])
            except Exception:
                pass
        elif kind == "partial":
            # 渐进渲染: 边抓边长 (缓存命中的分类先出图, 后面的逐步补进来)
            try:
                d = payload
                repos = d.get("repos") or {}
                if repos:
                    self._last_repos = repos
                    now = time.time()
                    if now - getattr(self, "_last_partial", 0.0) > 1.2:
                        self._last_partial = now
                        self.render(repos)
                self.status.configure(
                    text=f"· 后台补充 {d.get('done', 0)}/{d.get('total', 0)} 个分类 · "
                         f"{d.get('secs', 0)}s · 已 {len(repos)} 个仓库",
                    fg=ui["muted"]) if not getattr(self, "_quieting", False) else None
                if d.get("dates"):
                    self.note_lbl.configure(
                        text=f"窗口: {d['dates']['since']} ~ {d['dates']['until']} · 抓取中 …")
            except Exception:
                pass
        elif kind == "done":
            was_quiet = getattr(self, "_quieting", False)
            self._fetching = False
            self._enriching = False
            self._last_partial = 0.0
            repos, dates, note = payload
            self._last_repos = repos
            if not was_quiet:
                self.status.configure(
                    text=f"✅ {note} · 今天已更新 (每天自动更新一次, 也可点「⟳ 刷新」手动更新)",
                    fg=ui["ok"])
                self.note_lbl.configure(
                    text=f"窗口: {dates['since']} ~ {dates['until']} · 生成于 {datetime.now().strftime('%H:%M')}")
            self.render(repos)
            if self._lang != "off" and not self._translating:
                self.after(200, self.translate_now)
            self._quieting = False
            if getattr(self, "_quiet_runs", 0) < 4:
                self.after(95000, self._quiet_enrich)
        elif kind == "error":
            self._fetching = False
            self._enriching = False
            self.status.configure(text=f"❌ {payload}", fg=ui["err"])
        elif kind == "trans_progress":
            done, total = payload
            self.status.configure(text=f"🌐 翻译中 … {done}/{total}", fg=ui["warn"])
        elif kind == "trans_done":
            self._translating = False
            cnt = sum(1 for r in (self._last_repos or {}).values() if r.get("desc_tr"))
            if self._last_repos:
                self.render(self._last_repos)
            self.status.configure(text=f"✅ 已翻译 {cnt} 条简介 → {payload}", fg=ui["ok"])

    def refresh(self):
        # 后台补分类的时候点刷新: 让手动刷新优先, 不等它
        if getattr(self, "_enriching", False) and getattr(self, "_quieting", False):
            self._quieting = False
            self._enriching = False
        if getattr(self, "_fetching", False) or getattr(self, "_enriching", False):
            self.status.configure(text="正在拉取中, 稍等一下 …", fg=PALETTE["ui"]["warn"])
            return
        self._fetching = True
        self._spin_start()
        self.status.configure(text="网络请求中 …", fg=PALETTE["ui"]["warn"])
        threading.Thread(target=run_pipeline, args=(self._ui,), daemon=True).start()

    def _refresh_bg(self):
        """后台静默刷新数据 (不打扰当前画面)。"""
        if getattr(self, "_fetching", False) or getattr(self, "_enriching", False):
            return
        self._fetching = True
        self._spin_start()
        threading.Thread(target=run_pipeline, args=(self._ui,), daemon=True).start()

    def boot(self):
        snap = None
        try:
            snap = snapshot_load()
        except Exception:
            snap = None
        if snap:
            repos, dates, age = snap
            self._last_repos = repos
            mins = int(age // 60)
            ago = "刚刚" if mins < 1 else (f"{mins} 分钟前" if mins < 120 else f"{mins // 60} 小时前")
            self.note_lbl.configure(
                text=f"缓存: {dates['since']} ~ {dates['until']} · 上次更新 {ago}")
            self.render(repos)
            if self._lang != "off":
                self.after(600, self.translate_now)
            ts = last_snapshot_ts()
            if daily_due():
                self.status.configure(text="已显示缓存 · 今天还没更新, 正在拉取今日榜单 …",
                                      fg=PALETTE["ui"]["sub"])
                self.after(400, self._refresh_bg)
            else:
                when = datetime.fromtimestamp(ts).strftime("%H:%M") if ts else "—"
                self.status.configure(text=f"✅ 今日已更新 · {when} 拉的榜 (每天自动更新一次)",
                                      fg=PALETTE["ui"]["ok"])
            self._daily_watch()
        else:
            self.refresh()

    def _daily_watch(self):
        """开着不动也能跨天自动换榜: 每 20 分钟看一眼日期。"""
        try:
            if daily_due() and not getattr(self, "_fetching", False) \
                    and not getattr(self, "_enriching", False):
                self._refresh_bg()
        except Exception:
            pass
        self.after(20 * 60 * 1000, self._daily_watch)

    def _stale_cats(self):
        """还没到 20h 新鲜期、需要补抓的分类数。"""
        try:
            store = (load_cache().get(CAT_STORE_KEY) or {})
        except Exception:
            return 0
        n = 0
        now = time.time()
        for label, _q in (TECH_QUERIES + LIFE_QUERIES):
            sub = label.split(" ", 1)[1]
            ent = store.get(sub) or {}
            if (now - float(ent.get("ts") or 0)) > CAT_FRESH_SEC:
                n += 1
        return n

    def _quiet_enrich(self):
        """后台安静补分类: 用户不动的时候顺手补一小批, 最多 4 轮 —— 让下次刷新几乎不用等。"""
        try:
            busy = getattr(self, "_fetching", False) or getattr(self, "_enriching", False)
            if busy or getattr(self, "_quiet_runs", 0) >= 4:
                return
            if self._stale_cats() <= 0:
                return
            self._quiet_runs = getattr(self, "_quiet_runs", 0) + 1
            self._quieting = True
            threading.Thread(target=run_pipeline, args=(self._ui,), daemon=True).start()
        except Exception:
            pass

    # ── 滚动 ───────────────────────────────────────────────
    def _scroll_bar_hint(self):
        """壁纸模式: 用画布上一条细线代替滚动条, 不挡照片。"""
        c = self.canvas
        try:
            c.delete("sbar")
            if not self._wall_mode():
                return
            h = c.winfo_height()
            w = c.winfo_width()
            first, last = c.yview()
            if last - first >= 0.999:
                return
            ui = PALETTE["ui"]
            y0 = int(h * first)
            y1 = max(y0 + 30, int(h * last))
            x = w - 8
            c.create_line(x, y0 + 6, x, y1 - 6, fill=_mix(BG_DARK, ui["text"], 0.55),
                          width=4, capstyle="round", tags="sbar")
            c.tag_raise("sbar")
        except Exception:
            pass

    def _on_yscroll(self, first, last):
        try:
            self.vsb.set(first, last)
        except Exception:
            pass
        self._bg_follow()
        self._fade_sync()
        self._scroll_bar_hint()

    def _fade_sync(self):
        """只有滚到最顶上时才显示"磨砂 → 清晰"的渐隐条。"""
        try:
            at_top = self.canvas.yview()[0] <= 0.0015
        except Exception:
            return
        has = bool(self.canvas.find_withtag("fade"))
        if at_top and not has:
            self._top_fade()
            try:
                self.canvas.tag_lower("fade", "content")
            except Exception:
                pass
        elif not at_top and has:
            self.canvas.delete("fade")

    def _wheel(self, event):
        if event.widget.winfo_toplevel() is not self:
            return
        if getattr(event, "num", None) == 4:
            step = -_WHEEL_PX
        elif getattr(event, "num", None) == 5:
            step = _WHEEL_PX
        else:
            d = getattr(event, "delta", 0) or 0
            if not d:
                return
            self._wheel_acc += -d / 120.0 * _WHEEL_PX
            step = int(self._wheel_acc)
            if not step:
                return
            self._wheel_acc -= step
        try:
            self.canvas.yview_scroll(step, "units")
        except tk.TclError:
            return
        self._bg_follow()
        self._fade_sync()
        if step:
            self._sv_step(step)
        return "break"

    def _sv_step(self, step):
        """惯性: 按滚动方向再滑一小段, 力度递减。"""
        if not step:
            return
        boost = min(_INERTIA_MAX, abs(step) * 0.9)
        sgn = 1 if step > 0 else -1
        self._sv_v = sgn * boost

        def tick():
            self._sv_v *= _INERTIA_DECAY
            if abs(self._sv_v) < 0.6:
                self._sv_v = 0.0
                return
            try:
                self.canvas.yview_scroll(int(self._sv_v) or (1 if self._sv_v > 0 else -1), "units")
                self._bg_follow()
            except tk.TclError:
                return
            self.after(16, tick)

        self.after(16, tick)

    # ── 翻译 ───────────────────────────────────────────────
    def _lang_name(self, code=None):
        code = code if code else self._lang
        for name, c in TRANS_LANGS:
            if c == code:
                return name
        return "原文（不翻译）"

    def _lang_menu(self):
        ui = PALETTE["ui"]
        m = tk.Menu(self, tearoff=0, bg=_mix(BG_DARK, ui["text"], 0.10),
                    fg=ui["text"], activebackground=PALETTE["科技"]["accent"],
                    activeforeground="#0b0d12", bd=0, relief="flat",
                    font=("Microsoft YaHei UI", 10))
        for name, code in TRANS_LANGS:
            m.add_radiobutton(label=name, font=("Microsoft YaHei UI", 10),
                              command=lambda c=code: self._set_lang(c))
        try:
            x = self.lang_btn.winfo_rootx()
            y = self.lang_btn.winfo_rooty() - 380
            m.tk_popup(x, max(10, y))
        except Exception:
            pass
        finally:
            try:
                m.grab_release()
            except Exception:
                pass

    def _set_lang(self, code):
        self._lang = code
        SETTINGS["lang"] = code
        try:
            save_settings(SETTINGS)
        except Exception:
            pass
        try:
            self.lang_btn.configure(text="🌐 %s ▾" % self._lang_name())
        except Exception:
            pass
        if code == "off":
            for r in (self._last_repos or {}).values():
                r.pop("desc_tr", None)
            if self._last_repos:
                self.render(self._last_repos)
            try:
                self.status.configure(text="已切回原文", fg=PALETTE["ui"]["sub"])
            except Exception:
                pass
        else:
            self.translate_now()

    def _visible_repos(self):
        """只翻当前榜单真正显示的条目 (全部视图: 每类 Top10; 单类视图: 前 40)。"""
        repos = self._last_repos or {}
        out = {}
        if self._cat != "全部":
            items = sorted([r for r in repos.values() if self._cat in _subs(r)],
                           key=lambda r: -r["stars"])
            for r in items[:40]:
                out[r["full_name"]] = r
            return out
        for cat in ("科技", "生活"):
            items = sorted([r for r in repos.values() if r["category"] == cat],
                           key=lambda r: -r["stars"])[:10]
            for r in items:
                out[r["full_name"]] = r
        return out

    def translate_now(self):
        if self._translating:
            return
        if self._lang == "off":
            self._lang = "zh-CN"
            SETTINGS["lang"] = "zh-CN"
            try:
                save_settings(SETTINGS)
                self.lang_btn.configure(text="🌐 %s ▾" % self._lang_name())
            except Exception:
                pass
        if not self._last_repos:
            self.status.configure(text="还没有数据, 先刷新一次", fg=PALETTE["ui"]["warn"])
            return
        self._translating = True
        self.status.configure(text="🌐 正在翻译简介 …", fg=PALETTE["ui"]["warn"])
        target = self._lang
        subs = self._visible_repos()
        threading.Thread(target=translate_batch,
                         args=(subs, target, self._ui), daemon=True).start()

    # ── 设置 ───────────────────────────────────────────────
    def open_settings(self):
        _dbg("open_settings called, dlg=%r" % (getattr(self, "_dlg", None),))
        dlg = getattr(self, "_dlg", None)
        if dlg is not None:
            alive = False
            try:
                alive = bool(dlg.winfo_exists())
            except Exception:
                alive = False
            if alive:
                try:
                    dlg.deiconify()
                    dlg.lift()
                    dlg.focus_force()
                    return dlg
                except Exception:
                    pass
            self._dlg = None
            try:
                dlg.destroy()
            except Exception:
                pass
        try:
            self._dlg = SettingsDialog(self, origin=self._flat_rect(self._bar_top, "设"))
        except Exception as e:                        # 构建出错时不能把半截窗口留在屏幕上
            _dbg("open_settings EXC %r" % (e,))
            for w in list(self.winfo_children()):
                if isinstance(w, tk.Toplevel) and not getattr(w, "_ready", False):
                    try:
                        w.destroy()
                    except Exception:
                        pass
            self.status.configure(text="设置面板打开失败: %s" % e,
                                  fg=PALETTE["ui"]["err"])
            return None
        return self._dlg
    def _flat_rect(self, bar, needle):
        """取文字栏里某个按钮的屏幕矩形 (给"从按钮里长出来"的动画当起点)。"""
        try:
            for it in bar.items:
                if needle in (it.text or "") and getattr(it, "box", None):
                    b = it.box
                    return (bar.cv.winfo_rootx() + b[0] - 5,
                            bar.cv.winfo_rooty() + b[1] - 5,
                            max(28, b[2] - b[0] + 10), max(20, b[3] - b[1] + 10))
        except Exception:
            pass
        return None

    def apply_settings(self, new):
        SETTINGS.update(new)
        try:
            save_settings(SETTINGS)
        except Exception:
            pass
        apply_theme(SETTINGS)
        self._build_ui()
        if self._last_repos:
            self.render(self._last_repos)
        try:
            self.status.configure(text="✅ 外观已更新", fg=PALETTE["ui"]["ok"])
        except Exception:
            pass
        self.after(40, self._layers_build)
        # 配置变了, 旧位图作废; 过一会儿重新预热, 下次点设置还是秒出
        try:
            _SNAP_MEM.clear()
        except Exception:
            pass
        self.after(1800, self._warm_settings)

    # ── 背景层 ─────────────────────────────────────────────
    def _bg_path(self):
        p = (SETTINGS.get("bgimg") or "").strip().strip('"')
        return p if p and os.path.isfile(p) else ""

    def _on_configure(self, event=None):
        if getattr(event, "widget", None) is not self:
            return
        if getattr(self, "_win_anim", False):
            return                    # 开场缩放动画期间的尺寸变化不重建背景, 别卡
        try:
            if self._resize_job:
                self.after_cancel(self._resize_job)
        except Exception:
            pass
        self._resize_job = self.after(320, self._after_resize)

    def _after_resize(self):
        self._resize_job = None
        try:
            if not self.winfo_exists():
                return
        except Exception:
            return
        self._layers_build()
        # 宽度变化较大时重排一次, 让换行宽度跟上 (卡片模式才需要)
        try:
            cw = self.canvas.winfo_width()
        except Exception:
            cw = 0
        if cw > 200 and not self._wall_mode() and self._last_repos:
            if abs(cw - getattr(self, "_last_cw", 0)) > 44:
                self._last_cw = cw
                self._no_anim = True
                self.render(self._last_repos)
                self.after(80, lambda: setattr(self, "_no_anim", False))

    def _bg_follow(self):
        """让画布里的壁纸贴住视口, 并且和上面几条文字栏的贴图无缝对齐。

        画布不是从窗口 (0,0) 开始的 (上面有标题/状态/分类三条), 所以要把画布
        在窗口里的偏移反向补掉, 否则接缝处的壁纸会从照片顶部重新开始 —— 看着
        就是"壁纸后面还有一层"。
        """
        if self._bg_item is None or self._bg_view is None:
            return
        # 自愈: 窗口尺寸和背景图层尺寸不一致 (刚改过大小/换过 DPI) 就排队重建,
        # 否则壁纸只盖住一部分, 右侧或底部会露出纯色, 看着像"这里没有背景图"
        try:
            if (not getattr(self, "_win_anim", False)
                    and self._bg_view.size != (self.winfo_width(), self.winfo_height())):
                if not self._resize_job:
                    self._resize_job = self.after(60, self._after_resize)
        except Exception:
            pass
        try:
            dy = self.canvas.winfo_rooty() - self.winfo_rooty()
            dx = self.canvas.winfo_rootx() - self.winfo_rootx()
            self.canvas.coords(self._bg_item,
                               self.canvas.canvasx(0) - dx,
                               self.canvas.canvasy(0) - dy)
            self.canvas.tag_lower("bgimg")
        except Exception:
            pass

    def _layers_build(self):
        """重算背景层 (同一时间只跑一份; 开场缩放动画期间推迟到动画结束)。"""
        if getattr(self, "_win_anim", False):
            self._layers_pending = True
            return
        if getattr(self, "_building", False):
            return
        self._building = True
        try:
            self._layers_build_inner()
        finally:
            self._building = False

    def _layers_build_inner(self):
        """重算背景层: 整窗壁纸 + 画布铺图 + 面板/卡片取色。"""
        self._layers_pending = False
        self._bg_view = None
        self._bg_photo = None
        self._bg_tile_photo = None
        self._bg_item = None
        self._glass_pil = None
        try:
            self.canvas.delete("bgimg")
        except Exception:
            pass
        path = self._bg_path()
        if not path:
            try:
                if self._bg_lbl is not None:
                    self._bg_lbl.configure(image="", bg=BG_DARK, text="")
                self.configure(bg=BG_DARK)
                self.canvas.configure(bg=BG_DARK)
            except Exception:
                pass
            self._bg_pil = None
            self._paint_all_bars()
            return
        if not os.path.isfile(path):
            try:
                self._bg_lbl.configure(image="", bg=BG_DARK, text="⚠ 图片读取失败",
                                       fg=PALETTE["ui"]["err"], font=("Microsoft YaHei UI", 12))
            except Exception:
                pass
            return
        try:
            from PIL import ImageTk
        except Exception:
            return
        w = max(2, self.winfo_width())
        h = max(2, self.winfo_height())
        _dbg("layers_build win=%dx%d" % (w, h))
        if w < 220 or h < 220:
            # 窗口还没被 WM 真正映射出来 (尺寸还是 1x1), 这时候建的壁纸图会很小,
            # 之后只盖住中间一小块 -> 等它成型再来
            self.after(150, self._layers_build)
            return
        view = _bg_view_cached(path, w, h, SETTINGS.get("bgdim", 0.42), BG_DARK)
        if view is None:
            try:
                self._bg_lbl.configure(image="", bg=BG_DARK, text="⚠ 图片读取失败",
                                       fg=PALETTE["ui"]["err"], font=("Microsoft YaHei UI", 12))
            except Exception:
                pass
            return
        try:
            self._bg_view = view
            self._bg_pil = view
            self._glass_pil = _blur_fast(view, 10 + 20 * self._glass_strength())
            self._bg_photo = ImageTk.PhotoImage(view)
            if self._bg_lbl is not None:
                self._bg_lbl.configure(image=self._bg_photo, bg=BG_DARK, text="")
        except Exception:
            return
        try:
            self._bg_item = self.canvas.create_image(0, 0, anchor="nw",
                                                     image=self._bg_photo, tags="bgimg")
            self.canvas.tag_lower("bgimg")
        except Exception:
            self._bg_item = None
        self._apply_photo_tint()
        self._fit_inner_bg()
        self._bg_follow()
        self._paint_all_bars()
        if not self._wall_mode():
            self.after(30, self._frost_cards)

    def _photo_panel(self, x, y, w, h, keep=0.7):
        """取背景图上某块区域的平均色, 再向底色压回去, 用作面板"磨砂"色。"""
        if self._bg_view is None:
            return BG_DARK
        try:
            bw, bh = self._bg_view.size
            x = max(0, min(bw - 2, int(x)))
            y = max(0, min(bh - 2, int(y)))
            box = (x, y, min(bw, x + max(4, int(w))), min(bh, y + max(4, int(h))))
            avg = _bg_avg(self._bg_view, box)
            return _mix(avg, BG_DARK, keep) if avg else BG_DARK
        except Exception:
            return BG_DARK

    def _apply_photo_tint(self):
        """顶部/底部条、分组标题等原本纯色的面板改成照片同色系。"""
        if self._bg_view is None:
            return
        panel = self._photo_panel(0, 0, self.winfo_width(), 110, 0.66)
        try:
            self.configure(bg=panel)
        except Exception:
            pass
        old = str(BG_DARK).lower()

        def walk(w):
            for c in w.winfo_children():
                try:
                    if c is getattr(self, "canvas", None):
                        pass
                    elif str(c.cget("bg")).lower() == old:
                        c.configure(bg=panel)
                except Exception:
                    pass
                walk(c)
        walk(self)

    def _fit_inner_bg(self):
        """内容区铺一层图 (竖向镜像重复), 卡片间隙里能看到背景。"""
        lbl = getattr(self, "_inner_bg", None)
        if lbl is None:
            return
        try:
            if not lbl.winfo_exists():
                return
        except Exception:
            return
        if self._bg_view is None or self._wall_mode():
            try:
                lbl.configure(image="", bg=BG_DARK, text="")
            except Exception:
                pass
            return
        try:
            from PIL import ImageTk
            self.update_idletasks()
            w = max(2, self.inner.winfo_width())
            h = max(2, self.inner.winfo_height())
            tile = _bg_tile_vertical(self._bg_view, w, h)
            if tile is None:
                return
            self._bg_tile_photo = ImageTk.PhotoImage(tile)
            lbl.configure(image=self._bg_tile_photo, bg=BG_DARK, text="")
            lbl.place(x=0, y=0, relwidth=1, relheight=1)
            lbl.lower()
        except Exception:
            pass

    def _frost_cards(self):
        """每张卡片的底色取自它背后那块照片的平均色 → 毛玻璃效果。"""
        if self._bg_view is None or not self._rows:
            return
        if self._wall_mode():
            return
        try:
            op = max(0.45, min(1.0, float(SETTINGS.get("cardop", 0.86))))
        except Exception:
            op = 0.86
        bw, bh = self._bg_view.size
        dark_theme = _luma(BG_DARK) < 0.5
        for row in self._rows:
            card = row.get("card")
            colors = row.get("colors")
            if colors is None or card is None:
                continue
            try:
                if not card.winfo_exists():
                    continue
                cw = max(8, card.winfo_width())
                ch = max(8, card.winfo_height())
                x0 = max(0, min(bw - 4, card.winfo_x()))
                y0 = (max(0, card.winfo_y())) % max(1, bh - 4)
            except Exception:
                continue
            box = (x0, y0, min(bw, x0 + cw), min(bh, y0 + ch))
            if box[2] - box[0] < 2 or box[3] - box[1] < 2:
                continue
            pavg = _bg_avg(self._bg_view, box) or BG_DARK
            base = colors["_card0"]
            fc = _mix(pavg, base, op)
            if dark_theme and _luma(fc) > _luma(base) + 0.10:
                fc = _mix(fc, BG_DARK, 0.55)
            elif (not dark_theme) and _luma(fc) < _luma(base) - 0.10:
                fc = _mix(fc, BG_DARK, 0.45)
            colors["card"] = fc
            colors["badge"] = _mix(fc, colors["_badge0"], 0.55)
            colors["hover"] = _mix(fc, colors["_accent"], 0.2)
            for wgt, role in (row.get("bg_widgets") or []):
                try:
                    wgt.configure(bg=colors[role])
                except Exception:
                    pass

    # ── 渲染 ───────────────────────────────────────────────
    def _sections(self, repos):
        """把数据整理成 [(fam, 标题, 副标题, [(rank, repo), …]), …]"""
        week = self._week_label()
        if self._cat != "全部":
            emoji, fam_name = _CAT_META.get(self._cat, ("", "科技"))
            items = sorted([r for r in repos.values() if self._cat in _subs(r)],
                           key=lambda r: -r["stars"])[:50]
            rows = [(i, r) for i, r in enumerate(items, 1)]
            return [(fam_name, f"{emoji} {self._cat}".strip(),
                     f"{week} · 共 {len(items)} 个 · 按 Star 排序", rows)]
        out = []
        for cat in ("科技", "生活"):
            items = sorted([r for r in repos.values() if r["category"] == cat],
                           key=lambda r: -r["stars"])[:10]
            out.append((cat, f"{cat}榜", f"{week} · Star 增长 Top 10",
                        [(i, r) for i, r in enumerate(items, 1)]))
        return out

    def render(self, repos, first=None):
        self._load_anim_stop()
        cw = 0
        try:
            cw = self.canvas.winfo_width()
        except Exception:
            cw = 0
        if cw < 200:
            cw = max(900, self.winfo_width() - 40)
        # 名次列 ~62, 分类 chip ~130, 星数 ~150, 内边距 ~40
        self._wrap_desc = max(240, cw - 62 - 130 - 150 - 40)
        self._wrap_det = max(320, cw - 62 - 40)
        self._mcache = {}
        ui = PALETTE["ui"]
        for w in self.inner.winfo_children():
            w.destroy()
        self._rows.clear()
        self._open_count = 0
        self._last_repos = repos
        self._sync_filter(repos)
        secs = self._sections(repos)

        if not sum(len(rows) for _f, _t, _s, rows in secs):
            self._empty_state()
            self.after(60, self._refresh_layers)
            return

        if self._wall_mode():
            # 壁纸模式: 文字直接压在图上, 不放卡片色块
            self._draw_wall(secs)
            n = sum(len(rows) for _f, _t, _s, rows in secs)
            tip = (f"共 {n} 个项目 · 分类「{self._cat}」" if self._cat != "全部"
                   else f"共展示 {n} 个项目")
            tag = "" if self._lang == "off" else "　·　🌐 已翻译"
            self.lbl_total.configure(text=f"{tip}{tag}")
            self.after(60, self._refresh_layers)
            self.after(20, self._scroll_bar_hint)
            return

        # 内容区背景层 (先建, 位于卡片之下)
        self._inner_bg = tk.Label(self.inner, bd=0, highlightthickness=0, bg=BG_DARK)
        self._inner_bg.place(x=0, y=0, relwidth=1, relheight=1)
        self._inner_bg.lower()

        for fam, title, sub, rows in secs:
            p = PALETTE[fam]
            head = tk.Frame(self.inner, bg=BG_DARK)
            head.pack(anchor="w", padx=14, pady=(20 if fam == "科技" or self._cat != "全部" else 34, 12))
            tk.Frame(head, bg=p["accent"], width=6, height=28).pack(side="left", padx=(0, 14))
            tk.Label(head, text=title, font=("Microsoft YaHei UI", 19, "bold"),
                     bg=BG_DARK, fg=p["accent"]).pack(side="left")
            tk.Label(head, text=sub, font=("Microsoft YaHei UI", 11),
                     bg=BG_DARK, fg=ui["sub"]).pack(side="left", padx=(14, 0), pady=(7, 0))
            for i, r in (rows[:first] if first else rows):
                self._row(self.inner, fam, i, r)

        n = sum(len(rows) for _f, _t, _s, rows in secs)
        tip = (f"共 {n} 个项目 · 分类「{self._cat}」" if self._cat != "全部"
               else f"共展示 {n} 个项目")
        tag = "" if self._lang == "off" else "　·　🌐 已翻译"
        self.lbl_total.configure(text=f"{tip}{tag}")
        self.after(60, self._refresh_layers)
        self.after(140, self._animate_in)
        self.after(0, self._force_repaint)


    def _empty_state(self):
        """数据还没到的时候: 转圈 + 呼吸骨架条, 别让用户对着空白发呆。"""
        ui = PALETTE["ui"]
        try:
            self.canvas.delete("content")
        except tk.TclError:
            pass
        w = max(self.canvas.winfo_width(), 720)
        cx = w // 2
        self._load_arc = self.canvas.create_arc(
            cx - 23, 148, cx + 23, 194, start=0, extent=105, style="arc", width=4,
            outline=PALETTE["科技"]["accent"], tags="content")
        self.canvas.create_text(cx, 244, text="正在获取数据", font=self._font(21, "bold"),
                                fill=ui["text"], tags="content")
        self.canvas.create_text(
            cx, 292, text="首次启动要抓 22 个分类, 大约 3 分钟; 之后启动会先用缓存秒开",
            font=self._font(12), fill=ui["sub"], tags="content")
        self.canvas.create_text(cx, 324, text="顶栏的转圈和这里会一起显示抓取进度",
                                font=self._font(10), fill=ui["muted"], tags="content")
        self._load_bars = []
        for i in range(5):
            yy = 388 + i * 56
            bw = int(w * (0.66 - 0.055 * (i % 3)))
            self._load_bars.append(self.canvas.create_rectangle(
                cx - bw // 2, yy, cx + bw // 2, yy + 15,
                fill=_mix(BG_DARK, ui["text"], 0.07), outline="", tags="content"))
            self.canvas.create_rectangle(
                cx - bw // 2, yy, cx - bw // 2 + 7, yy + 15,
                fill=_mix(BG_DARK, ui["text"], 0.16), outline="", tags="content")
        self.canvas.configure(scrollregion=(0, 0, w, 700))
        self.lbl_total.configure(text="正在获取 …")
        self._load_angle = 0
        self._load_phase = 0.0
        self._load_on = True
        if not getattr(self, "_load_job", None):
            self._load_anim_tick()

    def _load_anim_tick(self):
        """加载期循环动画 (转圈 + 骨架条呼吸); 数据到了由 render() 关掉。"""
        self._load_job = None
        if not getattr(self, "_load_on", False):
            return
        try:
            self._load_angle = (self._load_angle + 12) % 360
            self.canvas.itemconfigure(self._load_arc, start=self._load_angle)
            self._load_phase += 0.30
            ui = PALETTE["ui"]
            for i, b in enumerate(self._load_bars):
                p = 0.5 + 0.5 * math.sin(self._load_phase - i * 0.6)
                self.canvas.itemconfigure(b, fill=_mix(BG_DARK, ui["text"], 0.05 + 0.10 * p))
        except tk.TclError:
            self._load_on = False
            return
        self._load_job = self.after(45, self._load_anim_tick)

    def _load_anim_stop(self):
        self._load_on = False
        job = getattr(self, "_load_job", None)
        if job:
            self._load_job = None
            try:
                self.after_cancel(job)
            except Exception:
                pass

    def _hover_glow(self, b):
        """按钮 hover 平滑过渡 (tk 的 activebackground 只有按下才生效)。"""
        try:
            base = b.cget("bg")
            fg = b.cget("fg")
        except Exception:
            return
        ui = PALETTE["ui"]
        hover = (_mix(base, "#ffffff", 0.13) if _luma(base) < 0.5
                 else _mix(base, "#000000", 0.07))
        key = "glow%d" % id(b)

        def fade(c0, c1):
            def apply(t):
                try:
                    b.configure(bg=_mix(c0, c1, t), fg=fg)
                except tk.TclError:
                    pass

            self._tick.add(key, 0.15, apply, ease=_ease_out_quint)

        b.bind("<Enter>", lambda e: fade(base, hover), add="+")
        b.bind("<Leave>", lambda e: fade(hover, base), add="+")

    def _install_hover_fx(self):
        def walk(w):
            for ch in w.winfo_children():
                if isinstance(ch, tk.Button):
                    self._hover_glow(ch)
                walk(ch)
        walk(self)

    def _animate_in(self):
        """入场: 卡片从背景色淡出来, 越靠下错开越多 (iOS 列表那种一层层)。"""
        if self._wall_mode() or getattr(self, "_no_anim", False):
            return
        if len(self._rows) > 24:      # 单类视图条目多, 逐行补间会拖慢滑动
            return
        for i, row in enumerate(self._rows):
            pairs = row.get("bg_widgets") or []
            colors = row.get("colors") or {}
            if not pairs:
                continue

            def apply(t, pairs=pairs, colors=colors):
                for w, role in pairs:
                    try:
                        w.configure(bg=_mix(BG_DARK, colors.get(role) or BG_DARK, t))
                    except tk.TclError:
                        pass

            self._tick.add("in%d" % id(row.get("card")), 0.30, apply,
                           delay=min(i, 16) * 0.015, ease=_ease_out_quint)

    def _glide_top(self, dur=0.26):
        """换分类时把列表滑回顶部, 不是瞬间跳。"""
        try:
            start = self.canvas.yview()[0]
        except Exception:
            return
        if start <= 0.0015:
            return

        def apply(t):
            try:
                self.canvas.yview_moveto(start * (1 - t))
                self._bg_follow()
            except tk.TclError:
                pass

        self._tick.add("glide", dur, apply, ease=_ease_in_out)

    def _refresh_layers(self):
        """内容变化后重新铺背景图 / 重算卡片毛玻璃色。"""
        if self._bg_view is None:
            return
        self._apply_photo_tint()
        if not self._wall_mode():
            self._fit_inner_bg()
        self._bg_follow()
        try:
            self.canvas.tag_raise("content")
            self.canvas.tag_raise("hover")
        except tk.TclError:
            pass
        if not self._wall_mode():
            self.after(30, self._frost_cards)

    # ── 壁纸模式: 文字直接画在画布上 (无卡片色块) ───────────
    def _wall_mode(self):
        return bool(self._bg_path()) and bool(SETTINGS.get("wallmode", True))

    def _font(self, size, weight="normal"):
        key = ("Microsoft YaHei UI", size, weight)
        f = self._fonts.get(key)
        if f is None:
            f = tkfont.Font(family="Microsoft YaHei UI", size=size, weight=weight)
            self._fonts[key] = f
        return f

    def _txt(self, x, y, text, font, fill, anchor="nw", halo=True, halo_fill=None):
        """带描边阴影的文字: 先画两圈反色描边再画正字, 压在照片上也读得清。"""
        if not text:
            return
        if halo:
            # 只留一层贴身影子: 两层描边在 1.5x DPI 下会像"重影/双写"
            hc = halo_fill or _shadow_of(fill)
            self.canvas.create_text(x + 1, y + 1, text=text, font=font, fill=hc,
                                    anchor=anchor, tags="content")
        self.canvas.create_text(x, y, text=text, font=font, fill=fill,
                                anchor=anchor, tags="content")

    def _wrap(self, text, font, maxw, maxlines=2):
        """按像素宽度折行, 超出 maxlines 用省略号收尾 (二分取行边界, 少调 measure)。"""
        if not text:
            return []
        lines, i, n = [], 0, len(text)
        while i < n:
            lo, hi, best = i + 1, n, i + 1
            while lo <= hi:
                mid = (lo + hi) // 2
                if self._m(font, text[i:mid]) <= maxw:
                    best = mid
                    lo = mid + 1
                else:
                    hi = mid - 1
            if best >= n:
                lines.append(text[i:])
                break
            if len(lines) + 1 >= maxlines:
                cur = text[i:best]
                while cur and self._m(font, cur + "…") > maxw:
                    cur = cur[:-1]
                lines.append(cur + "…")
                break
            lines.append(text[i:best])
            i = best
        return lines

    def _m(self, font, text):
        """measure 缓存 (每帧几百次调用, 不缓存会很卡)。"""
        key = (id(font), text)
        c = self._mcache
        v = c.get(key)
        if v is None:
            try:
                v = font.measure(text)
            except Exception:
                v = 0
            if len(c) < 6000:
                c[key] = v
        return v

    def _glass_strength(self):
        """毛玻璃强度 0~1 (0 = 关)。"""
        try:
            return max(0.0, min(1.0, float(SETTINGS.get("glass", 0.55) or 0)))
        except Exception:
            return 0.0

    def _wall_plate(self, y0, y1, accent_hex=None, hot=False):
        """在行后面贴一块圆角毛玻璃: 文字始终读得清, 壁纸仍然透得出来。"""
        g = self._glass_strength()
        if g <= 0.02:
            return None
        sharp = getattr(self, "_bg_pil", None)
        blur = getattr(self, "_glass_pil", None)
        if sharp is None or blur is None:
            return None
        c = self.canvas
        try:
            cw = max(160, c.winfo_width())
            dy = c.winfo_rooty() - self.winfo_rooty()
            wy0 = int(dy + c.canvasy(y0))
            h = int(max(10, y1 - y0))
            if wy0 < 0:
                wy0, h = 0, h + wy0
            h = min(h, max(10, sharp.size[1] - wy0))
            box = (6, wy0, cw - 6, wy0 + h)
            accent = accent_hex or PALETTE["ui"]["text"]
            light_theme = _luma(BG_DARK) > 0.5
            pavg = _bg_avg(blur, (max(0, box[0]), max(0, box[1]),
                                  max(0, box[2]), max(0, box[3]))) or BG_DARK
            if hot:
                tint = accent
                amount = 0.15
                edge = _mix(accent, "#ffffff" if _luma(accent) < 0.62 else "#000000", 0.45)
            else:
                tint = BG_DARK
                amount = 0.26 + 0.10 * g
                edge = _mix(pavg, "#ffffff" if not light_theme else "#05070c", 0.52)
            img = _glass_plate(sharp, blur, box, rad=14, tint=tint, amount=amount,
                               strength=g, edge=edge,
                               mask_cache=getattr(self, "_mask_cache", None))
            if img is None:
                return None
            from PIL import ImageTk
            ph = ImageTk.PhotoImage(img)
            refs = getattr(self, "_plate_refs", None)
            if refs is None:
                refs = self._plate_refs = []
            refs.append(ph)
            if len(refs) > 80:
                del refs[:30]
            return c.create_image(box[0], wy0 - dy, anchor="nw", image=ph,
                                  tags=("glass",))
        except Exception:
            return None

    def _top_fade(self, hgt=34):
        """列表顶部一条"磨砂 → 清晰"的渐隐, 让标题栏的玻璃和列表照片无缝接上。"""
        if self._glass_strength() <= 0.02:
            return
        sharp = getattr(self, "_bg_pil", None)
        blur = getattr(self, "_glass_pil", None)
        if sharp is None or blur is None:
            return
        try:
            from PIL import Image as _Im
            from PIL import ImageTk
            c = self.canvas
            cw = max(160, c.winfo_width())
            dy = max(0, c.winfo_rooty() - self.winfo_rooty())
            box = (0, dy, cw, min(sharp.size[1], dy + hgt))
            if box[3] - box[1] < 4:
                return
            a = sharp.crop(box)          # 清晰
            b = blur.crop(box)           # 磨砂
            col = _Im.new("L", (1, a.size[1]))
            for yy in range(a.size[1]):
                col.putpixel((0, yy), int(255 * (1 - yy / float(max(1, a.size[1] - 1)))))
            grad = col.resize((a.size[0], a.size[1]))
            out = _Im.composite(b, a, grad)   # 顶部磨砂, 向下渐变回清晰
            ph = ImageTk.PhotoImage(out)
            self._fade_ref = ph
            c.create_image(0, 0, anchor="nw", image=ph, tags=("bgimg", "fade"))
        except Exception:
            pass

    def _draw_wall(self, secs, first=None):
        c = self.canvas
        c.delete("content")
        c.delete("hover")
        c.delete("glass")
        self._plate_refs = []
        if not hasattr(self, "_mask_cache"):
            self._mask_cache = {}
        self._row_bands = []
        self._hover_repo = None
        ui = PALETTE["ui"]
        f_head = self._font(19, "bold")
        f_headsub = self._font(10)
        f_rank = self._font(20, "bold")
        f_title = self._font(15, "bold")
        f_desc = self._font(10)
        f_meta = self._font(9)
        f_star = self._font(15, "bold")
        # 壁纸模式: 简介/说明提亮, 避免压在亮照片上看不清
        f_sub = _mix(ui["sub"], ui["text"], 0.35)
        f_desc_col = _mix(ui["sub"], ui["text"], 0.50)
        f_meta_col = _mix(ui["muted"], ui["text"], 0.45)

        W = max(760, c.winfo_width() - 30)
        x0 = 10
        xr = x0 + 12
        xt = x0 + 62
        xstar = W - 10
        desc_w = max(240, W - xt - 320)
        lh_title = f_title.metrics("linespace")
        lh_desc = f_desc.metrics("linespace")
        lh_meta = f_meta.metrics("linespace")
        lh_head = f_head.metrics("linespace")
        y = 4

        for fam, title, sub, rows in secs:
            p = PALETTE[fam]
            c.create_rectangle(x0, y + 9, x0 + 5, y + 9 + lh_head - 8,
                               fill=p["accent"], outline="", tags="content")
            h_ink, h_halo = self._band_ink(y, y + lh_head + 24, x0, x0 + 520)
            self._txt(x0 + 18, y + 4, title, f_head, p["accent"], halo_fill=h_halo)
            self._txt(x0 + 26 + self._m(f_head, title), y + 14, sub, f_headsub,
                      _mix(h_ink, "#ffffff" if _luma(h_ink) < 0.5 else "#000000", 0.30),
                      halo_fill=h_halo)
            y += lh_head + 34

            for rank, r in rows:
                top = y + 3
                rp = PALETTE[r["category"]]
                _hasd = bool(r["desc"] or (self._lang != "off" and r.get("desc_tr")))
                _showd = _hasd and self._details_on
                # 行高只按"真正要画的东西"算, 再把每一行文字垂直居中到它那一格里
                body = (lh_title + (2 * lh_desc if _showd else 0)
                        + (lh_meta if self._details_on else 0))
                est = top + body + 14
                ink, halo = self._band_ink(top - 4, est, xt, xt + desc_w)
                self._wall_plate(top - 7, est - 8)  # 毛玻璃底: 圆角内模糊, 上下留缝
                _away = "#ffffff" if _luma(ink) < 0.5 else "#000000"
                dcol = _mix(ink, _away, 0.16)
                mcol = _mix(ink, _away, 0.30)
                cy = top + lh_title // 2           # 标题/名次/星数共用一条中线
                if rank <= 3:
                    self._txt(xr + 6, cy, str(rank), f_rank, rp["hl"], anchor="w", halo=False)
                else:
                    self._txt(xr + 6, cy, str(rank), self._font(16, "bold"),
                              ui["muted"], anchor="w", halo=False)
                self._txt(xt, cy, r["full_name"], f_title, ink, anchor="w", halo=False)
                self._txt(xstar, cy, f"★ {r['stars']:,}", f_star, rp["hl"],
                          anchor="e", halo=False)
                y = top + lh_title

                desc = r["desc"] or ""
                if self._lang != "off" and r.get("desc_tr"):
                    desc = r["desc_tr"]
                if _showd:
                    for line in self._wrap(desc, f_desc, desc_w, 2):
                        self._txt(xt, y + lh_desc // 2, line, f_desc, dcol, anchor="w", halo=False)
                        y += lh_desc
                if self._details_on:
                    meta = (f"主语言: {r['lang'] or '未知'}　·　"
                            f"Forks: {r['forks']:,}　·　Issues: {r['issues']:,}")
                    self._txt(xt, y + lh_meta // 2, meta, f_meta, mcol, anchor="w", halo=False)
                    y += lh_meta
                y += 11
                self._row_bands.append((top - 4, est - 2, r))
                self._rows.append({"card": None, "pal": rp, "open": rank <= 10,
                                   "repo": r, "colors": None, "bg_widgets": None})

        total = max(y + 10, c.winfo_height())
        c.configure(scrollregion=(0, 0, W, total))
        c.delete("fade")
        self._top_fade()
        c.tag_raise("content")
        self.canvas.configure(bg=BG_DARK)
        self.after(0, self._force_repaint)

    def _force_repaint(self):
        """强制重画: Tk 的重绘排进 idle 队列, 主线程被定时器占住时会一直不刷。

        表现就是"点了按钮状态变了, 但屏幕上没反应, 动一下鼠标才跳出来"。
        """
        try:
            self.canvas.update_idletasks()
            self.canvas.yview_scroll(0, "units")   # 0 位移, 只为逼 canvas 整片重绘
        except tk.TclError:
            pass

    def _band_ink(self, y0, y1, x0=None, x1=None):
        """按照片在该行区域的平均亮度选深色/浅色文字 (+ 相反色描边)。"""
        ui = PALETTE["ui"]
        try:
            g = self._glass_strength()
            bl = getattr(self, "_glass_pil", None)
            v = bl if (g > 0.02 and bl is not None) else self._bg_view
            if v is None:
                return ui["text"], None
            c = self.canvas
            dy = c.winfo_rooty() - self.winfo_rooty()    # 画布在窗口里的纵向偏移
            wy0 = max(0.0, dy + c.canvasy(y0))
            wy1 = max(wy0 + 4, dy + c.canvasy(y1))
            vw, vh = v.size
            cw = max(1, c.winfo_width())
            sc = vw / float(cw)
            bx0 = int(max(0, (x0 if x0 is not None else 0) * sc))
            bx1 = int(min(vw, (x1 if x1 is not None else cw) * sc))
            by0 = int(max(0, wy0 * sc))
            by1 = int(min(vh, wy1 * sc))
            if bx1 - bx0 < 4 or by1 - by0 < 4:
                return ui["text"], None
            av = _bg_avg(v, (bx0, by0, bx1, by1))
            if not av:
                return ui["text"], None
            if g > 0.02 and bl is not None:
                # 文字真正压在"磨砂 + 染色"之后, 不能只按原图亮度决定黑白
                av = _mix(av, BG_DARK, min(0.60, 0.26 + 0.10 * g))
            light = _luma(av) > 0.56
        except Exception:
            return ui["text"], None
        ink = "#12161c" if light else "#f5f8ff"
        return ink, _shadow_of(ink)

    def _wall_hit(self, e):
        cy = self.canvas.canvasy(e.y)
        for y0, y1, r in getattr(self, "_row_bands", []):
            if y0 <= cy <= y1:
                return r
        return None

    def _wall_click(self, e):
        """壁纸模式: 单击整行直接开浏览器 (行是画布图元, 没有控件可挂事件)。"""
        if not self._wall_mode():
            return
        r = self._wall_hit(e)
        _dbg("wall_click canvas y=%d hit=%r" % (e.y, r and r.get("full_name")))
        if not r:
            return
        try:
            self.status.configure(text="已在浏览器打开 %s" % (r.get("full_name") or r.get("url")),
                                  fg=PALETTE["ui"]["ok"])
        except Exception:
            pass
        webbrowser.open(r["url"])
        return "break"

    def _wall_motion(self, e):
        if not self._wall_mode():
            return
        hit = self._wall_hit(e)
        if hit is getattr(self, "_hover_repo", None):
            return
        self._hover_repo = hit
        self.canvas.delete("hover")
        if hit is None:
            self.canvas.configure(cursor="")
            return
        self.canvas.configure(cursor="hand2")
        for y0, y1, r in self._row_bands:
            if r is hit:
                acc = PALETTE[r["category"]]["accent"]
                iid = self._wall_plate(y0, y1 + 2, accent_hex=acc, hot=True)
                if iid is not None:
                    try:
                        self.canvas.itemconfigure(iid, tags=("hover",))
                        self.canvas.tag_raise("hover", "glass")
                    except Exception:
                        pass
                # 左侧 2px 类别色条: 当前行有明确落点 (不是糊一团)
                self.canvas.create_rectangle(8, y0 + 3, 11, y1 - 1, fill=acc,
                                             outline="", tags="hover")
                self.canvas.tag_raise("hover", "glass")
                break

    def _wall_open(self, e):
        if not self._wall_mode():
            return
        r = self._wall_hit(e)
        if r:
            webbrowser.open(r["url"])

    def _wall_menu(self, e):
        if not self._wall_mode():
            return
        r = self._wall_hit(e)
        if r:
            self._menu(e, r["url"])

    def _week_label(self):
        d = datetime.now()
        return f"{d.isocalendar()[0]}年第{d.isocalendar()[1]}周"

    def _row(self, parent, cat, rank, r):
        p = PALETTE[cat]
        open_now = self._details_on
        if open_now:
            self._open_count += 1

        card = tk.Frame(parent, bg=p["card"], cursor="hand2")
        card.pack(fill="x", pady=7, padx=8)
        card.columnconfigure(2, weight=1)

        # 左侧类别色条
        tk.Frame(card, bg=p["accent"], width=6).grid(row=0, column=0, rowspan=4, sticky="ns")

        has_desc = bool(r["desc"])
        desc_text = r["desc"] or ""
        if self._lang != "off" and r.get("desc_tr"):
            desc_text = r["desc_tr"]
        bottom_pad = 0 if open_now else 15

        # 名次 / 奖牌
        medal = {1: "🥇", 2: "🥈", 3: "🥉"}.get(rank, f"{rank}")
        rank_font = ("Segoe UI Emoji", 20, "bold") if rank <= 3 else ("Microsoft YaHei UI", 22, "bold")
        rank_lbl = tk.Label(card, text=medal, font=rank_font,
                            bg=p["card"], fg=p["hl"] if rank <= 3 else PALETTE["ui"]["muted"], width=3)
        rank_lbl.grid(row=0, column=1, rowspan=2, sticky="w", padx=(16, 8), pady=(12, 0))

        # 仓库名
        title_lbl = tk.Label(card, text=r["full_name"], font=self._font(14, "bold"),
                             bg=p["card"], fg=p["text"], anchor="w")
        title_lbl.grid(row=0, column=2, sticky="w", padx=(6, 8),
                       pady=(14, 4 if (has_desc and open_now) else bottom_pad))

        # 分类小标签
        # 分类小标签 (单分类视图下整屏同类, 不再重复显示)
        show_chip = self._cat == "全部"
        if show_chip:
            chip = tk.Label(card, text=r["sub"], font=self._font(9, "bold"),
                            bg=p["badge"], fg=p["accent"], padx=10, pady=3, cursor="hand2")
            chip.grid(row=0, column=3, sticky="e", padx=(6, 12),
                      pady=(14, 4 if (has_desc and open_now) else bottom_pad))
            chip.bind("<Button-1>", lambda e, c=r["sub"]: (self._set_cat(c), "break")[1])


        # Star 数
        star_lbl = tk.Label(card, text=f"★ {r['stars']:,}", font=self._font(16, "bold"),
                            bg=p["card"], fg=p["hl"], anchor="e")
        star_lbl.grid(row=0, column=4, rowspan=2, sticky="e", padx=(4, 16))

        hover_pairs = [(card, "card"), (rank_lbl, "card"),
                       (title_lbl, "card"), (star_lbl, "card")]
        if show_chip:
            hover_pairs.append((chip, "badge"))
        click_targets = [card, rank_lbl, title_lbl, star_lbl]
        colors = {"card": p["card"], "badge": p["badge"], "hover": p["hover"],
                  "_card0": p["card"], "_badge0": p["badge"], "_accent": p["accent"]}

        # 简介 (收起详情时整行收成一行, 变化才看得见)
        if has_desc and open_now:
            desc_lbl = tk.Label(card, text=desc_text, font=self._font(10),
                                bg=p["card"], fg=p["sub"], anchor="w",
                                wraplength=self._wrap_desc, justify="left")
            desc_lbl.grid(row=1, column=2, columnspan=2, sticky="ew", padx=(6, 12),
                          pady=(0, bottom_pad))
            hover_pairs.append((desc_lbl, "card"))
            click_targets.append(desc_lbl)

        # 详情行
        if open_now:
            det = tk.Label(card, text=self._detail_text(r), font=self._font(10),
                           bg=p["badge"], fg=p["sub"], anchor="w", justify="left",
                           wraplength=self._wrap_det, padx=12, pady=6)
            det.grid(row=3, column=1, columnspan=4, sticky="ew", padx=(8, 16), pady=(10, 14))
            hover_pairs.append((det, "badge"))
            click_targets.append(det)

        # 悬停高亮: 补间到 hover 色再补间回来 (不是硬切)
        hkey = "hov%d" % id(card)

        def _fade(to_hover):
            def apply(t):
                for w, role in hover_pairs:
                    try:
                        base = colors[role]
                        w.configure(bg=(_mix(base, colors["hover"], t) if to_hover
                                        else _mix(colors["hover"], base, t)))
                    except tk.TclError:
                        pass
            self._tick.add(hkey, 0.16, apply, ease=_ease_out_quint)

        def _enter(_e):
            _fade(True)

        def _leave(_e):
            _fade(False)

        for w in click_targets:
            w.bind("<Enter>", _enter)
            w.bind("<Leave>", _leave)
            w.bind("<Double-Button-1>", lambda e, u=r["url"]: webbrowser.open(u))
            w.bind("<Button-3>", lambda e, u=r["url"]: self._menu(e, u))

        card.bind("<Button-1>", lambda e: card.focus_set())
        card.bind("<Return>", lambda e, u=r["url"]: webbrowser.open(u))
        self._rows.append({"card": card, "pal": p, "open": open_now, "repo": r,
                           "colors": colors, "bg_widgets": hover_pairs})

    def _detail_text(self, r):
        icon = LANG_ICON.get(r["lang"], "📦")
        lang = r["lang"] or "未知"
        return (f"{icon} 主语言: {lang}　·　🍴 Forks: {r['forks']:,}　·　"
                f"🐛 Issues: {r['issues']:,}　·　双击此行在浏览器打开项目页")

    def _menu(self, event, url):
        m = tk.Menu(self, tearoff=0)
        m.add_command(label="🌐 在浏览器中打开", command=lambda: webbrowser.open(url))
        m.add_command(label="📋 复制链接", command=lambda: self.clipboard_clear() or self.clipboard_append(url))
        m.tk_popup(event.x_root, event.y_root)

    # ── 底部操作 ───────────────────────────────────────────
    def open_all(self):
        opened = 0
        for row in self._rows:
            if row["open"]:
                webbrowser.open(row["repo"]["url"])
                opened += 1
                if opened >= 10:
                    break
        self.status.configure(text=f"已打开 {opened} 个浏览器标签页", fg=PALETTE["ui"]["ok"])

    def toggle_details(self):
        _dbg("toggle_details -> %s" % (not self._details_on))
        self._details_on = not self._details_on
        SETTINGS["details"] = self._details_on
        save_settings(SETTINGS)
        self.detail_btn.configure(text="▾  收起详情" if self._details_on else "▸  展开详情")
        try:
            self.status.configure(
                text=("已收起: 只留项目名与星数" if not self._details_on
                      else "已展开: 显示简介与详情"),
                fg=PALETTE["ui"]["ok"])
        except Exception:
            pass
        self._rerender_inplace()

    def _rerender_inplace(self):
        """按内存中最近一次数据重渲染(不重新请求网络)。"""
        repos = getattr(self, "_last_repos", None)
        if not repos:
            return
        self.render(repos)


def _grab_window(win):
    """把已经画好的窗口抓成 PIL 图 (逻辑像素)。抓不到返回 None。

    用 PrintWindow 直接向系统要窗口画面, 所以窗口可以设成 alpha=0 隐形地把内容
    "预绘制"一遍, 用户看不到这个过程; 之后的展开/收起只搬这张位图, 不再让 Tk
    每帧重排/重画 100+ 个控件 (实测那样一帧 20~40ms, 必掉帧)。
    """
    try:
        import ctypes as _ct
        import ctypes.wintypes as _wt

        from PIL import Image as _Image
    except Exception:
        return None
    try:
        u = _ct.windll.user32
        gdi = _ct.windll.gdi32
        hwnd = int(win.winfo_id())
        rect = _wt.RECT()
        if not u.GetWindowRect(hwnd, _ct.byref(rect)):
            return None
        bw, bh = rect.right - rect.left, rect.bottom - rect.top
        if bw < 8 or bh < 8:
            return None

        class _BMI(_ct.Structure):
            _fields_ = [("biSize", _ct.c_uint32), ("biWidth", _ct.c_int32),
                        ("biHeight", _ct.c_int32), ("biPlanes", _ct.c_uint16),
                        ("biBitCount", _ct.c_uint16), ("biCompression", _ct.c_uint32),
                        ("biSizeImage", _ct.c_uint32), ("biXPelsPerMeter", _ct.c_int32),
                        ("biYPelsPerMeter", _ct.c_int32), ("biClrUsed", _ct.c_uint32),
                        ("biClrImportant", _ct.c_uint32)]

        hdc = u.GetWindowDC(hwnd)
        md = gdi.CreateCompatibleDC(hdc)
        bmp = gdi.CreateCompatibleBitmap(hdc, bw, bh)
        gdi.SelectObject(md, bmp)
        u.PrintWindow(hwnd, md, 3)          # PW_CLIENTONLY | PW_RENDERFULLCONTENT
        bi = _BMI()
        bi.biSize = _ct.sizeof(_BMI)
        bi.biWidth, bi.biHeight = bw, -bh
        bi.biPlanes, bi.biBitCount = 1, 32
        buf = _ct.create_string_buffer(bw * bh * 4)
        gdi.GetDIBits(md, bmp, 0, bh, buf, _ct.byref(bi), 0)
        gdi.DeleteObject(bmp)
        gdi.DeleteDC(md)
        u.ReleaseDC(hwnd, hdc)
        img = _Image.frombuffer("RGBA", (bw, bh), buf, "raw", "BGRA", 0, 1).convert("RGB")
        # 抓到的可能是 DPI 物理像素, 统一按 Tk 自己的逻辑尺寸对齐
        lw, lh = win.winfo_width(), win.winfo_height()
        if lw > 8 and lh > 8 and (lw, lh) != (bw, bh):
            img = img.resize((lw, lh), _Image.LANCZOS)
        return img
    except Exception:
        return None


_SNAP_MEM = {}


def _snap_key():
    """面板位图的缓存键: 配置或尺寸变了才需要重画。"""
    return (SETTINGS.get("bg"), SETTINGS.get("tech"), SETTINGS.get("life"),
            SETTINGS.get("bgimg") or "", SETTINGS.get("bgdim"),
            SETTINGS.get("cardop"), bool(SETTINGS.get("details", True)),
            bool(SETTINGS.get("wallmode", True)))


class SettingsDialog(tk.Toplevel):
    """外观设置: 预设主题 / 自定义三色 / 背景图 / 显示选项。"""

    def __init__(self, app, origin=None, warm=False):
        super().__init__(app)
        try:
            self.attributes("-alpha", 0.0)   # 先全透明: 构建中途出错也不会有半截窗口露出来
        except Exception:
            pass
        self.app = app
        self._origin = origin            # 触发按钮的屏幕矩形 (x, y, w, h), 动画起点
        self.ui = ui = PALETTE["ui"]
        self.title("设置")
        self.overrideredirect(True)
        border = _mix(BG_DARK, PALETTE["ui"]["text"], 0.22)
        self.configure(bg=border)
        self.resizable(False, False)
        self.transient(app)
        # 故意不用 grab_set: 模态 grab 一旦面板被主窗口盖住, 主窗口就点不动了
        self._closing = False
        self._bind_id = app.bind("<Button-1>", self._app_clicked, add="+")
        self._shell = tk.Frame(self, bg=BG_DARK)
        self._shell.pack(fill="both", expand=True, padx=1, pady=1)

        panel = self._panel = _mix(BG_DARK, ui["text"], 0.06)
        head_bg = _mix(BG_DARK, ui["text"], 0.11)
        rule = _mix(BG_DARK, ui["text"], 0.18)
        accent = PALETTE["科技"]["accent"]

        self.var_bg = tk.StringVar(value=SETTINGS["bg"])
        self.var_tech = tk.StringVar(value=SETTINGS["tech"])
        self.var_life = tk.StringVar(value=SETTINGS["life"])
        self.var_det = tk.BooleanVar(value=bool(SETTINGS.get("details", True)))
        self.var_wall = tk.BooleanVar(value=bool(SETTINGS.get("wallmode", True)))
        self.var_img = tk.StringVar(value=SETTINGS.get("bgimg", "") or "")
        self.var_dim = tk.DoubleVar(value=float(SETTINGS.get("bgdim", 0.42)))
        self.var_op = tk.DoubleVar(value=float(SETTINGS.get("cardop", 0.86)))
        self.var_glass = tk.DoubleVar(value=float(SETTINGS.get("glass", 0.55)))

        # ── 标题条 ──────────────────────────────────────────
        head = tk.Frame(self._shell, bg=head_bg)
        head.pack(fill="x")
        tk.Label(head, text="◈  设置", font=("Microsoft YaHei UI", 13, "bold"),
                 bg=head_bg, fg=ui["text"]).pack(side="left", padx=(18, 0), pady=12)
        tk.Label(head, text="外观 · 壁纸 · 显示", font=("Microsoft YaHei UI", 9),
                 bg=head_bg, fg=ui["sub"]).pack(side="left", padx=(12, 0))
        xb = tk.Label(head, text="✕", font=("Microsoft YaHei UI", 12, "bold"),
                      bg=head_bg, fg=ui["sub"], padx=16, cursor="hand2")
        xb.pack(side="right")
        xb.bind("<Button-1>", lambda e: (self._close(), "break")[1])
        head.bind("<Button-1>", self._drag_start)
        head.bind("<B1-Motion>", self._drag_move)
        for _w in head.winfo_children():
            if _w is xb:
                continue                      # ✕ 保持关闭绑定, 不能被拖动覆盖
            _w.bind("<Button-1>", self._drag_start, add="+")
            _w.bind("<B1-Motion>", self._drag_move, add="+")
        xb.bind("<Enter>", lambda e: xb.configure(fg=ui["text"]))
        xb.bind("<Leave>", lambda e: xb.configure(fg=ui["sub"]))

        body = tk.Frame(self._shell, bg=BG_DARK)
        body.pack(fill="both", expand=True, padx=18, pady=(14, 4))

        def section(title, hint):
            box = tk.Frame(body, bg=panel, padx=16, pady=11, highlightthickness=1,
                            highlightbackground=_mix(BG_DARK, ui["text"], 0.14))
            box.pack(fill="x", pady=(0, 9))
            top = tk.Frame(box, bg=panel)
            top.pack(fill="x")
            tk.Frame(top, bg=accent, width=3, height=14).pack(side="left", padx=(0, 9))
            tk.Label(top, text=title, font=("Microsoft YaHei UI", 11, "bold"),
                     bg=panel, fg=ui["text"]).pack(side="left")
            if hint:
                tk.Label(top, text=hint, font=("Microsoft YaHei UI", 9), bg=panel,
                         fg=ui["sub"]).pack(side="left", padx=(10, 0))
            return box

        # ① 预设主题
        sec = section("预设主题", "10 套配色 · 点一下立即预览")
        grid = tk.Frame(sec, bg=panel)
        grid.pack(fill="x", pady=(11, 0))
        self._cells = []
        for i, (name, bg, tech, life) in enumerate(THEME_PRESETS):
            wrap = tk.Frame(grid, bg=panel, highlightthickness=2, highlightbackground=panel)
            wrap.grid(row=i // 5, column=i % 5, padx=4, pady=4, sticky="nsew")
            cell = tk.Frame(wrap, bg=bg, width=148, height=52, cursor="hand2")
            cell.pack(padx=1, pady=1)
            cell.pack_propagate(False)
            fg1 = _mix(bg, "#ffffff" if _luma(bg) < 0.6 else "#000000", 0.92)
            tk.Label(cell, text=name, font=("Microsoft YaHei UI", 9, "bold"),
                     bg=bg, fg=fg1).place(x=10, y=8)
            tk.Frame(cell, bg=tech, height=7).place(x=10, y=34, width=61)
            tk.Frame(cell, bg=life, height=7).place(x=75, y=34, width=61)
            tick = tk.Label(cell, text="", font=("Microsoft YaHei UI", 11, "bold"),
                            bg=bg, fg=fg1)
            tick.place(relx=1.0, x=-9, y=5, anchor="ne")
            self._cells.append((wrap, bg, tick))
            for w in [wrap, cell] + list(cell.winfo_children()):
                w.bind("<Button-1>", lambda e, b=bg, t=tech, l=life, c=cell:
                       self._tap_preset(c, b) or self._pick_preset(b, t, l))
        for c in range(5):
            grid.columnconfigure(c, weight=1)
        self._mark_selected()

        # ② 自定义颜色
        sec = section("自定义颜色", "也可以直接填色号 #rrggbb")
        for label, var in (("背景色", self.var_bg), ("科技主色", self.var_tech),
                           ("生活主色", self.var_life)):
            self._color_row(sec, label, var)

        # ③ 背景图
        sec = section("背景图", "选一张本地图片当壁纸, 卡片会自动取背后那块图的颜色")
        row = tk.Frame(sec, bg=panel)
        row.pack(fill="x", pady=(10, 0))
        thbox = tk.Frame(row, bg=_mix(panel, ui["text"], 0.16), padx=1, pady=1)
        thbox.pack(side="left", padx=(0, 10))
        self._thumb = tk.Label(thbox, text="无图", font=("Microsoft YaHei UI", 8), width=10, height=3,
                               bg=_mix(panel, "#000000", 0.28), fg=ui["sub"])
        self._thumb.pack()
        self._thumb_img = None

        ebox = tk.Frame(row, bg=_mix(panel, ui["text"], 0.20), padx=1, pady=1)
        ebox.pack(side="left")
        ent = tk.Entry(ebox, textvariable=self.var_img, font=("Microsoft YaHei UI", 9),
                       width=36, bg=ui["entry"], fg=ui["text"],
                       insertbackground=ui["text"], relief="flat", bd=0)
        ent.pack(ipady=5)
        ent.bind("<Button-1>", lambda e: ent.focus_set())
        self._press_btn(row, "选择图片…", self._pick_image, soft=True,
                        small=True).pack(side="left", padx=(9, 0))
        self._press_btn(row, "清除", self._clear_image, soft=True,
                        small=True).pack(side="left", padx=(6, 0))

        def _load_thumb(*_a):
            p = (self.var_img.get() or "").strip().strip('"')
            self._thumb_img = None
            if p and os.path.isfile(p):
                try:
                    from PIL import ImageTk
                    im = _bg_open(p).copy()
                    im.thumbnail((88, 54))
                    self._thumb_img = ImageTk.PhotoImage(im)
                    self._thumb.configure(image=self._thumb_img, text="", width=0, height=0)
                    return
                except Exception:
                    pass
            self._thumb.configure(image="", text="无图", width=10, height=3)

        self.var_img.trace_add("write", _load_thumb)
        _load_thumb()

        srow = tk.Frame(sec, bg=panel)
        srow.pack(fill="x", pady=(14, 0))
        self._slider(srow, "图片压暗", self.var_dim, 0.0, 0.85)
        self._slider(srow, "卡片不透明度", self.var_op, 0.45, 1.0)

        # ④ 加速: GitHub Token
        sec = section("加速 · GitHub Token（可选）",
                      "填了就让抓取速度翻 3 倍: 30 次/分钟 且 4 路并发；不填也能用")
        trow = tk.Frame(sec, bg=panel)
        trow.pack(fill="x", pady=(10, 0))
        tbox = tk.Frame(trow, bg=_mix(panel, ui["text"], 0.20), padx=1, pady=1)
        tbox.pack(side="left")
        self.var_token = getattr(self, "var_token", None) or tk.StringVar(
            value=SETTINGS.get("token", "") or "")
        tent = tk.Entry(tbox, textvariable=self.var_token, font=("Consolas", 9),
                        width=44, bg=ui["entry"], fg=ui["text"],
                        insertbackground=ui["text"], relief="flat", bd=0)
        tent.pack(ipady=5)
        tent.bind("<Button-1>", lambda e: tent.focus_set())
        tip = tk.Label(sec, bg=panel, fg=ui["muted"], justify="left", anchor="w",
                       font=("Microsoft YaHei UI", 9),
                       text="生成: github.com/settings/tokens → Generate new token (classic) → 勾 public_repo (只读也够)")
        tip.pack(fill="x", pady=(7, 0))
        trow2 = tk.Frame(sec, bg=panel)
        trow2.pack(fill="x", pady=(7, 0))
        self._press_btn(trow2, "测试 Token", self._test_token, soft=True,
                        small=True).pack(side="left")
        self._token_stat = tk.Label(trow2, text="", bg=panel, fg=ui["muted"],
                                    font=("Microsoft YaHei UI", 9), anchor="w")
        self._token_stat.pack(side="left", padx=(10, 0))

        # ⑤ 显示
        sec = section("显示", "")
        self._check(sec, "卡片默认展开详情行（简介 + 语言 / Star / Fork / Issue）", self.var_det)
        self._check(sec, "壁纸模式（文字直接压在图上, 卡片不挡背景）", self.var_wall)
        grow = tk.Frame(sec, bg=panel)
        grow.pack(fill="x", pady=(13, 2))
        self._slider(grow, "毛玻璃强度", self.var_glass, 0.0, 1.0)
        tk.Label(sec, text="每行垫一层圆角磨砂: 照片糊在里面还看得见, 文字不用描边也读得清。"
                           "0 = 关掉, 回到纯净的「字直接压照片」。",
                 font=("Microsoft YaHei UI", 9), bg=panel, fg=ui["sub"],
                 justify="left", wraplength=560).pack(fill="x", pady=(2, 2))

        # ── 底部按钮条 ──────────────────────────────────────
        foot = tk.Frame(self._shell, bg=BG_DARK)
        foot.pack(fill="x", padx=18, pady=(2, 16))
        tk.Frame(foot, bg=rule, height=1).pack(fill="x", pady=(0, 12))
        bar = tk.Frame(foot, bg=BG_DARK)
        bar.pack(fill="x")
        self._press_btn(bar, "恢复默认", self._reset, soft=True, small=True).pack(side="left")
        self._press_btn(bar, "✓  应用", self._apply).pack(side="right")
        self._press_btn(bar, "取消", self._close, soft=True).pack(side="right", padx=(0, 10))

        self.var_bg.trace_add("write", lambda *a: self._mark_selected())
        self.update_idletasks()
        # 内容都挂在 _shell 里 (1px 边), 直接量 _shell 才准
        w = self._shell.winfo_reqwidth() + 2
        h = self._shell.winfo_reqheight() + 2
        sh = self.winfo_screenheight()
        if h > sh - 80:
            h = sh - 80
        x = app.winfo_rootx() + (app.winfo_width() - w) // 2
        y = app.winfo_rooty() + max(24, (app.winfo_height() - h) // 2 - 20)
        y = max(8, min(y, sh - h - 60))
        x, y = max(0, x), max(0, y)
        self._final_geo = (x, y, w, h)
        if not self._origin:
            self._origin = (x + w // 2 - 70, y + 12, 140, 34)
        self._snap_img = None
        self._curtain = None
        # 先按最终尺寸 alpha=0 把内容"预绘制"一遍, 再抓成位图; 之后动画只搬位图。
        # 这样点下去看到的就是一张画好的面板在长大, 而不是控件一个一个冒出来。
        # 抓到的位图按配置缓存, 预热/二次打开就不用再画一遍。
        _key = (_snap_key(), w)
        if _key in _SNAP_MEM:
            self._snap_img = _SNAP_MEM[_key]
            w, h = self._snap_img.size                 # 用缓存位图的真实尺寸
            self._final_geo = (x, y, w, h)
        else:
            try:
                self.attributes("-alpha", 0.0)
            except Exception:
                pass
            try:
                self.geometry("%dx%d+%d+%d" % (w, h, x, y))
                self.update_idletasks()
                h2 = self._shell.winfo_reqheight() + 12   # pack 布局下的真实高度
                if h2 > h and h2 <= max(260, sh - 80):
                    h = h2
                    self.geometry("%dx%d+%d+%d" % (w, h, x, y))
                self.update()
                self._snap_img = _grab_window(self)
                if self._snap_img is not None:
                    if len(_SNAP_MEM) >= 4:
                        _SNAP_MEM.clear()
                    _SNAP_MEM[_key] = self._snap_img
            except Exception:
                self._snap_img = None
            self._final_geo = (x, y, w, h)
        ox, oy, ow, oh = self._origin                 # 再缩成按钮那么大
        try:
            self.attributes("-alpha", 0.0)            # 真窗口先隐身, 只让位图窗帘出场
        except Exception:
            pass
        try:
            self._shell.place(x=ow - w, y=0, width=w, height=h)
            self.geometry("%dx%d+%d+%d" % (ow, oh, ox, oy))
            self.update_idletasks()
        except Exception:
            pass
        self.bind("<Escape>", lambda e: self._close())
        self._ready = True                            # 构建完成标记 (异常时 open_settings 靠它清理)
        if warm:
            return                                    # 预热用: 画完抓完就扔, 不显示
        try:
            self.deiconify()                          # alpha=0, 先让窗口真的存在
        except Exception:
            pass
        self.after(16, self._anim_open)
        self.after(120, self._fit_height)

    def _anim_open(self):
        """iOS 那种过渡: 一张已经画好的位图从按钮矩形"拉"成大面板, 0.40s。"""
        self._opening = True
        img = getattr(self, "_snap_img", None)
        _dbg("anim_open snap=%s" % (None if img is None else img.size,))
        if img is None or not self._curtain_open(img):
            self._resize_open()
        try:
            self.lift()
            # 不再全局置顶: 只靠 transient(app) 保证在主窗口之上, 切到别的程序不会被压屏
        except Exception:
            pass

    def _curtain_open(self, img):
        """展开: 位图从按钮矩形长到面板矩形。"""
        return self._curtain_run(img, self._origin, self._final_geo, 0.40,
                                 self._curtain_settle, (0.10, 1.0))

    def _curtain_run(self, img, a, b, dur, done, alpha=(0.10, 1.0)):
        """用一张"窗帘"窗口放位图动画。

        关键改动: 窗口一次铺到起止矩形的并集之后就固定不动, 每帧只改画布上那
        张图的大小和位置; 窗口本身用色键透明 (magenta = 全透明), 所以露不出多
        余底色。实测改窗口尺寸一帧 12ms, 这套 6~7ms, 120fps 跑得动。
        """
        try:
            from PIL import Image as _Image
            from PIL import ImageTk as _ImageTk
            self._kill_curtain()          # 上一张窗帘(可能还在跑)先清掉, 否则留残影
            cur = tk.Toplevel(self)
        except Exception:
            return False
        ax, ay, aw, ah = a
        bx, by, bw, bh = b
        left, top = min(ax, bx), min(ay, by)
        right = max(ax + aw, bx + bw)
        bot = max(ay + ah, by + bh)
        pad = 2
        cw, ch = right - left + pad * 2, bot - top + pad * 2
        a0, a1 = alpha
        try:
            cur.overrideredirect(True)
            cur.attributes("-topmost", True)
            cur.configure(bg=_CURTAIN_KEY)
            cur.geometry("%dx%d+%d+%d" % (cw, ch, left - pad, top - pad))
            cv = tk.Canvas(cur, bg=_CURTAIN_KEY, highlightthickness=0, bd=0,
                           width=cw, height=ch)
            cv.pack(fill="both", expand=True)
            cur.update_idletasks()
        except Exception:
            try:
                cur.destroy()
            except Exception:
                pass
            return False
        hw = cur.winfo_id()
        ok = _layer_key(hw, int(255 * a0))
        _dbg("curtain layered=%s geo=%s" % (ok, cur.geometry()))
        if not ok:
            try:                                 # 拿不到分层窗口 -> 退回改尺寸那套
                cur.destroy()
            except Exception:
                pass
            return self._curtain_run_win(img, a, b, dur, done, alpha)
        self._curtain = cur
        try:
            self._curtains = list(getattr(self, "_curtains", [])) + [cur]
        except Exception:
            pass
        # 缩放源用半尺寸: 输出像素一样多, 但源小了, BILINEAR 快将近一倍
        hy = img.resize((max(1, bw // 2), max(1, bh // 2)), _Image.LANCZOS)
        st = {"ph": None, "pid": cv.create_image(pad, pad, anchor="nw")}

        def step(t):
            ww = max(8, int(round(aw + (bw - aw) * t)))
            hh = max(8, int(round(ah + (bh - ah) * t)))
            xx = int(round(ax + (bx - ax) * t)) - left + pad
            yy = int(round(ay + (by - ay) * t)) - top + pad
            try:
                ph = _ImageTk.PhotoImage(hy.resize((ww, hh), _Image.BILINEAR))
                cv.coords(st["pid"], xx, yy)
                cv.itemconfigure(st["pid"], image=ph)
                st["ph"] = ph                       # 保住引用, 否则图会消失
                _layer_alpha(hw, int(255 * (a0 + (a1 - a0) * min(1.0, t ** 1.1))))
                cur.update_idletasks()
                if t >= 0.999:                      # 落地即拆, 不留残影窗口
                    self._kill_one_curtain(cur)
            except Exception:
                pass

        try:
            self.app._tick.add("dlgcurtain-%d" % id(cur), dur, step, ease=_ease_in_out)
        except Exception:
            pass
        # 兜底: 万一补间被打断(连点/刷新), 动画时长后必定拆掉这个窗口
        try:
            self.after(int(dur * 1000) + 140, lambda c=cur: self._kill_one_curtain(c))
        except Exception:
            pass
        if done:
            self.after(int(dur * 1000) + 30, done)
        return True

    def _curtain_run_win(self, img, a, b, dur, done, alpha=(0.10, 1.0)):
        """兜底: 没有分层窗口时, 每帧改窗口尺寸 (一帧 ~12ms)。"""
        try:
            from PIL import Image as _Image
            from PIL import ImageTk as _ImageTk
            cur = tk.Toplevel(self)
        except Exception:
            return False
        ax, ay, aw, ah = a
        bx, by, bw, bh = b
        try:
            cur.overrideredirect(True)
            cur.attributes("-topmost", True)
            cur.attributes("-alpha", 0.0)
            cur.geometry("%dx%d+%d+%d" % (aw, ah, ax, ay))
            cv = tk.Canvas(cur, bg=BG_DARK, highlightthickness=0, bd=0)
            cv.pack(fill="both", expand=True)
        except Exception:
            try:
                cur.destroy()
            except Exception:
                pass
            return False
        self._curtain = cur
        st = {"ph": None, "pid": None}
        a0, a1 = alpha

        def step(t):
            ww = max(8, int(round(aw + (bw - aw) * t)))
            hh = max(8, int(round(ah + (bh - ah) * t)))
            xx = int(round(ax + (bx - ax) * t))
            yy = int(round(ay + (by - ay) * t))
            try:
                fr = img.resize((ww, hh), _Image.BILINEAR)
                ph = _ImageTk.PhotoImage(fr)
                cur.geometry("%dx%d+%d+%d" % (ww, hh, xx, yy))
                if st["pid"] is None:
                    st["pid"] = cv.create_image(0, 0, anchor="nw", image=ph)
                else:
                    cv.itemconfigure(st["pid"], image=ph)
                st["ph"] = ph                       # 保住引用, 否则图会消失
                cur.attributes("-alpha", a0 + (a1 - a0) * min(1.0, t ** 1.1)
                               if a1 > a0 else a0 + (a1 - a0) * min(1.0, t))
            except Exception:
                pass

        try:
            self.app._tick.add("dlgcurtain", dur, step, ease=_ease_in_out)
        except Exception:
            pass
        if done:
            self.after(int(dur * 1000) + 30, done)
        return True

    def _curtain_close(self):
        img = getattr(self, "_snap_img", None)
        if img is None:
            return False
        try:
            self.attributes("-alpha", 0.0)      # 真窗口先隐形, 只留位图在缩
        except Exception:
            pass
        return self._curtain_run(img, self._final_geo, self._origin, 0.26,
                                 None, (1.0, 0.0))

    def _curtain_settle(self):
        self._kill_curtain()
        try:
            self.attributes("-alpha", 1.0)      # 位图动画完了才把真窗口亮出来 (1ms)
        except Exception:
            pass
        self._settle_geo()

    def _kill_curtain(self):
        """销毁所有窗帘窗口 (拖拽/连点/开关叠加时不再残留半透明残影)。"""
        cur, self._curtain = getattr(self, "_curtain", None), None
        for w in list(getattr(self, "_curtains", [])) + ([cur] if cur is not None else []):
            try:
                if w.winfo_exists():
                    w.destroy()
            except Exception:
                pass
        self._curtains = []

    def _kill_one_curtain(self, win):
        try:
            if win.winfo_exists():
                win.destroy()
        except Exception:
            pass
        try:
            self._curtains = [w for w in getattr(self, "_curtains", []) if w is not win]
            if getattr(self, "_curtain", None) is win:
                self._curtain = None
        except Exception:
            pass

    def _resize_open(self):
        """兜底: 抓不到位图时退回"真实窗口缩放"的老动画。"""
        x, y, w, h = self._final_geo
        ox, oy, ow, oh = self._origin
        self._shell.pack_forget()                     # 改用 place 才能裁剪
        try:
            self.attributes("-alpha", 0.0)
        except Exception:
            pass

        def place_center(ww, hh):
            # 内容钉在窗口右上角: 按钮在右上, 于是看到的是标题/✕ 那一角"拉"出来
            try:
                self._shell.place(x=ww - w, y=0, width=w, height=h)
            except tk.TclError:
                pass

        place_center(ow, oh)

        def step(t):
            ww = int(round(ow + (w - ow) * t))
            hh = int(round(oh + (h - oh) * t))
            xx = int(round(ox + (x - ox) * t))
            yy = int(round(oy + (y - oy) * t))
            try:
                self.geometry("%dx%d+%d+%d" % (ww, hh, xx, yy))
                place_center(ww, hh)
                self.attributes("-alpha", 0.06 + 0.94 * min(1.0, t ** 1.1))
            except tk.TclError:
                pass

        self.app._tick.add("dlgopen", 0.38, step, ease=_ease_in_out)
        self.after(400, self._curtain_settle)
        try:
            self.grab_set()
            self.focus_force()
        except Exception:
            pass


    def _app_clicked(self, _e=None):
        """主窗口被点 -> 面板抬到最前 (不抢输入, 只是别被压住看不见)。"""
        try:
            if self.winfo_exists():
                self.lift()
        except Exception:
            pass

    def _fit_height(self):
        """真实布局完成后按内容再校准一次高度 (缩略图等异步撑高时不会被裁掉)。"""
        if getattr(self, "_opening", False):
            return                      # 开场动画期间由补间控制几何, 别抢
        try:
            self.update_idletasks()
            w = self._shell.winfo_reqwidth() + 2
            h = self._shell.winfo_reqheight() + 12
            x, y, _w0, h0 = self._final_geo
            if abs(h - h0) <= 2 and abs(w - _w0) <= 2:
                return
            sh = self.winfo_screenheight()
            if h > sh - 80:
                h = sh - 80
            y = max(8, min(y, sh - h - 60))
            self._final_geo = (x, y, w, h)
            self.geometry("%dx%d+%d+%d" % (w, h, x, y))
        except Exception:
            pass

    def _bind_focus(self):
        if getattr(self, "_focus_ids", None):
            return
        try:
            self._focus_ids = [
                (self.app, "<FocusOut>"),
                (self.app, "<FocusIn>"),
            ]
            self._focus_out_id = self.app.bind("<FocusOut>", self._on_app_focus_out, add="+")
            self._focus_in_id = self.app.bind("<FocusIn>", self._on_app_focus_in, add="+")
        except Exception:
            pass

    def _on_app_focus_out(self, _e=None):
        pass

    def _on_app_focus_in(self, _e=None):
        # 主窗口拿到焦点时把设置面板抬到主窗口之上, 但不做全局置顶
        try:
            self.lift()
        except Exception:
            pass

    def _drop_topmost(self):
        try:
            self.attributes("-topmost", False)
            self.lift()
        except tk.TclError:
            pass

    def _settle_geo(self):
        self._opening = False
        x, y, w, h = self._final_geo
        try:
            self._shell.place_forget()
            self._shell.pack(fill="both", expand=True, padx=1, pady=1)
            self.geometry("%dx%d+%d+%d" % (w, h, x, y))
            self.attributes("-alpha", 1.0)
        except tk.TclError:
            pass
        try:
            self.grab_set()                 # 模态: 但它一直在最上面, 不会显得"点不动"
        except tk.TclError:
            pass
        try:
            self.focus_force()
        except Exception:
            pass
        self.after(30, self._fit_height)

    def _drag_start(self, e):
        self._dx, self._dy = e.x_root - self.winfo_rootx(), e.y_root - self.winfo_rooty()

    def _drag_move(self, e):
        try:
            x = e.x_root - getattr(self, "_dx", 0)
            y = e.y_root - getattr(self, "_dy", 0)
            # 夹在屏幕内: 标题条始终留在屏幕上, 不会拖到看不见的地方
            sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
            w, h = self.winfo_width(), self.winfo_height()
            x = max(-w + 140, min(x, sw - 140))
            y = max(0, min(y, sh - 40))
            self.geometry("+%d+%d" % (x, y))
        except tk.TclError:
            pass

    # ── 控件工厂 ────────────────────────────────────────────
    def _press_btn(self, parent, text, cmd, soft=False, small=False):
        """按下即执行: 局部 grab 时 ButtonRelease 会丢, 只有 Button-1 稳定。"""
        ui, panel = self.ui, getattr(self, "_panel", BG_DARK)
        fs = ("Microsoft YaHei UI", 9) if small else ("Microsoft YaHei UI", 11, "bold")
        b = tk.Button(parent, text=text, font=fs, bd=0, relief="flat", cursor="hand2",
                      bg=panel if soft else ui["btn"],
                      fg=ui["sub"] if soft else ui["btn_fg"],
                      activebackground=ui["btn_soft_hover"] if soft else ui["btn_hover"],
                      activeforeground=ui["text"] if soft else ui["btn_fg"],
                      padx=12 if small else 22, pady=2 if small else 6,
                      highlightthickness=1 if soft else 0,
                      highlightbackground=ui.get("btn_edge", ui["btn_soft_hover"]))

        def fire(_e=None):
            try:
                cmd()
            except Exception:
                pass
            return "break"

        b.bind("<Button-1>", fire)

        bg0 = panel if soft else ui["btn"]
        fg0 = ui["sub"] if soft else ui["btn_fg"]
        bg1 = ui["btn_soft_hover"] if soft else ui["btn_hover"]
        fg1 = ui["text"] if soft else ui["btn_fg"]

        def tween(c0, c1, f0, f1, dur=0.14):
            key = "btn%d" % id(b)

            def apply(t):
                try:
                    b.configure(bg=_mix(c0, c1, t), fg=_mix(f0, f1, t))
                except tk.TclError:
                    pass

            self.app._tick.add(key, dur, apply, ease=_ease_out_quint)

        b.bind("<Enter>", lambda e: tween(bg0, bg1, fg0, fg1))
        b.bind("<Leave>", lambda e: tween(bg1, bg0, fg1, fg0))
        return b

    def _tap_preset(self, cell, bg):
        """色卡按下时短暂亮一下, 让点击有手感。"""
        hot = _mix(bg, "#ffffff", 0.22 if _luma(bg) < 0.6 else -0.0)
        hot = _mix(bg, "#000000", 0.0) if _luma(bg) >= 0.6 else hot

        def step(t):
            try:
                cell.configure(bg=_mix(hot, bg, t))
            except tk.TclError:
                pass

        self.app._tick.add("preset%d" % id(cell), 0.22, step, ease=_ease_out_quint)
        return False

    def _color_row(self, parent, label, var):
        ui, panel = self.ui, self._panel
        row = tk.Frame(parent, bg=panel)
        row.pack(fill="x", pady=4)
        tk.Label(row, text=label, font=("Microsoft YaHei UI", 10), bg=panel,
                 fg=ui["text"], width=7, anchor="w").pack(side="left")
        prev = tk.Frame(row, bg=var.get() or "#000000", width=26, height=20,
                        highlightthickness=1,
                        highlightbackground=_mix(panel, ui["text"], 0.3))
        prev.pack(side="left", padx=(4, 10))
        prev.pack_propagate(False)
        ent = tk.Entry(row, textvariable=var, font=("Consolas", 10), width=10,
                       bg=ui["entry"], fg=ui["text"], insertbackground=ui["text"],
                       relief="flat", bd=0)
        ent.pack(side="left", ipady=4, padx=(0, 9))
        ent.bind("<Button-1>", lambda e: ent.focus_set())

        def pick():
            c = colorchooser.askcolor(color=var.get() or "#000000", parent=self, title=label)
            if c and c[1]:
                var.set(c[1])
                prev.configure(bg=c[1])

        self._press_btn(row, "选择…", pick, soft=True, small=True).pack(side="left")
        var.trace_add("write", lambda *a: self._sync_prev(prev, var))

    def _slider(self, parent, label, var, lo, hi):
        """自绘滑杆: 圆头 + 填充轨道 (tk.Scale 在深色下几乎看不见)。"""
        ui, panel = self.ui, self._panel
        box = tk.Frame(parent, bg=panel)
        box.pack(side="left", padx=(0, 28))
        top = tk.Frame(box, bg=panel)
        top.pack(fill="x")
        tk.Label(top, text=label, font=("Microsoft YaHei UI", 9), bg=panel,
                 fg=ui["sub"]).pack(side="left")
        val = tk.StringVar(value=f"{float(var.get()):.2f}")
        tk.Label(top, textvariable=val, font=("Consolas", 9, "bold"), bg=panel,
                 fg=ui["text"]).pack(side="right")

        W, H, PD = 234, 20, 9
        cv = tk.Canvas(box, width=W, height=H, bg=panel, highlightthickness=0, bd=0)
        cv.pack(pady=(5, 0))
        accent = PALETTE["科技"]["accent"]
        track = _mix(panel, ui["text"], 0.24)

        def v_to_x(v):
            span = (hi - lo) or 1
            r = (float(v) - lo) / span
            return PD + max(0.0, min(1.0, r)) * (W - 2 * PD)

        def draw(*_a):
            try:
                cv.delete("all")
                y = H // 2
                cv.create_line(PD, y, W - PD, y, fill=track, width=5, capstyle="round")
                kx = v_to_x(var.get())
                cv.create_line(PD, y, kx, y, fill=accent, width=5, capstyle="round")
                cv.create_oval(kx - 7, y - 7, kx + 7, y + 7, fill=accent,
                               outline=_mix(panel, accent, 0.5), width=1)
                val.set(f"{float(var.get()):.2f}")
            except Exception:
                pass

        def on_drag(e):
            r = max(0.0, min(1.0, (e.x - PD) / float(W - 2 * PD)))
            v = lo + r * (hi - lo)
            var.set(round(round(v / 0.05) * 0.05, 2))
            return "break"

        cv.bind("<Button-1>", on_drag)
        cv.bind("<B1-Motion>", on_drag)
        var.trace_add("write", draw)
        draw()
        return cv

    def _check(self, parent, text, var):
        ui, panel = self.ui, self._panel
        row = tk.Frame(parent, bg=panel)
        row.pack(fill="x", pady=1)
        cb = tk.Checkbutton(row, text=text, variable=var, font=("Microsoft YaHei UI", 10),
                            bg=panel, fg=ui["text"], activebackground=panel,
                            activeforeground=ui["text"], selectcolor=ui["entry"],
                            bd=0, highlightthickness=0, anchor="w", cursor="hand2", pady=3)
        cb.pack(fill="x")
        cb.bind("<Button-1>", lambda e: (var.set(not var.get()), "break")[1])

    # ── 行为 ───────────────────────────────────────────────
    def _close(self, *_a):
        if getattr(self, "_closing", False):
            return
        self._closing = True
        try:
            self.grab_release()
        except Exception:
            pass
        try:
            if getattr(self.app, "_dlg", None) is self:
                self.app._dlg = None
        except Exception:
            pass
        self._opening = False
        if self._curtain_close():
            self.after(300, self._hard_destroy)
        else:
            self._resize_close()
            self.after(280, self._hard_destroy)

    def _resize_close(self):
        """兜底: 没有位图可搬时, 让真窗口自己一边缩一边淡出。"""
        def step(t):
            # 往回缩: 边走边收回到按钮矩形 (iOS 返回那种"缩回去") + 淡出
            try:
                x, y, w, h = self._final_geo
                ox, oy, ow, oh = self._origin
                ww = int(round(w + (ow - w) * t))
                hh = int(round(h + (oh - h) * t))
                xx = int(round(x + (ox - x) * t))
                yy = int(round(y + (oy - y) * t))
                self.geometry("%dx%d+%d+%d" % (max(8, ww), max(8, hh), xx, yy))
                self._shell.place_forget()
                self._shell.place(x=ww - w, y=0, width=w, height=h)
                self.attributes("-alpha", max(0.0, 1.0 - t))
            except tk.TclError:
                pass

        try:
            self.app._tick.add("dlgclose", 0.24, step, ease=_ease_in_out)
        except Exception:
            pass

    def _hard_destroy(self):
        self._kill_curtain()
        try:
            self.grab_release()
        except Exception:
            pass
        try:
            self.attributes("-topmost", False)
        except Exception:
            pass
        try:
            self.app.unbind("<Button-1>", self._bind_id)
        except Exception:
            pass
        try:
            self.destroy()
        except Exception:
            pass

    @staticmethod
    def _sync_prev(prev, var):
        try:
            v = (var.get() or "").strip()
            if re.fullmatch(r"#[0-9a-fA-F]{6}", v):
                prev.configure(bg=v)
        except Exception:
            pass

    def _mark_selected(self):
        """高亮当前色卡 (按背景色匹配) 并打勾。"""
        cur = (self.var_bg.get() or "").lower()
        accent = PALETTE["科技"]["accent"]
        for wrap, bg, tick in getattr(self, "_cells", []):
            on = bg.lower() == cur
            try:
                wrap.configure(highlightbackground=accent if on else self._panel)
                tick.configure(text="✓" if on else "")
            except Exception:
                pass

    def _pick_preset(self, bg, tech, life):
        self.var_bg.set(bg)
        self.var_tech.set(tech)
        self.var_life.set(life)
        self._mark_selected()

    def _pick_image(self):
        try:
            p = filedialog.askopenfilename(
                parent=self, title="选择背景图片",
                filetypes=[("图片", "*.png *.jpg *.jpeg *.bmp *.webp *.gif *.tif *.tiff"),
                           ("所有文件", "*.*")])
        except Exception:
            p = ""
        if p:
            self.var_img.set(p)
        try:
            self.lift()
        except Exception:
            pass

    def _clear_image(self):
        self.var_img.set("")

    def _reset(self):
        self.var_bg.set(DEFAULT_SETTINGS["bg"])
        self.var_tech.set(DEFAULT_SETTINGS["tech"])
        self.var_life.set(DEFAULT_SETTINGS["life"])
        self.var_det.set(True)
        self.var_wall.set(True)
        self.var_img.set("")
        self.var_dim.set(DEFAULT_SETTINGS["bgdim"])
        self.var_op.set(DEFAULT_SETTINGS["cardop"])
        self.var_glass.set(DEFAULT_SETTINGS["glass"])
        self._mark_selected()

    def _test_token(self):
        """「测试 Token」: 打一次 /rate_limit, 看配额和限速档位。"""
        t = (self.var_token.get() or "").strip()
        ui = PALETTE["ui"]

        def show(txt, color):
            try:
                self._token_stat.configure(text=txt, fg=color)
            except Exception:
                pass

        if not t:
            show("没填 Token", ui["muted"])
            return
        show("正在测试 …", ui["warn"])
        try:
            self.update_idletasks()
        except Exception:
            pass
        old = SETTINGS.get("token", "")
        SETTINGS["token"] = t
        try:
            d = _http_json("https://api.github.com/rate_limit", timeout=10)
            s = (d.get("resources") or {}).get("search") or {}
            lim, rem = int(s.get("limit") or 0), int(s.get("remaining") or 0)
            if lim >= 30:
                show(f"✅ 有效 · 搜索配额 {rem}/{lim} · 走 30 次/分钟 + 4 路并发", ui["ok"])
            else:
                show(f"⚠ 没生效 · 搜索配额 {rem}/{lim}（Token 可能无效）", ui["warn"])
        except RuntimeError as e:
            show(f"❌ {e}", ui["err"])
        except Exception as e:
            show(f"❌ {type(e).__name__}", ui["err"])
        finally:
            SETTINGS["token"] = old

    def _apply(self):
        def ok(v, fb):
            v = (v or "").strip()
            return v if re.fullmatch(r"#[0-9a-fA-F]{6}", v) else fb
        img = (self.var_img.get() or "").strip().strip('"')
        if img and not os.path.isfile(img):
            img = ""
        try:
            dim = max(0.0, min(0.85, float(self.var_dim.get())))
        except Exception:
            dim = DEFAULT_SETTINGS["bgdim"]
        try:
            op = max(0.45, min(1.0, float(self.var_op.get())))
        except Exception:
            op = DEFAULT_SETTINGS["cardop"]
        try:
            gl = max(0.0, min(1.0, float(self.var_glass.get())))
        except Exception:
            gl = DEFAULT_SETTINGS["glass"]
        new = {
            "bg": ok(self.var_bg.get(), DEFAULT_SETTINGS["bg"]),
            "tech": ok(self.var_tech.get(), DEFAULT_SETTINGS["tech"]),
            "life": ok(self.var_life.get(), DEFAULT_SETTINGS["life"]),
            "details": bool(self.var_det.get()),
            "wallmode": bool(self.var_wall.get()),
            "lang": SETTINGS.get("lang", "off"),
            "bgimg": img,
            "bgdim": round(dim, 2),
            "cardop": round(op, 2),
            "glass": round(gl, 2),
            "token": (self.var_token.get() or "").strip(),
        }
        self._close()
        self.after(10, lambda: self.app.apply_settings(new))


def main():
    app = TrendingApp()
    app.mainloop()


if __name__ == "__main__":
    main()
