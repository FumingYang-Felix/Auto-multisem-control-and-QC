"""Batch-level and run-level (wafer) standalone QC reports.

These mirror the structure and function of the acquisition side's own two
reports:

  <batch>/w<N>_<ts>_qc_report.html   -> standalone_qc_batch_report.html
  <run>/w<N>_global_qc_report.html   -> standalone_qc_global_report.html

Same visual language (the CSS below is the acquisition reports' own),
same three-level hierarchy, and the same Accept/Retake/Not-verified controls
writing the same region_review_status.json - see review.py for why that UI is
shared rather than reimplemented.

The difference is what fills the quality columns. The acquisition reports show
whatever its live QC recorded, which in lightweight mode is a NaN placeholder;
these show real all-91-beam scores computed on this machine, plus the
per-beam outlier verdict that only full beam coverage can produce.

Written under the run/batch folders alongside the acquisition side's reports,
never over them - our filenames are distinct.
"""
from __future__ import annotations

import html as _html
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import ledger, portable_paths, review, section_facts
from .config import policy_fingerprint
from .dashboard import DASHBOARD_NAME
from .discover import RegionRef, list_regions, parse_sample_id
from .layout import read_region_timing

BATCH_REPORT_NAME = "standalone_qc_batch_report.html"
GLOBAL_REPORT_NAME = "standalone_qc_global_report.html"
SECTION_REPORT_NAME = "standalone_qc_report.html"
SUMMARY_NAME = "standalone_qc_summary.json"

# The acquisition reports' stylesheet, so all three levels read as one system.
# Copied (not imported) for the same reason gui/theme.py copies the app's
# stylesheet: it is presentation, not logic, and it lives in the other clone.
_CSS = """
  body { margin:0; font-family: ui-monospace, Menlo, Consolas, monospace; background:#f4f4f6; color:#1c1c1f; }
  header { padding:14px 18px; background:#fff; border-bottom:1px solid #d8d8de; }
  header h1 { margin:0 0 4px 0; font-size:16px; font-weight:600; }
  header .summary { font-size:12px; color:#555; }
  .progress { background:#e6e6e6; border-radius:6px; height:10px; width:100%; max-width:420px; overflow:hidden; margin-top:6px; }
  .progress > div { background:#2e8b3d; height:100%; }
  main { padding:14px; }
  h2 { font-size:13px; color:#333; margin:18px 0 6px 0; }
  table { width:100%; border-collapse:collapse; background:#fff; border:1px solid #d8d8de; border-radius:6px; overflow:hidden; margin-bottom:20px; }
  th, td { padding:8px 12px; text-align:left; border-bottom:1px solid #eee; font-size:13px; vertical-align:middle; }
  th { background:#f8f8fa; color:#555; font-weight:600; font-size:12px; }
  tr:last-child td { border-bottom:none; }
  tr:hover { background:#fafafa; }
  a { color:#1d4f9e; text-decoration:none; }
  a:hover { text-decoration:underline; }
  .empty { padding:24px; text-align:center; color:#888; }
  .chip { display:inline-block; padding:2px 8px; border-radius:10px; font-size:11px; font-weight:600; white-space:nowrap; }
  .detail { margin-top:4px; font-size:10px; color:#a33; max-width:340px; white-space:normal; word-break:break-word; }
  .num { text-align:right; font-variant-numeric:tabular-nums; }
  .warnchip { background:#f3e0f7; color:#6b2c7d; }
  /* Not-yet-implemented columns are shown greyed rather than omitted, so
     the gap between this and the Zen Report it replaces stays visible. */
  .tbd { color:#b0b4ba; font-style:italic; }
  th.tbd { color:#9aa0a6; }
  .refresh-box { margin-left:14px; }
  body.hide-q .q-col { display:none; }

  .review-banner { margin-top:10px; padding:8px 12px; background:#fff3cd; color:#7a5b00; border:1px solid #ffe08a; border-radius:4px; font-size:12px; max-width:900px; }
  .review-banner.synced { background:#e6f4ea; color:#1e5b2e; border-color:#bfe3c9; }
  .review-controls { margin-top:8px; display:flex; align-items:center; gap:10px; flex-wrap:wrap; }
  .review-controls button { font:inherit; padding:5px 10px; border:1px solid #aaa; background:#f6f6f8; border-radius:4px; cursor:pointer; font-size:12px; }
  .review-controls button.primary { background:#2b66c2; color:#fff; border-color:#1d4f9e; }
  .review-controls button.primary:hover { background:#3a78d4; }
  .review-controls button:hover { background:#e8e8ec; }
  .review-status-line { font-size:11px; color:#555; }
  .review-status-line .file { color:#1d4f9e; word-break:break-all; }
  .review-group { display:inline-flex; border:1px solid #ccc; border-radius:4px; overflow:hidden; }
  .rev-btn { font:inherit; font-size:11px; padding:4px 8px; border:none; border-right:1px solid #ccc; background:#f6f6f8; color:#444; cursor:pointer; }
  .rev-btn:last-child { border-right:none; }
  .rev-btn:hover { background:#e8e8ec; }
  /* Red, not grey: "not verified" is the state the operator still has work
     to do in, and on a 29-row table a grey chip reads as "nothing here".
     It stops being red the moment Accept or Retake is clicked, because the
     script only puts .active on the chosen one. */
  .rev-btn.active[data-status="not_verified"] { background:#c0392b; color:#fff; font-weight:600; }
  .rev-btn.active[data-status="accepted"] { background:#2e8b3d; color:#fff; font-weight:600; }
  .rev-btn.active[data-status="retake"] { background:#8e44ad; color:#fff; font-weight:600; }
  .rev-reason { display:block; margin-top:4px; font:inherit; font-size:11px; width:220px; padding:3px 6px; border:1px solid #ccc; border-radius:3px; }
  .toast { position:fixed; right:14px; bottom:14px; background:#222; color:#fff; padding:6px 10px; border-radius:4px; font-size:12px; opacity:0; transition:opacity 0.2s; pointer-events:none; }
  .toast.show { opacity:0.95; }
"""

