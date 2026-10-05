"""Interactive 3D scatter plots, self-contained.

Replaces the flat (x, y) residual maps for OL / StigX / StigY with a real
rotatable 3D scatter: x and y are stage position, the vertical axis is the
measured quantity, and the fitted plane is drawn as a wireframe through the
points. A tilted plane is what these pages are trying to show, and a plane is
much easier to read as a plane than as a colour gradient.

No plotting library. The dashboards are opened over file://, where an
external script would need working internet access on a machine that may not
have it, and the CDN allowlist does not extend to a lab share - so the
renderer here is ~200 lines of canvas drawing emitted inline. It draws:

  - the data box with real-valued ticks on all three axes
  - the fitted plane as a wireframe (skipped when no plane could be fitted)
  - points coloured by residual against that plane, depth-sorted and
    depth-scaled so nearer points read as nearer
  - a hover tooltip naming the mFoV and its residual

Interaction is DRAG to rotate and BUTTONS to zoom. The wheel is deliberately
left alone: hijacking it would both fight the page's own scrolling and repeat
the inadvertent-change problem that got scroll-to-edit removed from the GUI.
"""
from __future__ import annotations

import html as _h
import json
import math
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .focus_planes import _color

# Bumped into the page once, however many plots it carries.
_SCRIPT_MARKER = "data-p3d-engine"

