#!/usr/bin/env python3
"""Draw the 0Hero icon: a bold zero ring with an up arrow breaking through it."""
import sys
from PIL import Image, ImageDraw

S = 1024  # draw large, then downscale for smooth edges
OUT = sys.argv[1] if len(sys.argv) > 1 else 'zerohero.png'
SIZE = int(sys.argv[2]) if len(sys.argv) > 2 else 256
NAVY, NAVY_HI = (14, 22, 34, 255), (20, 32, 48, 255)
CYAN, GREEN = (56, 214, 255, 255), (51, 224, 106, 255)

img = Image.new('RGBA', (S, S), (0, 0, 0, 0))
d = ImageDraw.Draw(img)
d.rounded_rectangle((32, 32, S - 32, S - 32), radius=200, fill=NAVY)
d.rounded_rectangle((32, 32, S - 32, S - 32), radius=200, outline=CYAN, width=14)

# the zero
cx, cy = S // 2, S // 2 + 40
rx, ry, w = 240, 320, 90
d.ellipse((cx - rx, cy - ry, cx + rx, cy + ry), fill=CYAN)
d.ellipse((cx - rx + w, cy - ry + w, cx + rx - w, cy + ry - w), fill=NAVY)

# the arrow, with a navy halo so it sits in front of the ring
top, head_w, head_h, shaft_w = 110, 300, 230, 100
halo = 30
d.polygon([(cx, top - halo * 1.6), (cx + head_w // 2 + halo * 1.5, top + head_h + halo // 2),
           (cx - head_w // 2 - halo * 1.5, top + head_h + halo // 2)], fill=NAVY)
d.rectangle((cx - shaft_w // 2 - halo, top + head_h, cx + shaft_w // 2 + halo, cy + 150), fill=NAVY)
d.polygon([(cx, top), (cx + head_w // 2, top + head_h), (cx - head_w // 2, top + head_h)], fill=GREEN)
d.rectangle((cx - shaft_w // 2, top + head_h - 4, cx + shaft_w // 2, cy + 150), fill=GREEN)

img.resize((SIZE, SIZE), Image.LANCZOS).save(OUT)
print(OUT)
