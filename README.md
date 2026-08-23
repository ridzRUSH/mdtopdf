# markmap-pdf

Turn a Markdown file into a **complete** [Markmap](https://markmap.js.org/) mind map
and export it as a high-quality PDF.

```bash
python markmap_to_pdf.py input.md
# -> input.pdf
```

No GUI, no server, no database — one command-line utility.

The map is produced by the **real Markmap engine** (`markmap-lib` + `markmap-view` + `d3`)
running inside headless Chromium, so the output is a genuine Markmap render, not a
look-alike tree drawn by hand.

---

## Install

```bash
npm install
```

```bash
pip install -r requirements.txt
```

`npm install` pulls in Markmap, D3 and Puppeteer (which downloads the Chromium build it
uses for rendering). The Python packages are only needed by the fallback pipelines — the
default path needs nothing but the standard library, and `npm install` is run
automatically on first use if `node_modules` is missing.

Requirements: **Python 3.9+** and **Node.js 18+**.

---

## Usage

```bash
python markmap_to_pdf.py input.md
python markmap_to_pdf.py input.md --output my-mindmap.pdf
python markmap_to_pdf.py input.md --scale 4
python markmap_to_pdf.py input.md --svg
python markmap_to_pdf.py input.md --svg --svg-output architecture.svg
python markmap_to_pdf.py input.md --page a4 --open
cat input.md | python markmap_to_pdf.py -
```

Output while running:

```
Reading Markdown...
Rendering Markmap...
Calculating complete SVG bounds...
SVG size: 3840 x 1720
Map: 589 nodes, 588 branches (landscape)
Generating PDF...
PDF exported successfully:

architecture.pdf
```

### Options

| Option | Meaning |
| --- | --- |
| `input` | Markdown file, or `-` to read Markdown from stdin |
| `--output`, `-o` | PDF output path (default: `<input>.pdf`) |
| `--svg` | also export the complete SVG (default: `<input>.svg`) |
| `--svg-output` | SVG output path (implies `--svg`) |
| `--svg-mode` | `portable` (default) or `native` — see [SVG output](#svg-output) |
| `--zoom` | enlarge the exported page by this factor (default: `1`) — see [Zoom depth](#zoom-depth) |
| `--scale` | raster fallback resolution multiplier: `1`, `2`, `4` (default), `8` |
| `--padding` | padding around the complete map, in SVG units (default: `40`) |
| `--page` | `auto` (default) or `a4` |
| `--engine` | `auto` (default), `chrome`, `cairosvg`, `svglib`, `raster` |
| `--open` | open the finished PDF in the default viewer |
| `--max-width` | wrap node text wider than N pixels (`0` = never wrap, default) |
| `--background` | page background colour (default: `#ffffff`) |
| `--timeout` | rendering timeout in seconds (default: `300`) |
| `--no-assets` | skip downloading the optional KaTeX / highlight.js styles (fully offline) |
| `--no-install` | never run `npm install` automatically |
| `--quiet`, `-q` | only report errors |

---

## The complete map, always

This is the point of the tool. The export is never a screenshot of a viewport.

1. Markmap renders the map in a real browser (real fonts, real text measurement).
2. The script asks the browser for the map's full bounding box via `getBBox()` on the
   map's root group — a value expressed in the group's own coordinate space, so the
   **current zoom level and pan position cannot influence it**.
3. The bounding box is widened by `--padding` (plus half the widest stroke, so thick
   branches can never be clipped) and written back as the SVG `viewBox`:

   ```
   viewBox = (minX - padding) (minY - padding) (width + 2*padding) (height + 2*padding)
   ```

4. The zoom/pan transform is removed, and the page is sized to exactly those dimensions
   before the PDF is printed.

A 500-node, 1,000-node, extremely wide or extremely tall map all come out whole.

---

## Zoom depth

The default PDF is **vector**: node labels are real text, branches are real curves, so
there is no resolution to run out of. Zoom to 1600 %, 6400 %, as far as your viewer
goes — it stays sharp, and the text stays selectable and searchable.

Two things can still get in the way, and both have a lever:

**Your viewer's zoom ceiling.** Some readers stop at 500 % or 6400 % of the page's
physical size. `--zoom N` makes the exported page N times larger, so the same ceiling
reveals N times more detail:

```bash
python markmap_to_pdf.py notes.md --zoom 4
```

The map is vector, so this costs nothing in quality — it is pure geometry. (With
`--engine raster` a larger page also means proportionally more pixels, so sharpness per
inch is unchanged.)

**The 200 in page-box limit.** No PDF page box may exceed 200 in on a side. Rather than
shrinking the map to fit, the script caps the box and writes a `/UserUnit` entry
declaring each unit proportionally bigger, so the map keeps its full size. Acrobat
honours `/UserUnit`; viewers that ignore it show a smaller page — still vector, still
sharp at any zoom. The script says so when this happens.

One thing `--zoom` cannot change: how large the text is *relative to the whole map*. A
tall map is a tall map — zoomed out you see the shape, zoomed in you read the leaves.

## Page size

`--page auto` (default) gives the PDF page the map's own dimensions and orientation:
a 3840 × 1720 map becomes a landscape page of that exact aspect ratio; a tall map
becomes a portrait page. Nothing is squeezed into A4 and nothing is cropped.

Chromium will not print a page larger than 200 in on a side. A map bigger than that is
scaled down proportionally onto a single page (the script says so when it happens) —
it is still complete, just smaller.

`--page a4` fits the whole map proportionally onto A4, choosing landscape or portrait to
match the map. The aspect ratio is never distorted; a very wide map simply ends up small.

---

## PDF pipelines

`--engine auto` (the default) tries these in order and reports when it falls through:

| Engine | Pipeline | Result |
| --- | --- | --- |
| `chrome` | complete SVG → Chromium print-to-PDF | **vector**, selectable text, best fidelity |
| `cairosvg` | complete SVG → CairoSVG | vector (needs the native Cairo library) |
| `svglib` | complete SVG → svglib + ReportLab | vector, pure Python |
| `raster` | complete SVG → PNG at `--scale` → PDF | high-resolution image |

Markmap draws node labels inside SVG `<foreignObject>` elements, which most SVG
libraries ignore. Before handing the map to CairoSVG or svglib, the script converts
every `foreignObject` into native SVG `<text>` using the geometry the browser actually
measured, and bakes computed colours into presentation attributes — so those pipelines
render the same map, with real text, rather than a page of empty branches.

The raster fallback renders the **complete** SVG (never the viewport) at `--scale`×
resolution and wraps it in a PDF page of the correct physical size, so it stays sharp
when zoomed. Very large maps have their scale reduced automatically to stay inside the
renderer's maximum image size; the script tells you when that happens.

---

## SVG output

`--svg` writes the complete map as SVG as well.

- `--svg-mode portable` (default) — `foreignObject` flattened into native `<text>`,
  colours resolved into attributes. Opens correctly in Inkscape, Illustrator, CairoSVG,
  svglib and browsers.
- `--svg-mode native` — Markmap's raw SVG with `foreignObject` intact. Highest fidelity
  in browsers, blank labels in tools that do not implement `foreignObject`.

Both contain the complete map with the full bounding box.

---

## Markdown support

Headings, nested headings, bullet lists, nested bullet lists, inline Markdown (bold,
italic, inline code, strikethrough, links), fenced code blocks with syntax highlighting,
KaTeX math, and YAML frontmatter `markmap:` options are all handled by `markmap-lib`
exactly as they are on markmap.js.org.

UTF-8 throughout: Hindi, CJK, Arabic and emoji render with the system fonts Chromium
finds. Long labels, deep nesting, wide trees and tall trees are never cropped.

Code highlighting and math need two small CSS files from a CDN; if you are offline they
are skipped (or pass `--no-assets`) and everything else still renders.

### One upstream behaviour worth knowing

When a heading contains both loose list items *and* deeper sub-headings, `markmap-lib`
keeps the sub-headings and drops those loose items:

```markdown
## Backend

- Python        <- dropped by Markmap
- FastAPI       <- dropped by Markmap

### Database

- PostgreSQL
```

That is Markmap's own parser, not this script — markmap.js.org produces the same tree.
Put such bullets under their own sub-heading (or below the sub-sections) if you need
them in the map.

---

## Files

```
markmap-pdf/
├── markmap_to_pdf.py   the CLI (contains the JavaScript renderer)
├── requirements.txt    Python dependencies
├── package.json        Node dependencies: markmap-lib, markmap-view, d3, puppeteer
├── README.md
└── sample.md           the example map used below
```

You never have to write or configure any JavaScript. On first run the script writes its
renderer to `.markmap_cache/renderer.mjs` next to `node_modules` and drives it with Node.
Both `node_modules/` and `.markmap_cache/` are generated — safe to delete, they come back.

---

## Try it

```bash
python markmap_to_pdf.py sample.md --svg
```

`sample.md` is the software-architecture example; it produces a landscape PDF around
760 × 387 units with 19 nodes, plus the matching SVG.

---

## Troubleshooting

**`Node.js is required ... but was not found on PATH`** — install Node.js 18+ from
<https://nodejs.org/> and reopen your terminal.

**`Missing Node packages`** — run `npm install` in this directory (the script tries this
for you unless `--no-install` is set).

**Chromium missing** — the script runs `npx puppeteer browsers install chrome` and
retries automatically.

**`CairoSVG is not usable (No module named 'cairosvg')`** — only relevant if you asked
for `--engine cairosvg` explicitly; `--engine auto` simply moves on to the next pipeline.

**Rendering timed out** — raise `--timeout` for very large maps.