_ENGINE = """
<script data-p3d-engine="1">
(function () {
  'use strict';
  if (window.__p3dInit) { return; }
  window.__p3dInit = true;

  var TAU = Math.PI * 2;

  function fmt(v) {
    if (v === null || v === undefined || !isFinite(v)) { return '-'; }
    var a = Math.abs(v);
    if (a !== 0 && (a < 1e-3 || a >= 1e6)) { return v.toExponential(2); }
    return String(Math.round(v * 1e4) / 1e4);
  }

  function makePlot(root) {
    var payload = JSON.parse(
      root.querySelector('script[type="application/json"]').textContent);
    var canvas = root.querySelector('canvas');
    var tip = root.querySelector('.p3d-tip');
    var ctx = canvas.getContext('2d');
    var pts = payload.points || [];
    if (!pts.length) { return; }

    // Per-axis data ranges. A degenerate axis gets a nominal span so the
    // box stays a box instead of collapsing to a line.
    function span(get) {
      var lo = Infinity, hi = -Infinity;
      for (var i = 0; i < pts.length; i++) {
        var v = get(pts[i]);
        if (v < lo) { lo = v; }
        if (v > hi) { hi = v; }
      }
      if (!(hi > lo)) { var pad = Math.abs(hi) * 0.05 || 1; lo -= pad; hi += pad; }
      return [lo, hi];
    }
    var rx = span(function (p) { return p[0]; });
    var ry = span(function (p) { return p[1]; });
    var rz = span(function (p) { return p[2]; });
    // The plane must fit inside the box too, or the wireframe gets clipped.
    var pl = payload.plane;
    if (pl) {
      [[rx[0], ry[0]], [rx[0], ry[1]], [rx[1], ry[0]], [rx[1], ry[1]]]
        .forEach(function (c) {
          var z = pl[0] * c[0] + pl[1] * c[1] + pl[2];
          if (z < rz[0]) { rz[0] = z; }
          if (z > rz[1]) { rz[1] = z; }
        });
      if (!(rz[1] > rz[0])) { rz[0] -= 1; rz[1] += 1; }
    }

    function nx(v) { return (v - rx[0]) / (rx[1] - rx[0]) - 0.5; }
    function ny(v) { return (v - ry[0]) / (ry[1] - ry[0]) - 0.5; }
    function nz(v) { return (v - rz[0]) / (rz[1] - rz[0]) - 0.5; }

    var st = {yaw: -0.62, pitch: 0.42, zoom: 1.0};
    var W = 0, H = 0, cx = 0, cy = 0, base = 0;

    function resize() {
      var dpr = window.devicePixelRatio || 1;
      var w = root.clientWidth || 320;
      var h = payload.height || 300;
      canvas.width = Math.round(w * dpr);
      canvas.height = Math.round(h * dpr);
      canvas.style.width = w + 'px';
      canvas.style.height = h + 'px';
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      W = w; H = h; cx = w / 2; cy = h / 2 + 8;
      base = Math.min(w, h) * 0.64;
    }

    // Orthographic: yaw about the vertical axis, then tilt by pitch.
    // Returns screen x/y plus a depth key for painter-order sorting.
    function proj(x, y, z) {
      var ca = Math.cos(st.yaw), sa = Math.sin(st.yaw);
      var xr = x * ca - y * sa;
      var yr = x * sa + y * ca;
      var ce = Math.cos(st.pitch), se = Math.sin(st.pitch);
      var s = base * st.zoom;
      return {
        x: cx + xr * s,
        y: cy - (z * ce - yr * se) * s,
        d: yr * ce + z * se
      };
    }

    function line(a, b, color, width) {
      ctx.strokeStyle = color;
      ctx.lineWidth = width || 1;
      ctx.beginPath();
      ctx.moveTo(a.x, a.y);
      ctx.lineTo(b.x, b.y);
      ctx.stroke();
    }

    var CORNERS = [
      [-0.5, -0.5, -0.5], [0.5, -0.5, -0.5], [0.5, 0.5, -0.5], [-0.5, 0.5, -0.5],
      [-0.5, -0.5, 0.5], [0.5, -0.5, 0.5], [0.5, 0.5, 0.5], [-0.5, 0.5, 0.5]
    ];
    var EDGES = [[0, 1], [1, 2], [2, 3], [3, 0], [4, 5], [5, 6], [6, 7], [7, 4],
                 [0, 4], [1, 5], [2, 6], [3, 7]];

    function ticks(lo, hi) {
      return [lo, (lo + hi) / 2, hi];
    }

    function draw() {
      ctx.clearRect(0, 0, W, H);
      var P = CORNERS.map(function (c) { return proj(c[0], c[1], c[2]); });

      // Box, back edges first so the front ones overdraw them.
      var order = EDGES.slice().sort(function (e1, e2) {
        return (P[e1[0]].d + P[e1[1]].d) - (P[e2[0]].d + P[e2[1]].d);
      });
      order.forEach(function (e, i) {
        line(P[e[0]], P[e[1]], i < 6 ? '#e6e6ea' : '#c9c9d2', 1);
      });

      // Axis labels and ticks along the three edges meeting at corner 0.
      //
      // Each tick is pushed OUTWARD along its own axis. All three axes
      // start at that same corner, so without this the three minimum
      // values printed on top of one another and none of them could be
      // read - which is exactly what an operator reported. Pushing x out
      // along -x, y along -y and z along -z separates them, because those
      // are three different directions whatever the rotation.
      var PAD = 0.14;
      ctx.font = '12px ui-monospace, Consolas, monospace';
      ctx.textAlign = 'center';
      ctx.textBaseline = 'middle';
      var axes = [
        {lo: rx[0], hi: rx[1], label: payload.labels[0],
         at: function (t, o) { return [t - 0.5 + o, -0.5, -0.5]; }},
        {lo: ry[0], hi: ry[1], label: payload.labels[1],
         at: function (t, o) { return [-0.5, t - 0.5 + o, -0.5]; }},
        {lo: rz[0], hi: rz[1], label: payload.labels[2],
         at: function (t, o) { return [-0.5, -0.5, t - 0.5 + o]; }}
      ];
      axes.forEach(function (ax) {
        [[0, -PAD], [1, PAD]].forEach(function (end) {
          var t = end[0];
          var tv = t ? ax.hi : ax.lo;
          var c = ax.at(t, end[1]);
          var p = proj(c[0], c[1], c[2]);
          // A halo, so a tick that lands over the plane wireframe or a
          // point is still legible.
          ctx.lineWidth = 3;
          ctx.strokeStyle = '#fff';
          ctx.strokeText(fmt(tv), p.x, p.y);
          ctx.fillStyle = '#555';
          ctx.fillText(fmt(tv), p.x, p.y);
        });
        var mid = ax.at(0.5, 0);
        var pm = proj(mid[0], mid[1], mid[2]);
        ctx.font = 'bold 12px ui-monospace, Consolas, monospace';
        ctx.lineWidth = 3;
        ctx.strokeStyle = '#fff';
        ctx.strokeText(ax.label, pm.x, pm.y - 14);
        ctx.fillStyle = '#222';
        ctx.fillText(ax.label, pm.x, pm.y - 14);
        ctx.font = '12px ui-monospace, Consolas, monospace';
      });

      // Fitted plane as a wireframe in real coordinates.
      if (pl) {
        var N = 6;
        for (var i = 0; i <= N; i++) {
          var f = i / N;
          var xa = rx[0] + f * (rx[1] - rx[0]);
          var ya = ry[0] + f * (ry[1] - ry[0]);
          var rowA = null, colA = null;
          for (var j = 0; j <= N; j++) {
            var g = j / N;
            var xb = rx[0] + g * (rx[1] - rx[0]);
            var yb = ry[0] + g * (ry[1] - ry[0]);
            var rz1 = pl[0] * xb + pl[1] * ya + pl[2];
            var rz2 = pl[0] * xa + pl[1] * yb + pl[2];
            var pA = proj(nx(xb), ny(ya), nz(rz1));
            var pB = proj(nx(xa), ny(yb), nz(rz2));
            if (rowA) { line(rowA, pA, 'rgba(70,110,170,0.40)', 1); }
            if (colA) { line(colA, pB, 'rgba(70,110,170,0.40)', 1); }
            rowA = pA; colA = pB;
          }
        }
      }

      // Points, far to near, so nearer ones overdraw further ones.
      var proj_pts = pts.map(function (p, i) {
        var q = proj(nx(p[0]), ny(p[1]), nz(p[2]));
        q.i = i;
        return q;
      });
      proj_pts.sort(function (a, b) { return a.d - b.d; });
      proj_pts.forEach(function (q) {
        var p = pts[q.i];
        var r = (3.0 + 2.2 * (q.d + 0.9)) * st.zoom;
        ctx.beginPath();
        ctx.arc(q.x, q.y, Math.max(2, r), 0, TAU);
        ctx.fillStyle = p[3];
        ctx.fill();
        ctx.strokeStyle = 'rgba(40,40,45,0.55)';
        ctx.lineWidth = 0.6;
        ctx.stroke();
        q.r = Math.max(2, r);
      });
      drawColorBar();
      root.__proj = proj_pts;
    }

    // Legend for the point colours. Screen space, not part of the scene:
    // it must stay put and stay level however the box is turned.
    function drawColorBar() {
      var ramp = payload.ramp || [];
      var m = payload.resid_max;
      if (!ramp.length || !(m > 0)) { return; }
      var bw = Math.min(150, W * 0.42), bh = 9;
      var x0 = 10, y0 = H - 24;
      var g = ctx.createLinearGradient(x0, 0, x0 + bw, 0);
      ramp.forEach(function (stop) { g.addColorStop(stop[0], stop[1]); });
      ctx.fillStyle = g;
      ctx.fillRect(x0, y0, bw, bh);
      ctx.strokeStyle = '#c9c9d2';
      ctx.lineWidth = 1;
      ctx.strokeRect(x0 + 0.5, y0 + 0.5, bw - 1, bh - 1);

      ctx.font = '11px ui-monospace, Consolas, monospace';
      ctx.fillStyle = '#666';
      ctx.textBaseline = 'top';
      ctx.textAlign = 'left';
      ctx.fillText('-' + fmt(m), x0, y0 + bh + 2);
      ctx.textAlign = 'center';
      ctx.fillText('0', x0 + bw / 2, y0 + bh + 2);
      ctx.textAlign = 'right';
      ctx.fillText('+' + fmt(m), x0 + bw, y0 + bh + 2);

      ctx.textAlign = 'left';
      ctx.textBaseline = 'bottom';
      ctx.fillStyle = '#333';
      ctx.fillText(payload.color_label || '', x0, y0 - 2);
    }

    // ---- interaction ----
    var drag = null;
    canvas.addEventListener('mousedown', function (e) {
      drag = {x: e.clientX, y: e.clientY, yaw: st.yaw, pitch: st.pitch};
      canvas.style.cursor = 'grabbing';
      e.preventDefault();
    });
    window.addEventListener('mouseup', function () {
      drag = null;
      canvas.style.cursor = 'grab';
    });
    window.addEventListener('mousemove', function (e) {
      if (!drag) { return; }
      st.yaw = drag.yaw + (e.clientX - drag.x) * 0.010;
      st.pitch = drag.pitch + (e.clientY - drag.y) * 0.008;
      st.pitch = Math.max(-1.45, Math.min(1.45, st.pitch));
      draw();
    });

    canvas.addEventListener('mousemove', function (e) {
      if (drag) { return; }
      var b = canvas.getBoundingClientRect();
      var mx = e.clientX - b.left, my = e.clientY - b.top;
      var best = null, bestd = 1e9;
      (root.__proj || []).forEach(function (q) {
        var d = Math.hypot(q.x - mx, q.y - my);
        if (d < Math.max(7, q.r + 4) && d < bestd) { bestd = d; best = q; }
      });
      if (!best) { tip.style.display = 'none'; return; }
      var p = pts[best.i];
      tip.innerHTML = p[4];
      tip.style.display = 'block';
      tip.style.left = Math.min(mx + 12, b.width - 150) + 'px';
      tip.style.top = Math.max(0, my - 8) + 'px';
    });
    canvas.addEventListener('mouseleave', function () {
      tip.style.display = 'none';
    });

    root.querySelectorAll('[data-p3d]').forEach(function (btn) {
      btn.addEventListener('click', function () {
        var a = btn.getAttribute('data-p3d');
        if (a === 'in') { st.zoom = Math.min(8.0, st.zoom * 1.3); }
        else if (a === 'out') { st.zoom = Math.max(0.3, st.zoom / 1.3); }
        else if (a === 'top') { st.yaw = 0; st.pitch = 1.45; }
        else { st.yaw = -0.62; st.pitch = 0.42; st.zoom = 1.0; }
        draw();
      });
    });

    canvas.style.cursor = 'grab';
    resize();
    draw();
    window.addEventListener('resize', function () { resize(); draw(); });
  }

  function boot() {
    document.querySelectorAll('.p3d').forEach(function (root) {
      try { makePlot(root); } catch (err) {
        root.insertAdjacentHTML('beforeend',
          '<div style="font-size:11px;color:#a33">3D plot failed: ' +
          String(err) + '</div>');
      }
    });
  }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
})();
</script>
"""

