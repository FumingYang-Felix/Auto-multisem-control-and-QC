"""The interactive per-section QC dashboard.

This calls `live_qc_support._write_dashboard_html()` - the exact function the
acquisition side's live QC used - rather than reimplementing it. What that
buys, and why reimplementing was the wrong call:

  ⓪ Region overview      the placed section image, click an mFoV to open it
  ① Focus quality        one hex per mFoV, hover tooltip on each
  ② Stig anisotropy      hex map + quiver arrows
  ③ Status               hex map (blank / failed / ok)
  ④ Image quality per beam   box plot, one box per beam
  ⑤ Retake selection     lasso-select mFoVs, "+ low Q only" /
                         "+ stig outliers only" etc., and it writes a Zeiss
                         retake region XML

plus pan/zoom, ~900 SVG tooltips and an inverted-image viewer. That is
~1,400 lines of interactive SVG and browser JavaScript. An earlier version of
this package wrote its own static scatter plot instead, on the reasoning that
depending on a private function in the other clone was risky - the result was
a page that answered none of the questions an operator opens a dashboard to
answer.

Two things this feeds it that the live path no longer can:
  - `results_by_tile` from a map pass over EVERY mFoV (see score.map_region)
  - `beam_quality_values` from the all-beam pass, which is what panel ④ plots
    and which the acquisition side stopped computing when QC moved here.

Guarded: if the upstream function is renamed, dashboard generation is skipped
with a visible warning rather than taking the run down. Claude 1 has been
asked to add it to the notify list.
"""
from __future__ import annotations

import html as _h
import math
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import portable_paths
from .score import RegionScore

DASHBOARD_NAME = "standalone_qc_dashboard.html"

try:
    from live_qc_support import _write_dashboard_html
    DASHBOARD_AVAILABLE = True
    DASHBOARD_ERROR = ""
except Exception as _exc:  # pragma: no cover - only on an upstream rename
    DASHBOARD_AVAILABLE = False
    DASHBOARD_ERROR = f"{type(_exc).__name__}: {_exc}"


