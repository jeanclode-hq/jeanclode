"""Turn a traced ink drawing into a compact stroke list for the canvas renderer.

usage: svg2strokes.py <in.svg> <out.json>

The tracer paints in layers: dark shapes, then paper-coloured shapes on top to cut holes. Ink shapes are kept
with a per-stroke alpha for their tone; paper shapes are kept only when they sit inside an ink shape, as that
shape's cutout, so the renderer can move both together. Loose paper speckles are dropped.
"""
import json
import re
import sys

from svgelements import Matrix, Path

src, dst = sys.argv[1], sys.argv[2]
svg = open(src).read()
vb = re.search(r'viewBox="([^"]+)"', svg).group(1).split()
W, H = float(vb[2]), float(vb[3])

INK_MAX = 150  # mean channel value below which a fill is ink

strokes = []
for m in re.finditer(r'<path([^>]*)>', svg):
    attrs = m.group(1)
    fill = re.search(r'fill="([^"]+)"', attrs)
    if not fill or not fill.group(1).startswith('rgb('):
        continue
    r, g, b = (int(x) for x in re.findall(r'\d+', fill.group(1))[:3])
    avg = (r + g + b) / 3
    red = r > 150 and g < 130 and r - b > 80
    ink = red or avg < INK_MAX
    d = re.search(r' d="([^"]+)"', attrs).group(1)
    p = Path(d)
    tr = re.search(r'transform="translate\(([^,]+),([^)]+)\)"', attrs)
    if tr and (float(tr.group(1)) or float(tr.group(2))):
        p *= Matrix.translate(float(tr.group(1)), float(tr.group(2)))
    x0, y0, x1, y1 = p.bbox()
    w, h = x1 - x0, y1 - y0
    if w >= W * 0.98 and h >= H * 0.98:
        continue  # background rectangle
    if (w * h < 60 or max(w, h) < 7) if ink else w * h < 90:
        continue  # speck, invisible at display size
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2

    parent = -1
    if not ink:
        for j in range(len(strokes) - 1, -1, -1):
            s = strokes[j]
            if s['p'] != -1 or s['w'] * s['h'] <= w * h:
                continue
            if abs(s['x'] - cx) <= s['w'] / 2 and abs(s['y'] - cy) <= s['h'] / 2:
                parent = j
                break
        if parent == -1:
            continue

    p *= Matrix.translate(-cx, -cy)
    # integers are plenty: the drawing is 2816 wide and displays at a third of that
    d2 = re.sub(r'(-?\d+)\.\d+', r'\1', p.d())
    d2 = re.sub(r'\s+', ' ', d2).strip()
    strokes.append({
        'd': d2, 'x': round(cx, 1), 'y': round(cy, 1), 'w': round(w, 1), 'h': round(h, 1),
        'r': red, 'a': 1.0 if red else round(min(1.0, max(0.35, (240 - avg) / 180)), 2), 'p': parent,
    })

areas = sorted((s['w'] * s['h'] for s in strokes if s['p'] == -1), reverse=True)
big = areas[min(6, len(areas) - 1)] * 0.8 if areas else 0
for s in strokes:
    s['k'] = s['p'] == -1 and s['w'] * s['h'] >= big
out = {'w': W, 'h': H, 'strokes': strokes}
json.dump(out, open(dst, 'w'), separators=(',', ':'))
n_ink = sum(s['p'] == -1 for s in strokes)
print(f"{n_ink} ink, {len(strokes) - n_ink} cutouts, {sum(s['k'] for s in strokes)} anchors, {sum(s['r'] for s in strokes)} red, {len(json.dumps(out)) // 1024} KB")
