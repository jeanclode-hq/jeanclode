# Ink drawings

The landing and pricing artwork are traced ink drawings rendered stroke by stroke by
`app/components/InkCanvas.vue` from the JSON files in `public/ink/`.

Pipeline for a new drawing:

1. Generate the still (black ink strokes on warm paper, a vermilion accent or two).
2. Trace it to SVG with a layered tracer (svgai.org was used: it paints dark shapes, then
   paper-coloured shapes on top to cut holes).
3. `uv run --with svgelements python scripts/ink/svg2strokes.py in.svg public/ink/<name>.json`
4. Reference it from the page with a motion: `converge`, `lanes`, `flow`, `draw` or `align`.

`og.py` rasterises a stroke JSON into the 1200×630 social image:
`uv run --with svgelements --with pillow python scripts/ink/og.py public/ink/listens.json public/og-image.jpg`
