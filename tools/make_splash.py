# -*- coding: utf-8 -*-
"""生成 PyInstaller 启动闪屏 (深色霓虹风, 与图标一致)。"""
import os

from PIL import Image, ImageDraw, ImageFilter, ImageFont

W, H = 560, 300
S = 2                      # 超采样
DEEP = (6, 9, 20)
DEEP2 = (12, 8, 30)
BLUE = (91, 157, 255)
CYAN = (0, 212, 255)
VIOLET = (139, 123, 255)
AMBER = (255, 200, 87)


def lerp(a, b, t):
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


def font(size, bold=True):
    for name in ("msyhbd.ttc" if bold else "msyh.ttc", "segoeuib.ttf", "arialbd.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except Exception:
            continue
    return ImageFont.load_default()


def radial(size, center, radius, color, power=2.0, strength=1.0):
    """径向辉光层 (返回 RGB 图, 供 screen 混合)。"""
    w, h = size
    layer = Image.new("RGB", (int(w), int(h)), (0, 0, 0))
    px = layer.load()
    cx, cy = center
    for y in range(int(h)):
        dy = (y - cy) / radius
        for x in range(int(w)):
            dx = (x - cx) / radius
            d = (dx * dx + dy * dy) ** 0.5
            if d >= 1.0:
                continue
            k = (1.0 - d) ** power * strength
            px[x, y] = (int(color[0] * k), int(color[1] * k), int(color[2] * k))
    return layer


def screen(base, layer):
    b = base.load()
    l = layer.load()
    w, h = base.size
    for y in range(int(h)):
        for x in range(int(w)):
            r1, g1, b1 = b[x, y]
            r2, g2, b2 = l[x, y]
            b[x, y] = (255 - (255 - r1) * (255 - r2) // 255,
                       255 - (255 - g1) * (255 - g2) // 255,
                       255 - (255 - b1) * (255 - b2) // 255)


img = Image.new("RGB", (W * S, H * S), DEEP)
d = ImageDraw.Draw(img)
w, h = img.size
for y in range(h):
    d.line([(0, y), (w, y)], fill=lerp(DEEP, DEEP2, y / h))

glow = Image.new("RGB", (w, h), (0, 0, 0))
for cx, cy, rad, col, pw, st in ((w * 0.18, h * 0.20, w * 0.55, BLUE, 2.2, 0.40),
                                 (w * 0.86, h * 0.92, w * 0.50, VIOLET, 2.2, 0.34),
                                 (w * 0.62, h * 0.10, w * 0.34, CYAN, 2.6, 0.20)):
    screen(glow, radial((w, h), (cx, cy), rad, col, pw, st))
screen(img, glow)

# 霓虹折线 (与图标同构, 压在文字下方)
pts = [(w * 0.06, h * 0.98), (w * 0.24, h * 0.80), (w * 0.40, h * 0.90),
       (w * 0.58, h * 0.68), (w * 0.94, h * 0.78)]
cols = [CYAN, BLUE, BLUE, VIOLET, AMBER]
layers = Image.new("RGB", (w, h), (0, 0, 0))
ld = ImageDraw.Draw(layers)
for i in range(len(pts) - 1):
    ld.line([pts[i], pts[i + 1]], fill=cols[i], width=int(9 * S))
layers = layers.filter(ImageFilter.GaussianBlur(14 * S / 2))
screen(img, layers)
d = ImageDraw.Draw(img)
for i in range(len(pts) - 1):
    d.line([pts[i], pts[i + 1]], fill=cols[i], width=int(4 * S))
    d.line([pts[i], pts[i + 1]], fill=(255, 255, 255), width=int(1.4 * S))

# 标题
d.text((w * 0.50, h * 0.30), "GitHub 每周 Star 榜", font=font(int(30 * S)),
       fill=(240, 245, 255), anchor="mm")
d.text((w * 0.50, h * 0.43), "TOP 10  ·  正在启动 …", font=font(int(13 * S), bold=False),
       fill=(150, 172, 210), anchor="mm")

# 细进度线 (静态, 只是视觉暗示)
d.line([(w * 0.30, h * 0.53), (w * 0.70, h * 0.53)], fill=(40, 52, 76), width=int(3 * S))
d.line([(w * 0.30, h * 0.53), (w * 0.56, h * 0.53)], fill=BLUE, width=int(3 * S))

img = img.resize((W, H), Image.LANCZOS)
_out = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "assets")
os.makedirs(_out, exist_ok=True)
img.save(os.path.join(_out, "splash.png"))
print("saved assets/splash.png", img.size)