# Acquisition status chips, matching _REGION_STATUS_LABELS in live_qc_support
# so a section reads the same on either side's report.
_ACQ_CHIP = {
    "done": ("Done", "#fff", "#2e8b3d"),
    "running": ("Running…", "#fff", "#1d6fb8"),
    "pending": ("Pending", "#666", "#e6e6e6"),
    "failed": ("Failed", "#fff", "#c0392b"),
    "skipped": ("Skipped", "#fff", "#c98a13"),
    "retake": ("Retake", "#fff", "#8e44ad"),
    "not_started": ("Not started", "#666", "#eee"),
}
_QC_CHIP = {
    "scored": ("Analyzed", "#fff", "#2e8b3d"),
    "awaiting": ("Awaiting analysis", "#7a5b00", "#ffe08a"),
    "no_data": ("No data", "#666", "#eee"),
    "error": ("Error", "#fff", "#c0392b"),
    "none": ("—", "#666", "#f4f4f6"),
}


def _chip(spec: Tuple[str, str, str]) -> str:
    label, fg, bg = spec
    return f'<span class="chip" style="background:{bg};color:{fg};">{_html.escape(label)}</span>'


def _esc(v: Any) -> str:
    return _html.escape(str(v))


def _dash(v: Any, nd: int = 2, suffix: str = "") -> str:
    """A number, or an em dash when it isn't one - matching the acquisition
    reports' convention of never printing a misleading 0 for "unknown"."""
    if v is None:
        return "—"
    if isinstance(v, str):
        return _esc(v) if v.strip() else "—"
    try:
        f = float(v)
    except (TypeError, ValueError):
        return "—"
    if math.isnan(f):
        return "—"
    return f"{f:.{nd}f}{suffix}"


def _duration(seconds: Any) -> str:
    try:
        s = float(seconds)
    except (TypeError, ValueError):
        return "—"
    if math.isnan(s) or s <= 0:
        return "—"
    if s < 60:
        return f"{s:.1f}s"
    m, rem = divmod(s, 60)
    if m < 60:
        return f"{int(m)}m {rem:.1f}s"
    h, m = divmod(int(m), 60)
    return f"{h}h {m}m"


def _tick(ok: Optional[int], total: Optional[int]) -> str:
    """A checkmark when every mFoV has the file, the shortfall when not.

    "23/231" is far more actionable than a bare cross: it says whether one
    tile is missing or the whole section never wrote them.
    """
    if ok is None or not total:
        return "—"
    if ok >= total:
        return '<span style="color:#2e8b3d;">&#10003;</span>'
    return (
        f'<span class="chip" style="background:#f8d7da;color:#a33;">'
        f"{ok}/{total}</span>"
    )


def _bool_tick(v: Optional[bool]) -> str:
    if v is None:
        return "—"
    return ('<span style="color:#2e8b3d;">&#10003;</span>' if v
            else '<span style="color:#a33;">&#10007;</span>')


def _fsp(facts: Dict[str, Any]) -> str:
    """Focus support points as succeeded/attempted, Zen's "1/1" format."""
    att = facts.get("fsp_attempted")
    ok = facts.get("fsp_succeeded")
    if not att:
        return "—"
    cell = f"{ok}/{att}"
    if not ok:
        return f'<span class="chip" style="background:#f8d7da;color:#a33;">{cell}</span>'
    return cell



def _imqa_cell(s: Dict[str, Any]) -> str:
    """mean +/- std over the non-negative mFoVs, with the dropped ones named.

    The count is not decoration. Excluding negatives removes empty-resin
    tiles, which is the point, but it would also hide a genuinely defocused
    one, so the number that was dropped stays on screen (as a superscript
    once it is more than a rounding detail) and the all-inclusive figure is
    one hover away.
    """
    if s.get("imqa_mean") is None:
        return "&mdash;"
    cell = f'{_dash(s["imqa_mean"], 1)} ± {_dash(s.get("imqa_std"), 1)}'
    n_neg = int(s.get("imqa_n_negative") or 0)
    n_use = int(s.get("imqa_n") or 0)
    bits = [f"mean ± sd over the {n_use} non-blank mFoV(s) scoring >= 0"]
    if n_neg:
        m_all, sd_all = s.get("imqa_mean_all"), s.get("imqa_std_all")
        bits.append(
            f"{n_neg} further mFoV(s) scored below zero and are excluded "
            "(no tissue, or badly out of focus)"
        )
        if m_all is not None:
            bits.append(
                f"including them: {float(m_all):.1f} ± "
                f"{float(sd_all or 0.0):.1f} over {n_neg + n_use}"
            )
    title = _html.escape("; ".join(bits))
    if not n_neg:
        return f'<span title="{title}">{cell}</span>'
    frac = n_neg / max(n_neg + n_use, 1)
    # Amber once a large part of the region scored negative: still usually
    # just a wide ROI, but worth a second look before the mean is trusted.
    colour = "#b36b00" if frac >= 0.25 else "#888"
    return (f'<span title="{title}">{cell} '
            f'<sup style="font-size:9px;color:{colour}">-{n_neg}</sup></span>')

def _focus_plane_ok(facts: Dict[str, Any]) -> Optional[bool]:
    """Was a focus/stig plane actually established for this section?

    Replaces a check for `<section>/FocusMap.txt`, which this pipeline never
    writes - so that column read as failed on every section and told nobody
    anything. A measured focus support point is the real evidence that a
    plane was established.
    """
    if not facts:
        return None
    if facts.get("fsp_attempted"):
        return bool(facts.get("fsp_succeeded"))
    if facts.get("plane_method"):
        return True
    return None


# How the focus/stig plane for a section was established, as focus_map names
# it in the console log. The raw constants are long and shouty, so each gets a
# short label with the original on hover - an operator comparing batches wants
# to see at a glance that one ran fiducial-file-direct and another a single
# offset point.
_FOCUS_STRATEGY_LABELS = {
    "FIDUCIAL_FILE_DIRECT_PER_REGION": "fiducial file, direct",
    "FIDUCIAL_FOCUS_POINT_XY_REGION_OFFSET": "fiducial point + offset",
    "PREV_FOCUSMAP": "previous focus map",
    "FOCUS_MAP": "focus map",
    "AF_MINIMIZE_OL": "focus map, AF minimize-OL",
}