_CSS = """
<style data-p3d-engine="1">
  .p3d { position:relative; font-family: ui-monospace, Consolas, monospace; }
  .p3d canvas { display:block; width:100%; background:#fff;
                border:1px solid #e2e2e8; border-radius:4px;
                touch-action:none; }
  .p3d-tip { position:absolute; display:none; pointer-events:none;
             background:rgba(20,20,24,0.92); color:#fff; font-size:10px;
             padding:4px 6px; border-radius:3px; white-space:nowrap;
             z-index:5; }
  .p3d-bar { display:flex; gap:4px; align-items:center; margin:4px 0 0 0;
             font-size:10px; color:#777; }
  .p3d-bar button { font:inherit; font-size:10px; padding:1px 6px;
                    border:1px solid #ccc; background:#f7f7f9; color:#333;
                    border-radius:3px; cursor:pointer; }
  .p3d-bar button:hover { background:#ececf2; }
</style>
"""


def engine_html() -> str:
    """The shared CSS + JS. Emit once per page; `scatter3d_html` blocks are
    inert without it."""
    return _CSS + _ENGINE


def scatter3d_html(
    points: Sequence[Tuple[float, float, float]],
    title: str,
    unit: str = "",
    plane: Optional[Tuple[float, float, float]] = None,
    labels: Tuple[str, str, str] = ("x (µm)", "y (µm)", "value"),
    # Matches mfov_overlap.coverage_svg's 420px, so the plane plots are not
    # the smallest thing on a page full of maps.
    height: int = 420,
    point_labels: Optional[Sequence[str]] = None,
    plot_id: Optional[str] = None,
) -> str:
    """One interactive 3D scatter of (x, y, value) with its fitted plane.

    Points are coloured by RESIDUAL against `plane` when one is given (so the
    colour answers "which side of the plane, and by how much"), and by value
    otherwise. Colours come from focus_planes._color, the same blue-white-red
    ramp the 2D maps used, so a colour means here what it meant there.
    """
    if not points:
        return f'<div class="muted">{_h.escape(title)}: no data</div>'

    def _resid(x: float, y: float, v: float) -> float:
        if plane is None:
            return v
        return v - (plane[0] * x + plane[1] * y + plane[2])

    resids = [_resid(x, y, v) for x, y, v in points]
    m = max((abs(r) for r in resids), default=0.0) or 1e-12

    # The bar is drawn in JS from these stops, so it is the SAME ramp the
    # points were coloured with rather than a hand-matched copy that could
    # drift out of step with focus_planes._color.
    from .focus_planes import _DIVERGING

    payload: Dict[str, Any] = {
        "points": [],
        "plane": list(plane) if plane else None,
        "labels": list(labels),
        "height": int(height),
        "resid_max": float(m),
        "ramp": [[t, "#%02x%02x%02x" % rgb] for t, rgb in _DIVERGING],
        "color_label": (
            f"residual from the plane{' (' + unit + ')' if unit else ''}"
            if plane is not None
            else f"value{' (' + unit + ')' if unit else ''}"
        ),
    }
    for i, ((x, y, v), r) in enumerate(zip(points, resids)):
        name = (point_labels[i] if point_labels and i < len(point_labels)
                else f"point {i + 1}")
        u = f" {_h.escape(unit)}" if unit else ""
        tip = (f"{_h.escape(str(name))}<br>x={x:.1f} y={y:.1f}"
               f"<br>{_h.escape(title)} = {v:.6g}{u}")
        if plane is not None:
            tip += f"<br>residual = {r:+.4g}"
        payload["points"].append(
            [float(x), float(y), float(v), _color(0.5 + 0.5 * (r / m)), tip]
        )

    pid = plot_id or f"p3d{abs(hash((title, len(points)))) % 10**8}"
    hint = ("drag to rotate · colour = distance from the plane"
            if plane else "drag to rotate")
    return (
        f"<div class='p3d' id='{_h.escape(pid)}'>"
        f"<div style='font-size:11px;color:#333;margin-bottom:3px'>"
        f"{_h.escape(title)}</div>"
        f"<canvas></canvas><div class='p3d-tip'></div>"
        f"<div class='p3d-bar'>"
        f"<button type='button' data-p3d='in'>zoom &#43;</button>"
        f"<button type='button' data-p3d='out'>zoom &minus;</button>"
        f"<button type='button' data-p3d='top'>top view</button>"
        f"<button type='button' data-p3d='reset'>&#8635; reset</button>"
        f"<span>{hint}</span></div>"
        f"<script type='application/json'>"
        f"{json.dumps(payload, ensure_ascii=False)}</script>"
        f"</div>"
    )


