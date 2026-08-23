#!/usr/bin/env python3
"""
markmap_to_pdf.py -- Markdown -> Markmap mind map -> high-quality PDF.

The map is produced by the real Markmap engine (markmap-lib + markmap-view +
d3) running inside headless Chromium, so the output is a genuine Markmap
render rather than a look-alike tree.

The export always covers the COMPLETE map: after rendering, the script asks
the browser for the map's full bounding box (getBBox), rewrites the SVG
viewBox to that box plus padding and removes the zoom/pan transform.  The
current zoom level and pan position therefore have no effect on the result,
and nothing is ever cropped.

Pipelines, tried in order (see --engine):

    chrome    complete SVG -> Chromium print-to-PDF   (vector, default)
    cairosvg  complete SVG -> CairoSVG                (vector)
    svglib    complete SVG -> svglib + reportlab      (vector)
    raster    complete SVG -> PNG at --scale          (high-resolution image)

Examples:

    python markmap_to_pdf.py input.md
    python markmap_to_pdf.py input.md --output my-mindmap.pdf
    python markmap_to_pdf.py input.md --scale 8
    python markmap_to_pdf.py input.md --svg --svg-output architecture.svg
    cat input.md | python markmap_to_pdf.py -
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

__version__ = "1.0.0"

NODE_PACKAGES = ("markmap-lib", "markmap-view", "d3", "puppeteer")
ENGINE_CHAINS = {
    "auto": ("chrome", "cairosvg", "svglib", "raster"),
    "chrome": ("chrome",),
    "cairosvg": ("cairosvg",),
    "svglib": ("svglib",),
    "raster": ("raster",),
}
A4_PT = (595.28, 841.89)
PX_PER_IN = 96.0
# A PDF page box may not exceed 200 in on a side; bigger pages are expressed
# with /UserUnit rather than by shrinking the map.
MAX_PDF_PT = 200 * 72.0


class MarkmapError(Exception):
    """A user-facing failure: reported as a clean message, never a traceback."""


# --------------------------------------------------------------------------
# renderer.mjs -- written to disk next to node_modules and run by Node.
# This is the single source of truth for the JavaScript side.
# --------------------------------------------------------------------------
RENDERER_JS = r'''/**
 * markmap_renderer.mjs -- Node side of markmap_to_pdf.py
 *
 * Runs the real Markmap engine (markmap-lib + markmap-view + d3) inside a
 * headless Chromium instance driven by Puppeteer, then exports the COMPLETE
 * map (never the viewport) as SVG / vector PDF / high-resolution PNG.
 *
 * Usage:  node renderer.mjs <job.json>
 *
 * The job file is written by the Python driver; results are written back to
 * job.resultPath as JSON.  Nothing meaningful is printed on stdout.
 */
import fs from 'node:fs/promises';
import { Transformer } from 'markmap-lib';
import puppeteer from 'puppeteer';

/* ------------------------------------------------------------------ */
/* helpers                                                             */
/* ------------------------------------------------------------------ */

const PX_PER_IN = 96;
// Chromium refuses PDF pages larger than 200in and screenshots wider than
// ~16k device pixels; we scale down (never crop) when a map exceeds these.
const MAX_PDF_PX = 200 * PX_PER_IN;
const MAX_RASTER_SIDE = 16000;
const MAX_RASTER_PIXELS = 160e6;

const A4_PX = { width: 8.27 * PX_PER_IN, height: 11.69 * PX_PER_IN };

function fail(message) {
  throw new Error(message);
}

/**
 * Locate a file inside an installed package (e.g. d3/dist/d3.min.js).
 * Deep imports are blocked by some "exports" maps, so resolve the package
 * entry point and walk up to the directory holding its package.json.
 */
async function resolvePackageFile(pkg, relPath) {
  let dir;
  try {
    dir = new URL('./', import.meta.resolve(pkg));
  } catch {
    fail(
      'Cannot resolve the "' + pkg + '" package. Run "npm install" in the ' +
        'markmap-pdf project directory.',
    );
  }
  for (let i = 0; i < 12; i += 1) {
    const manifest = new URL('package.json', dir);
    try {
      const raw = JSON.parse(await fs.readFile(manifest, 'utf8'));
      if (raw.name === pkg) return new URL(relPath, dir);
    } catch {
      /* keep walking up */
    }
    const parent = new URL('../', dir);
    if (parent.href === dir.href) break;
    dir = parent;
  }
  fail('Cannot locate ' + pkg + '/' + relPath + '. Try re-running "npm install".');
}

/** Best-effort download of the CSS assets markmap asks for (KaTeX, hljs). */
async function fetchAssets(assets, timeoutMs) {
  const out = [];
  const hrefs = (assets && assets.styles ? assets.styles : [])
    .filter((s) => s.type === 'stylesheet' && s.data && s.data.href)
    .map((s) => s.data.href);
  for (const href of hrefs) {
    try {
      const ctrl = new AbortController();
      const timer = setTimeout(() => ctrl.abort(), timeoutMs);
      const res = await fetch(href, { signal: ctrl.signal });
      clearTimeout(timer);
      if (!res.ok) continue;
      let css = await res.text();
      // Re-point relative url(...) references at the CDN so fonts still load
      // once the stylesheet is inlined into the page / exported SVG.
      const base = href.slice(0, href.lastIndexOf('/') + 1);
      css = css.replace(
        /url\((['"]?)(?!data:|https?:|\/\/)([^'")]+)\1\)/g,
        (_m, q, url) => 'url(' + q + base + url + q + ')',
      );
      out.push(css);
    } catch {
      /* offline or blocked: markmap still renders, only optional styling is lost */
    }
  }
  return out;
}

/* ------------------------------------------------------------------ */
/* browser-side code (serialised into page.evaluate)                   */
/* ------------------------------------------------------------------ */

/** Render the markmap into #mindmap.  Runs inside the page. */
async function pageRender({ data, jsonOptions, overrides }) {
  const { Markmap, deriveOptions } = window.markmap;
  const svg = document.querySelector('#mindmap');
  const options = {
    ...deriveOptions(jsonOptions || {}),
    ...overrides,
    autoFit: false,
    duration: 0,
    zoom: false,
    pan: false,
  };
  const mm = new Markmap(svg, options);
  window.__mm = mm;
  await mm.setData(data);
  await mm.fit();
  if (document.fonts && document.fonts.ready) await document.fonts.ready;
  // Second pass once webfonts have landed: text metrics may have changed.
  await mm.renderData();
  return {
    nodes: svg.querySelectorAll('g.markmap-node').length,
    links: svg.querySelectorAll('path.markmap-link').length,
  };
}

/**
 * Replace the live viewBox with the map's COMPLETE bounding box and drop the
 * zoom/pan transform, so neither zoom nor pan can influence the export.
 * Runs inside the page.
 */
function pageFinalizeBounds({ padding }) {
  const svg = document.querySelector('#mindmap');
  const g = svg.querySelector('g'); // markmap's zoom container
  if (!g) throw new Error('markmap did not produce any content');

  // getBBox() on the zoom container is expressed in its own user space, so it
  // is unaffected by the current zoom/pan transform.
  const bb = g.getBBox();
  if (!isFinite(bb.width) || !isFinite(bb.height) || bb.width <= 0 || bb.height <= 0) {
    throw new Error('computed an empty bounding box');
  }

  // getBBox() ignores stroke width; make sure thick branches are never clipped.
  let halfStroke = 0;
  for (const el of svg.querySelectorAll('path, line, rect, circle')) {
    const w = parseFloat(getComputedStyle(el).strokeWidth);
    if (isFinite(w) && w / 2 > halfStroke) halfStroke = w / 2;
  }

  const pad = padding + halfStroke;
  const minX = bb.x - pad;
  const minY = bb.y - pad;
  const width = bb.width + pad * 2;
  const height = bb.height + pad * 2;

  g.removeAttribute('transform');
  svg.setAttribute('viewBox', minX + ' ' + minY + ' ' + width + ' ' + height);
  svg.setAttribute('width', String(width));
  svg.setAttribute('height', String(height));
  svg.setAttribute('xmlns', 'http://www.w3.org/2000/svg');
  svg.setAttribute('xmlns:xlink', 'http://www.w3.org/1999/xlink');
  svg.style.width = width + 'px';
  svg.style.height = height + 'px';
  svg.style.maxWidth = 'none';
  svg.style.maxHeight = 'none';
  return {
    minX,
    minY,
    width,
    height,
    contentWidth: bb.width,
    contentHeight: bb.height,
    padding: pad,
  };
}

/** Resize the page so the whole map is laid out 1:1 (or fitted to a page). */
function pageLayout({ mode, renderWidth, renderHeight, pageWidth, pageHeight, background }) {
  const svg = document.querySelector('#mindmap');
  const wrap = document.getElementById('wrap');
  svg.style.width = renderWidth + 'px';
  svg.style.height = renderHeight + 'px';
  document.documentElement.style.background = background;
  document.body.style.background = background;
  if (mode === 'page') {
    wrap.style.cssText =
      'position:relative;width:' + pageWidth + 'px;height:' + pageHeight + 'px;' +
      'display:flex;align-items:center;justify-content:center;overflow:hidden;' +
      'background:' + background + ';';
  } else {
    wrap.style.cssText =
      'position:relative;width:' + renderWidth + 'px;height:' + renderHeight + 'px;' +
      'overflow:hidden;background:' + background + ';';
  }
  document.body.style.margin = '0';
  document.body.style.overflow = 'hidden';
}

/**
 * Convert every <foreignObject> into native SVG <text>, using real measured
 * geometry from the browser.  Produces an SVG that renders identically in
 * tools with no foreignObject support (Inkscape, Illustrator, CairoSVG,
 * svglib), which is what the vector fallbacks need.  Runs inside the page.
 */
function pageFlatten() {
  const SVG_NS = 'http://www.w3.org/2000/svg';
  const svg = document.querySelector('#mindmap');
  const ctx = document.createElement('canvas').getContext('2d');
  const point = svg.createSVGPoint();

  // Screen pixels -> the user space of `owner`'s children.  Each markmap node
  // sits in its own translated <g>, so the mapping must be computed against
  // the element the generated <text> is actually inserted into.
  const mapper = (owner) => {
    const inv = owner.getScreenCTM().inverse();
    return (x, y) => {
      point.x = x;
      point.y = y;
      return point.matrixTransform(inv);
    };
  };

  // Split a text node into one run per visual line (handles wrapped text and
  // multi-line <pre> code blocks).
  const runsOf = (node) => {
    const range = document.createRange();
    range.selectNodeContents(node);
    const rects = Array.from(range.getClientRects()).filter((r) => r.width > 0 || r.height > 0);
    if (rects.length <= 1) {
      return rects.length ? [{ text: node.nodeValue, rect: rects[0] }] : [];
    }
    const runs = [];
    const text = node.nodeValue;
    const range2 = document.createRange();
    let current = null;
    for (let i = 0; i < text.length; i += 1) {
      range2.setStart(node, i);
      range2.setEnd(node, i + 1);
      const r = range2.getBoundingClientRect();
      if (r.width === 0 && r.height === 0) {
        if (current) current.text += text[i];
        continue;
      }
      if (current && Math.abs(r.top - current.rect.top) < 0.5) {
        current.text += text[i];
        current.rect = {
          top: Math.min(current.rect.top, r.top),
          left: current.rect.left,
          bottom: Math.max(current.rect.bottom, r.bottom),
          right: r.right,
        };
      } else {
        current = {
          text: text[i],
          rect: { top: r.top, left: r.left, bottom: r.bottom, right: r.right },
        };
        runs.push(current);
      }
    }
    return runs;
  };

  // Markmap paints via CSS, including custom properties such as
  // var(--markmap-circle-open-bg).  Bake the resolved values into
  // presentation attributes so renderers with no CSS engine -- CairoSVG,
  // svglib, Illustrator, Inkscape -- still get the right colours.
  const PAINT = [
    'fill',
    'stroke',
    'stroke-width',
    'stroke-linecap',
    'stroke-linejoin',
    'stroke-dasharray',
  ];
  for (const el of svg.querySelectorAll('path, line, circle, rect, polyline, polygon, ellipse')) {
    const cs = getComputedStyle(el);
    for (const prop of PAINT) {
      const value = cs.getPropertyValue(prop);
      if (value && value !== 'auto' && value.indexOf('var(') < 0) {
        el.setAttribute(prop, value.trim());
      }
    }
    const opacity = parseFloat(cs.opacity);
    if (isFinite(opacity) && opacity < 1) el.setAttribute('opacity', String(opacity));
  }

  let converted = 0;
  for (const fo of Array.from(svg.querySelectorAll('foreignObject'))) {
    const group = document.createElementNS(SVG_NS, 'g');
    group.setAttribute('class', 'markmap-text');
    const toUser = mapper(fo.parentNode);

    // Painted HTML backgrounds (code blocks, highlighted spans) become rects,
    // drawn first so the text keeps sitting on top of them.
    const elementWalker = document.createTreeWalker(fo, NodeFilter.SHOW_ELEMENT);
    let boxed;
    while ((boxed = elementWalker.nextNode())) {
      const cs = getComputedStyle(boxed);
      const bg = cs.backgroundColor;
      if (!bg || bg === 'transparent' || /rgba\(\s*0,\s*0,\s*0,\s*0\s*\)/.test(bg)) continue;
      const box = boxed.getBoundingClientRect();
      if (box.width <= 0 || box.height <= 0) continue;
      const topLeft = toUser(box.left, box.top);
      const bottomRight = toUser(box.right, box.bottom);
      const rect = document.createElementNS(SVG_NS, 'rect');
      rect.setAttribute('x', topLeft.x.toFixed(3));
      rect.setAttribute('y', topLeft.y.toFixed(3));
      rect.setAttribute('width', (bottomRight.x - topLeft.x).toFixed(3));
      rect.setAttribute('height', (bottomRight.y - topLeft.y).toFixed(3));
      const radius = parseFloat(cs.borderTopLeftRadius);
      if (isFinite(radius) && radius > 0) rect.setAttribute('rx', String(radius));
      rect.setAttribute('fill', bg);
      group.appendChild(rect);
    }
    const walker = document.createTreeWalker(fo, NodeFilter.SHOW_TEXT);
    let node;
    while ((node = walker.nextNode())) {
      if (!node.nodeValue || !node.nodeValue.trim()) continue;
      const el = node.parentElement;
      if (!el) continue;
      const cs = getComputedStyle(el);
      if (cs.display === 'none' || cs.visibility === 'hidden') continue;
      const fontSize = parseFloat(cs.fontSize) || 16;
      ctx.font = cs.fontStyle + ' ' + cs.fontWeight + ' ' + fontSize + 'px ' + cs.fontFamily;
      const metrics = ctx.measureText(node.nodeValue);
      const ascent =
        metrics.fontBoundingBoxAscent || metrics.actualBoundingBoxAscent || fontSize * 0.8;
      const descent =
        metrics.fontBoundingBoxDescent || metrics.actualBoundingBoxDescent || fontSize * 0.2;

      for (const run of runsOf(node)) {
        const topLeft = toUser(run.rect.left, run.rect.top);
        const bottomRight = toUser(run.rect.right, run.rect.bottom);
        const boxHeight = bottomRight.y - topLeft.y;
        // Centre the font box inside the measured line box, then drop to the
        // baseline.  Everything here is already in SVG user units.
        const baseline =
          topLeft.y + Math.max(0, (boxHeight - (ascent + descent)) / 2) + ascent;
        const text = document.createElementNS(SVG_NS, 'text');
        text.setAttribute('x', topLeft.x.toFixed(3));
        text.setAttribute('y', baseline.toFixed(3));
        text.setAttribute('font-family', cs.fontFamily);
        text.setAttribute('font-size', String(fontSize));
        if (cs.fontWeight && cs.fontWeight !== '400' && cs.fontWeight !== 'normal') {
          text.setAttribute('font-weight', cs.fontWeight);
        }
        if (cs.fontStyle && cs.fontStyle !== 'normal') {
          text.setAttribute('font-style', cs.fontStyle);
        }
        text.setAttribute('fill', cs.color || '#000');
        const decoration = cs.textDecorationLine || '';
        const decorations = [];
        if (decoration.indexOf('underline') >= 0) decorations.push('underline');
        if (decoration.indexOf('line-through') >= 0) decorations.push('line-through');
        if (decorations.length) text.setAttribute('text-decoration', decorations.join(' '));
        text.setAttribute('xml:space', 'preserve');
        text.textContent = run.text;
        group.appendChild(text);
        converted += 1;
      }
    }
    fo.parentNode.insertBefore(group, fo);
    fo.parentNode.removeChild(fo);
  }

  // Every painted value now lives in a presentation attribute, so drop the CSS
  // declarations that lean on custom properties.  Parsers without var()
  // support (svglib, CairoSVG) would otherwise warn and discard the colour.
  for (const styleEl of svg.querySelectorAll('style')) {
    styleEl.textContent = styleEl.textContent.replace(
      /[\w-]+\s*:\s*[^;{}]*var\([^;{}]*\)[^;{}]*;?/g,
      '',
    );
  }

  return { converted };
}

/** Serialise #mindmap as a standalone SVG document.  Runs inside the page. */
function pageSerialize({ extraCss }) {
  const svg = document.querySelector('#mindmap');
  const clone = svg.cloneNode(true);
  clone.removeAttribute('id');
  clone.removeAttribute('style');
  clone.setAttribute('xmlns', 'http://www.w3.org/2000/svg');
  clone.setAttribute('xmlns:xlink', 'http://www.w3.org/1999/xlink');
  const width = svg.getAttribute('width');
  const height = svg.getAttribute('height');
  if (width) clone.setAttribute('width', width);
  if (height) clone.setAttribute('height', height);
  if (extraCss) {
    const style = document.createElementNS('http://www.w3.org/2000/svg', 'style');
    style.textContent = extraCss;
    clone.insertBefore(style, clone.firstChild);
  }
  const xml = new XMLSerializer().serializeToString(clone);
  return '<?xml version="1.0" encoding="UTF-8" standalone="no"?>\n' + xml + '\n';
}

/* ------------------------------------------------------------------ */
/* main                                                                */
/* ------------------------------------------------------------------ */

async function main() {
  const jobPath = process.argv[2];
  if (!jobPath) fail('usage: node renderer.mjs <job.json>');
  const job = JSON.parse(await fs.readFile(jobPath, 'utf8'));

  const {
    markdown,
    padding = 40,
    scale = 4,
    zoom = 1,
    page: pageMode = 'auto',
    background = '#ffffff',
    outputs = {},
    options: userOptions = {},
    loadAssets = true,
    assetTimeoutMs = 8000,
    resultPath,
  } = job;

  /* 1. Markdown -> markmap tree (the real markmap-lib parser). */
  const transformer = new Transformer();
  const { root, features, frontmatter } = transformer.transform(markdown);
  if (!root || (!root.content && !(root.children && root.children.length))) {
    fail(
      'the Markdown has no headings or list items, so Markmap has nothing to map. ' +
        'Add at least one heading (# Title) or bullet (- item).',
    );
  }
  const assets = transformer.getUsedAssets(features);
  const assetCss = loadAssets ? await fetchAssets(assets, assetTimeoutMs) : [];

  /* 2. Render with markmap-view + d3 inside headless Chromium. */
  const [d3Url, viewUrl] = await Promise.all([
    resolvePackageFile('d3', 'dist/d3.min.js'),
    resolvePackageFile('markmap-view', 'dist/browser/index.js'),
  ]);
  const [d3Source, viewSource] = await Promise.all([
    fs.readFile(d3Url, 'utf8'),
    fs.readFile(viewUrl, 'utf8'),
  ]);

  const browser = await puppeteer.launch({
    headless: true,
    args: ['--no-sandbox', '--disable-dev-shm-usage', '--font-render-hinting=none'],
  });

  const result = { ok: true };
  try {
    const page = await browser.newPage();
    await page.setViewport({ width: 1600, height: 1200, deviceScaleFactor: 1 });
    page.setDefaultTimeout(job.timeoutMs || 120000);
    await page.setContent(
      '<!DOCTYPE html><html><head><meta charset="utf-8"><title>markmap</title>' +
        '<style>html,body{margin:0;padding:0;background:' + background + ';}' +
        '#wrap{position:relative;}svg#mindmap{display:block;width:1600px;height:1200px;}</style>' +
        '</head><body><div id="wrap"><svg id="mindmap"></svg></div></body></html>',
      { waitUntil: 'load' },
    );
    await page.addScriptTag({ content: d3Source });
    await page.addScriptTag({ content: viewSource });
    for (const css of assetCss) await page.addStyleTag({ content: css });

    const stats = await page.evaluate(pageRender, {
      data: root,
      jsonOptions: (frontmatter && frontmatter.markmap) || {},
      overrides: userOptions,
    });

    const bounds = await page.evaluate(pageFinalizeBounds, { padding });
    result.bounds = bounds;
    result.stats = stats;

    /* 3. Decide the physical page geometry (never distorts the map). */
    const { width, height } = bounds;
    let renderWidth = width;
    let renderHeight = height;
    let pageWidth = width;
    let pageHeight = height;
    let layoutMode = 'exact';
    let fitScale = 1;

    if (pageMode === 'a4') {
      const landscape = width > height;
      pageWidth = landscape ? A4_PX.height : A4_PX.width;
      pageHeight = landscape ? A4_PX.width : A4_PX.height;
      fitScale = Math.min(pageWidth / width, pageHeight / height);
      renderWidth = width * fitScale;
      renderHeight = height * fitScale;
      layoutMode = 'page';
      result.userUnit = 1; // an A4 page is A4; --zoom does not apply
      result.orientation = landscape ? 'landscape' : 'portrait';
    } else {
      // --zoom enlarges the exported page.  The map stays vector, so this is
      // pure geometry: an N x page shows N x more detail at a viewer's maximum
      // zoom, and the raster engine gets N x more pixels for free.
      const targetWidth = width * zoom;
      const targetHeight = height * zoom;
      // Chromium (like Acrobat) refuses a page box beyond 200in.  Instead of
      // shrinking the map, the box is capped here and the Python side sets
      // /UserUnit so the page keeps its full physical size.
      const limit = Math.min(1, MAX_PDF_PX / targetWidth, MAX_PDF_PX / targetHeight);
      renderWidth = targetWidth * limit;
      renderHeight = targetHeight * limit;
      pageWidth = renderWidth;
      pageHeight = renderHeight;
      fitScale = zoom * limit;
      result.userUnit = 1 / limit;
      result.orientation = width > height ? 'landscape' : 'portrait';
    }
    result.zoom = zoom;
    result.fitScale = fitScale;
    result.pageWidthPx = pageWidth;
    result.pageHeightPx = pageHeight;

    await page.evaluate(pageLayout, {
      mode: layoutMode,
      renderWidth,
      renderHeight,
      pageWidth,
      pageHeight,
      background,
    });

    /* 4. Native (foreignObject) SVG + vector PDF straight from Chromium. */
    const extraCss = assetCss.join('\n');
    if (outputs.nativeSvg) {
      const svgText = await page.evaluate(pageSerialize, { extraCss });
      await fs.writeFile(outputs.nativeSvg, svgText, 'utf8');
      result.nativeSvg = outputs.nativeSvg;
    }

    if (outputs.pdf) {
      const pdf = await page.pdf({
        width: pageWidth + 'px',
        height: pageHeight + 'px',
        printBackground: true,
        pageRanges: '1',
        margin: { top: '0', right: '0', bottom: '0', left: '0' },
        preferCSSPageSize: false,
      });
      await fs.writeFile(outputs.pdf, pdf);
      result.pdf = outputs.pdf;
      result.pdfWidthPt = (pageWidth * 72) / PX_PER_IN;
      result.pdfHeightPt = (pageHeight * 72) / PX_PER_IN;
    }

    /* 5. High-resolution raster of the COMPLETE map (fallback pipeline). */
    if (outputs.png) {
      let effective = Math.max(1, scale);
      const side = Math.max(pageWidth, pageHeight);
      effective = Math.min(effective, MAX_RASTER_SIDE / side);
      effective = Math.min(effective, Math.sqrt(MAX_RASTER_PIXELS / (pageWidth * pageHeight)));
      effective = Math.max(0.25, effective);
      await page.setViewport({
        width: Math.max(1, Math.ceil(pageWidth)),
        height: Math.max(1, Math.ceil(pageHeight)),
        deviceScaleFactor: effective,
      });
      const shot = await page.screenshot({
        type: 'png',
        clip: { x: 0, y: 0, width: pageWidth, height: pageHeight, scale: 1 },
        captureBeyondViewport: true,
      });
      await fs.writeFile(outputs.png, shot);
      result.png = outputs.png;
      result.rasterScale = effective;
      result.rasterRequestedScale = scale;
    }

    /* 6. Portable SVG: foreignObject flattened into native <text>. */
    if (outputs.portableSvg) {
      const flat = await page.evaluate(pageFlatten);
      const svgText = await page.evaluate(pageSerialize, { extraCss });
      await fs.writeFile(outputs.portableSvg, svgText, 'utf8');
      result.portableSvg = outputs.portableSvg;
      result.flattenedRuns = flat.converted;
    }
  } finally {
    await browser.close();
  }

  await fs.writeFile(resultPath, JSON.stringify(result), 'utf8');
}

main().catch(async (err) => {
  const payload = JSON.stringify({
    ok: false,
    error: String(err && err.message ? err.message : err),
    stack: String(err && err.stack ? err.stack : ''),
  });
  try {
    const jobPath = process.argv[2];
    const job = JSON.parse(await fs.readFile(jobPath, 'utf8'));
    if (job.resultPath) await fs.writeFile(job.resultPath, payload, 'utf8');
  } catch {
    /* fall through to stderr */
  }
  process.stderr.write(payload + '\n');
  process.exit(1);
});
'''


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------
def use_utf8_console() -> None:
    """Never fail while printing a Unicode path on a legacy code page."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):  # pragma: no cover
            pass