def _focus_strategy_cell(facts):
    """Which focus strategy imaged this section."""
    method = (facts or {}).get("plane_method")
    if not method:
        return '<span class="muted">&mdash;</span>'
    method = str(method)
    label = _FOCUS_STRATEGY_LABELS.get(method)
    if label is None:
        # An unmapped method is shown verbatim rather than hidden: a new
        # strategy appearing on the acquisition side should be visible here
        # the first time it is used, not silently blank.
        return f'<span title="{_esc(method)}">{_esc(method.lower())}</span>'
    n = ((facts.get("support_points") or {}).get("points") or [])
    suffix = f' <span class="muted">({len(n)} pts)</span>' if len(n) > 1 else ""
    return f'<span title="{_esc(method)}">{_esc(label)}</span>{suffix}'


def _plane_fit(summary: Dict[str, Any], quantity: str) -> Dict[str, Any]:
    for f in summary.get("plane_fits") or []:
        if f.get("quantity") == quantity:
            return f
    return {}


def _focus_plane_cell(facts: Dict[str, Any], summary: Dict[str, Any]) -> str:
    """Was a plane established, and how steep is the one we measured?

    The tick alone said only "a focus support point succeeded". The slope is
    the number that distinguishes a section sitting flat from one tilted
    enough to walk out of focus across itself - and the two batches measured
    so far differ by 100x in it, so it is worth seeing per section rather
    than only inside a dashboard.
    """
    tick = _bool_tick(_focus_plane_ok(facts))
    fit = _plane_fit(summary, "ol")
    grad = (fit.get("gradient") or {})
    slope = grad.get("grad_um_per_mm")
    if slope is None or (isinstance(slope, float) and math.isnan(slope)):
        return tick
    az = grad.get("azimuth_deg")
    tilt = grad.get("tilt_deg")
    across = _focus_change_across(fit, slope)
    title = _html.escape(
        f"OL focus plane slope {slope:.4g} um/mm"
        + (f" = {across * 1000.0:.3g} nm of focus across the whole section"
           if across is not None else "")
        + (f", steepest ascent at {az:.0f}° in the xy plane"
           if az is not None else "")
        + (f", {tilt:.3g}° between the fitted plane and xy"
           if tilt is not None else "")
        + ". Full plots in the section View."
    )
    return (f'{tick} <span class="muted" style="font-size:10px;" '
            f'title="{title}">{_slope_text(slope)}</span>')


def _slope_text(slope: float) -> str:
    """The slope at a scale it can be read at.

    It used to be printed as "%.2f um/mm" unconditionally. Real sections
    here fit planes around 1.3e-4 um/mm - which is correct, and matches
    focus_map's own commanded plane to four significant figures - so every
    section in the batch displayed "0.00" and the column looked broken or
    unpopulated. Below 0.1 um/mm the same number is shown in nm/mm, where
    it is an ordinary small number instead of a rounding artefact.
    """
    a = abs(slope)
    if a == 0.0:
        return "0 &micro;m/mm"
    if a < 0.1:
        return f"{slope * 1000.0:.3g} nm/mm"
    return f"{slope:.3g} &micro;m/mm"


def _focus_change_across(fit: Dict[str, Any],
                         slope_um_per_mm: float) -> Optional[float]:
    """Microns of focus between the two furthest-apart points of the fit.

    A slope needs a distance before it means anything, and a section is only
    a few millimetres across: 1.3e-4 um/mm sounds small and IS small, but
    saying so costs one multiplication and saves the operator doing it.
    """
    ext = fit.get("extent_um") or {}
    dx, dy = ext.get("x"), ext.get("y")
    if dx is None or dy is None:
        return None
    try:
        diag_mm = math.hypot(float(dx), float(dy)) / 1000.0
    except (TypeError, ValueError):
        return None
    return slope_um_per_mm * diag_mm


def _scanfault_cell(summary: Dict[str, Any]) -> str:
    """mFoVs whose checked sFoV showed a scan fault."""
    sc = summary.get("scan_checks") or {}
    judged = sc.get("scanfault_n_judged")
    if not judged:
        return "—"
    n_fail = int(sc.get("scanfault_n_fail") or 0)
    if not n_fail:
        return '<span style="color:#2e8b3d;">&#10003;</span>'
    return (
        f'<span class="chip" style="background:#f8d7da;color:#a33;">'
        f"{n_fail}/{judged}</span>"
    )


def _overlap_cell(summary: Dict[str, Any]) -> str:
    """mFoV coverage: a tick plus the median adjacent-pair overlap, or the
    gap count when coverage failed."""
    ov = summary.get("overlap") or {}
    if ov.get("passed") is None:
        return "—"
    med = ov.get("pair_overlap_median_frac")
    if ov["passed"]:
        pct = f" {100.0 * med:.0f}%" if med is not None else ""
        return (
            f'<span style="color:#2e8b3d;">&#10003;</span>'
            f'<span class="muted" style="font-size:10px;">{pct}</span>'
        )
    bits = []
    if ov.get("n_gap_cells"):
        bits.append(f"{ov['n_gap_cells']} gap")
    if ov.get("weak_pairs"):
        bits.append(f"{len(ov['weak_pairs'])} weak")
    return (
        f'<span class="chip" style="background:#f8d7da;color:#a33;">'
        f"{', '.join(bits) or 'fail'}</span>"
    )


def _jitter_cell(summary: Dict[str, Any]) -> str:
    n = summary.get("n_jittery")
    if n is None:
        return "—"
    if not n:
        return '<span style="color:#2e8b3d;">&#10003;</span>'
    return f'<span class="chip warnchip">{n}</span>'


def _status_chip(row: "SectionRow", facts: Dict[str, Any]):
    """Acquisition status, refined by what console.log recorded.

    A section the manifest calls "done" but whose focus support points all
    failed is an AFAS failure - the Zen Report treats that as its own status
    because the images exist but are not worth keeping.
    """
    if row.region.status == "done" and facts.get("fsp_attempted"):
        if not facts.get("fsp_succeeded"):
            return ("AFAS fail", "#fff", "#c0392b")
    return _ACQ_CHIP.get(row.region.status, _ACQ_CHIP["pending"])


