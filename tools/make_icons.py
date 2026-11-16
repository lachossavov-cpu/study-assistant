#!/usr/bin/env python3
"""Draw the JARVIS icon set — arc-reactor ring, readable down to 16px.

  python tools/make_icons.py
"""
import math
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "server" / "static"

BG = (4, 7, 13, 255)
DEEP = (10, 22, 34, 255)
CYAN = (79, 216, 232, 255)
CYAN_SOFT = (79, 216, 232, 90)
CYAN_FAINT = (79, 216, 232, 45)

SS = 4  # supersample factor — draw big, shrink down, get clean edges


def draw(size: int, maskable: bool) -> Image.Image:
    """One icon. `maskable` fills the whole square and keeps art inside the safe zone."""
    s = size * SS
    img = Image.new("RGBA", (s, s), BG if maskable else (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    c = s / 2
    # maskable icons get cropped to a circle by the OS — keep the art at 80%
    r = c * (0.80 if maskable else 0.98)

    if not maskable:
        d.ellipse([c - r, c - r, c + r, c + r], fill=DEEP)
        d.ellipse([c - r, c - r, c + r, c + r], outline=CYAN_FAINT, width=max(1, int(s * 0.008)))

    # 60 tick marks, every 5th longer — same rhythm as the dashboard ring
    for i in range(60):
        a = i / 60 * math.tau - math.pi / 2
        r1 = r * 0.90
        r2 = r * (0.80 if i % 5 == 0 else 0.855)
        d.line([c + math.cos(a) * r1, c + math.sin(a) * r1,
                c + math.cos(a) * r2, c + math.sin(a) * r2],
               fill=CYAN_SOFT, width=max(1, int(s * 0.006)))

    # the main arc: a gap at the top, like a countdown mid-run
    ring_r = r * 0.66
    box = [c - ring_r, c - ring_r, c + ring_r, c + ring_r]
    w = max(2, int(s * 0.055))
    d.arc(box, start=-70, end=250, fill=CYAN, width=w)

    # inner halo + core
    core_r = r * 0.30
    d.ellipse([c - core_r, c - core_r, c + core_r, c + core_r], fill=CYAN_SOFT)
    core_r = r * 0.17
    d.ellipse([c - core_r, c - core_r, c + core_r, c + core_r], fill=CYAN)

    return img.resize((size, size), Image.LANCZOS)


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    made = []
    for size in (192, 512):
        p = OUT / f"icon-{size}.png"
        draw(size, maskable=False).save(p)
        made.append(p)
    p = OUT / "icon-512-maskable.png"
    draw(512, maskable=True).save(p)
    made.append(p)

    ico = ROOT / "jarvis.ico"
    base = draw(256, maskable=False)
    base.save(ico, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    made.append(ico)

    for p in made:
        print(f"{p.relative_to(ROOT)}  {p.stat().st_size / 1024:.1f} KB")


if __name__ == "__main__":
    main()