def log(message: str, quiet: bool = False) -> None:
    if not quiet:
        print(message, flush=True)


def human(value: float) -> str:
    """Render a dimension the way a person would write it."""
    return str(int(round(value)))


def open_file(path: Path) -> None:
    """Open a finished document with the platform's default viewer."""
    try:
        if sys.platform.startswith("win"):
            os.startfile(str(path))  # noqa: S606 - intentional, user asked
        elif sys.platform == "darwin":
            subprocess.run(["open", str(path)], check=False)
        else:
            subprocess.run(["xdg-open", str(path)], check=False)
    except Exception as exc:  # pragma: no cover - platform dependent
        print(f"Could not open {path}: {exc}", file=sys.stderr)


# --------------------------------------------------------------------------
# input
# --------------------------------------------------------------------------
def read_markdown(source: str) -> tuple[str, Path | None]:
    """Read UTF-8 Markdown from a file or from stdin ('-')."""
    if source == "-":
        data = sys.stdin.buffer.read()
        if not data.strip():
            raise MarkmapError("No Markdown received on stdin.")
        return decode(data, "<stdin>"), None

    path = Path(source).expanduser()
    if not path.exists():
        raise MarkmapError(f"Markdown file not found: {path}")
    if path.is_dir():
        raise MarkmapError(f"Expected a Markdown file but got a directory: {path}")
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise MarkmapError(f"Could not read {path}: {exc}") from exc
    if not data.strip():
        raise MarkmapError(f"Markdown file is empty: {path}")
    return decode(data, str(path)), path.resolve()