def _rel(target: Path, start: Path) -> str:
    """A relative href so the report keeps working if the tree is copied or
    opened over a different drive mapping."""
    try:
        return str(target.relative_to(start)).replace("\\", "/")
    except ValueError:
        return target.as_uri()


# ---------------------------------------------------------------------------
# data collection
# ---------------------------------------------------------------------------
@dataclass
class SectionRow:
    region: RegionRef
    summary: Optional[Dict[str, Any]] = None
    acq_timing: Optional[Dict[str, Any]] = None
    cached_facts: Optional[Dict[str, Any]] = None

    @property
    def facts(self) -> Dict[str, Any]:
        """Acquisition-derived facts: duration, FSP, file checks, AF/AS.

        None of these need image scoring, so an unscored section still fills
        those columns - which is the point of the post-acquisition workflow,
        where pointing at a batch should show a useful table immediately.
        Prefers the scoring summary (freshest), falling back to the standalone
        facts cache written by a scan.
        """
        from_summary = (self.summary or {}).get("facts")
        if isinstance(from_summary, dict) and from_summary:
            return from_summary
        return self.cached_facts or {}

    @property
    def section_dir(self) -> Path:
        return self.region.region_dir

    @property
    def qc_state(self) -> str:
        if self.summary is None:
            return "awaiting" if self.region.status == "done" else "none"
        if self.summary.get("errors"):
            return "error"
        if not self.summary.get("scored_tile_idxs"):
            return "no_data"
        return "scored"

    @property
    def median_q(self) -> Optional[float]:
        return (self.summary or {}).get("quality_median")

    @property
    def outlier_beams(self) -> List[int]:
        return [
            int(b["beam_index"])
            for b in (self.summary or {}).get("outlier_beams", [])
            if isinstance(b, dict) and b.get("beam_index") is not None
        ]

    @property
    def report_path(self) -> Optional[Path]:
        """What the View column links to. The interactive dashboard when it
        exists - that is the page an operator wants - falling back to the
        plain per-section summary page."""
        d = self.section_dir / "StandaloneQC" / DASHBOARD_NAME
        if d.is_file():
            return d
        p = self.section_dir / "StandaloneQC" / SECTION_REPORT_NAME
        return p if p.is_file() else None

    @property
    def summary_page(self) -> Optional[Path]:
        p = self.section_dir / "StandaloneQC" / SECTION_REPORT_NAME
        return p if p.is_file() else None


def batch_timing(batch_dir: Path) -> Dict[str, Any]:
    """Per-batch focus / stig / move / QC-catch-up / total, via
    live_qc_support.aggregate_batch_timing - the same public function that
    fills these columns in the acquisition side's own Batches table, so both
    reports show identical numbers rather than two independent tallies.

    Guarded: an upstream rename degrades to empty timings (columns show an em
    dash) instead of taking the whole report down.
    """
    try:
        from live_qc_support import aggregate_batch_timing
    except Exception:
        return {}
    try:
        return aggregate_batch_timing(batch_dir) or {}
    except Exception:
        return {}


def partial_retakes(run_root: Path) -> Dict[str, List[str]]:
    """Pending per-mFoV retakes flagged in a section dashboard's selection
    panel - the acquisition report's "Partial retake" column. A separate
    mechanism from the whole-section Accept/Retake buttons."""
    try:
        from live_qc_support import scan_partial_retakes_for_wafer
    except Exception:
        return {}
    try:
        return scan_partial_retakes_for_wafer(run_root) or {}
    except Exception:
        return {}


def _read_summary(section_dir: Path, subdir: str) -> Optional[Dict[str, Any]]:
    path = section_dir / subdir / SUMMARY_NAME
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def collect_rows(
    run_root: str | Path,
    cfg: Dict[str, Any],
    batch_filter: Optional[str] = None,
) -> List[SectionRow]:
    """One row per section known to any batch under run_root."""
    subdir = str(cfg.get("output_subdir", "StandaloneQC"))
    rows: List[SectionRow] = []
    for region in list_regions(run_root, batch_filter):
        rows.append(
            SectionRow(
                region=region,
                summary=_read_summary(region.region_dir, subdir),
                acq_timing=read_region_timing(region.region_dir) or None,
                # Cache read only - never gathered here. Gathering is ~2.3s
                # per section and would stall every report rebuild; see
                # scan_sections() for the explicit pass that populates it.
                cached_facts=section_facts.load_cached(region.region_dir, subdir),
            )
        )
    return rows


def scan_sections(
    run_root: str | Path,
    cfg: Dict[str, Any],
    batch_filter: Optional[str] = None,
    only_missing: bool = True,
    on_progress: Optional[Any] = None,
) -> int:
    """Gather and cache acquisition-derived facts for every finished section.

    Reads console.log, timing_summary.txt and directory listings - no image
    data - so it is safe to run against a batch while the next one is still
    imaging. ~2.3s per section. Returns how many sections were scanned.
    """
    run_root = Path(run_root)
    subdir = str(cfg.get("output_subdir", "StandaloneQC"))
    regions = [
        r for r in list_regions(run_root, batch_filter)
        if r.status in ("done", "failed", "skipped") and r.region_dir.is_dir()
    ]
    timing_by_batch: Dict[Path, Dict[str, Any]] = {}
    n = 0
    for i, region in enumerate(regions, start=1):
        if only_missing and section_facts.load_cached(region.region_dir, subdir):
            continue
        if region.block_dir not in timing_by_batch:
            timing_by_batch[region.block_dir] = section_facts.parse_timing_summary(
                region.block_dir
            )
        try:
            section_facts.load_or_collect(
                region.region_dir, region.name,
                timing_by_batch[region.block_dir], subdir, force=not only_missing,
            )
            n += 1
        except Exception as exc:
            print(f"[StandaloneQC] facts scan failed for {region.key}: {exc}")
        if on_progress:
            on_progress(i, len(regions), region.name)
    return n


