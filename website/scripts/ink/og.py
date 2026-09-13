"""Rasterise a stroke JSON into an image.

usage: og.py <in.json> <out.jpg> [width height]   (default 1200x630, the social card)
"""
import json, sys
from PIL import Image, ImageDraw
from svgelements import Path, Line, Close, Move, CubicBezier, QuadraticBezier, Arc

src, dst = sys.argv[1], sys.argv[2]
OW, OH = (int(sys.argv[3]), int(sys.argv[4])) if len(sys.argv) > 4 else (1200, 630)
data = json.load(open(src))
W, H = data['w'], data['h']
SS = 2
scale = OW * SS / W
img = Image.new('RGB', (int(W * scale), int(H * scale)), (247, 244, 238))
draw = ImageDraw.Draw(img)
paper, ink, red = (247, 244, 238), (23, 21, 15), (232, 71, 31)

def mix(c, a):
    return tuple(int(paper[i] * (1 - a) + c[i] * a) for i in range(3))

for s in data['strokes']:
    color = paper if s['p'] != -1 else red if s['r'] else mix(ink, s['a'])
    poly = []
    polys = []
    for seg in Path(s['d']):
        if isinstance(seg, Move):
            if len(poly) > 2: polys.append(poly)
            poly = [(seg.end.x, seg.end.y)]
        elif isinstance(seg, (Line, Close)):
            poly.append((seg.end.x, seg.end.y))
        else:
            for k in range(1, 7):
                pt = seg.point(k / 6)
                poly.append((pt.x, pt.y))
    if len(poly) > 2: polys.append(poly)
    for pl in polys:
        draw.polygon([((s['x'] + x) * scale, (s['y'] + y) * scale) for x, y in pl], fill=color)

# centre-crop to the target aspect, then downsample
if img.height < OH * SS:
    img = img.resize((round(img.width * OH * SS / img.height), OH * SS), Image.LANCZOS)
left = (img.width - OW * SS) // 2
top = (img.height - OH * SS) // 2
img = img.crop((left, top, left + OW * SS, top + OH * SS)).resize((OW, OH), Image.LANCZOS)
img.save(dst, quality=int(__import__("os").environ.get("OGQ", "82")), method=6) if dst.endswith(".webp") else img.save(dst, quality=86)
print(img.size)
