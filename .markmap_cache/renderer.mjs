/**
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