def estimate_hex_radius(layout: List[Dict[str, Any]], fallback: float = 50.0) -> float:
    """Half the median nearest-neighbour spacing, so hexes just touch at the
    real mFoV pitch. Mirrors live_qc_support._estimate_hex_radius, which is
    private; the maths is three lines and not worth a second dependency."""
    pts = [(float(r["x_um"]), float(r["y_um"])) for r in layout]
    if len(pts) < 2:
        return fallback
    nearest: List[float] = []
    for i, (x0, y0) in enumerate(pts):
        best = None
        for j, (x1, y1) in enumerate(pts):
            if i == j:
                continue
            d = math.hypot(x1 - x0, y1 - y0)
            if d > 1e-9 and (best is None or d < best):
                best = d
        if best is not None:
            nearest.append(best)
    if not nearest:
        return fallback
    return max(fallback, 0.45 * sorted(nearest)[len(nearest) // 2])


def build_colormaps(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """The matplotlib cmap/norm pairs _write_dashboard_html expects.

    Values match live_qc_support's own viewer setup exactly, so a colour here
    means what the same colour meant on the live dashboard. matplotlib is
    imported lazily: it is only needed for this, and importing it at module
    scope would put a GUI-stack import in the path of every CLI run.
    """
    from matplotlib import colormaps
    from matplotlib.colors import BoundaryNorm, ListedColormap, Normalize

    low = float(cfg.get("low_quality_threshold", 10.0))
    high = float(cfg.get("high_quality_threshold", 30.0))
    mode = str(cfg.get("focus_color_mode", "continuous")).lower()

    if high <= low:
        # A single hard cutoff. Setting the two ends equal is a reasonable
        # way to ask for pass/fail, but it used to make
        # Normalize(vmin=30, vmax=30) - a degenerate scale that painted
        # EVERY mFoV the same colour whatever its quality, so the overlay
        # looked like it ignored the threshold entirely. Honour the intent
        # instead: red below, green at or above. The huge outer bounds just
        # keep every real quality inside the mapped range.
        cmap_focus: Any = ListedColormap(["#c0392b", "#2e8b3d"])
        norm_focus: Any = BoundaryNorm(
            [low - 1e6, low, low + 1e6], ncolors=2, clip=True
        )
    elif mode == "tiered":
        pad = max(1.0, (high - low) * 0.15)
        cmap_focus = ListedColormap(["#c0392b", "#f0a83a", "#2e8b3d"])
        norm_focus = BoundaryNorm(
            [low - pad, low, high, high + pad], ncolors=cmap_focus.N, clip=True
        )
    else:
        cmap_focus = colormaps[str(cfg.get("colormap", "RdYlGn"))]
        norm_focus = Normalize(vmin=low, vmax=high)

    stig_centered = float(cfg.get("stig_centered_display_max", 30.0))
    return {
        "cmap_focus": cmap_focus,
        "norm_focus": norm_focus,
        "cmap_stig": colormaps["bwr"],
        "norm_stig": Normalize(vmin=-stig_centered, vmax=stig_centered),
        "cmap_outside": colormaps["Reds"],
        "norm_outside": Normalize(vmin=0.0, vmax=100.0),
        "cmap_jitter": colormaps["Reds"],
        "norm_jitter": Normalize(
            vmin=float(cfg.get("jitter_display_min", 1.0)),
            vmax=float(cfg.get("jitter_display_max", 2.5)),
        ),
        "cmap_arrow": colormaps["inferno"],
    }


def _counters(score: RegionScore) -> Dict[str, Any]:
    results = list(score.results_by_tile.values())
    q_vals = [
        float(r.get("quality", -100.0)) for r in results
        if r.get("status") == "ok" and not r.get("is_blank_outside_roi")
    ]
    return {
        "q_vals": q_vals,
        "done": len(results),
        "total": len(score.tiles),
        "fail": sum(
            1 for r in results
            if str(r.get("status_category", "ok")) in ("acquisition_fail", "error",
                                                        "no_valid_images",
                                                        "insufficient_signal")
        ),
        "outside": sum(1 for r in results if r.get("is_blank_outside_roi")),
        "jittery": sum(1 for r in results if r.get("is_jittery")),
    }


# Hover text for panel 2. A title= attribute rather than a custom tooltip:
# it needs no JS or CSS, works over file://, and survives the fact that the
# surrounding page is generated by a module this package must not edit.
# &#10; is a newline inside an attribute; literal quotes would end it.
_STIG_TIP = (
    "Measured from the IMAGES - this is NOT the stigmator setting.&#10;&#10;"
    "Each mFoV's focus quality is computed separately along the horizontal "
    "and vertical directions, each a centre-vs-surround contrast ratio in "
    "percent. This map shows horz - vert, in percentage POINTS.&#10;&#10;"
    "0 = equally sharp both ways.&#10;"
    "Negative = sharper vertically than horizontally.&#10;"
    "Positive = sharper horizontally than vertically.&#10;&#10;"
    "A consistent sign across a whole section points at astigmatism in the "
    "column rather than anything about the sample; a random mix of signs "
    "points at focus or sample noise.&#10;&#10;"
    "Do not confuse with StigX / StigY, the stigmator values of roughly "
    "+/-0.3 plotted in panel 7 - those are a hardware setting, these are "
    "what the resulting images look like.&#10;&#10;"
    "Arrows show the direction and size of each mFoV's anisotropy."
)


# Module-level so retrofit.py can apply the same swap to pages that
# were written before the rename, without re-running any analysis.
STIG_HEADING_OLD = "<h3>② Stig anisotropy + arrows</h3>"
STIG_HEADING_NEW = (
    f'<h3 title="{_STIG_TIP}" style="cursor:help">'
    f'② Stig Sharpness Anisotropy (horz − vert, %) '
    f'<span style="font-weight:400;opacity:.65">ⓘ</span></h3>'
)


def _relabel(html: str, score: RegionScore) -> str:
    """Fix up vocabulary and the page title in the generated HTML.

    Done as post-processing because these strings are hardcoded inside
    live_qc_support._write_dashboard_html, which belongs to the other clone.

    Deliberately NARROW replacements, each counted, so a silent upstream
    wording change shows up as a warning rather than quietly leaving "Block"
    on the page. In particular the retake-panel text and the
    `<batch>_retake.json` filename also mention block_label - those refer to
    the batch folder genuinely and must NOT be touched, which is why this
    does not do a blanket "Block" -> "Sample" replace.
    """
    sample = score.sample_id or "—"
    batch = score.block_label or "—"
    subs = [
        # Stats list: one Block row becomes Sample + Batch.
        (
            f"<dt>Block</dt><dd>{batch}</dd>",
            f"<dt>Sample</dt><dd>{sample}</dd><dt>Batch</dt><dd>{batch}</dd>",
        ),
        # Header strip.
        (
            f"Block: {batch} ",
            f"Sample: {sample} &nbsp;|&nbsp; Batch: {batch} ",
        ),
        # "Region" is this project's "Section".
        ("<dt>Region</dt>", "<dt>Section</dt>"),
        # This page is no longer produced by live QC.
        (f"<title>Live QC — {score.region_folder}</title>",
         f"<title>Standalone QC — {score.region_folder}</title>"),
        # "Stig anisotropy" reads like a stigmator quantity, and an operator
        # reasonably expected the +/-0.3 range StigX/StigY live in. It is
        # not that: it is a measured IMAGE property in percentage points,
        # tens of units wide. Renamed, and given a hover explanation.
        (STIG_HEADING_OLD, STIG_HEADING_NEW),
    ]
    missed = []
    for old, new in subs:
        if old in html:
            html = html.replace(old, new, 1)
        else:
            missed.append(old[:48])
    if missed:
        score.errors.append(
            "dashboard relabel: upstream wording changed, these were not "
            f"found and may still show old vocabulary: {missed}"
        )
    return html


# Panels the section dashboard no longer shows. Per the operator: the focus
# and status hex maps restate what the overview overlay and the IMQA numbers
# already say, and stig is better read off the fitted planes in panel 7 than
# off a hex map. Keyed by the circled digit rather than the heading text, so
# this keeps working through wording changes (including our own rename of
# panel 2).
#
# Safe to remove: the only panel-scoped JS in the generated page is
# `.panel.overview .qtile/.qlabel`; these three carry no ids and nothing
# queries them. The retake lasso in panel 5 draws its own hex map.
_HIDDEN_PANEL_MARKS = ("①", "②", "③")   # circled 1, 2, 3

_PANEL_RE_TEMPLATE = r"<section class=\"panel\"><h3[^>]*>\s*%s.*?</section>"


# The overlay's colours are baked server-side from low/high_quality_threshold,
# so asking "which mFoVs fall below 12 instead of 10?" used to mean
# re-analyzing the section. Everything needed to answer it is already in the
# page: each mFoV's quality is printed in its own <text class="qlabel">. This
# injects a control that re-reads those numbers and recolours the rectangles
# in the browser - no re-analysis, and the underlying results are untouched.
_QTHRESH_MARK = "Show focus-quality overlay</label>"

_QTHRESH_UI = """
<div class="qthresh" style="font-size:11px;margin:2px 0 6px 0;
     display:flex;align-items:center;gap:8px;flex-wrap:wrap;">
  <label>Image quality threshold:
    <input type="number" id="qc-threshold" step="0.5" style="width:74px;"
           value="__QC_LOW__"></label>
  <button type="button" id="qc-threshold-apply">Apply to overlay</button>
  <button type="button" id="qc-threshold-reset">Original colours</button>
  <span id="qc-threshold-count" style="color:#666;"></span>
</div>
<div class="qbeam" style="font-size:11px;margin:0 0 6px 0;
     display:flex;align-items:center;gap:8px;flex-wrap:wrap;">
  <label>Click an mFoV to open:
    <select id="qc-beam" style="min-width:150px;">
      <option value="">every beam (default)</option>
    </select></label>
  <span style="color:#666;">Ctrl/Cmd+click = open the folder
    &nbsp;|&nbsp; Shift+click = copy the path</span>
</div>
<script>
(function () {
  // The rectangles this recolours are emitted AFTER this script in the
  // document, so running at parse time finds none of them and silently
  // takes the fail-safe path below. Wait for the document.
  function boot() {
  var ov = document.querySelector('.panel.overview');
  if (!ov) { return; }
  var rects = ov.querySelectorAll('rect.qtile');
  var labels = ov.querySelectorAll('text.qlabel');
  // Pairing is by document order: the generator emits one rect and one
  // label per mFoV, always both. If that ever stops holding, do nothing
  // rather than colour mFoVs by someone else's number.
  if (!rects.length || rects.length !== labels.length) { return; }

  var orig = [], qs = [], strokes = [];
  for (var i = 0; i < rects.length; i++) {
    orig.push(rects[i].getAttribute('fill'));
    strokes.push(rects[i].getAttribute('stroke-opacity') || '0.25');
    qs.push(parseFloat(labels[i].textContent));
  }

  // "Show focus-quality overlay" only cleared the FILL upstream, leaving a
  // black outline around every mFoV - so unticking it still covered the
  // overview image in a grid. Clear the outline with it.
  var toggle = document.getElementById('toggle-overview-quality');
  function setOutlines(on) {
    for (var i = 0; i < rects.length; i++) {
      rects[i].setAttribute('stroke-opacity', on ? strokes[i] : '0');
    }
  }
  if (toggle) {
    toggle.addEventListener('change', function (e) {
      setOutlines(!!e.target.checked);
    });
    setOutlines(!!toggle.checked);
  }
  var input = document.getElementById('qc-threshold');
  var count = document.getElementById('qc-threshold-count');
  var apply = document.getElementById('qc-threshold-apply');
  var reset = document.getElementById('qc-threshold-reset');

  function recolour() {
    var t = parseFloat(input.value);
    if (!isFinite(t)) { return; }
    var below = 0;
    for (var i = 0; i < rects.length; i++) {
      var bad = isFinite(qs[i]) && qs[i] < t;
      if (bad) { below++; }
      rects[i].setAttribute('fill', bad ? '#c0392b' : '#2e8b3d');
    }
    count.textContent = below + ' of ' + rects.length +
      ' mFoV(s) below ' + t + '  (red = below, green = at or above)';
  }
  function restore() {
    for (var i = 0; i < rects.length; i++) {
      rects[i].setAttribute('fill', orig[i]);
    }
    count.textContent = 'showing the original continuous colour scale';
  }
  // Shared with the overview-tools block, which is a separate IIFE and
  // cannot see this scope otherwise.
  window.__qcOpenBeam = openOneBeam;

  input.addEventListener('input', recolour);
  input.addEventListener('change', recolour);
  apply.addEventListener('click', recolour);
  reset.addEventListener('click', restore);
  restore();

  // ---- which beam does a click open? -------------------------------
  // Upstream opens EVERY beam of the mFoV in one popup (91 images, lazy
  // loaded, captioned by filename), which is why it was unclear what a
  // click selects: nothing is selected. Leave that as the default, and
  // add a way to ask for one beam.
  var beamBox = document.getElementById('qc-beam');
  var bmps = {};
  try {
    bmps = JSON.parse(
      document.getElementById('tile-bmps-payload').textContent || '{}');
  } catch (e) { bmps = {}; }

  function w_doc_option(n) {
    var o = document.createElement('option');
    o.value = String(n);
    o.textContent = 'beam ' + n + ' only';
    return o;
  }

  function openOneBeam(tileIdx, beam) {
    var urls = bmps[tileIdx] || bmps[String(tileIdx)] || [];
    if (!urls.length) { return false; }
    // Filenames end __001.bmp ... __091.bmp and the list is sorted, so
    // beam N is entry N-1. Matched by name where possible so a short or
    // gappy list cannot silently show the wrong beam.
    var want = ('00' + beam).slice(-3) + '.bmp';
    var url = null;
    for (var i = 0; i < urls.length; i++) {
      if (String(urls[i]).indexOf(want) >= 0) { url = urls[i]; break; }
    }
    if (url === null) {
      if (beam < 1 || beam > urls.length) { return false; }
      url = urls[beam - 1];
    }
    var w = window.open('about:blank', '_blank');
    if (!w) { return false; }
    var safe = String(url).replace(/"/g, '&quot;');
    w.document.open();
    w.document.write(
      '<!doctype html><html><head><meta charset="utf-8"><title>mFoV '
      + tileIdx + ' beam ' + beam + '</title><style>'
      + 'body{margin:0;padding:14px;background:#111;color:#ccc;'
      + 'font-family:ui-monospace,Consolas,monospace;}'
      + 'h1{font-size:14px;margin:0 0 6px;color:#eee;}'
      + '.note{font-size:11px;color:#888;margin-bottom:12px;}'
      + '.zoombox{overflow:hidden;position:relative;max-height:88vh;'
      + 'border:1px solid #333;cursor:grab;}'
      + '.zoombox.dragging{cursor:grabbing;}'
      + '.zoombox img{display:block;max-width:100%;height:auto;'
      + 'transform-origin:0 0;filter:invert(1);user-select:none;}'
      + '.zoom-badge{position:absolute;top:6px;right:8px;z-index:2;'
      + 'background:rgba(0,0,0,0.6);color:#ddd;font-size:11px;'
      + 'padding:2px 6px;border-radius:3px;}'
      + '</style></head><body>'
      + '<h1>mFoV ' + tileIdx + ' &middot; beam ' + beam + '</h1>'
      + '<div class="note">Scroll to zoom, drag to pan, double-click to '
      + 'reset. Display-inverted (black&#8596;white) for viewing only - '
      + 'the file on disk is unchanged.<br>' + safe + '</div>'
      + '<div class="zoombox" id="zb"><span class="zoom-badge" id="zbadge">'
      + '100%</span><img id="zimg" src="' + safe + '" draggable="false">'
      + '</div></body></html>');
    w.document.close();

    // Wired from HERE rather than from a script element inside the
    // written document. A closing script tag anywhere in this block -
    // even inside a comment, as this one originally was - ends the script
    // element this code lives in, and everything after it is discarded as
    // stray text. Same approach, and the same interaction, as the
    // all-beams viewer: wheel zooms about the cursor, drag pans once
    // zoomed, double-click resets.
    var box = w.document.getElementById('zb');
    var img = w.document.getElementById('zimg');
    var badge = w.document.getElementById('zbadge');
    if (!box || !img) { return true; }
    var st2 = {scale: 1, tx: 0, ty: 0};
    var MIN = 1, MAX = 40;
    function apply2() {
      img.style.transform = 'translate(' + st2.tx + 'px,' + st2.ty
        + 'px) scale(' + st2.scale + ')';
      if (badge) { badge.textContent = Math.round(st2.scale * 100) + '%'; }
    }
    box.addEventListener('wheel', function (e) {
      e.preventDefault();
      var rect = box.getBoundingClientRect();
      var mx = e.clientX - rect.left, my = e.clientY - rect.top;
      var factor = e.deltaY < 0 ? 1.2 : 1 / 1.2;
      var ns = Math.min(MAX, Math.max(MIN, st2.scale * factor));
      if (ns === st2.scale) { return; }
      var ratio = ns / st2.scale;
      st2.tx = mx - (mx - st2.tx) * ratio;
      st2.ty = my - (my - st2.ty) * ratio;
      st2.scale = ns;
      if (st2.scale === MIN) { st2.tx = 0; st2.ty = 0; }
      apply2();
    }, {passive: false});
    var drag2 = false, lx = 0, ly = 0;
    box.addEventListener('mousedown', function (e) {
      if (st2.scale <= MIN) { return; }
      drag2 = true; lx = e.clientX; ly = e.clientY;
      box.classList.add('dragging');
      e.preventDefault();
    });
    w.document.addEventListener('mouseup', function () {
      drag2 = false; box.classList.remove('dragging');
    });
    w.document.addEventListener('mousemove', function (e) {
      if (!drag2) { return; }
      st2.tx += e.clientX - lx; st2.ty += e.clientY - ly;
      lx = e.clientX; ly = e.clientY;
      apply2();
    });
    box.addEventListener('dblclick', function () {
      st2.scale = 1; st2.tx = 0; st2.ty = 0; apply2();
    });
    apply2();
    return true;
  }

  // Fill the dropdown from the images actually present, rather than
  // assuming 91: a section may legitimately carry a different beam set,
  // and offering a beam that is not there would open nothing.
  if (beamBox) {
    var most = 0, anyTile = null;
    for (var k in bmps) {
      if (bmps[k] && bmps[k].length > most) { most = bmps[k].length; anyTile = k; }
    }
    for (var n = 1; n <= most; n++) {
      var o = w_doc_option(n);
      beamBox.appendChild(o);
    }
    if (most) {
      beamBox.options[0].textContent = 'every beam (all ' + most + ')';
    }
  }

  // Capture phase, so this runs BEFORE the page's own click handler and
  // can stop it opening every image as well.
  document.addEventListener('click', function (ev) {
    if (!beamBox || !beamBox.value) { return; }   // "" = every beam
    if (ev.shiftKey || ev.metaKey || ev.ctrlKey) { return; }  // copy / folder
    var a = ev.target.closest ? ev.target.closest('a[data-tile]') : null;
    if (!a || !ov.contains(a)) { return; }
    var beam = parseInt(beamBox.value, 10);
    if (!isFinite(beam)) { return; }
    if (openOneBeam(parseInt(a.getAttribute('data-tile'), 10), beam)) {
      ev.preventDefault();
      ev.stopPropagation();
    }
  }, true);
  }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
})();
</script>
"""


def _inject_fsp_markers(html: str, score: RegionScore, cfg: Dict[str, Any]) -> str:
    """Mark where the focus support points were measured, on the overview.

    Inserted into the overview panel's own <svg>, so the markers pan and
    scale with the image rather than floating over it.
    """
    from . import fsp_overlay

    sp = ((score.facts or {}).get("support_points") or {})
    points = sp.get("points") or []
    if not points:
        return html
    ov = score.overview or {}
    bboxes = ov.get("bboxes") or {}
    W, H = int(ov.get("width") or 0), int(ov.get("height") or 0)
    if not bboxes or W <= 0 or H <= 0:
        return html

    fit = fsp_overlay.fit_stage_to_overview(score.tiles, bboxes)
    if fit is None:
        score.notes.append(
            "focus support points not drawn: could not fit the stage-to-"
            "overview mapping from this section's mFoV positions")
        return html

    frag, notes = fsp_overlay.markers_svg(points, fit, W, H)
    for n in notes:
        score.notes.append(n)
    if not frag:
        return html

    # The overview panel's SVG, not one of the others on the page.
    start = html.find('class="panel overview"')
    if start < 0:
        return html
    end = html.find("</svg>", start)
    if end < 0:
        return html
    score.notes.append(
        f"{len(points)} focus support point(s) marked on the overview "
        f"({fit['um_per_px']:.3f} um/px, fit residual "
        f"{max(fit['resid_x_px'], fit['resid_y_px']):.0f} px over "
        f"{fit['n']} mFoVs)"
    )
    return html[:end] + frag + html[end:]


# Pan/zoom for the section overview, plus a middle-click viewer for one
# mFoV. The overview is an <svg> with a viewBox, so zooming is just a matter
# of moving that box - it was never wired, not impossible. Everything in the
# panel (the mFoV rectangles, the quality labels, the FSP markers) lives in
# the same SVG, so it all scales together for free.
_OVERVIEW_TOOLS_UI = """
<div class="qovtools" style="font-size:11px;margin:0 0 6px 0;
     display:flex;align-items:center;gap:8px;flex-wrap:wrap;">
  <span id="qc-zoom-badge" style="color:#666;">zoom 100%</span>
  <button type="button" id="qc-zoom-in">+</button>
  <button type="button" id="qc-zoom-out">&minus;</button>
  <button type="button" id="qc-zoom-reset">fit</button>
  <span style="color:#666;">scroll over the image to zoom &nbsp;|&nbsp;
    drag to pan &nbsp;|&nbsp; MIDDLE-click an mFoV to see its beams</span>
</div>
<script>
(function () {
  function boot() {
    var ov = document.querySelector('.panel.overview');
    if (!ov) { return; }
    var svg = ov.querySelector('svg');
    if (!svg) { return; }
    var vb = (svg.getAttribute('viewBox') || '').trim().split(/\s+/).map(Number);
    if (vb.length !== 4) { return; }
    var home = vb.slice();
    var cur = vb.slice();
    var badge = document.getElementById('qc-zoom-badge');

    function apply() {
      svg.setAttribute('viewBox', cur.join(' '));
      if (badge) {
        badge.textContent = 'zoom ' + Math.round(home[2] / cur[2] * 100) + '%';
      }
    }
    function clampBox() {
      // Never zoom out past the whole image, and never wander off it.
      if (cur[2] > home[2]) { cur[2] = home[2]; cur[3] = home[3]; }
      cur[0] = Math.max(home[0], Math.min(cur[0], home[0] + home[2] - cur[2]));
      cur[1] = Math.max(home[1], Math.min(cur[1], home[1] + home[3] - cur[3]));
    }
    function zoomAt(fx, fy, factor) {
      // Never let a bad number reach the viewBox: one NaN there and the
      // overview is blank until the page is reloaded.
      if (!isFinite(fx) || !isFinite(fy) || !isFinite(factor) || factor <= 0) {
        return;
      }
      var nw = cur[2] / factor, nh = cur[3] / factor;
      var minw = home[2] / 60;
      if (nw < minw) { nw = minw; nh = minw * home[3] / home[2]; }
      cur[0] = fx - (fx - cur[0]) * (nw / cur[2]);
      cur[1] = fy - (fy - cur[1]) * (nh / cur[3]);
      cur[2] = nw; cur[3] = nh;
      if (!cur.every(isFinite)) { cur = home.slice(); }
      clampBox(); apply();
    }
    function svgPoint(e) {
      var r = svg.getBoundingClientRect();
      // A zero-sized element (panel not laid out yet, or collapsed) makes
      // the scale below 0 and every coordinate Infinity, which lands NaN
      // in the viewBox and the image disappears for good. Fall back to the
      // centre of the current view.
      if (!(r.width > 0 && r.height > 0)) {
        return [cur[0] + cur[2] / 2, cur[1] + cur[3] / 2];
      }
      // preserveAspectRatio="xMidYMid meet": the box is letterboxed inside
      // the element, so the scale is the smaller of the two and the unused
      // margin has to come off before converting.
      var sc = Math.min(r.width / cur[2], r.height / cur[3]);
      var ox = (r.width - cur[2] * sc) / 2, oy = (r.height - cur[3] * sc) / 2;
      return [cur[0] + (e.clientX - r.left - ox) / sc,
              cur[1] + (e.clientY - r.top - oy) / sc];
    }

    svg.addEventListener('wheel', function (e) {
      e.preventDefault();          // or the page scrolls instead
      var p = svgPoint(e);
      zoomAt(p[0], p[1], e.deltaY < 0 ? 1.25 : 1 / 1.25);
    }, {passive: false});

    var drag = null, moved = 0;
    svg.addEventListener('mousedown', function (e) {
      if (e.button !== 0) { return; }
      drag = {x: e.clientX, y: e.clientY, box: cur.slice()};
      moved = 0;
    });
    window.addEventListener('mousemove', function (e) {
      if (!drag) { return; }
      var r = svg.getBoundingClientRect();
      var sc = Math.min(r.width / drag.box[2], r.height / drag.box[3]);
      var dx = (e.clientX - drag.x) / sc, dy = (e.clientY - drag.y) / sc;
      moved = Math.max(moved, Math.abs(e.clientX - drag.x),
                       Math.abs(e.clientY - drag.y));
      cur[0] = drag.box[0] - dx; cur[1] = drag.box[1] - dy;
      clampBox(); apply();
      if (moved > 3) { svg.style.cursor = 'grabbing'; }
    });
    window.addEventListener('mouseup', function () {
      drag = null; svg.style.cursor = '';
    });
    // A pan must not also open an mFoV. Capture phase so this runs before
    // the link handlers.
    svg.addEventListener('click', function (e) {
      if (moved > 3) { e.preventDefault(); e.stopPropagation(); moved = 0; }
    }, true);

    document.getElementById('qc-zoom-in').addEventListener('click', function () {
      zoomAt(cur[0] + cur[2] / 2, cur[1] + cur[3] / 2, 1.4);
    });
    document.getElementById('qc-zoom-out').addEventListener('click', function () {
      zoomAt(cur[0] + cur[2] / 2, cur[1] + cur[3] / 2, 1 / 1.4);
    });
    document.getElementById('qc-zoom-reset').addEventListener('click', function () {
      cur = home.slice(); apply();
    });
    svg.style.cursor = 'grab';
    apply();

    // ---- MIDDLE-click an mFoV: see its beams ------------------------
    // Laid out from one shared offset table (the 91 beams sit the same way
    // in every mFoV), so this costs about a kilobyte rather than shipping
    // 36,000 absolute coordinates.
    var layout = null, bmps = null;
    try {
      layout = JSON.parse(
        document.getElementById('qc-beam-layout').textContent);
      bmps = JSON.parse(
        document.getElementById('tile-bmps-payload').textContent || '{}');
    } catch (e) { layout = null; }

    function openMfov(tileIdx) {
      if (!layout || !bmps) { return false; }
      var urls = bmps[tileIdx] || bmps[String(tileIdx)] || [];
      if (!urls.length) { return false; }
      var offs = layout.offsets, bw = layout.beam_w, bh = layout.beam_h;
      var xs = [], ys = [];
      for (var k in offs) { xs.push(offs[k][0]); ys.push(offs[k][1]); }
      var minx = Math.min.apply(null, xs), maxx = Math.max.apply(null, xs);
      var miny = Math.min.apply(null, ys), maxy = Math.max.apply(null, ys);
      var fullW = (maxx - minx) + bw, fullH = (maxy - miny) + bh;
      var scale = Math.min(1500 / fullW, 950 / fullH);

      var parts = [];
      for (var b in offs) {
        var url = null, want = ('00' + b).slice(-3) + '.bmp';
        for (var i = 0; i < urls.length; i++) {
          if (String(urls[i]).indexOf(want) >= 0) { url = urls[i]; break; }
        }
        if (url === null) { continue; }
        var x = (offs[b][0] - minx) * scale;
        var y = (offs[b][1] - miny) * scale;
        parts.push('<img class="bm" data-beam="' + b + '" src="'
          + String(url).replace(/"/g, '&quot;') + '" title="beam ' + b
          + ' - click for full resolution" style="left:' + x.toFixed(1)
          + 'px;top:' + y.toFixed(1) + 'px;width:' + (bw * scale).toFixed(1)
          + 'px;height:' + (bh * scale).toFixed(1) + 'px;">');
      }
      var w = window.open('about:blank', '_blank');
      if (!w) { return false; }
      w.document.open();
      w.document.write(
        '<!doctype html><html><head><meta charset="utf-8"><title>mFoV '
        + tileIdx + ' - all beams</title><style>'
        + 'body{margin:0;padding:12px;background:#111;color:#ccc;'
        + 'font-family:ui-monospace,Consolas,monospace;}'
        + 'h1{font-size:14px;margin:0 0 4px;color:#eee;}'
        + '.note{font-size:11px;color:#888;margin-bottom:10px;}'
        + '.stage{position:relative;width:' + (fullW * scale).toFixed(0)
        + 'px;height:' + (fullH * scale).toFixed(0) + 'px;}'
        + 'img.bm{position:absolute;filter:invert(1);cursor:zoom-in;}'
        + 'img.bm:hover{outline:2px solid #ff00ff;z-index:3;}'
        + '</style></head><body><h1>mFoV ' + tileIdx + ' &middot; '
        + parts.length + ' beams at ' + Math.round(scale * 100) + '%</h1>'
        + '<div class="note">Beams at their true positions in the mFoV. '
        + 'Click any beam for full resolution. Display-inverted for '
        + 'viewing only; files on disk are unchanged.</div>'
        + '<div class="stage">' + parts.join('') + '</div></body></html>');
      w.document.close();
      // Full-res on click, wired from here for the same reason the
      // single-beam viewer is: a closing script tag in a written document
      // would end this one.
      var imgs = w.document.querySelectorAll('img.bm');
      for (var n = 0; n < imgs.length; n++) {
        imgs[n].addEventListener('click', function (ev) {
          if (window.__qcOpenBeam) {
            window.__qcOpenBeam(tileIdx, parseInt(
              ev.target.getAttribute('data-beam'), 10));
          }
        });
      }
      return true;
    }

    // auxclick fires for the middle button; mousedown alone would also
    // trigger the browser's autoscroll.
    svg.addEventListener('auxclick', function (e) {
      if (e.button !== 1) { return; }
      var a = e.target.closest ? e.target.closest('a[data-tile]') : null;
      if (!a) { return; }
      e.preventDefault();
      e.stopPropagation();
      openMfov(parseInt(a.getAttribute('data-tile'), 10));
    }, true);
    svg.addEventListener('mousedown', function (e) {
      if (e.button === 1) { e.preventDefault(); }   // no autoscroll cursor
    }, true);
  }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else { boot(); }
})();
</script>
"""


def _inject_overview_tools(html: str, score: RegionScore,
                           cfg: Dict[str, Any]) -> str:
    """Pan/zoom controls, and the beam table the mFoV viewer lays out from."""
    import json as _json

    from . import beam_layout

    if _QTHRESH_MARK not in html:
        return html

    payload = beam_layout.read_beam_offsets(score.tiles, cfg)
    if payload is None:
        # Without offsets the middle-click viewer cannot place beams; the
        # pan/zoom half still works, so ship that and say nothing more.
        score.notes.append(
            "no beam layout readable, so middle-click mFoV view is "
            "unavailable on this section")
        tools = _OVERVIEW_TOOLS_UI
    else:
        score.notes.append(
            f"beam layout: {len(payload['offsets'])} beams, "
            f"{payload['beam_w']}x{payload['beam_h']} px each")
        tools = (
            f'<script id="qc-beam-layout" type="application/json">'
            f'{_json.dumps(payload, separators=(",", ":"))}</script>'
            + _OVERVIEW_TOOLS_UI
        )
    return html.replace(_QTHRESH_MARK, _QTHRESH_MARK + tools, 1)


def _inject_quality_threshold(html: str, cfg: Dict[str, Any]) -> str:
    """Add the live threshold control above the overview overlay."""
    if _QTHRESH_MARK not in html:
        return html          # no overview on this page, nothing to recolour
    # Token replacement, not %-formatting: this template carries CSS and
    # JS, and a literal "100%" in it silently broke the whole injection.
    ui = (_QTHRESH_UI
          .replace("__QC_LOW__",
                   f"{float(cfg.get('low_quality_threshold', 10.0)):.1f}")
          )
    return html.replace(_QTHRESH_MARK, _QTHRESH_MARK + ui, 1)


# "Tile" is the acquisition code's word; the operator's word is mFoV, and
# everything else in these reports already says mFoV. The remaining uses
# come from live_qc_support, which belongs to the other clone, so they are
# corrected here on the generated HTML.
#
# Each replacement is an exact, user-VISIBLE string. A blanket
# "tile" -> "mFoV" would also rewrite data-tile, tile-bmps-payload,
# .qtile, tileIdx and tile_data, and break the page - those are
# identifiers, not prose.
_TILE_WORDING = (
    # the per-beam popup's title bar and heading
    ("'<title>Tile ' + tileIdx + ' — inverted preview</title>'",
     "'<title>mFoV ' + tileIdx + ' — inverted preview</title>'"),
    ("'<h1>Tile ' + tileIdx + ' — '",
     "'<h1>mFoV ' + tileIdx + ' — '"),
    # hex and overlay hover tooltips, which open: <title>tile 7 ...
    ("<title>tile ", "<title>mFoV "),
    # the header strip's instructions
    ("Cmd/Ctrl+click opens the tile folder",
     "Cmd/Ctrl+click opens the mFoV folder"),
    # a CSS comment in the retake panel, which wraps across two lines
    ("on exactly\n     the tiles most worth selecting",
     "on exactly\n     the mFoVs most worth selecting"),
)


def _relabel_tiles(html: str, score: RegionScore) -> str:
    """Say mFoV where the page means mFoV."""
    for old, new in _TILE_WORDING:
        html = html.replace(old, new)
    return html


def _strip_panels(html: str, cfg: Dict[str, Any]) -> str:
    """Remove the hex-map panels the operator does not want displayed."""
    marks = cfg.get("dashboard_hidden_panels")
    if marks is None:
        marks = _HIDDEN_PANEL_MARKS
    for mark in marks:
        pattern = re.compile(_PANEL_RE_TEMPLATE % re.escape(mark), re.S)
        m = pattern.search(html)
        if not m:
            continue
        # A panel must not contain another panel; if upstream ever nests
        # them, removing to the FIRST </section> would silently swallow the
        # wrong markup, so leave it alone and say nothing was removed.
        if "<section" in m.group(0)[len("<section"):]:
            continue
        html = html[:m.start()] + html[m.end():]
    return html


def _inject_panels(html: str, score: RegionScore, cfg: Dict[str, Any]) -> str:
    """Add our own panels to the generated dashboard.

    Appended after the existing content rather than woven into it: the panel
    grid belongs to live_qc_support._write_dashboard_html, and reaching into
    its layout would break the moment that function changes. Inserting before
    </body> only needs the document to be valid HTML.
    """
    from . import focus_planes, fsp_timing, mfov_overlap, plot3d

    blocks = []

    # --- mFoV coverage -----------------------------------------------------
    ov = score.overlap or {}
    if ov.get("passed") is not None:
        verdict = (
            "<span style='color:#2e8b3d;font-weight:600'>no gaps</span>"
            if ov["passed"] else
            f"<span style='color:#c0392b;font-weight:600'>"
            f"{_h.escape(str(ov.get('note') or 'coverage problem'))}</span>"
        )
        # Percent AND pixels. A fraction says whether there is overlap;
        # pixels say how much, which is the number anyone acts on - "110 px
        # of a 2000 px sFoV" is actionable in a way "5.5%" is not.
        bw = ov.get("beam_w")

        def _ov(key: str) -> str:
            frac = ov.get(key)
            if frac is None:
                return "—"
            txt = f"{100.0 * frac:.1f}%"
            if bw:
                txt += f"  ({frac * float(bw):,.0f} px)"
            return txt

        rows = [
            ("mFoVs", ov.get("n_mfovs")),
            ("sFoV size",
             f"{bw:,.0f} x {ov['beam_h']:,.0f} px"
             if bw and ov.get("beam_h") else "—"),
            ("sFoV overlap within an mFoV", _ov("sfov_overlap_frac")),
            ("adjacent mFoV pairs", ov.get("n_adjacent_pairs")),
            ("pair overlap (median)", _ov("pair_overlap_median_frac")),
            ("pair overlap (min)", _ov("pair_overlap_min_frac")),
            ("mFoV pitch",
             f"{ov['mfov_pitch_px']:,.0f} px"
             if ov.get("mfov_pitch_px") else "—"),
            ("interior gap cells", ov.get("n_gap_cells")),
        ]
        stats = "".join(
            f"<tr><th style='text-align:left;color:#666;font-weight:500'>"
            f"{_h.escape(str(k))}</th><td>{_h.escape(str(v))}</td></tr>"
            for k, v in rows
        )
        svg = ""
        try:
            res = mfov_overlap.analyze_section(score.region_dir, cfg)
            svg = mfov_overlap.coverage_svg(res)
        except Exception:
            pass
        blocks.append(
            "<section class='panel'><h3>⑥ mFoV coverage</h3>"
            f"<p style='font-size:11px;color:#666'>{verdict} — measured from "
            f"image_coordinates.txt: the positions ZEN RECORDED, not the "
            f"image content. An mFoV recorded at a wrong coordinate would "
            f"still interlock perfectly here.</p>"
            f"<table style='font-size:11px'>{stats}</table>{svg}</section>"
        )

    # --- focus / stig planes ----------------------------------------------
    try:
        fits, fid = focus_planes.analyze_section(
            score.region_dir, cfg, score.sample_id
        )
    except Exception:
        fits, fid = [], None
    plane_blocks = []
    skipped: List[str] = []
    for f in fits:
        if not f.n or f.degenerate or not f.plane:
            # Say WHY rather than silently omitting the quantity. A missing
            # StigX plot is indistinguishable from a broken one otherwise,
            # and in batches with no LiveQC CSV the stig readback really is
            # constant to metadata.txt's three decimals, so there is nothing
            # to fit - that is a fact about the data, not a failure here.
            why = focus_planes.degenerate_reason(f)
            skipped.append(f"<li><b>{_h.escape(f.label)}</b>: {_h.escape(why)}</li>")
            continue
        g = f.gradient or {}
        extra = ""
        if g:
            extra = (
                f"slope {g['grad_um_per_mm']:.4g} µm/mm · "
                f"max gradient {g['azimuth_deg']:.1f}° from +x · "
                f"plane vs xy {g['tilt_deg']:.4g}°<br>"
            )
        # OL is shown in MICRONS of focus, not the raw uA readback; RMS and
        # max-min are scaled with it so the numbers match the axis.
        d = focus_planes.to_display_units(f)
        u = f" {d.unit}" if d.unit else ""
        src = _h.escape(f"{f.source}; {d.note}" if d.note else f.source)
        # The measured values in 3D against the fitted plane, rather than the
        # residual painted flat: the quantity these panels exist to show IS a
        # tilted plane, and a plane reads as a plane when you can turn it.
        plane_blocks.append(
            f"<div style='flex:1 1 420px;min-width:320px'>"
            f"<div style='font-size:11px;color:#666' title='{src}'>{extra}"
            f"RMS {f.rms * d.scale:.4g}{u} · "
            f"max−min {f.span * d.scale:.4g}{u} ({_h.escape(f.source)})</div>"
            + plot3d.scatter3d_html(
                d.points, f.label, d.unit, plane=d.plane,
                labels=("x (µm)", "y (µm)", d.axis_label),
                point_labels=d.tips,
                plot_id=f"p3d-{score.region_folder}-{f.quantity}",
            )
            + "</div>"
        )
        if f.ref_available and f.ref_points:
            ref_pts = [(x, y, v * d.scale) for x, y, v in f.ref_points]
            plane_blocks.append(
                f"<div style='flex:1 1 420px;min-width:320px'>"
                f"<div style='font-size:11px;color:#666'>"
                f"reference &minus; measured · RMS {f.ref_rms * d.scale:.4g}{u} · "
                f"mean {f.ref_mean * d.scale:+.4g}{u} · "
                f"max&minus;min {f.ref_span * d.scale:.4g}{u}</div>"
                + plot3d.scatter3d_html(
                    # Literal "−", not "&minus;": scatter3d_html escapes the
                    # title, so an entity would render as text.
                    ref_pts, f"{f.label}: reference − measured", d.unit,
                    labels=("x (µm)", "y (µm)",
                            f"difference{(' (' + d.unit + ')') if d.unit else ''}"),
                    point_labels=[f"mFoV {i + 1}"
                                  for i in range(len(ref_pts))],
                    plot_id=f"p3d-{score.region_folder}-{f.quantity}-ref",
                )
                + "</div>"
            )
    if plane_blocks or skipped:
        note = ""
        if skipped:
            note = ("<ul style='font-size:11px;color:#666;margin:6px 0 0 0;"
                    "padding-left:18px'>" + "".join(skipped) + "</ul>")
        # Full-width: three 420px plots side by side need more than one
        # column of the panel grid.
        blocks.append(
            "<section class='panel' style='grid-column:1/-1'>"
            "<h3>⑦ Focus &amp; stigmation planes</h3>"
            "<div style='display:flex;flex-wrap:wrap;gap:14px'>"
            + "".join(plane_blocks) + "</div>" + note + "</section>"
        )

    # --- step timings ------------------------------------------------------
    # Two charts, because they answer two different questions and share no
    # scale: what the whole region spent on each step (hundreds of seconds,
    # stage movement dominated by the move to every mFoV), and what each
    # individual focus support point cost (single-digit seconds).
    facts = score.facts or {}
    fsps = facts.get("fsp_timings") or []
    region_chart = fsp_timing.region_bars_html(
        facts, region=score.region_name or "region", width=820, height=330
    )
    if region_chart or fsps:
        agg = facts.get("aggregate") or {}
        tot = agg.get("grand_total_s") or facts.get("duration_s")
        blocks.append(
            "<section class='panel' style='grid-column:1/-1'>"
            "<h3>⑧ Step timing</h3>"
        )
        if region_chart:
            blocks[-1] += (
                "<p style='font-size:11px;color:#666'>Time this region spent "
                "on autofocus, autostigmation, beam-to-feature alignment and "
                "stage movement."
                + (f" The region took {float(tot):.1f}s in total."
                   if tot is not None else "")
                + "</p>" + region_chart
            )
        if fsps:
            ftot = (agg or {}).get("focus_stig_s")
            blocks[-1] += (
                "<p style='font-size:11px;color:#666;margin-top:14px'>"
                "Per focus support point."
                + (f" The region's focus/stigmation total was "
                   f"{float(ftot):.1f}s." if ftot is not None else "")
                + "</p>" + fsp_timing.bars_html(fsps, width=820)
            )
        blocks[-1] += "</section>"

    if not blocks:
        return html
    extra_html = (
        "<div class='grid' style='display:grid;"
        "grid-template-columns:repeat(auto-fit,minmax(430px,1fr));"
        "gap:12px;margin:0 12px 16px 12px'>" + "".join(blocks) + "</div>"
    )
    # The 3D renderer's CSS+JS, once per page however many plots it carries.
    # It goes with the panels, i.e. inside the real document, for the same
    # reason they do - see _document_body_end.
    if "class='p3d'" in extra_html:
        extra_html += plot3d.engine_html()
    at = _document_body_end(html)
    if at < 0:
        return html + extra_html
    return html[:at] + extra_html + html[at:]


def _document_body_end(html: str) -> int:
    """Index of the real document's closing </body>, or -1.

    NOT the first one. The dashboard's inverted-preview viewer builds a whole
    second HTML document inside a JavaScript string
    (`w.document.write('<html>...</body></html>')`), so "</body>" occurs twice
    and the FIRST occurrence is inside a script.

    Injecting there put panels 6 and 7 inside a JS string literal, where the
    browser never rendered them - and the single quotes in their markup
    terminated that string literal early, which threw a syntax error that
    killed the click handler for the whole page. A click on an mFoV then fell
    through to the <a href="..."> underneath, which points at the tile FOLDER,
    so the browser showed a directory index instead of opening the images.
    One bad insertion point, two symptoms.

    So: scan closing </body> tags from the end and take the first one that is
    not inside a <script> element. Counting script tags is enough - HTML
    forbids nesting them, so balanced counts mean we are outside one.
    """
    pos = len(html)
    while True:
        at = html.rfind("</body>", 0, pos)
        if at < 0:
            return -1
        head = html[:at].lower()
        if head.count("<script") == head.count("</script>"):
            return at
        pos = at


def region_xml_missing(json_path, region_name: str) -> bool:
    """True when the JSON exists but has no region of that name.

    Checked here rather than left to the browser: the panel fails at the
    moment someone finishes lassoing mFoVs, which is the worst time to find
    out. A note at analysis time puts it in the section's own report.
    """
    from . import imaging_json

    return imaging_json.region_xml(json_path, region_name) is None



_BEAMPLOT_OPEN = '<section class="panel beamplot">'


def _replace_beam_panel(html: str, score: RegionScore,
                        cfg: Dict[str, Any]) -> str:
    """Swap panel 4's box plot for beam_quality's.

    The heading is left alone - its advice ("look for a box shifted low or
    unusually wide") is right, it just could not be followed at an axis set
    by the single worst image in the section.
    """
    from . import beam_quality

    at = html.find(_BEAMPLOT_OPEN)
    if at < 0:
        return html
    h3 = html.find("</h3>", at)
    end = html.find("</section>", at)
    if h3 < 0 or end < 0 or h3 > end:
        return html
    try:
        body = beam_quality.panel_html(score.beam_quality_values, cfg)
    except Exception as exc:          # a chart is never worth a dead page
        score.errors.append(
            f"beam quality panel not replaced: {type(exc).__name__}: {exc}"
        )
        return html
    if not body:
        return html
    return html[:h3 + len("</h3>")] + body + html[end:]


# The retake panel's own failure message arrives at autosave time, which on a
# real section is after someone has lassoed a couple of hundred mFoVs - the
# selection is then unsaveable and the work is lost. The cause is knowable
# before the page is even written (either no imaging JSON was found for the
# batch, or the one found has no region of this name), so it is said here, at
# the top of the panel, with the fix spelled out.
_RETAKE_WARN = (
    "<div style=\"border:1px solid #c0392b;background:#fdecea;color:#7b241c;"
    "border-radius:4px;padding:8px 10px;margin:6px 0;font-size:12px;"
    "line-height:1.45\">"
    "<strong>&#9888; Retakes cannot be saved for this section.</strong><br>"
    "__WHY__<br>"
    "A partial retake is built by cloning this region's entry out of the "
    "imaging JSON the batch was acquired from, so without that file nothing "
    "can be written — selecting mFoVs here will fail at save time.<br>"
    "<strong>To fix:</strong> in the standalone QC window set "
    "<em>Imaging JSON</em> (Analysis settings) to the .json this batch was "
    "acquired from, then re-analyze this section with <em>Force</em>."
    "__DETAIL__"
    "</div>"
)

_RETAKE_H3 = "<h3>⑤ Retake selection</h3>"


def _inject_retake_precheck(html: str, why: str, detail: str = "") -> str:
    """Put `why` in a banner at the top of the Retake selection panel."""
    at = html.find(_RETAKE_H3)
    if at < 0:
        return html
    at += len(_RETAKE_H3)
    block = _RETAKE_WARN.replace("__WHY__", why)
    block = block.replace(
        "__DETAIL__",
        ("<div style='margin-top:6px;font-size:11px;opacity:.85'>"
         + detail + "</div>") if detail else "",
    )
    return html[:at] + block + html[at:]

def _resolve_imaging_json(score: RegionScore, cfg: Dict[str, Any]):
    """(path, info) for this section's batch. Cached per batch folder: the
    search reads every candidate .json, and a 25-section batch would
    otherwise repeat that 25 times for one answer."""
    from . import imaging_json

    batch_dir = score.region_dir.parent
    key = str(batch_dir)
    cache = _IMAGING_JSON_CACHE
    if key not in cache:
        wanted = _batch_region_names(batch_dir)
        cache[key] = imaging_json.find_for_batch(batch_dir, wanted, cfg)
    return cache[key]


def _batch_region_names(batch_dir: Path) -> list:
    """Region names from the batch manifest - what a candidate JSON has to
    contain to be the one this batch came from."""
    import json as _json

    man = batch_dir / "region_manifest.json"
    try:
        data = _json.loads(man.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return []
    return [str(r.get("name")) for r in (data.get("regions") or [])
            if isinstance(r, dict) and r.get("name")]


# batch folder -> (path, info). Keyed by folder, so one search per batch per
# process rather than one per section.
_IMAGING_JSON_CACHE: Dict[str, Any] = {}


def write_section_dashboard(
    score: RegionScore,
    cfg: Dict[str, Any],
    out_dir: Optional[Path] = None,
) -> Optional[Path]:
    """Write the interactive dashboard for one section. Returns its path, or
    None if it could not be produced (reason appended to score.errors)."""
    if not DASHBOARD_AVAILABLE:
        score.errors.append(
            f"interactive dashboard unavailable: could not load "
            f"_write_dashboard_html from live_qc_support ({DASHBOARD_ERROR})"
        )
        return None
    if not score.tiles:
        return None
    if not score.results_by_tile:
        score.errors.append(
            "no per-mFoV results, so the dashboard's hex maps would be empty "
            "- is the map pass disabled (tissue_probe_enabled=false)?"
        )
        return None

    out_dir = out_dir or (
        score.region_dir / str(cfg.get("output_subdir", "StandaloneQC"))
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / DASHBOARD_NAME

    layout = [
        {"tile_idx": t.tile_idx, "x_um": t.x_um, "y_um": t.y_um}
        for t in score.tiles
    ]
    radius = float(cfg.get("hex_radius_um") or 0.0) or estimate_hex_radius(layout)
    cmaps = build_colormaps(cfg)

    # Acquisition's own timings for this section, forwarded so the header
    # reads real numbers. Without them _write_dashboard_html falls back to
    # "—" for Focus/Total and the literal string "in progress" for Imaging -
    # actively wrong on a post-acquisition run, where imaging finished long
    # ago. These are ACQUISITION timings (the AF/AS focus phase and the tile
    # loop's wall clock), not anything measured by standalone QC.
    acq = score.acq_timing or {}
    focus_elapsed_s = float(acq.get("focus_s") or 0.0)
    timing_info: Optional[Dict[str, Any]] = None
    if acq.get("wall_s") is not None:
        timing_info = {
            "wall_s": float(acq.get("wall_s") or 0.0),
            "n_tiles": int(acq.get("n_tiles") or len(score.tiles)),
        }

    # The imaging JSON the batch was acquired from. Panel 5's retake lasso
    # clones the original region's XML out of it; without it that panel
    # throws and no partial retake can be built at all. Resolved by content
    # (see imaging_json.py) because nothing in a batch folder records which
    # JSON produced it.
    json_path, json_info = _resolve_imaging_json(score, cfg)
    # retake_warn is (why, detail) for the panel banner, or None when the
    # region XML will be there.
    retake_warn: Optional[tuple] = None
    if json_path is None:
        score.notes.append(
            "no imaging JSON found, so the dashboard's Retake selection "
            "panel cannot build partial-retake regions"
            + (f" ({json_info['note']})" if json_info.get("note") else "")
        )
        retake_warn = (
            "No imaging JSON could be found for this batch, so the original "
            f"XML for region <code>{_h.escape(score.region_name or '?')}"
            "</code> is not available.",
            _h.escape(str(json_info.get("note") or "")),
        )
    elif region_xml_missing(json_path, score.region_name):
        score.notes.append(
            f"imaging JSON {Path(json_path).name} does not contain a region "
            f"named '{score.region_name}', so the Retake selection panel "
            f"cannot build a partial retake for it"
        )
        retake_warn = (
            f"The imaging JSON in use "
            f"(<code>{_h.escape(Path(json_path).name)}</code>) has no region "
            f"named <code>{_h.escape(score.region_name or '?')}</code>.",
            _h.escape(f"Full path: {json_path}"),
        )
    else:
        score.notes.append(
            f"imaging JSON for retake selection: {json_path}"
            + (f" (matched {json_info['matched']} region names)"
               if json_info.get("matched") else "")
        )

    try:
        _write_dashboard_html(
            out_path,
            region_label=score.region_folder,
            block_label=score.block_label,
            layout=layout,
            results_by_tile=score.results_by_tile,
            radius=radius,
            counters=_counters(score),
            region_name=score.region_name,
            low_quality_threshold=float(cfg.get("low_quality_threshold", 10.0)),
            high_quality_threshold=float(cfg.get("high_quality_threshold", 30.0)),
            region_overview=score.overview,
            beam_quality_values=score.beam_quality_values,
            focus_elapsed_s=focus_elapsed_s,
            timing_info=timing_info,
            # The batch folder: the dashboard's retake panel resolves mFoV
            # folders and its "connect batch folder" save target against this.
            block_folder_path=str(score.region_dir.parent),
            imaging_json_path=str(json_path) if json_path else None,
            stig_dmax=float(cfg.get("stig_display_max", 50.0)),
            jitter_dmax=float(cfg.get("jitter_display_max", 2.5)),
            **cmaps,
        )
    except Exception as exc:
        import traceback
        score.errors.append(f"dashboard write failed: {type(exc).__name__}: {exc}")
        traceback.print_exc()
        return None

    if not out_path.is_file():
        return None
    try:
        page = out_path.read_text(encoding="utf-8")
        page = _relabel(page, score)
        page = _strip_panels(page, cfg)
        page = _relabel_tiles(page, score)
        page = _inject_quality_threshold(page, cfg)
        page = _inject_overview_tools(page, score, cfg)
        page = _inject_fsp_markers(page, score, cfg)
        page = _inject_panels(page, score, cfg)
        page = _replace_beam_panel(page, score, cfg)
        if retake_warn is not None:
            page = _inject_retake_precheck(page, retake_warn[0],
                                           retake_warn[1])
        # Drive-letter links only resolve on a machine that maps the
        # share to the same letter. UNC works from anywhere.
        page = portable_paths.apply(page, cfg)
        out_path.write_text(page, encoding="utf-8")
    except OSError as exc:
        score.errors.append(f"dashboard post-processing failed: {exc}")
    return out_path
