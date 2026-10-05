"""Panel 4: per-beam image-quality box plot, rebuilt.

live_qc_support's own version has two problems that this replaces rather
than patches (that module is shared and not edited here).

1. THE AXIS IS SET BY THE SINGLE WORST IMAGE. It spans min(all) to max(all),
   so on S12R5 one image at 888 against an interquartile band of roughly
   20-80 flattens all 91 boxes into a few pixels at the bottom of the frame.
   The chart is captioned "look for a box shifted low or unusually wide" and
   at that scale no box can be seen to be either. Here the whiskers are
   Tukey's (the furthest value within 1.5*IQR) and the axis is set by them;
   everything past a whisker is still drawn, as a dot, and anything past the
   axis is counted in a marker at the frame edge so nothing is silently
   dropped. A "Full range" button restores the old scaling.

2. THE COLOUR SATURATES. Fill is the beam's median on the quality colour
   scale, which runs from the low threshold to the high one. With the
   threshold at 50 and a section whose beam medians run 12-55, 86 of 91
   beams sit at or below the bottom of the scale and get the identical
   darkest red - the colour then says "this section is poor", which the
   caption already says, and nothing about which BEAM is unusual. The
   second colour mode fixes the scale to the section's own spread instead,
   so a beam is coloured by how far it sits from its peers.

Both controls are presentation only. No value is recomputed between modes,
and every box keeps the same hover text.
"""
from __future__ import annotations

import html as _h
import math
from typing import Any, Dict, List, Optional, Sequence, Tuple

__all__ = ["panel_html", "beam_stats"]

# The frame. bar_w/gap match live_qc_support's so the panel does not jump
# in width when this replaces it.
BAR_W, GAP = 14.0, 4.0
PLOT_H = 260.0
PAD_L, PAD_T, PAD_B, PAD_R = 46.0, 12.0, 30.0, 12.0


def beam_stats(
    beam_quality_values: Dict[int, Sequence[float]]
) -> List[Dict[str, Any]]:
    """Per beam: quartiles, Tukey whiskers, and the values beyond them.

    Outliers are kept as values, not just counted: where they land is the
    whole question when a beam is suspected of occasional blow-ups.
    """
    import numpy as np

    out: List[Dict[str, Any]] = []
    for b in sorted(beam_quality_values):
        vals = beam_quality_values.get(b) or []
        arr = np.asarray([v for v in vals if v is not None], dtype=np.float64)
        arr = arr[np.isfinite(arr)]
        if not arr.size:
            continue
        q1, med, q3 = (float(x) for x in np.percentile(arr, [25, 50, 75]))
        iqr = q3 - q1
        # 1.5*IQR is Tukey's convention. With iqr == 0 (every image on this
        # beam scored the same) the fences collapse onto the box and every
        # other value becomes an outlier, which is the honest reading.
        lo_f, hi_f = q1 - 1.5 * iqr, q3 + 1.5 * iqr
        inside = arr[(arr >= lo_f) & (arr <= hi_f)]
        w_lo = float(inside.min()) if inside.size else q1
        w_hi = float(inside.max()) if inside.size else q3
        out.append({
            "beam": int(b),
            "n": int(arr.size),
            "min": float(arr.min()), "max": float(arr.max()),
            "q1": q1, "med": med, "q3": q3,
            "w_lo": w_lo, "w_hi": w_hi,
            "out_lo": sorted(float(v) for v in arr[arr < w_lo]),
            "out_hi": sorted(float(v) for v in arr[arr > w_hi]),
        })
    return out


def _hex(rgba) -> str:
    r, g, b = (int(round(255 * c)) for c in rgba[:3])
    return f"#{r:02x}{g:02x}{b:02x}"