def decode(data: bytes, label: str) -> str:
    """Decode UTF-8 (tolerating a BOM) so Unicode, Hindi and emoji survive."""
    for encoding in ("utf-8-sig", "utf-8"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise MarkmapError(f"{label} is not valid UTF-8 text.")


# --------------------------------------------------------------------------
# node environment
# --------------------------------------------------------------------------
def find_project_dir() -> Path:
    """Locate the directory holding package.json / node_modules."""
    override = os.environ.get("MARKMAP_PDF_HOME")
    if override:
        return Path(override).expanduser().resolve()
    here = Path(__file__).resolve().parent
    for candidate in (here, *here.parents):
        if (candidate / "package.json").is_file() or (candidate / "node_modules").is_dir():
            return candidate
    return here


def require_node() -> str:
    node = shutil.which("node")
    if not node:
        raise MarkmapError(
            "Node.js is required to run the Markmap engine but was not found on PATH.\n"
            "Install Node.js 18 or newer from https://nodejs.org/ and try again."
        )
    try:
        out = subprocess.run(
            [node, "--version"], capture_output=True, text=True, check=False
        ).stdout.strip()
        major = int(out.lstrip("v").split(".")[0])
        if major < 18:
            raise MarkmapError(
                f"Node.js 18 or newer is required (found {out}). Please upgrade Node.js."
            )
    except (ValueError, IndexError):
        pass  # unknown version string: let the renderer be the judge
    return node


def ensure_node_modules(project: Path, auto_install: bool, quiet: bool) -> None:
    """Make sure the Markmap/Puppeteer packages are installed."""
    missing = [p for p in NODE_PACKAGES if not (project / "node_modules" / p).is_dir()]
    if not missing:
        return

    hint = (
        f"Missing Node packages: {', '.join(missing)}.\n"
        f"Run:  npm install    (inside {project})"
    )
    if not auto_install:
        raise MarkmapError(hint)
    if not (project / "package.json").is_file():
        raise MarkmapError(f"No package.json found in {project}.\n{hint}")

    npm = shutil.which("npm")
    if not npm:
        raise MarkmapError(f"npm was not found on PATH.\n{hint}")

    log("Installing Node dependencies (first run only)...", quiet)
    result = subprocess.run(
        [npm, "install", "--no-audit", "--no-fund"],
        cwd=str(project),
        text=True,
        capture_output=quiet,
        check=False,
    )
    if result.returncode != 0:
        detail = (result.stderr or "").strip()
        raise MarkmapError(f"npm install failed.\n{hint}\n{detail}")

    still_missing = [p for p in NODE_PACKAGES if not (project / "node_modules" / p).is_dir()]
    if still_missing:
        raise MarkmapError(f"npm install did not provide: {', '.join(still_missing)}.\n{hint}")


def ensure_renderer(project: Path) -> Path:
    """Write the bundled JavaScript renderer beside node_modules."""
    cache = project / ".markmap_cache"
    try:
        cache.mkdir(parents=True, exist_ok=True)
        target = cache / "renderer.mjs"
        if not target.is_file() or target.read_text(encoding="utf-8") != RENDERER_JS:
            target.write_text(RENDERER_JS, encoding="utf-8")
        return target
    except OSError as exc:
        raise MarkmapError(f"Could not write the JavaScript renderer into {cache}: {exc}") from exc


def install_chromium(project: Path, quiet: bool) -> bool:
    """Download the Chromium build Puppeteer expects."""
    npx = shutil.which("npx")
    if not npx:
        return False
    log("Downloading the Chromium build used for rendering...", quiet)
    result = subprocess.run(
        [npx, "--yes", "puppeteer", "browsers", "install", "chrome"],
        cwd=str(project),
        text=True,
        capture_output=quiet,
        check=False,
    )
    return result.returncode == 0


def run_renderer(
    node: str, renderer: Path, project: Path, job: dict, quiet: bool, timeout: float
) -> dict:
    """Run the Node renderer once and return its JSON result."""
    with tempfile.TemporaryDirectory(prefix="markmap-pdf-") as tmp:
        job_path = Path(tmp) / "job.json"
        result_path = Path(tmp) / "result.json"
        job = {**job, "resultPath": str(result_path)}
        job_path.write_text(json.dumps(job), encoding="utf-8")

        def invoke() -> subprocess.CompletedProcess:
            return subprocess.run(
                [node, str(renderer), str(job_path)],
                cwd=str(project),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                check=False,
            )

        try:
            proc = invoke()
            combined = f"{proc.stdout}\n{proc.stderr}"
            if proc.returncode != 0 and "Could not find Chrome" in combined:
                if install_chromium(project, quiet):
                    proc = invoke()
        except subprocess.TimeoutExpired as exc:
            raise MarkmapError(
                f"Rendering timed out after {timeout:.0f}s. "
                "Use --timeout to allow more time for very large maps."
            ) from exc

        payload: dict | None = None
        if result_path.is_file():
            try:
                payload = json.loads(result_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                payload = None

        if payload is None or not payload.get("ok"):
            message = (payload or {}).get("error") or (proc.stderr or "").strip()
            if not message:
                message = f"the Markmap renderer exited with code {proc.returncode}"
            raise MarkmapError(f"Markmap rendering failed: {message}")
        return payload


# --------------------------------------------------------------------------
# PDF back-ends
# --------------------------------------------------------------------------
def valid_pdf(path: Path) -> bool:
    try:
        with path.open("rb") as handle:
            return handle.read(5) == b"%PDF-"
    except OSError:
        return False


def place_on_a4(source: Path, destination: Path) -> None:
    """Centre an arbitrarily sized PDF page on A4, preserving aspect ratio."""
    try:
        from pypdf import PageObject, PdfReader, PdfWriter, Transformation
    except ImportError as exc:  # pragma: no cover - optional dependency
        raise MarkmapError(
            "--page a4 with this engine needs pypdf. Install it with: pip install pypdf"
        ) from exc

    reader = PdfReader(str(source))
    page = reader.pages[0]
    width = float(page.mediabox.width)
    height = float(page.mediabox.height)
    landscape = width > height
    page_w, page_h = (A4_PT[1], A4_PT[0]) if landscape else A4_PT
    scale = min(page_w / width, page_h / height)
    offset_x = (page_w - width * scale) / 2
    offset_y = (page_h - height * scale) / 2

    blank = PageObject.create_blank_page(width=page_w, height=page_h)
    page.add_transformation(Transformation().scale(scale, scale).translate(offset_x, offset_y))
    blank.merge_page(page)
    writer = PdfWriter()
    writer.add_page(blank)
    with destination.open("wb") as handle:
        writer.write(handle)


def finalize_pdf(
    source: Path, destination: Path, zoom: float = 1.0, base_user_unit: float = 1.0
) -> float:
    """
    Give the finished page its finalPhysical size.

    `zoom` enlarges the page (vector content, so it stays sharp at any zoom).
    A PDF page box may not exceed 200 in, so anything larger is expressed with
    /UserUnit -- the page box is capped and each unit is declared bigger, which
    keeps the map at full size instead of shrinking it.  Viewers that ignore
    /UserUnit simply show a smaller page; the content is vector either way.

    Returns the /UserUnit written (1.0 when none was needed).
    """
    try:
        from pypdf import PageObject, PdfReader, PdfWriter, Transformation
        from pypdf.generic import FloatObject, NameObject
    except ImportError as exc:
        if zoom != 1.0 or base_user_unit > 1.0001:
            raise MarkmapError(
                "--zoom and oversized pages need pypdf. Install it with: pip install pypdf"
            ) from exc
        shutil.copyfile(source, destination)
        return 1.0

    reader = PdfReader(str(source))
    page = reader.pages[0]
    width = float(page.mediabox.width)
    height = float(page.mediabox.height)

    target_width = width * zoom
    target_height = height * zoom
    box_limit = min(1.0, MAX_PDF_PT / target_width, MAX_PDF_PT / target_height)
    scale = zoom * box_limit
    user_unit = base_user_unit / box_limit

    if abs(scale - 1.0) < 1e-9 and user_unit <= 1.0001:
        shutil.copyfile(source, destination)
        return 1.0

    canvas = PageObject.create_blank_page(
        width=target_width * box_limit, height=target_height * box_limit
    )
    if abs(scale - 1.0) >= 1e-9:
        page.add_transformation(Transformation().scale(scale, scale))
    canvas.merge_page(page)
    if user_unit > 1.0001:
        canvas[NameObject("/UserUnit")] = FloatObject(user_unit)

    writer = PdfWriter()
    writer.add_page(canvas)
    with destination.open("wb") as handle:
        writer.write(handle)
    return user_unit


def pdf_from_cairosvg(svg: Path, destination: Path) -> None:
    try:
        import cairosvg
    except Exception as exc:  # ImportError, or missing cairo shared library
        raise MarkmapError(f"CairoSVG is not usable ({exc}).") from exc
    cairosvg.svg2pdf(url=svg.as_uri(), write_to=str(destination))


def pdf_from_svglib(svg: Path, destination: Path) -> None:
    try:
        from reportlab.graphics import renderPDF
        from svglib.svglib import svg2rlg
    except Exception as exc:
        raise MarkmapError(f"svglib/reportlab are not usable ({exc}).") from exc
    drawing = svg2rlg(str(svg))
    if drawing is None:
        raise MarkmapError("svglib could not parse the exported SVG.")
    renderPDF.drawToFile(drawing, str(destination))


def pdf_from_png(png: Path, destination: Path, dpi: float) -> None:
    """Wrap a high-resolution raster in a PDF page of the correct physical size."""
    try:
        import img2pdf  # lossless: the PNG is embedded as-is

        layout = img2pdf.get_fixed_dpi_layout_fun((dpi, dpi))
        with destination.open("wb") as handle:
            handle.write(img2pdf.convert(str(png), layout_fun=layout))
        return
    except ImportError:
        pass
    except Exception as exc:
        raise MarkmapError(f"img2pdf could not build the PDF ({exc}).") from exc

    try:
        from PIL import Image
    except ImportError as exc:
        raise MarkmapError(
            "The raster fallback needs Pillow or img2pdf. Install with: "
            "pip install -r requirements.txt"
        ) from exc

    with Image.open(png) as image:
        image.load()
        if image.mode in ("RGBA", "LA", "P"):
            background = Image.new("RGB", image.size, (255, 255, 255))
            background.paste(image, mask=image.convert("RGBA").split()[-1])
            image = background
        elif image.mode != "RGB":
            image = image.convert("RGB")
        image.save(str(destination), "PDF", resolution=dpi)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="markmap_to_pdf.py",
        description=(
            "Convert a Markdown file into a complete Markmap mind map exported as a "
            "high-quality PDF (and optionally SVG)."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  python markmap_to_pdf.py architecture.md\n"
            "  python markmap_to_pdf.py architecture.md --output architecture.pdf\n"
            "  python markmap_to_pdf.py architecture.md --scale 8 --engine raster\n"
            "  python markmap_to_pdf.py architecture.md --svg --svg-output architecture.svg\n"
            "  cat architecture.md | python markmap_to_pdf.py -\n"
        ),
    )
    parser.add_argument("input", help="Markdown file to convert, or '-' to read stdin")
    parser.add_argument("--output", "-o", help="PDF output path (default: <input>.pdf)")
    parser.add_argument("--svg", action="store_true", help="also export the complete SVG")
    parser.add_argument("--svg-output", help="SVG output path (implies --svg)")
    parser.add_argument(
        "--svg-mode",
        choices=("portable", "native"),
        default="portable",
        help=(
            "portable: foreignObject text converted to native SVG <text> so the file "
            "opens correctly everywhere (default); native: markmap's raw SVG"
        ),
    )
    parser.add_argument(
        "--scale",
        type=float,
        default=4.0,
        help="raster fallback resolution multiplier, e.g. 1 2 4 8 (default: 4)",
    )
    parser.add_argument(
        "--zoom",
        type=float,
        default=1.0,
        help=(
            "enlarge the exported page by this factor, e.g. 4 or 10 (default: 1). "
            "The map stays vector, so a bigger page means a viewer's maximum zoom "
            "reveals proportionally more detail. Ignored with --page a4"
        ),
    )
    parser.add_argument(
        "--padding",
        type=float,
        default=40.0,
        help="padding added around the complete map, in SVG units (default: 40)",
    )
    parser.add_argument(
        "--page",
        choices=("auto", "a4"),
        default="auto",
        help="auto: page matches the map's own size and orientation (default); "
        "a4: fit the whole map proportionally on A4",
    )
    parser.add_argument(
        "--engine",
        choices=tuple(ENGINE_CHAINS),
        default="auto",
        help="PDF pipeline: auto (default) tries chrome, cairosvg, svglib, then raster",
    )
    parser.add_argument("--open", action="store_true", help="open the PDF when it is ready")
    parser.add_argument(
        "--max-width",
        type=int,
        default=0,
        help="wrap node text wider than this many pixels (0 = never wrap, default)",
    )
    parser.add_argument(
        "--background", default="#ffffff", help="page background colour (default: #ffffff)"
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=300.0,
        help="rendering timeout in seconds (default: 300)",
    )
    parser.add_argument(
        "--no-assets",
        action="store_true",
        help="skip downloading optional KaTeX/highlight.js styles (fully offline)",
    )
    parser.add_argument(
        "--no-install",
        action="store_true",
        help="never run npm install automatically",
    )
    parser.add_argument("--quiet", "-q", action="store_true", help="only report errors")
    parser.add_argument("--version", action="version", version=f"markmap_to_pdf {__version__}")
    return parser


def resolve_outputs(args: argparse.Namespace, source: Path | None) -> tuple[Path, Path | None]:
    stem = source.with_suffix("") if source else Path.cwd() / "mindmap"
    pdf_path = Path(args.output).expanduser() if args.output else stem.with_suffix(".pdf")
    svg_path: Path | None = None
    if args.svg_output:
        svg_path = Path(args.svg_output).expanduser()
    elif args.svg:
        svg_path = stem.with_suffix(".svg")
    return pdf_path.resolve(), svg_path.resolve() if svg_path else None


def validate(args: argparse.Namespace) -> None:
    if not 0.1 <= args.scale <= 16:
        raise MarkmapError("--scale must be between 0.1 and 16.")
    if not 0.1 <= args.zoom <= 200:
        raise MarkmapError("--zoom must be between 0.1 and 200.")
    if args.padding < 0:
        raise MarkmapError("--padding cannot be negative.")
    if args.max_width < 0:
        raise MarkmapError("--max-width cannot be negative.")
    if args.timeout <= 0:
        raise MarkmapError("--timeout must be positive.")


def convert(args: argparse.Namespace) -> Path:
    validate(args)
    quiet = args.quiet

    log("Reading Markdown...", quiet)
    markdown, source = read_markdown(args.input)
    pdf_path, svg_path = resolve_outputs(args, source)
    for target in (pdf_path, svg_path):
        if target:
            target.parent.mkdir(parents=True, exist_ok=True)

    project = find_project_dir()
    node = require_node()
    ensure_node_modules(project, auto_install=not args.no_install, quiet=quiet)
    renderer = ensure_renderer(project)

    chain = ENGINE_CHAINS[args.engine]
    strict = args.engine != "auto"

    with tempfile.TemporaryDirectory(prefix="markmap-out-") as tmp:
        work = Path(tmp)
        native_svg = work / "map-native.svg"
        portable_svg = work / "map-portable.svg"
        chrome_pdf = work / "map-chrome.pdf"
        raster_png = work / "map.png"

        # Ask the renderer only for what this run actually needs: flattening the
        # map into native <text> is measurable work on a map with 1000+ nodes,
        # and the vector fallbacks are rarely reached.
        wants_native_svg = svg_path is not None and args.svg_mode == "native"
        wants_portable_svg = (svg_path is not None and args.svg_mode == "portable") or chain[
            0
        ] in ("cairosvg", "svglib")

        outputs: dict[str, str] = {}
        if wants_native_svg:
            outputs["nativeSvg"] = str(native_svg)
        if wants_portable_svg:
            outputs["portableSvg"] = str(portable_svg)
        if chain[0] == "chrome":
            outputs["pdf"] = str(chrome_pdf)
        if chain[0] == "raster":
            outputs["png"] = str(raster_png)

        job = {
            "markdown": markdown,
            "padding": args.padding,
            "scale": args.scale,
            "zoom": args.zoom,
            "page": args.page,
            "background": args.background,
            "outputs": outputs,
            "options": {"maxWidth": args.max_width},
            "loadAssets": not args.no_assets,
            "timeoutMs": int(args.timeout * 1000),
        }

        log("Rendering Markmap...", quiet)
        result = run_renderer(node, renderer, project, job, quiet, args.timeout + 60)

        def render_extra(extra_outputs: dict[str, str]) -> dict:
            """Re-render for a fallback pipeline that needs another artefact."""
            return run_renderer(
                node,
                renderer,
                project,
                {**job, "outputs": extra_outputs},
                quiet,
                args.timeout + 60,
            )

        log("Calculating complete SVG bounds...", quiet)
        bounds = result["bounds"]
        log(f"SVG size: {human(bounds['width'])} x {human(bounds['height'])}", quiet)
        stats = result.get("stats") or {}
        if stats:
            log(
                f"Map: {stats.get('nodes', 0)} nodes, {stats.get('links', 0)} branches "
                f"({result.get('orientation', 'auto')})",
                quiet,
            )
        if args.zoom != 1.0 and args.page == "a4":
            log("Note: --zoom does not apply to --page a4; the page stays A4.", quiet)
        elif args.zoom != 1.0:
            log(f"Exporting at {args.zoom:g}x page size (vector, sharp at any zoom).", quiet)

        if svg_path:
            chosen = native_svg if args.svg_mode == "native" else portable_svg
            if not chosen.is_file():
                raise MarkmapError("The renderer did not produce an SVG file.")
            shutil.copyfile(chosen, svg_path)
            log(f"SVG exported successfully:\n\n{svg_path}\n", quiet)

        log("Generating PDF...", quiet)
        failures: list[str] = []

        def deliver(staged: Path, *, zoom: float, base_user_unit: float) -> None:
            """Apply the final page geometry and write the user's PDF."""
            if args.page == "a4":
                place_on_a4(staged, pdf_path)
                return
            user_unit = finalize_pdf(
                staged, pdf_path, zoom=zoom, base_user_unit=base_user_unit
            )
            if user_unit > 1.0001:
                log(
                    f"Note: the page is larger than the 200 in PDF page-box limit, so it "
                    f"carries /UserUnit {user_unit:.3f} to keep the map at full size. "
                    "Acrobat honours this; viewers that ignore it show a smaller page "
                    "(still vector, still sharp at any zoom).",
                    quiet,
                )

        for engine in chain:
            try:
                if engine == "chrome":
                    if not valid_pdf(chrome_pdf):
                        raise MarkmapError("Chromium did not produce a valid PDF.")
                    # Chromium already laid the page out at --zoom.
                    deliver(
                        chrome_pdf, zoom=1.0, base_user_unit=float(result.get("userUnit") or 1.0)
                    )
                elif engine in ("cairosvg", "svglib"):
                    if not portable_svg.is_file():
                        render_extra({"portableSvg": str(portable_svg)})
                    if not portable_svg.is_file():
                        raise MarkmapError("No SVG available for vector conversion.")
                    staged = work / f"map-{engine}.pdf"
                    if engine == "cairosvg":
                        pdf_from_cairosvg(portable_svg, staged)
                    else:
                        pdf_from_svglib(portable_svg, staged)
                    if not valid_pdf(staged):
                        raise MarkmapError(f"{engine} produced an invalid PDF.")
                    # These engines convert the natural-size SVG, so --zoom is
                    # applied here instead of at render time.
                    deliver(staged, zoom=args.zoom, base_user_unit=1.0)
                elif engine == "raster":
                    if not raster_png.is_file():
                        result = render_extra({"png": str(raster_png)})
                    effective = float(result.get("rasterScale") or args.scale)
                    if effective < float(result.get("rasterRequestedScale") or args.scale):
                        log(
                            f"Note: --scale reduced to {effective:.2f} to stay within the "
                            "renderer's maximum image size.",
                            quiet,
                        )
                    staged = work / "map-raster.pdf"
                    pdf_from_png(raster_png, staged, PX_PER_IN * effective)
                    if not valid_pdf(staged):
                        raise MarkmapError("The raster fallback produced an invalid PDF.")
                    deliver(
                        staged, zoom=1.0, base_user_unit=float(result.get("userUnit") or 1.0)
                    )
                else:  # pragma: no cover - guarded by argparse choices
                    raise MarkmapError(f"Unknown engine: {engine}")

                if engine != chain[0] and not strict:
                    log(f"(vector export fell back to the {engine} pipeline)", quiet)
                return pdf_path
            except MarkmapError as exc:
                failures.append(f"{engine}: {exc}")
                if strict:
                    raise
            except Exception as exc:  # keep trying the remaining engines
                failures.append(f"{engine}: {exc}")
                if strict:
                    raise MarkmapError(f"{engine} failed: {exc}") from exc

    raise MarkmapError("Could not generate a PDF.\n  " + "\n  ".join(failures))


def main(argv: list[str] | None = None) -> int:
    use_utf8_console()
    args = build_parser().parse_args(argv)
    try:
        pdf_path = convert(args)
    except MarkmapError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:  # pragma: no cover
        print("\nAborted.", file=sys.stderr)
        return 130

    log(f"PDF exported successfully:\n\n{pdf_path}", args.quiet)
    if args.open:
        open_file(pdf_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