# ---------------------------------------------------------------------------
# shared page scaffolding
# ---------------------------------------------------------------------------
def _page(title: str, header_html: str, main_html: str, script_html: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{_esc(title)}</title>
<style>{_CSS}</style>
</head>
<body>
<header>
{header_html}
</header>
<main>
{main_html}
</main>
{script_html}
</body>
</html>
"""


# ---------------------------------------------------------------------------
# batch report
# ---------------------------------------------------------------------------
def write_batch_report(
    run_root: str | Path,
    batch_dir: str | Path,
    cfg: Dict[str, Any],
    rows: Optional[List[SectionRow]] = None,
) -> Optional[Path]:
    """`<batch>/standalone_qc_batch_report.html` - one row per section in
    this batch, mirroring the acquisition side's per-batch report."""
    run_root = Path(run_root)
    batch_dir = Path(batch_dir)
    if rows is None:
        rows = collect_rows(run_root, cfg, batch_filter=batch_dir.name)
    rows = [r for r in rows if r.region.block_dir == batch_dir]
    if not rows:
        return None

    review_status = review.load_review_status(run_root)
    review_defaults: Dict[str, Dict[str, str]] = {}
    sample = parse_sample_id(batch_dir.name) or "—"
    refresh_s = int(cfg.get("report_auto_refresh_s", 30) or 0)

    n_scored = sum(1 for r in rows if r.qc_state == "scored")
    all_outliers = sorted({b for r in rows for b in r.outlier_beams})

    # Column set follows the Zen Report this replaces. Five of its columns -
    # sFoV stitch, mFoV stitch, skew, scanfault, ROI align - need image
    # analysis that does not exist yet; they are shown as explicit TBD rather
    # than omitted, so the gap is visible instead of looking finished.
    body: List[str] = [
        "<table><thead><tr>"
        "<th>Section</th>"
        # "Imaging Status" rather than "Status": this is acquisition's
        # verdict on whether the section was imaged, not a QC verdict.
        "<th>Imaging Status</th>"
        # The former "QC" column (Scored / Awaiting QC) is gone - it restated
        # what the IMQA column already shows by being empty or filled, and
        # "scored" was not meaningful to read.
        '<th class="num">mFoV #</th>'
        '<th class="num" title="Focus support points: measured successfully '
        '/ attempted, parsed from this section\'s console.log">FSP #</th>'
        '<th title="How the focus/stigmation plane was established for this section, as named in its console.log. Hover a value for the exact constant.">focus strategy</th>'
        '<th class="num">Duration</th>'
        '<th title="Every mFoV has its full complement of beam images">full-res imgs</th>'
        '<th title="Every mFoV has its beam thumbnails">thumb imgs</th>'
        "<th>full-res coord</th><th>thum coord</th>"
        "<th>metadata</th>"
        '<th title="Was a focus/stig plane established for this section '
        '(a [NewPlane] line in its console.log), and the measured slope of '
        'the fitted OL plane in microns of focus per mm of stage travel. '
        'Hover for the steepest-ascent azimuth and the angle to the xy '
        'plane; the x,y,OL and stig plots are in the section View.">'
        'focus plane</th>'
        '<th title="Do the mFoVs cover the section without gaps, and do '
        'adjacent mFoVs overlap? Checked from image_coordinates.txt - the '
        'geometry ZEN recorded, not the image content.">mFoV overlap</th>'
        "<th>jitter</th>"
        '<th title="One sFoV per mFoV checked for dead scan lines, abrupt '
        'torn bands and duplicated rows. Shows the number of mFoVs that '
        'failed.">scanfault</th>'
        '<th class="tbd" title="Not implemented yet">ROI align</th>'
        '<th class="num q-col" title="Section image quality: mean +/- '
        'standard deviation over the non-blank mFoVs that scored at or '
        'above zero. Quality is a contrast ratio minus one, so an mFoV '
        'imaged off the tissue scores negative; those are excluded so the '
        'spread reflects focus rather than how much resin the region '
        'covers. Hover a value for the excluded count and for the figure '
        'with them included.">IMQA</th>'
        '<th title="Beams whose median quality is a statistical outlier vs. '
        'the other beams across this section\'s analyzed mFoVs">worst beams</th>'
        "<th>Log</th><th>View</th>"
        "<th>Review (does it actually look in focus?)</th>"
        "</tr></thead><tbody>"
    ]

    for r in sorted(rows, key=lambda x: _natural_key(x.region.name)):
        cell, entry = review.cell_html(r.region.name, review_status)
        review_defaults[r.region.name] = entry
        s = r.summary or {}
        facts = r.facts
        files = facts.get("files") or {}
        n_mf = files.get("n_mfovs") or s.get("n_tiles")

        log = r.section_dir / "console.log"
        log_html = (
            f'<a href="{_esc(_rel(log, batch_dir))}" target="_blank">console.log</a>'
            if log.is_file() else "—"
        )
        rep = r.report_path
        rep_html = (
            f'<a href="{_esc(_rel(rep, batch_dir))}" target="_blank">view</a>'
            if rep else "—"
        )
        outliers = (
            f'<span class="chip warnchip">{", ".join(str(b) for b in r.outlier_beams)}</span>'
            if r.outlier_beams else "—"
        )
        detail = (
            f'<div class="detail">{_esc(r.region.detail)}</div>'
            if r.region.detail else ""
        )
        imqa = _imqa_cell(s)

        body.append(
            "<tr>"
            f"<td><strong>{_esc(r.region.name)}</strong></td>"
            f"<td>{_chip(_status_chip(r, facts))}{detail}</td>"
            f'<td class="num">{_dash(n_mf, 0)}</td>'
            f'<td class="num">{_fsp(facts)}</td>'
            f"<td>{_focus_strategy_cell(facts)}</td>"
            f'<td class="num">{_duration(facts.get("duration_s"))}</td>'
            f"<td>{_tick(files.get('full_res_ok'), n_mf)}</td>"
            f"<td>{_tick(files.get('thumb_ok'), n_mf)}</td>"
            f"<td>{_tick(files.get('coords_ok'), n_mf)}</td>"
            f"<td>{_tick(files.get('thumb_coords_ok'), n_mf)}</td>"
            f"<td>{_tick(files.get('metadata_ok'), n_mf)}</td>"
            f"<td>{_focus_plane_cell(facts, s)}</td>"
            f"<td>{_overlap_cell(s)}</td>"
            f"<td>{_jitter_cell(s)}</td>"
            f"<td>{_scanfault_cell(s)}</td>"
            f'<td class="tbd">TBD</td>'
            f'<td class="num q-col">{imqa}</td>'
            f"<td>{outliers}</td>"
            f"<td>{log_html}</td><td>{rep_html}</td>"
            f"<td>{cell}</td>"
            "</tr>"
        )
    body.append("</tbody></table>")
    body.append(_fsp_chart_html(rows))

    # Emitted unconditionally, NOT gated on the run report already existing:
    # write_all_reports writes the batch reports first, so on a first run that
    # file does not exist yet at this point and the link would be silently
    # dropped from every batch report. Both are always written together, so by
    # the time anyone opens this page the target is there.
    global_report = run_root / GLOBAL_REPORT_NAME
    up_link = f' · <a href="{_esc(_rel(global_report, batch_dir))}">whole run ↑</a>'
    header = f"""
  <h1>Standalone QC — {_esc(batch_dir.name)}</h1>
  <div class="summary"><strong>Sample {_esc(sample)}</strong> · run <code>{_esc(run_root.name)}</code>
    · {len(rows)} section(s) in <code>{_esc(batch_dir)}</code>{up_link}</div>
  <div class="summary">{n_scored} of {len(rows)} section(s) have image quality{
      ' · outlier beam(s) across this batch: <strong>' + ', '.join(str(b) for b in all_outliers) + '</strong>'
      if all_outliers else ''}</div>
  <div class="summary" style="margin-top:8px;">
    <button type="button" id="btn-toggle-q">Hide image quality</button>
    {_auto_refresh_controls_html(refresh_s)}
  </div>
{review.controls_header_html(review.retake_sync_note())}
"""
    script = (
        review.controls_script_html(run_root, review_defaults)
        + _TOGGLE_JS
        + _auto_refresh_js(refresh_s)
    )
    out = batch_dir / BATCH_REPORT_NAME
    out.write_text(
        portable_paths.apply(
            _page(f"Standalone QC — {batch_dir.name}", header,
                  "".join(body), script),
            cfg),
        encoding="utf-8",
    )
    return out


def _fsp_chart_html(rows: List["SectionRow"]) -> str:
    """Focus / stig / move per focus support point, across the batch.

    One cluster per SECTION rather than per support point, because every
    section acquired so far establishes exactly one - so a per-section chart
    is the one that shows more than a single cluster, and comparing sections
    is the question it answers ("why did that one take 26s of B2F when the
    rest took 1.4s?"). A section with several support points contributes the
    sum of its own, and its own dashboard breaks them out individually.
    """
    from .plot3d import BAR_COLOURS, grouped_bars_svg

    groups: List[str] = []
    per: Dict[str, List[Optional[float]]] = {
        "focus_s": [], "stig_s": [], "move_s": [], "b2f_s": []
    }
    for r in sorted(rows, key=lambda x: _natural_key(x.region.name)):
        fsps = (r.facts or {}).get("fsp_timings") or []
        if not fsps:
            continue
        groups.append(r.region.name)
        for key in per:
            vals = [f.get(key) for f in fsps if f.get(key) is not None]
            per[key].append(sum(vals) if vals else None)
    if not groups:
        return ""

    series = []
    missing = []
    for label, key, colour in (
        ("focus (AF)", "focus_s", BAR_COLOURS["focus"]),
        ("stig (AS)", "stig_s", BAR_COLOURS["stig"]),
        ("move", "move_s", BAR_COLOURS["move"]),
        ("B2F", "b2f_s", "#8a6fb0"),
    ):
        if all(v is None for v in per[key]):
            missing.append(label)
            continue
        series.append((label, colour, per[key]))
    if not series:
        return ""

    note = ""
    if missing:
        note = (f"<p class='muted' style='font-size:11px'>Not timed in this "
                f"run's log format: {', '.join(missing)}. Only the steps the "
                f"acquisition log timed are charted - a missing bar means "
                f"not measured, not zero.</p>")
    svg = grouped_bars_svg(
        groups, series,
        title="Focus support point cost by section", unit="s",
        width=min(980, max(420, 42 * len(groups) + 90)),
    )
    return (
        "<h2 style='margin:18px 0 4px 0;font-size:15px'>Focus support point "
        "timing</h2>"
        "<p class='muted' style='font-size:11px'>Stage move, B2F alignment, "
        "autofocus (AF) and autostig (AS) for each section's focus support "
        "point. Hover a bar for its exact value.</p>"
        + note + f"<div class='card' style='padding:8px'>{svg}</div>"
    )


_TOGGLE_JS = """
<script>
(function() {
  const btn = document.getElementById('btn-toggle-q');
  if (!btn) return;
  btn.onclick = () => {
    document.body.classList.toggle('hide-q');
    btn.textContent = document.body.classList.contains('hide-q')
      ? 'Show image quality' : 'Hide image quality';
  };
})();
</script>
"""


def _auto_refresh_controls_html(period_s: int) -> str:
    if period_s <= 0:
        return ""
    return (
        '<span class="refresh-box">'
        '<button type="button" id="btn-auto-refresh">Pause auto-refresh</button>'
        '<span id="auto-refresh-status" class="review-status-line"></span>'
        "</span>"
    )


def _auto_refresh_js(period_s: int) -> str:
    """Reload the page periodically so one left open during a run keeps
    showing current results.

    A full reload, not an in-place update: these pages are opened over
    file://, where fetch()/XHR against local files is blocked, so there is no
    way to pull fresh rows without re-reading the document. Two things make
    that non-annoying:

      - scroll position is saved and restored, so the reload does not throw
        away where you were in a 27-row table;
      - the countdown resets while a field has focus, so a half-typed retake
        reason is never wiped mid-sentence. Accept/Retake clicks themselves
        are already autosaved within 400ms and reconciled from disk on load.
    """
    if period_s <= 0:
        return ""
    return f"""
<script>
(function() {{
  const PERIOD = {int(period_s)};
  const KEY = 'sqc_scroll_' + location.pathname;
  try {{
    const y = sessionStorage.getItem(KEY);
    if (y !== null) window.scrollTo(0, parseFloat(y));
  }} catch (e) {{}}

  const btn = document.getElementById('btn-auto-refresh');
  const out = document.getElementById('auto-refresh-status');
  if (!btn || !out) return;

  let paused = false;
  let left = PERIOD;
  const stamp = new Date().toLocaleTimeString();

  function label() {{
    out.textContent = paused
      ? ' · generated ' + stamp + ' · auto-refresh paused'
      : ' · generated ' + stamp + ' · reloading in ' + left + 's';
  }}
  btn.onclick = () => {{
    paused = !paused;
    btn.textContent = paused ? 'Resume auto-refresh' : 'Pause auto-refresh';
    left = PERIOD;
    label();
  }};

  setInterval(() => {{
    if (paused) {{ label(); return; }}
    const el = document.activeElement;
    if (el && (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA')) {{
      left = PERIOD;   // someone is typing - do not reload under them
      label();
      return;
    }}
    left -= 1;
    label();
    if (left <= 0) {{
      try {{ sessionStorage.setItem(KEY, String(window.scrollY)); }} catch (e) {{}}
      location.reload();
    }}
  }}, 1000);
  label();
}})();
</script>
"""


def _natural_key(name: str) -> Tuple:
    """S2R3 before S10R3, matching how an operator reads a section list."""
    import re
    return tuple(
        int(part) if part.isdigit() else part.lower()
        for part in re.split(r"(\d+)", str(name))
    )


# ---------------------------------------------------------------------------
# global (run-level) report
# ---------------------------------------------------------------------------
def write_global_report(
    run_root: str | Path,
    cfg: Dict[str, Any],
    rows: Optional[List[SectionRow]] = None,
) -> Optional[Path]:
    """`<run>/standalone_qc_global_report.html` - every section across every
    batch, plus a per-batch summary, mirroring the acquisition side's
    wafer-level global report."""
    run_root = Path(run_root)
    if rows is None:
        rows = collect_rows(run_root, cfg)
    if not rows:
        return None

    review_status = review.load_review_status(run_root)
    review_defaults: Dict[str, Dict[str, str]] = {}
    fingerprint = policy_fingerprint(cfg)
    entries = ledger.load(run_root)

    batches = sorted({r.region.block_dir for r in rows}, key=lambda p: p.name)
    samples = sorted({parse_sample_id(b.name) or "—" for b in batches})

    # Collapse attempts: the same section can be imaged in several batches.
    by_section: Dict[str, List[SectionRow]] = {}
    for r in rows:
        by_section.setdefault(r.region.name, []).append(r)

    n_sections = len(by_section)
    n_scored = sum(
        1 for attempts in by_section.values()
        if any(a.qc_state == "scored" for a in attempts)
    )
    pct = (100.0 * n_scored / n_sections) if n_sections else 0.0
    awaiting = [
        name for name, attempts in by_section.items()
        if any(a.region.status == "done" for a in attempts)
        and not any(a.qc_state == "scored" for a in attempts)
    ]

    # --- sections table ---
    # ONE ROW PER SECTION, showing its most recent batch.
    #
    # The Attempt column ("attempt 1/2 — <batch>") and the Analyzed column
    # are both gone. Dropping Attempt means multiple rows per section could
    # no longer be told apart, so the rows are collapsed instead: a section
    # imaged in several batches shows the newest, and its earlier attempts
    # stay reachable through that batch's own report. Batch folder names are
    # timestamped, so sorting by name is chronological - and unlike mtime it
    # is not disturbed by us writing a report into the folder.
    partials = partial_retakes(run_root)
    body: List[str] = [
        f"<h2>Sections ({n_sections})</h2>",
        "<table><thead><tr>"
        "<th>Section</th><th>Imaging Status</th>"
        "<th>View</th><th>Log</th>"
        '<th class="num q-col" title="Median all-beam quality over the mFoVs '
        'that were fully analyzed">Image quality</th>'
        '<th title="Beams whose median quality is a statistical outlier vs. '
        'the other beams - a hardware suspicion, not mFoV-to-mFoV noise">'
        "Outlier beams</th>"
        "<th>Partial retake</th>"
        "<th>Review (does it actually look in focus?)</th>"
        "</tr></thead><tbody>",
    ]

    for name in sorted(by_section, key=_natural_key):
        attempts = sorted(by_section[name], key=lambda a: a.region.block_dir.name)
        a = attempts[-1]                       # newest batch
        cell, entry = review.cell_html(name, review_status)
        review_defaults[name] = entry
        pend = partials.get(name) or []
        partial_html = (
            f'<span class="chip warnchip">{len(pend)} pending</span>' if pend else "—"
        )
        rep = a.report_path
        view_html = (
            f'<a href="{_esc(_rel(rep, run_root))}" target="_blank">view</a>'
            if rep else "—"
        )
        log = a.section_dir / "console.log"
        log_html = (
            f'<a href="{_esc(_rel(log, run_root))}" target="_blank">console.log</a>'
            if log.is_file() else "—"
        )
        outliers = (
            f'<span class="chip warnchip">{", ".join(str(b) for b in a.outlier_beams)}</span>'
            if a.outlier_beams else "—"
        )
        detail = (
            f'<div class="detail">{_esc(a.region.detail)}</div>'
            if a.region.detail else ""
        )
        # Say so when earlier attempts exist, so collapsing never silently
        # hides that a section was imaged more than once.
        if len(attempts) > 1:
            detail += (
                f'<div class="detail" style="color:#666;">newest of '
                f'{len(attempts)} batches</div>'
            )
        body.append(
            "<tr>"
            f"<td><strong>{_esc(name)}</strong></td>"
            f"<td>{_chip(_ACQ_CHIP.get(a.region.status, _ACQ_CHIP['pending']))}{detail}</td>"
            f"<td>{view_html}</td><td>{log_html}</td>"
            f'<td class="num q-col">{_dash(a.median_q)}</td>'
            f"<td>{outliers}</td>"
            f"<td>{partial_html}</td>"
            f"<td>{cell}</td>"
            "</tr>"
        )
    body.append("</tbody></table>")

    # --- batches table ---
    # The acquisition report's own column set (Batch / Regions / Done /
    # Failed / Focus / Stig / Move / QC catch-up / Total imaging time), from
    # the same aggregate_batch_timing() it uses, with our QC columns appended.
    body.append(
        f"<h2>Batches ({len(batches)}) "
        '<span style="font-weight:normal;color:#777;">'
        "(* = see tooltip on the marked cell)</span></h2>"
    )
    body.append(
        "<table><thead><tr>"
        "<th>Batch</th><th>Sample</th>"
        '<th class="num">Sections</th><th class="num">Done</th>'
        '<th class="num">Failed</th>'
        '<th class="num">Focus</th><th class="num">Stig</th>'
        '<th class="num" title="Stage move + settle between mFoV tiles during '
        'imaging - not the per-focus-point move, which is inside Focus">'
        "Move (mfov→mfov)</th>"
        # QC catch-up is deliberately NOT a column. It measured how long
        # acquisition waited for its own live-QC subprocesses after imaging
        # finished - routinely its full 300s-per-section timeout, and the
        # reason scoring moved to this machine in the first place. It should
        # now always be ~0, so a column of zeros earns nothing. It is still
        # read, still in the summary JSON, and still raises a warning if a
        # lightweight-mode run ever shows a real value (see score.py).
        '<th class="num">Total imaging time</th>'
        '<th class="num">Analyzed</th>'
        '<th class="num q-col">Median quality</th><th>Outlier beams</th>'
        '<th class="num">QC time</th><th>Report</th>'
        "</tr></thead><tbody>"
    )
    for b in batches:
        brows = [r for r in rows if r.region.block_dir == b]
        scored = [r for r in brows if r.qc_state == "scored"]
        qs = [r.median_q for r in scored if isinstance(r.median_q, (int, float))]
        med = (sorted(qs)[len(qs) // 2] if qs else None)
        outl = sorted({x for r in brows for x in r.outlier_beams})
        secs = sum(float((r.summary or {}).get("elapsed_s") or 0.0) for r in brows)
        n_done = sum(1 for r in brows if r.region.status == "done")
        n_failed = sum(1 for r in brows if r.region.status == "failed")

        t = batch_timing(b)
        unsplit_note = ""
        if t.get("n_unsplit"):
            unsplit_note = (
                f'<span title="{t["n_unsplit"]} of {t.get("n_regions", 0)} '
                f"section(s) in this batch predate the focus/stig timing split "
                f'- their combined focus+stig time is counted under Focus">*</span>'
            )
        est_note = ""
        if t.get("n_total_estimated"):
            est_note = (
                f'<span title="{t["n_total_estimated"]} of '
                f'{t.get("n_regions", 0)} section(s) have no timing_summary.txt '
                f"yet - their total is estimated as focus+imaging time only and "
                f'may undercount real elapsed time">*</span>'
            )

        rep = b / BATCH_REPORT_NAME
        rep_html = (
            f'<a href="{_esc(_rel(rep, run_root))}" target="_blank">open</a>'
        )
        body.append(
            "<tr>"
            f"<td><strong>{_esc(b.name)}</strong></td>"
            f"<td>{_esc(parse_sample_id(b.name) or '—')}</td>"
            f'<td class="num">{len(brows)}</td>'
            f'<td class="num">{n_done}</td>'
            f'<td class="num">{n_failed}</td>'
            f'<td class="num">{_duration(t.get("focus_s"))}{unsplit_note}</td>'
            f'<td class="num">{_duration(t.get("stig_s"))}</td>'
            f'<td class="num">{_duration(t.get("move_s"))}</td>'
            f'<td class="num">{_duration(t.get("total_s"))}{est_note}</td>'
            f'<td class="num">{len(scored)}</td>'
            f'<td class="num q-col">{_dash(med)}</td>'
            f'<td>{", ".join(str(x) for x in outl) if outl else "—"}</td>'
            f'<td class="num">{_duration(secs)}</td>'
            f"<td>{rep_html}</td>"
            "</tr>"
        )
    body.append("</tbody></table>")

    awaiting_html = ""
    if awaiting:
        chips = " ".join(
            f'<span class="chip" style="background:#ffe08a;color:#7a5b00;">{_esc(n)}</span>'
            for n in sorted(awaiting, key=_natural_key)
        )
        awaiting_html = (
            f'<div class="summary" style="margin-top:8px;">'
            f"<strong>Awaiting analysis ({len(awaiting)}):</strong> {chips}</div>"
        )

    header = f"""
  <h1>Standalone QC — {_esc(' / '.join(samples))} · {_esc(run_root.name)}</h1>
  <div class="summary"><strong>{n_scored}/{n_sections} section(s) analyzed ({pct:.0f}%)</strong>
    across {len(batches)} batch(es) in <code>{_esc(run_root)}</code></div>
  <div class="progress"><div style="width:{pct:.0f}%;"></div></div>
{awaiting_html}
  <div class="summary" style="margin-top:8px;">
    <button type="button" id="btn-toggle-q">Hide image quality</button>
    &nbsp;·&nbsp; scoring policy <code>{_esc(fingerprint)}</code>,
    {len(entries)} section(s) in the analysis ledger
  </div>
{review.controls_header_html(review.retake_sync_note())}
"""
    script = review.controls_script_html(run_root, review_defaults) + _TOGGLE_JS
    out = run_root / GLOBAL_REPORT_NAME
    out.write_text(
        portable_paths.apply(
            _page(f"Standalone QC — {run_root.name}", header,
                  "".join(body), script),
            cfg),
        encoding="utf-8",
    )

    # Push whole-section Retake decisions into retake_regions.txt, the file
    # the acquisition pipeline actually consumes.
    review.sync_retake_list_if_possible(run_root, review_status)
    return out


def write_all_reports(
    run_root: str | Path,
    cfg: Dict[str, Any],
    batch_filter: Optional[str] = None,
) -> Dict[str, Path]:
    """Regenerate the batch report for each batch plus the run-level report.
    Cheap - reads only the per-section summary JSONs, never image data - so
    it is fine to call after every scoring run."""
    run_root = Path(run_root)
    rows = collect_rows(run_root, cfg)
    written: Dict[str, Path] = {}

    batches = sorted({r.region.block_dir for r in rows}, key=lambda p: p.name)
    for b in batches:
        if batch_filter and b.name != batch_filter:
            continue
        path = write_batch_report(run_root, b, cfg, rows=rows)
        if path is not None:
            written[b.name] = path

    global_path = write_global_report(run_root, cfg, rows=rows)
    if global_path is not None:
        written["__global__"] = global_path
    return written