# ---------------------------------------------------------------------------
# grouped bar chart (FSP timings)
# ---------------------------------------------------------------------------
def grouped_bars_svg(
    groups: Sequence[str],
    series: Sequence[Tuple[str, str, Sequence[Optional[float]]]],
    title: str = "",
    unit: str = "s",
    width: int = 720,
    height: int = 380,
    y_label: str = "Time (s)",
) -> str:
    """Grouped vertical bars as inline SVG: one cluster per group, one bar per
    series. `series` entries are (label, colour, values aligned to `groups`).

    Static rather than interactive on purpose - a bar chart's value is
    already readable at a glance, and every bar carries a <title> tooltip
    with its exact number.
    """
    if not groups or not series:
        return '<div class="muted">no FSP timing data</div>'

    vals = [v for _, _, vs in series for v in vs
            if v is not None and not math.isnan(v)]
    if not vals:
        return '<div class="muted">no FSP timing data</div>'
    vmax = max(vals) or 1.0

    # padl leaves room for the rotated axis label plus the tick numbers.
    padl, padr, padt, padb = 62, 8, 18 if title else 8, 52
    plot_w = width - padl - padr
    plot_h = height - padt - padb
    n_g = len(groups)
    gw = plot_w / n_g
    bw = min(26.0, (gw * 0.78) / len(series))

    parts = [
        f"<svg viewBox='0 0 {width} {height}' width='100%' "
        f"style='max-width:{width}px' role='img' "
        f"aria-label='{_h.escape(title or 'FSP timings')}'>"
    ]
    if title:
        parts.append(
            f"<text x='0' y='11' font-size='11' fill='#333'>"
            f"{_h.escape(title)}</text>"
        )

    # y grid + labels
    for k in range(5):
        v = vmax * k / 4.0
        y = padt + plot_h - (v / vmax) * plot_h
        parts.append(
            f"<line x1='{padl}' y1='{y:.1f}' x2='{padl + plot_w}' "
            f"y2='{y:.1f}' stroke='#ececf0' stroke-width='1'/>"
            f"<text x='{padl - 5}' y='{y + 3:.1f}' font-size='9' fill='#888' "
            f"text-anchor='end'>{v:.0f}</text>"
        )
    # Vertical axis label, rotated up the left edge.
    if y_label:
        ymid = padt + plot_h / 2.0
        parts.append(
            f"<text x='12' y='{ymid:.1f}' font-size='10' fill='#555' "
            f"text-anchor='middle' transform='rotate(-90 12 {ymid:.1f})'>"
            f"{_h.escape(y_label)}</text>"
        )

    for gi, g in enumerate(groups):
        x0 = padl + gi * gw + (gw - bw * len(series)) / 2.0
        for si, (label, colour, vs) in enumerate(series):
            v = vs[gi] if gi < len(vs) else None
            if v is None or math.isnan(v):
                continue
            h = (v / vmax) * plot_h
            x = x0 + si * bw
            y = padt + plot_h - h
            parts.append(
                f"<rect x='{x:.1f}' y='{y:.1f}' width='{bw - 1.5:.1f}' "
                f"height='{max(0.6, h):.1f}' fill='{colour}'>"
                f"<title>{_h.escape(str(g))} · {_h.escape(label)}: "
                f"{v:.2f}{_h.escape(unit)}</title></rect>"
            )
        # Group label, rotated when there are many groups.
        cxg = padl + gi * gw + gw / 2.0
        if n_g > 12:
            parts.append(
                f"<text x='{cxg:.1f}' y='{padt + plot_h + 6}' font-size='9' "
                f"fill='#666' text-anchor='end' "
                f"transform='rotate(-60 {cxg:.1f} {padt + plot_h + 6})'>"
                f"{_h.escape(str(g))}</text>"
            )
        else:
            parts.append(
                f"<text x='{cxg:.1f}' y='{padt + plot_h + 12}' font-size='9' "
                f"fill='#666' text-anchor='middle'>{_h.escape(str(g))}</text>"
            )

    parts.append(
        f"<line x1='{padl}' y1='{padt + plot_h}' x2='{padl + plot_w}' "
        f"y2='{padt + plot_h}' stroke='#c9c9d2' stroke-width='1'/>"
    )
    # Legend
    lx = padl
    ly = height - 6
    for label, colour, _vs in series:
        parts.append(
            f"<rect x='{lx}' y='{ly - 8}' width='9' height='9' fill='{colour}'/>"
            f"<text x='{lx + 13}' y='{ly}' font-size='9' fill='#555'>"
            f"{_h.escape(label)}</text>"
        )
        lx += 22 + 6 * len(label)
    parts.append("</svg>")
    return "".join(parts)


BAR_COLOURS = {
    "focus": "#2e6fb7",
    "stig": "#c58a1a",
    "move": "#4f9d60",
}
