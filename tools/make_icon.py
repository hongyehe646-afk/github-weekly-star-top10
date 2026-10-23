# -*- coding: utf-8 -*-
r"""GitHub 鍛ㄦ Top10 搴旂敤鍥炬爣鐢熸垚鍣ㄣ€?
杩愯: python make_icon.py  鈫? 鐢熸垚 icon.ico / icon_256.png / icon_512.png
鎵撳寘: python -m PyInstaller --onefile --windowed --name GHTrendingTop10 \
          -i "%CD%\icon.ico" github_trending.py

璁捐: 娣辩┖鑳屾櫙 + 鍙岃壊姘涘洿鍏?+ 鐐归樀缃戞牸 + 闇撹櫣鍏夌涓婂崌鎶樼嚎锛堣摑鈫掗潚鈫掔传鈫掔惀鐝€锛?      + 鐞ョ弨绠ご + 鏄熻姃 + 闈掔传娓愬彉鎻忚竟, 鍦嗚鏂瑰舰瀹瑰櫒銆?"""
import math
import os
from PIL import Image, ImageDraw, ImageFilter

S = 1024
DEEP = (6, 9, 20)
DEEP2 = (12, 8, 30)
CYAN = (0, 226, 255)
BLUE = (58, 130, 255)
VIOLET = (168, 108, 255)
AMBER = (255, 199, 70)
WHITE = (240, 250, 255)


def lerp(a, b, t):
    return tuple(int(round(a[i] + (b[i] - a[i]) * t)) for i in range(3))


def rounded_mask(size, radius, ss=4):
    m = Image.new("L", (size * ss, size * ss), 0)
    d = ImageDraw.Draw(m)
    d.rounded_rectangle((0, 0, size * ss - 1, size * ss - 1), radius=radius * ss, fill=255)
    return m.resize((size, size), Image.LANCZOS)


def vgrad(size, top, bottom):
    img = Image.new("RGB", (1, size))
    d = ImageDraw.Draw(img)
    for y in range(size):
        d.point((0, y), lerp(top, bottom, y / max(1, size - 1)))
    return img.resize((size, size), Image.BILINEAR)


def radial_glow(center, radius, color, alpha):
    layer = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    cx, cy = center
    d.ellipse((cx - radius, cy - radius, cx + radius, cy + radius), fill=color + (alpha,))
    return layer.filter(ImageFilter.GaussianBlur(radius * 0.55))


def star_layer(cx, cy, r_out, r_in, fill, alpha=255, points=4):
    layer = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    poly = []
    for k in range(points * 2):
        ang = -math.pi / 2 + k * math.pi / points
        rr = r_out if k % 2 == 0 else r_in
        poly.append((cx + math.cos(ang) * rr, cy + math.sin(ang) * rr))
    d.polygon(poly, fill=fill + (alpha,))
    return layer


def main():
    base = vgrad(S, DEEP, DEEP2).convert("RGBA")
    base = Image.alpha_composite(base, radial_glow((S * 0.80, S * 0.16), S * 0.62, (0, 150, 255), 105))
    base = Image.alpha_composite(base, radial_glow((S * 0.16, S * 0.88), S * 0.55, (128, 70, 255), 95))

    dots = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    dd = ImageDraw.Draw(dots)
    gap = S // 20
    for gx in range(1, 20):
        for gy in range(1, 20):
            x, y = gx * gap, gy * gap
            r = S * 0.0034
            dd.ellipse((x - r, y - r, x + r, y + r), fill=(140, 200, 255, 30))
    base = Image.alpha_composite(base, dots)

    pts = [(S * 0.185, S * 0.715), (S * 0.355, S * 0.575),
           (S * 0.505, S * 0.645), (S * 0.655, S * 0.415), (S * 0.775, S * 0.315)]
    cols = [BLUE, CYAN, VIOLET, AMBER]

    tube = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    td = ImageDraw.Draw(tube)
    w = int(S * 0.072)
    for i in range(len(pts) - 1):
        td.line([pts[i], pts[i + 1]], fill=cols[i] + (255,), width=w, joint="curve")
        for p in (pts[i], pts[i + 1]):
            td.ellipse((p[0] - w / 2, p[1] - w / 2, p[0] + w / 2, p[1] + w / 2), fill=cols[i] + (255,))

    core = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    cd = ImageDraw.Draw(core)
    for i in range(len(pts) - 1):
        cd.line([pts[i], pts[i + 1]], fill=WHITE + (235,), width=int(S * 0.026), joint="curve")
        for p in (pts[i], pts[i + 1]):
            rr = S * 0.013
            cd.ellipse((p[0] - rr, p[1] - rr, p[0] + rr, p[1] + rr), fill=WHITE + (235,))

    for _ in range(3):
        base = Image.alpha_composite(base, tube.filter(ImageFilter.GaussianBlur(S * 0.045)))
    base = Image.alpha_composite(base, tube)
    base = Image.alpha_composite(base, core.filter(ImageFilter.GaussianBlur(S * 0.006)))
    base = Image.alpha_composite(base, core)

    tip = (S * 0.885, S * 0.205)
    arrow = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    ad = ImageDraw.Draw(arrow)
    ang = math.atan2(tip[1] - pts[-1][1], tip[0] - pts[-1][0])
    ln, half = S * 0.115, S * 0.062
    left = (tip[0] - math.cos(ang) * ln + math.sin(ang) * half,
            tip[1] - math.sin(ang) * ln - math.cos(ang) * half)
    right = (tip[0] - math.cos(ang) * ln - math.sin(ang) * half,
             tip[1] - math.sin(ang) * ln + math.cos(ang) * half)
    ad.polygon([tip, left, right], fill=AMBER + (255,))
    base = Image.alpha_composite(base, arrow.filter(ImageFilter.GaussianBlur(S * 0.03)))
    base = Image.alpha_composite(base, arrow)

    s1 = star_layer(S * 0.30, S * 0.255, S * 0.070, S * 0.016, (190, 232, 255), 215)
    base = Image.alpha_composite(base, s1.filter(ImageFilter.GaussianBlur(S * 0.02)))
    base = Image.alpha_composite(base, s1)
    base = Image.alpha_composite(base, star_layer(S * 0.62, S * 0.185, S * 0.038, S * 0.009,
                                                 (255, 240, 200), 190))

    ring = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    rd = ImageDraw.Draw(ring)
    steps = 160
    for k in range(steps):
        t = k / steps
        col = lerp((0, 220, 255), (150, 90, 255), t)
        a0 = -90 + t * 360
        a1 = -90 + (t + 1.02) * 360 / steps + 1.4
        rd.arc((S * 0.035, S * 0.035, S * 0.965, S * 0.965),
               start=a0, end=a1, fill=col + (170,), width=int(S * 0.012))
    base = Image.alpha_composite(base, ring.filter(ImageFilter.GaussianBlur(S * 0.012)))
    base = Image.alpha_composite(base, ring)

    base.putalpha(rounded_mask(S, int(S * 0.235)))
    out = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "assets")
    os.makedirs(out, exist_ok=True)
    base.resize((256, 256), Image.LANCZOS).save(os.path.join(out, "icon_256.png"))
    base.resize((512, 512), Image.LANCZOS).save(os.path.join(out, "icon_512.png"))
    base.resize((256, 256), Image.LANCZOS).save(
        os.path.join(out, "icon.ico"),
        sizes=[(256, 256), (128, 128), (64, 64), (48, 48), (32, 32), (16, 16)])
    print("saved assets/icon.ico / assets/icon_256.png / assets/icon_512.png")


if __name__ == "__main__":
    main()