def _colours(stats: List[Dict[str, Any]], cfg: Dict[str, Any]):
    """(threshold colours, relative colours, legend text for each).

    Threshold mode reuses the page's own quality scale so a colour means
    here what it means on the hex overlay. Relative mode re-centres on this
    section: the scale is the median of the beam medians, +/- 3 robust
    sigmas of them, so "unusual for this section" is visible even when the
    whole section sits below the quality threshold.
    """
    import numpy as np
    from matplotlib import colormaps
    from matplotlib.colors import Normalize

    meds = np.asarray([s["med"] for s in stats], dtype=np.float64)
    low = float(cfg.get("low_quality_threshold", 10.0))
    high = float(cfg.get("high_quality_threshold", 30.0))
    cmap = colormaps[str(cfg.get("colormap", "RdYlGn"))]

    if high > low:
        n_abs = Normalize(vmin=low, vmax=high)
        clamped = int(((meds <= low) | (meds >= high)).sum())
        leg_abs = (
            f"fill = the beam's median quality on the page's quality scale: "
            f"red at {low:g}, green at {high:g}"
        )
        if clamped >= max(3, int(0.5 * len(stats))):
            leg_abs += (
                f" — but {clamped} of {len(stats)} beam medians fall "
                f"outside {low:g}–{high:g} and share the end colour, so "
                f"this mode separates almost nothing on this section. Use "
                f"“vs section”."
            )
        cols_abs = [_hex(cmap(n_abs(m))) for m in meds]
    else:
        cols_abs = ["#c0392b" if m < low else "#2e8b3d" for m in meds]
        leg_abs = (f"fill = pass/fail at the quality threshold {low:g} "
                   f"(the two thresholds are equal)")

    centre = float(np.median(meds))
    mad = float(np.median(np.abs(meds - centre)))
    # 1.4826*MAD is the normal-consistent sigma; the floor keeps a section
    # whose beams are nearly identical from painting noise as a 3-sigma
    # spread.
    sigma = max(1.4826 * mad, 1e-6, 0.02 * abs(centre))
    span = 3.0 * sigma
    n_rel = Normalize(vmin=centre - span, vmax=centre + span)
    cols_rel = [_hex(cmap(n_rel(m))) for m in meds]
    leg_rel = (
        f"fill = how far the beam's median sits from this section's "
        f"({centre:.1f}); red at {centre - span:.1f}, green at "
        f"{centre + span:.1f} (±3 robust σ of the 91 medians)"
    )
    return cols_abs, cols_rel, leg_abs, leg_rel


