"""Per-focus-support-point timings from a section's console.log.

One FSP costs three distinguishable things, and the acquisition log records
them as separate steps:

    [FiducialDirect] xy=(-26913.0,-16150.5) z=38859.6: seeded ...  (stage move 0.44s)
    [FiducialDirect] B2F ok=True rc=0 projective=(-610.5,685.2)    (26.18s)
    [FiducialDirect] AF (fine-only, range=+/-1200uA): ... success=True  (2.63s)
    [FiducialDirect] AS: confX=0.4579 confY=0.4062 ok=True         (4.88s)

  move  = stage move to the support point
  focus = AF (autofocus)
  stig  = AS (autostig)

B2F is kept separate rather than folded into "move": it is a beam-to-feature
alignment, not stage travel, and on the run surveyed here it was by far the
largest single cost (26.2s against 2.6s of AF), so merging it would hide the
thing most worth seeing.

THREE LOG FORMATS, and they do not carry the same information:

  [FiducialDirect]       move, B2F, AF and AS are each timed.
  [TopographyPrecheck]   only AS is timed. Its AF line reads
                         "AF: fine=0.0000, coarse=None, success=True" with no
                         duration, and its move line has none either.
  [Offset FocusPoint N]  AF and AS are timed but the step lines carry NO tag
                         at all; B2F and the move are untimed.

So for some sections focus and move are genuinely UNKNOWN, and this module
reports them as None rather than guessing. A caller that wants a number for
the whole step can use the section-level aggregate
(`facts["aggregate"]["focus_stig_s"]`, the "focus/stigmation: 17.5s" line),
but that is a total for the section, not a per-FSP AF time, and the two must
not be presented as the same measurement.

region_steps()/region_bars_html() chart that section-level view on purpose
and separately: its stage movement is every move in the region, not just the
moves to the support points, so the two charts must not share an axis.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

_XY = r"\(\s*(-?[\d.]+)\s*,\s*(-?[\d.]+)\s*\)"
# A duration in trailing parentheses: "(26.18s)" or "(4.15s, measured only...)".
_DUR = r"\(\s*([\d.]+)s"

_TAG = r"\[(?:FiducialDirect|TopographyPrecheck)\]"
# A third shape logs the steps with NO tag at all, under a focus-point
# header:
#     [Offset FocusPoint 0] Stage -> (B2F) -> AF -> AS
#     AF: fine=0.0000, coarse=None, success=True  (6.18s)
#     AS: rc=0, confX=0.2369, ok=True            (4.08s)
# so the tag is optional throughout. Anchoring to the start of the
# STRIPPED line is what keeps '[AF] stage Z readback ...' and
# 'OL after AF: ...' from being mistaken for timed AF steps.
_OPT_TAG = r"^(?:" + _TAG + r"\s+)?"

# --- markers that begin a new support point --------------------------------
# "[PlaneUpdate] METHOD: measuring one point for 'S3R2' at xy=(2563.2,3958.1)"
#
# "measuring", not just "at xy=": the RESULT line for the same point reads
# "measured OL=... at xy=(...)", so a looser pattern matched it too and opened
# a phantom second FSP with no timings - which also meant the measured OL /
# StigX / StigY never reached the record they belonged to.
_RE_START_MEASURING = re.compile(
    r"^\[PlaneUpdate\]\s+(\w+):.*?\bmeasuring\b.*?\bat\s+xy=" + _XY
)
# "[PlaneUpdate] METHOD: region 'S10R3' matched fiducial row xy=(-26913.0,...)"
_RE_START_MATCHED = re.compile(
    r"^\[PlaneUpdate\]\s+(\w+):.*?matched fiducial row\s+xy=" + _XY
)
# "[TopographyPrecheck] point xy=(x,y): moving stage (z=... um ...)"
# "[Offset FocusPoint 3] Stage -> (B2F) -> AF -> AS (no minimize-OL)"
_RE_START_FOCUSPOINT = re.compile(r"^\[(?:Offset\s+)?FocusPoint\s+(\d+)\]")
_RE_START_POINT = re.compile(
    r"^\[TopographyPrecheck\]\s+point\s+xy=" + _XY
)
# "[FiducialDirect] xy=(x,y) z=...: seeded OL=... (stage move 0.44s)"
_RE_START_SEEDED = re.compile(
    r"^\[FiducialDirect\]\s+xy=" + _XY + r".*?\(stage move\s+([\d.]+)s\)"
)

# --- per-step timings and outcomes -----------------------------------------
_RE_B2F = re.compile(_OPT_TAG + r"(?:\[B2F\]|B2F\b).*?" + _DUR)
_RE_B2F_OK = re.compile(_OPT_TAG + r"(?:\[B2F\]|B2F\b).*?\bok=(True|False)")
_RE_B2F_RC = re.compile(_OPT_TAG + r"(?:\[B2F\]|B2F\b).*?\brc=(-?\d+)")
_RE_AF = re.compile(_OPT_TAG + r"AF\b(?!\s+dz).*?" + _DUR)
_RE_AF_OK = re.compile(_OPT_TAG + r"AF\b(?!\s+dz).*?\bsuccess=(True|False)")
_RE_AF_RESCUE = re.compile(_OPT_TAG + r"fine AF failed, coarse AF rescued")
_RE_AS = re.compile(_OPT_TAG + r"AS:.*?" + _DUR)
_RE_AS_OK = re.compile(_OPT_TAG + r"AS:.*?\bok=(True|False)")
_RE_AS_SKIPPED = re.compile(_OPT_TAG + r"AS skipped")
# "[PlaneUpdate] ...: measured OL=1.821 uA, StigX=0.107337, StigY=0.275880"
_RE_MEASURED = re.compile(
    r"^\[PlaneUpdate\].*?\bmeasured\b.*?OL=(-?[\d.]+)"
    r"(?:.*?StigX=(-?[\d.]+))?(?:.*?StigY=(-?[\d.]+))?"
)


# --- multi-point focus maps -------------------------------------------------
# A different, richer shape from the single-point modes above. focus_map logs
# each support point it measured, with the OL it read there, the OL the fitted
# plane predicts, and the residual between them:
#
#   [PlaneUpdate] Measuring 7 support points using method=AF_MINIMIZE_OL
#   [NewPlanePt] sp[0] xy=(-15550.3,-19769.1) ol_meas=-170.866 ol_pred=-166.638 resid=-4.228
#   [NewPlaneFit] a=-2.371654e-02, b=-8.116514e-03, c=-695.894
#
# The xy are STAGE microns in the same frame as the mFoV layout, which is what
# lets them be drawn on the section overview.
_RE_SP_METHOD = re.compile(
    r"^\[PlaneUpdate\]\s+Measuring\s+(\d+)\s+support points?\s+using\s+method=(\S+)"
)
# Only the index and xy here. The three readings are matched separately
# below: an OPTIONAL group preceded by a lazy ".*?" is skipped by
# preference, so folding them into one pattern parsed the position and
# then silently dropped every number after it.
_RE_SP_POINT = re.compile(
    r"^\[NewPlanePt\]\s+sp\[(\d+)\]\s+xy=" + _XY
)
_RE_SP_FIELDS = {
    "ol_meas": re.compile(r"\bol_meas=(-?[\d.]+)"),
    "ol_pred": re.compile(r"\bol_pred=(-?[\d.]+)"),
    "resid": re.compile(r"\bresid=(-?[\d.]+)"),
}
_RE_SP_FIT = re.compile(
    r"^\[NewPlaneFit\]\s+a=(\S+?),\s*b=(\S+?),\s*c=(\S+)"
)


def parse_support_points(path: str | Path) -> Dict[str, Any]:
    """Where the focus support points were measured, and how well they fit.

    Returns {"method", "n_declared", "points": [...], "plane": [a,b,c]}.
    Empty points is normal: the single-point strategies log nothing of this
    shape (see parse_console_log for those).
    """
    p = Path(path)
    out: Dict[str, Any] = {"method": None, "n_declared": None,
                           "points": [], "plane": None}
    if not p.is_file():
        return out
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return out

    seen: Dict[int, Dict[str, Any]] = {}
    for raw in text.splitlines():
        line = raw.strip()
        m = _RE_SP_METHOD.match(line)
        if m:
            out["n_declared"] = int(m.group(1))
            out["method"] = m.group(2)
            continue
        m = _RE_SP_POINT.match(line)
        if m:
            idx = int(m.group(1))
            # Keyed by sp index: a retried point is logged twice and the
            # later reading is the one the plane was fitted to.
            rec: Dict[str, Any] = {
                "index": idx,
                "x": _f(m.group(2)), "y": _f(m.group(3)),
            }
            for key, rx in _RE_SP_FIELDS.items():
                mm = rx.search(line)
                rec[key] = _f(mm.group(1)) if mm else None
            seen[idx] = rec
            continue
        m = _RE_SP_FIT.match(line)
        if m:
            vals = [_f(m.group(i)) for i in (1, 2, 3)]
            if all(v is not None for v in vals):
                out["plane"] = vals
    out["points"] = [seen[k] for k in sorted(seen)]
    return out


@dataclass
class FspRecord:
    """One focus support point. Times in seconds; None means the log format
    did not record that step, NOT zero."""
    index: int = 0
    method: str = ""
    x: Optional[float] = None
    y: Optional[float] = None
    move_s: Optional[float] = None
    b2f_s: Optional[float] = None
    focus_s: Optional[float] = None       # AF
    stig_s: Optional[float] = None        # AS
    af_ok: Optional[bool] = None
    as_ok: Optional[bool] = None
    b2f_ok: Optional[bool] = None
    af_coarse_rescue: bool = False
    as_skipped: bool = False
    ol: Optional[float] = None
    stig_x: Optional[float] = None
    stig_y: Optional[float] = None
    notes: List[str] = field(default_factory=list)

    @property
    def has_any_timing(self) -> bool:
        return any(v is not None for v in
                   (self.move_s, self.b2f_s, self.focus_s, self.stig_s))

    @property
    def total_s(self) -> Optional[float]:
        vals = [v for v in (self.move_s, self.b2f_s, self.focus_s, self.stig_s)
                if v is not None]
        return sum(vals) if vals else None

    def as_dict(self) -> Dict[str, Any]:
        return {
            "index": self.index, "method": self.method,
            "x": self.x, "y": self.y,
            "move_s": self.move_s, "b2f_s": self.b2f_s,
            "focus_s": self.focus_s, "stig_s": self.stig_s,
            "af_ok": self.af_ok, "as_ok": self.as_ok, "b2f_ok": self.b2f_ok,
            "af_coarse_rescue": self.af_coarse_rescue,
            "as_skipped": self.as_skipped,
            "ol": self.ol, "stig_x": self.stig_x, "stig_y": self.stig_y,
            "total_s": self.total_s,
            "notes": list(self.notes),
        }


def _f(v: Optional[str]) -> Optional[float]:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def parse_console_log(path: str | Path) -> List[Dict[str, Any]]:
    """Per-FSP records for one section, in the order the log reports them."""
    p = Path(path)
    if not p.is_file():
        return []
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []

    out: List[FspRecord] = []
    cur: Optional[FspRecord] = None

    def start(method: str = "", x: Optional[float] = None,
              y: Optional[float] = None) -> None:
        """Begin a support point - or fill in the one already open.

        Both formats announce a point twice: a [PlaneUpdate] line, then a
        [TopographyPrecheck]/[FiducialDirect] line for the same point. Only
        the first of the pair should create a record, so a second marker
        merges into the open one as long as no timing has landed in it yet.
        """
        nonlocal cur
        if cur is not None and not cur.has_any_timing:
            cur.method = cur.method or method
            if x is not None:
                cur.x, cur.y = x, y
            return
        cur = FspRecord(index=len(out) + 1, method=method, x=x, y=y)
        out.append(cur)

    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue

        # Results for the point already open, checked before the start
        # markers so a "measured ... at xy=" line can never be mistaken for
        # the announcement of a new point.
        m = _RE_MEASURED.match(line)
        if m and cur is not None:
            cur.ol = _f(m.group(1))
            cur.stig_x = _f(m.group(2))
            cur.stig_y = _f(m.group(3))
            continue

        m = _RE_START_MEASURING.match(line) or _RE_START_MATCHED.match(line)
        if m:
            start(m.group(1), _f(m.group(2)), _f(m.group(3)))
            continue
        m = _RE_START_SEEDED.match(line)
        if m:
            start("", _f(m.group(1)), _f(m.group(2)))
            if cur is not None:
                cur.move_s = _f(m.group(3))
            continue
        m = _RE_START_POINT.match(line)
        if m:
            start("", _f(m.group(1)), _f(m.group(2)))
            continue
        m = _RE_START_FOCUSPOINT.match(line)
        if m:
            # This header carries no xy of its own; the position comes
            # from a [NewPlanePt] line for the same point when the run
            # logs one (see parse_support_points).
            start("OFFSET_FOCUS_POINT", None, None)
            continue

        if cur is None:
            continue

        m = _RE_B2F.search(line)
        if m:
            cur.b2f_s = _f(m.group(1))
        m = _RE_B2F_OK.search(line)
        if m:
            cur.b2f_ok = m.group(1) == "True"
        elif _RE_B2F_RC.search(line):
            # The older format reports only rc; rc=0 is success.
            cur.b2f_ok = _RE_B2F_RC.search(line).group(1) == "0"

        m = _RE_AF.search(line)
        if m:
            cur.focus_s = _f(m.group(1))
        m = _RE_AF_OK.search(line)
        if m:
            cur.af_ok = m.group(1) == "True"
        if _RE_AF_RESCUE.search(line):
            cur.af_coarse_rescue = True

        m = _RE_AS.search(line)
        if m:
            cur.stig_s = _f(m.group(1))
        m = _RE_AS_OK.search(line)
        if m:
            cur.as_ok = m.group(1) == "True"
        if _RE_AS_SKIPPED.search(line):
            cur.as_skipped = True
            cur.as_ok = False
            cur.notes.append("AS skipped (AF failed)")

    for r in out:
        if r.focus_s is None and r.af_ok is not None:
            r.notes.append("AF not timed in this log format")
        if r.move_s is None:
            r.notes.append("stage move not timed in this log format")
    return [r.as_dict() for r in out]


# ---------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------
_SERIES = (
    ("focus (AF)", "focus_s", "focus"),
    ("stig (AS)", "stig_s", "stig"),
    ("move", "move_s", "move"),
    ("B2F", "b2f_s", "b2f"),
)


def bars_html(
    records: List[Dict[str, Any]],
    labels: Optional[List[str]] = None,
    title: str = "",
    width: int = 720,
) -> str:
    """Grouped bar chart of focus / stig / move (and B2F) per FSP.

    Steps the log did not time are simply absent - no bar - and a footnote
    names them, because a zero-height bar would read as "instant" rather than
    "not measured".
    """
    from .plot3d import BAR_COLOURS, grouped_bars_svg

    if not records:
        return '<div class="muted">no focus support point found in console.log</div>'

    groups = labels or [
        (f"FSP {r.get('index', i + 1)}" if len(records) > 1 else "FSP")
        for i, r in enumerate(records)
    ]
    colours = dict(BAR_COLOURS, b2f="#8a6fb0")
    series = []
    missing = []
    for label, key, ckey in _SERIES:
        vals = [r.get(key) for r in records]
        if all(v is None for v in vals):
            missing.append(label)
            continue
        series.append((label, colours[ckey], vals))

    if not series:
        # Two very different reasons for an empty chart, and blaming the log
        # format for the second one buries a real failure: S27R2's autofocus
        # failed, autostig was therefore skipped, and NO step ran to be
        # timed. That deserves a visible warning, not a note about logging.
        failures = []
        for r in records:
            what = []
            if r.get("af_ok") is False:
                what.append("autofocus failed")
            if r.get("as_skipped"):
                what.append("autostig skipped")
            elif r.get("as_ok") is False:
                what.append("autostig failed")
            if what:
                failures.append(f"FSP {r.get('index', '?')}: "
                                + ", ".join(what))
        if failures:
            return ('<div style="font-size:11px;color:#a33">'
                    '<strong>No focus step completed on this section</strong>'
                    ', so there is nothing to time &mdash; '
                    + "; ".join(failures) + ".</div>")
        return ('<div class="muted">this log format records no per-FSP '
                'timings (only the section total)</div>')

    svg = grouped_bars_svg(groups, series, title=title, unit="s", width=width)
    foot = ""
    if missing:
        foot = (f"<div style='font-size:10px;color:#888;margin-top:2px'>"
                f"not timed in this log format: {', '.join(missing)}</div>")
    return svg + foot


# --- region-level step timing ---------------------------------------------
#
# The per-FSP chart above answers "what did each support point cost". It does
# NOT answer the question the operator actually asks of a finished region --
# "where did this region's 942.7s go" -- because stage movement there is only
# the few moves to the support points, while the region also moves between
# every one of its 399 mFoVs.
#
# The region numbers come from the "[Timing] Region '...' breakdown" block.
# Newer runs itemise focus/stigmation:
#       focus/stigmation  :  140.9s
#         - focus (AF+minimize-OL):  61.2s
#         - stig (AS)             :  28.6s
#         - stage/B2F within focus:  51.1s
# and those are used verbatim. Older runs print only the 140.9s total, so AF
# and AS are summed from the per-FSP lines and B2F is what is left over. That
# leftover is B2F PLUS any focus overhead the log never attributed, so it is
# labelled as derived and never presented as a measured B2F time.


# focus_move_s = focus_b2f_s + focus_stage_s, so charting focus_move_s
# alongside either of its parts would double-count it. It is used only when
# the two parts are absent.
_FOCUS_KEYS = ("focus_af_s", "focus_as_s", "focus_b2f_s", "focus_stage_s",
               "focus_move_s")


def _itemised_focus_steps(
    lq: Dict[str, Any]
) -> Optional[List[Dict[str, Any]]]:
    """The focus phase as the acquisition side itemises it, or None.

    From focus_map, for runs after 2026-10-02. All of these sit inside
    focus_s, the region's setup phase BEFORE tile acquisition:

        focus_af_s     AF plus minimize-OL, and (in the per-region
                       topography measurement) the one corrective Z move
                       and AF re-check taken when AF is >20 um out
        focus_minimize_s  the minimize-OL part OF focus_af_s - a component,
                       not a sibling, so it is a note and never a bar
        focus_as_s     AS
        focus_b2f_s    beam-to-fibre, measured rather than inferred
        focus_stage_s  stage moves for the focus measurements, including the
                       landing Z read where the ~4s telemetry glitch reads
                       happen
        focus_move_s   = focus_b2f_s + focus_stage_s

    Whatever is left of focus_s is unitemized setup: JSON and plane loading,
    ROI and tile-plan generation, Z safety checks.

    `lq` must already have zeros stripped: in older runs a 0 means "not
    recorded", not "took no time", and the two have to stay distinguishable.
    """
    if not any(lq.get(k) for k in ("focus_af_s", "focus_as_s")):
        return None

    out: List[Dict[str, Any]] = []
    af = lq.get("focus_af_s")
    if af is not None:
        note = ("autofocus plus minimize-OL, measured by the acquisition "
                "software over the region's focus phase")
        mini = lq.get("focus_minimize_s")
        if mini:
            note += (f"; {float(mini):.1f}s of it is minimize-OL, which runs "
                     "from the reload anchor and so is non-zero only in a "
                     "batch's first region")
        out.append({"key": "af", "label": "focus (AF)", "seconds": float(af),
                    "derived": False, "note": note})
    if lq.get("focus_as_s") is not None:
        out.append({"key": "as", "label": "stig (AS)",
                    "seconds": float(lq["focus_as_s"]), "derived": False,
                    "note": "autostigmation, measured"})

    b2f, fstage = lq.get("focus_b2f_s"), lq.get("focus_stage_s")
    if b2f is not None:
        out.append({"key": "b2f", "label": "B2F", "seconds": float(b2f),
                    "derived": False,
                    "note": "beam-to-fibre alignment, measured directly"})
    if fstage is not None:
        out.append({"key": "fstage", "label": "stage (focus)",
                    "seconds": float(fstage), "derived": False,
                    "note": ("stage moves for the focus measurements, "
                             "including the landing Z read - the ~4s "
                             "telemetry glitch reads land in here")})
    if b2f is None and fstage is None and lq.get("focus_move_s") is not None:
        out.append({"key": "b2f", "label": "B2F + focus stage",
                    "seconds": float(lq["focus_move_s"]), "derived": False,
                    "note": ("this run records only their sum "
                             "(focus_move_s), not the two separately")})

    focus_s = lq.get("focus_s")
    if focus_s is not None:
        move = lq.get("focus_move_s")
        if move is None:
            move = (b2f or 0.0) + (fstage or 0.0)
        rest = float(focus_s) - float(af or 0.0)             - float(lq.get("focus_as_s") or 0.0) - float(move)
        if rest > 0.05:
            out.append({
                "key": "setup", "label": "setup (unitemized)",
                "seconds": rest, "derived": True,
                "note": ("what is left of the region's focus phase: JSON and "
                         "plane loading, ROI and tile-plan generation, Z "
                         "safety checks"),
            })
    return out or None

def region_steps(facts: Dict[str, Any]) -> List[Dict[str, Any]]:
    """[{key, label, seconds, derived, note}] for AF / AS / B2F / stage move.

    Steps with no number from either source are omitted rather than drawn as
    zero. `facts` is a section_facts dict (needs "aggregate" and, for the
    fallback, "fsp_timings").
    """
    agg = dict(facts.get("aggregate") or {})
    fsps = list(facts.get("fsp_timings") or [])
    # LiveQC's file has slots for exactly the split this function otherwise
    # derives. They are zero in every file on the share today, so a zero is
    # treated as "not filled in" rather than "took no time" - which is the
    # right reading for a slot the acquisition side never writes, and would
    # be the wrong one if it ever genuinely measured zero. That trade is
    # worth it: a real AF step cannot take 0.000s.
    lq = {k: v for k, v in (facts.get("live_qc_timing") or {}).items() if v}
    if agg.get("move_s") is None and lq.get("move_s") is not None:
        agg["move_s"] = lq["move_s"]
    if agg.get("focus_stig_s") is None and lq.get("focus_s") is not None:
        agg["focus_stig_s"] = lq["focus_s"]

    # Runs after 2026-10-02 itemise the focus phase themselves, which is
    # strictly better than anything derivable here, so that path is taken
    # whole rather than blended with the fallback below.
    itemised = _itemised_focus_steps(lq)
    if itemised is not None:
        move = agg.get("move_s")
        if move is not None:
            itemised.append({
                "key": "move", "label": "stage move+settle (tiles)",
                "seconds": float(move), "derived": False,
                "note": ("stage travel during tile acquisition - the move to "
                         "every mFoV. Separate from the focus phase's own "
                         "stage moves, which are their own bar."),
            })
        return itemised

    def _summed(key: str) -> Optional[float]:
        vals = [r.get(key) for r in fsps if r.get(key) is not None]
        return float(sum(vals)) if vals else None

    af = agg.get("af_s")
    af_derived = False
    if af is None:
        af = _summed("focus_s")
        af_derived = af is not None
    as_ = agg.get("as_s")
    as_derived = False
    if as_ is None:
        as_ = _summed("stig_s")
        as_derived = as_ is not None

    # A measured B2F is always preferred to the leftover, even though the
    # leftover is the larger number: the extra seconds in it are overhead the
    # log never attributed to B2F, and rolling them in would overstate the
    # cost of the alignment itself. The unattributed remainder is reported
    # separately by region_unattributed_s().
    b2f = agg.get("b2f_s")
    b2f_note = "stage + beam-to-feature alignment inside the focus routine"
    b2f_derived = False
    if b2f is None:
        b2f = _summed("b2f_s")
        b2f_derived = b2f is not None
        if b2f is not None:
            b2f_note += ", summed over the focus support points"
    if b2f is None:
        total = agg.get("focus_stig_s")
        if total is not None and (af is not None or as_ is not None):
            rest = float(total) - float(af or 0.0) - float(as_ or 0.0)
            if rest > 0.05:
                b2f = rest
                b2f_derived = True
                b2f_note = ("what is left of the region's "
                            f"{float(total):.1f}s focus/stigmation total "
                            "after AF and AS: B2F plus any focus overhead "
                            "the log did not itemise")

    out: List[Dict[str, Any]] = []
    spec = (
        ("af", "focus (AF)", af, af_derived,
         "autofocus, summed over the region's focus support points"),
        ("as", "stig (AS)", as_, as_derived,
         "autostigmation, summed over the region's focus support points"),
        ("b2f", "B2F", b2f, b2f_derived, b2f_note),
        ("move", "stage move+settle", agg.get("move_s"), False,
         "all stage travel in the region, including the move to every mFoV "
         "- not just the moves to the focus support points"),
    )
    for key, label, val, derived, note in spec:
        if val is None:
            continue
        if derived and key != "b2f":
            note += " (the region breakdown did not itemise it)"
        out.append({"key": key, "label": label, "seconds": float(val),
                    "derived": bool(derived), "note": note})
    return out


def region_unattributed_s(facts: Dict[str, Any]) -> Optional[float]:
    """Focus/stigmation seconds the itemised steps do not account for.

    None when there is no region total to compare against, and None rather
    than a negative number when the steps already add up (rounding in the
    log's one-decimal figures can push the sum a hair over the total).
    """
    total = (facts.get("aggregate") or {}).get("focus_stig_s")
    if total is None:
        return None
    named = sum(s["seconds"] for s in region_steps(facts)
                if s["key"] in ("af", "as", "b2f"))
    rest = float(total) - named
    return rest if rest > 0.05 else None


def region_bars_html(
    facts: Dict[str, Any],
    region: str = "",
    width: int = 720,
    height: int = 320,
) -> str:
    """One cluster of AF / AS / B2F / stage-move bars for the whole region."""
    from .plot3d import BAR_COLOURS, grouped_bars_svg

    # With no [Timing] breakdown and no LiveQC timing file there is nothing
    # region-wide to show: every number would be a sum over the support
    # points, which the per-FSP chart below already draws, and labelling it
    # "region" would claim a coverage it does not have.
    if not (facts.get("aggregate") or facts.get("live_qc_timing")):
        return ""
    steps = region_steps(facts)
    if not steps:
        return ""
    ov = region_overview(facts)
    rest = ov.get("unattributed_s") or 0.0
    if rest > 0.05:
        # Charted, not just footnoted. On this batch it is 40-55s a region -
        # bigger than the whole focus/stigmation pass on most of them - and
        # a reader comparing four bars against a stated region total will
        # otherwise assume they add up.
        steps = list(steps) + [{
            "key": "other", "label": "unattributed", "seconds": rest,
            "derived": True,
            "note": (f"the region total minus the steps above; nothing in "
                     f"the log or LiveQC's timing file says where these "
                     f"{rest:.0f}s went"),
        }]
    colours = dict(BAR_COLOURS, b2f="#8a6fb0", af=BAR_COLOURS["focus"],
                   fstage="#7fbf8c", setup="#c2b280", other="#9aa0a6",
                   **{"as": BAR_COLOURS["stig"]})
    group = region or "region"
    series = []
    for s in steps:
        label = s["label"] + (" *" if s["derived"] else "")
        series.append((label, colours[s["key"]], [s["seconds"]]))

    svg = grouped_bars_svg([group], series, unit="s", width=width,
                           height=height)

    agg = dict(facts.get("aggregate") or {})
    lq = dict(facts.get("live_qc_timing") or {})
    bits = []
    for key, label in (("scan_s", "beam scan"),
                       ("storage_s", "storage overhead")):
        v = agg.get(key, lq.get(key))
        if v is not None:
            bits.append(f"{label} {float(v):.1f}s")
    if lq.get("qc_catchup_s") is not None:
        bits.append(f"QC catch-up {float(lq['qc_catchup_s']):.1f}s")
    elif agg.get("qc_catchup_s") is not None:
        bits.append(f"QC catch-up {float(agg['qc_catchup_s']):.1f}s")
    rest = ("; not charted: " + ", ".join(bits)) if bits else ""
    src = "the region's [Timing] breakdown"
    if lq:
        src += " and LiveQC/live_qc_timing.json"
    foot = []
    if ov.get("total_s") is not None:
        line = (f"<strong>{float(ov['total_s']):.0f}s</strong> for the whole "
                f"region")
        if ov.get("n_tiles"):
            line += (f", {int(ov['n_tiles'])} mFoV at "
                     f"{float(ov['s_per_mfov']):.2f}s each")
        line += (f" = {float(ov.get('named_s', 0.0)):.0f}s charted + "
                 f"{float(ov.get('scan_storage_s', 0.0)):.0f}s beam scan and "
                 f"storage")
        if ov.get("unattributed_s"):
            line += f" + {float(ov['unattributed_s']):.0f}s unattributed"
        foot.append(f"<div style='font-size:11px;color:#444;margin-top:4px'>"
                    f"{line}.</div>")
    foot.append(f"<div style='font-size:10px;color:#888'>"
                f"from {src}{rest}.</div>")

    # wall_s is not the region total and the steps are not a partition of
    # it: focus/stigmation falls outside it. Saying so is the difference
    # between a useful second clock and an apparent contradiction.
    if lq.get("wall_s") is not None:
        line = f"Tile loop (LiveQC wall clock) {float(lq['wall_s']):.0f}s"
        if lq.get("n_tiles"):
            line += (f" over {int(lq['n_tiles'])} mFoV, "
                     f"{float(lq['wall_s']) / float(lq['n_tiles']):.2f}s each")
        line += ("; this covers stage move, beam scan and storage, not the "
                 "focus/stigmation pass, so it is smaller than the region "
                 "total.")
        foot.append(f"<div style='font-size:10px;color:#888'>{line}</div>")
    if lq.get("n_tiles_z_glitch"):
        foot.append(
            f"<div style='font-size:10px;color:#b36b00'>"
            f"{int(lq['n_tiles_z_glitch'])} mFoV had a stage-Z glitch.</div>"
        )

    rest_s = region_unattributed_s(facts)
    if rest_s is not None and agg.get("focus_stig_s") is not None:
        foot.append(
            f"<div style='font-size:10px;color:#888'>{rest_s:.1f}s of the "
            f"region's {float(agg['focus_stig_s']):.1f}s focus/stigmation "
            f"total is not attributed to any focus step above.</div>"
        )
    derived = [s for s in steps if s["derived"]]
    if derived:
        foot.append(
            "<div style='font-size:10px;color:#888'>* derived, not timed "
            "directly: " + "; ".join(f"{s['label']} - {s['note']}"
                                     for s in derived) + ".</div>"
        )
    return svg + "".join(foot)


# --- LiveQC's own timing file ---------------------------------------------
#
# <section>/LiveQC/live_qc_timing.json is written by the acquisition side and
# carries the same focus/move/scan/storage seconds as the console.log
# breakdown - verified equal to one decimal on all 14 sections of
# w10_h31_Slice2_8NM_20261002_101931 - plus three things the log does not
# print:
#
#   wall_s             wall clock for the tile loop
#   n_tiles            tiles in that loop, so a seconds-per-mFoV rate exists
#   n_tiles_z_glitch   tiles whose stage Z jumped
#   qc_catchup_*       time the acquisition spent waiting on its own QC,
#                      split into worker and viewer
#
# wall_s is NOT the region total and the steps are not a partition of it.
# On this batch wall_s tracks move + scan + storage + 34-35s of per-region
# overhead, with focus/stigmation sitting OUTSIDE it (S10R5: steps sum to
# 896.9s against a wall_s of 801.6s, which is only contradictory if wall_s
# is read as the whole region). The console log's "Region ... Ns total" is
# the figure that contains everything, and it exceeds the four steps by
# 40-55s on every section here. That remainder is reported rather than
# divided up, because nothing in either file says where it went.
#
# focus_af_s / focus_as_s / focus_move_s are the per-step split this module
# otherwise has to derive. In runs up to 2026-10-02 they are present in the
# schema and ZERO - meaning "not recorded", not "took no time" - so a zero
# is ignored and the derived values stand.
#
# Runs after 2026-10-02 fill them in and add focus_minimize_s, focus_b2f_s
# and focus_stage_s; see _itemised_focus_steps, which is then used instead
# of deriving anything. Two traps in that schema, both load-bearing:
#
#   focus_move_s = focus_b2f_s + focus_stage_s   (a sum, not a sibling)
#   focus_minimize_s is a PART of focus_af_s     (a component, not a step)
#
# so charting either beside what it contains double-counts it.
#
# One thing neither file can give: in REFERENCE_POINTS reload mode the
# reference-point measurements run before the first region and are charged
# to no region at all, so a batch's regions will not sum to its wall time.

_TIMING_JSON = ("LiveQC", "live_qc_timing.json")


def read_live_qc_timing(section_dir: str | Path) -> Dict[str, Any]:
    """{} when the file is absent or unreadable - it is an extra, not a
    dependency, and sections acquired before it existed must still chart."""
    import json as _json

    p = Path(section_dir).joinpath(*_TIMING_JSON)
    try:
        data = _json.loads(p.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    out: Dict[str, Any] = {}
    for k, v in data.items():
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            out[str(k)] = float(v)
    return out


def region_overview(facts: Dict[str, Any]) -> Dict[str, Any]:
    """The numbers that describe where a region's time went.

    Keys are only present when a source actually supplied them; a caller
    must not turn a missing one into a zero.
    """
    agg = dict(facts.get("aggregate") or {})
    lq = dict(facts.get("live_qc_timing") or {})
    out: Dict[str, Any] = {}

    total = agg.get("grand_total_s")
    if total is None:
        total = facts.get("duration_s")
    if total is not None:
        out["total_s"] = float(total)
    for key, src in (("wall_s", "wall_s"), ("n_tiles", "n_tiles"),
                     ("z_glitch", "n_tiles_z_glitch"),
                     ("qc_catchup_s", "qc_catchup_s"),
                     ("qc_catchup_worker_s", "qc_catchup_worker_s"),
                     ("qc_catchup_viewer_s", "qc_catchup_viewer_s")):
        if lq.get(src) is not None:
            out[key] = lq[src]
    if out.get("n_tiles") and out.get("total_s"):
        out["s_per_mfov"] = out["total_s"] / float(out["n_tiles"])

    named = sum(s["seconds"] for s in region_steps(facts))
    out["named_s"] = named

    # Beam scan and storage are not charted - a 598s scan bar next to a 4s
    # autostig bar makes the chart useless - but they MUST be subtracted
    # here. Leaving them out put 598s of ordinary imaging into a bucket
    # labelled "unattributed", which is a far worse error than not drawing
    # them: it invents a 650s mystery out of the thing the microscope is
    # supposed to spend its time on.
    other = 0.0
    for key in ("scan_s", "storage_s", "qc_catchup_s"):
        v = agg.get(key, lq.get(key))
        if v is not None:
            other += float(v)
    # Sanity check rather than arithmetic: when the focus phase is itemised
    # its parts must add back up to focus_s, and a mismatch means this code
    # has misread the schema. Recorded so it shows up instead of quietly
    # shifting seconds into "unattributed".
    focus_s = agg.get("focus_stig_s", lq.get("focus_s"))
    if focus_s is not None:
        charted_focus = sum(
            s["seconds"] for s in region_steps(facts) if s["key"] != "move"
        )
        gap = float(focus_s) - charted_focus
        if abs(gap) > 0.1:
            out["focus_split_gap_s"] = gap
    out["scan_storage_s"] = other
    if out.get("total_s") is not None:
        rest = float(out["total_s"]) - named - other
        out["unattributed_s"] = rest if rest > 0.05 else 0.0
    return out