def _svg(
    stats: List[Dict[str, Any]],
    cols_abs: List[str],
    cols_rel: List[str],
    y_min: float,
    y_max: float,
    svg_id: str,
    hidden: bool,
) -> str:
    """One box plot at a fixed y range. Values outside it are not dropped:
    they become a count at the frame edge."""
    n = len(stats)
    plot_w = n * (BAR_W + GAP)
    width = plot_w + PAD_L + PAD_R
    height = PLOT_H + PAD_T + PAD_B
    if y_max <= y_min:
        y_max = y_min + 1.0

    def Y(v: float) -> float:
        return PAD_T + (y_max - v) / (y_max - y_min) * PLOT_H

    def clamp(v: float) -> float:
        return min(max(Y(v), PAD_T), PAD_T + PLOT_H)

    meds = sorted(s["med"] for s in stats)
    overall = meds[len(meds) // 2]
    n_img = sum(s["n"] for s in stats)

    p = [f'<svg id="{svg_id}" viewBox="0 0 {width:.1f} {height:.1f}" '
         f'preserveAspectRatio="xMidYMid meet" '
         f'style="width:100%;{"display:none" if hidden else ""}">']
    p.append(f'<line x1="{PAD_L:.1f}" y1="{PAD_T:.1f}" x2="{PAD_L:.1f}" '
             f'y2="{PAD_T + PLOT_H:.1f}" stroke="#999" stroke-width="1"/>')
    p.append(f'<line x1="{PAD_L:.1f}" y1="{PAD_T + PLOT_H:.1f}" '
             f'x2="{width - PAD_R:.1f}" y2="{PAD_T + PLOT_H:.1f}" '
             f'stroke="#999" stroke-width="1"/>')
    for i in range(5):
        v = y_min + (y_max - y_min) * i / 4.0
        yy = Y(v)
        p.append(f'<line x1="{PAD_L - 4:.1f}" y1="{yy:.1f}" x2="{PAD_L:.1f}" '
                 f'y2="{yy:.1f}" stroke="#999" stroke-width="1"/>')
        p.append(f'<text x="{PAD_L - 7:.1f}" y="{yy + 3:.1f}" font-size="9" '
                 f'text-anchor="end" fill="#555">{v:,.0f}</text>')
    my = Y(overall)
    if PAD_T <= my <= PAD_T + PLOT_H:
        p.append(f'<line x1="{PAD_L:.1f}" y1="{my:.1f}" '
                 f'x2="{width - PAD_R:.1f}" y2="{my:.1f}" stroke="#000" '
                 f'stroke-width="0.6" stroke-dasharray="4,3" opacity="0.4"/>')

    first_b, last_b = stats[0]["beam"], stats[-1]["beam"]
    for i, s in enumerate(stats):
        b = s["beam"]
        cx = PAD_L + i * (BAR_W + GAP) + BAR_W / 2.0 + GAP
        bx = cx - BAR_W / 2.0
        n_out = len(s["out_lo"]) + len(s["out_hi"])
        tip = (f'beam {b}: median={s["med"]:.2f}  '
               f'IQR=[{s["q1"]:.2f}, {s["q3"]:.2f}]  '
               f'whiskers=[{s["w_lo"]:.2f}, {s["w_hi"]:.2f}]  '
               f'full range=[{s["min"]:.2f}, {s["max"]:.2f}]  '
               f'n={s["n"]}  outliers={n_out}')
        p.append(f'<line x1="{cx:.1f}" y1="{clamp(s["w_lo"]):.1f}" '
                 f'x2="{cx:.1f}" y2="{clamp(s["w_hi"]):.1f}" stroke="#666" '
                 f'stroke-width="0.6"/>')
        for v in (s["w_lo"], s["w_hi"]):
            yy = clamp(v)
            p.append(f'<line x1="{cx - 3:.1f}" y1="{yy:.1f}" '
                     f'x2="{cx + 3:.1f}" y2="{yy:.1f}" stroke="#666" '
                     f'stroke-width="0.6"/>')
        # Outlier dots, and a count for the ones off the top/bottom of the
        # frame - the dots are the reason a "robust" axis is not a way of
        # hiding bad images.
        above = below = 0
        for v in s["out_lo"] + s["out_hi"]:
            if v > y_max:
                above += 1
                continue
            if v < y_min:
                below += 1
                continue
            p.append(f'<circle cx="{cx:.1f}" cy="{Y(v):.1f}" r="1.1" '
                     f'fill="#444" fill-opacity="0.45"/>')
        for cnt, yy, sign in ((above, PAD_T + 6.5, "▲"),
                              (below, PAD_T + PLOT_H - 2.5, "▼")):
            if cnt:
                p.append(f'<text x="{cx:.1f}" y="{yy:.1f}" font-size="7" '
                         f'text-anchor="middle" fill="#a50026">{sign}{cnt}'
                         f'<title>beam {b}: {cnt} image(s) past this axis; '
                         f'full range [{s["min"]:.2f}, {s["max"]:.2f}]'
                         f'</title></text>')
        y1, y3 = clamp(s["q1"]), clamp(s["q3"])
        p.append(
            f'<rect class="bq-box" data-abs="{cols_abs[i]}" '
            f'data-rel="{cols_rel[i]}" x="{bx:.1f}" y="{min(y1, y3):.1f}" '
            f'width="{BAR_W:.1f}" height="{max(abs(y1 - y3), 0.5):.1f}" '
            f'fill="{cols_abs[i]}" fill-opacity="0.9" stroke="#555" '
            f'stroke-width="0.6"><title>{_h.escape(tip)}</title></rect>'
        )
        ym = clamp(s["med"])
        p.append(f'<line x1="{bx:.1f}" y1="{ym:.1f}" x2="{bx + BAR_W:.1f}" '
                 f'y2="{ym:.1f}" stroke="#000" stroke-width="1"/>')
        if b % 5 == 0 or b in (first_b, last_b):
            p.append(f'<text x="{cx:.1f}" y="{PAD_T + PLOT_H + 13:.1f}" '
                     f'font-size="8" text-anchor="middle" fill="#555">{b}'
                     f'</text>')

    p.append(f'<text x="{PAD_L + plot_w / 2.0:.1f}" y="{height - 3:.1f}" '
             f'font-size="9" text-anchor="middle" fill="#333">Beam number '
             f'(median of the {n} beam medians = {overall:.2f}, '
             f'{n_img:,} images)</text>')
    p.append("</svg>")
    return "".join(p)


_BTN = ("padding:1px 7px;font-size:11px;border:1px solid #bbb;"
        "background:#f3f3f3;border-radius:3px;cursor:pointer")
_BTN_ON = ("padding:1px 7px;font-size:11px;border:1px solid #555;"
           "background:#555;color:#fff;border-radius:3px;cursor:pointer")

_SCRIPT = """
<script>
(function () {
  var root = document.getElementById('bq-panel');
  if (!root) return;
  function pick(group, val) {
    var btns = root.querySelectorAll('[data-group="' + group + '"]');
    for (var i = 0; i < btns.length; i++) {
      var on = btns[i].getAttribute('data-val') === val;
      btns[i].setAttribute('style', on ? ON : OFF);
    }
  }
  var ON = root.getAttribute('data-on'), OFF = root.getAttribute('data-off');
  function setScale(val) {
    document.getElementById('bq-svg-robust').style.display =
      (val === 'robust') ? '' : 'none';
    document.getElementById('bq-svg-full').style.display =
      (val === 'full') ? '' : 'none';
    pick('scale', val);
    document.getElementById('bq-note-robust').style.display =
      (val === 'robust') ? '' : 'none';
    document.getElementById('bq-note-full').style.display =
      (val === 'full') ? '' : 'none';
  }
  function setColour(val) {
    var boxes = root.querySelectorAll('rect.bq-box');
    for (var i = 0; i < boxes.length; i++) {
      boxes[i].setAttribute('fill',
        boxes[i].getAttribute(val === 'rel' ? 'data-rel' : 'data-abs'));
    }
    pick('colour', val);
    document.getElementById('bq-leg-abs').style.display =
      (val === 'abs') ? '' : 'none';
    document.getElementById('bq-leg-rel').style.display =
      (val === 'rel') ? '' : 'none';
  }
  root.addEventListener('click', function (ev) {
    var t = ev.target.closest ? ev.target.closest('[data-group]') : null;
    if (!t) return;
    if (t.getAttribute('data-group') === 'scale') setScale(t.getAttribute('data-val'));
    else setColour(t.getAttribute('data-val'));
  });
  setScale('robust');
  setColour('abs');
})();
</script>
"""


def panel_html(
    beam_quality_values: Optional[Dict[int, Sequence[float]]],
    cfg: Dict[str, Any],
) -> str:
    """The whole replacement panel body: controls, both charts, legends."""
    if not beam_quality_values:
        return ""
    stats = beam_stats(beam_quality_values)
    if not stats:
        return ""

    cols_abs, cols_rel, leg_abs, leg_rel = _colours(stats, cfg)

    w_lo = min(s["w_lo"] for s in stats)
    w_hi = max(s["w_hi"] for s in stats)
    pad = max(1.0, (w_hi - w_lo) * 0.08)
    f_lo = min(s["min"] for s in stats)
    f_hi = max(s["max"] for s in stats)
    fpad = max(1.0, (f_hi - f_lo) * 0.08)

    n_out = sum(len(s["out_lo"]) + len(s["out_hi"]) for s in stats)
    n_img = sum(s["n"] for s in stats)
    off = sum(1 for s in stats for v in s["out_lo"] + s["out_hi"]
              if v > w_hi + pad or v < w_lo - pad)

    def btn(group: str, val: str, label: str) -> str:
        return (f'<button type="button" data-group="{group}" '
                f'data-val="{val}" style="{_BTN}">{label}</button>')

    controls = (
        '<div style="font-size:11px;display:flex;gap:14px;flex-wrap:wrap;'
        'align-items:center;margin:2px 0 6px 0">'
        '<span>Y axis: ' + btn("scale", "robust", "Whiskers (1.5×IQR)")
        + " " + btn("scale", "full", "Full range") + '</span>'
        '<span>Colour: ' + btn("colour", "abs", "vs threshold") + " "
        + btn("colour", "rel", "vs section") + '</span>'
        '</div>'
    )

    note_robust = (
        f'<div id="bq-note-robust" style="font-size:10px;color:#888">'
        f'Axis set by the whiskers, not by the worst image. Dots are the '
        f'{n_out:,} values beyond a whisker ({100.0 * n_out / max(n_img, 1):.1f}% '
        f'of {n_img:,} images); a red ▲n / ▼n at the frame edge '
        f'counts values past the axis ({off:,} here) — hover it for the '
        f"beam's full range.</div>"
    )
    note_full = (
        f'<div id="bq-note-full" style="font-size:10px;color:#888;'
        f'display:none">Axis spans every image, {f_lo:,.0f} to {f_hi:,.0f}. '
        f'This is how the panel was scaled before: one extreme image '
        f'compresses all {len(stats)} boxes.</div>'
    )
    legends = (
        f'<div id="bq-leg-abs" style="font-size:10px;color:#888">{leg_abs}'
        f'</div>'
        f'<div id="bq-leg-rel" style="font-size:10px;color:#888;'
        f'display:none">{leg_rel}</div>'
    )

    return (
        f'<div id="bq-panel" data-on="{_BTN_ON}" data-off="{_BTN}">'
        + controls
        + _svg(stats, cols_abs, cols_rel, w_lo - pad, w_hi + pad,
               "bq-svg-robust", False)
        + _svg(stats, cols_abs, cols_rel, f_lo - fpad, f_hi + fpad,
               "bq-svg-full", True)
        + note_robust + note_full + legends
        + "</div>" + _SCRIPT
    )
