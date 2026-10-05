"""Per-sFoV scan-fault detection.

One beam image (one sFoV) per mFoV is checked, per the operator's spec, and
each mFoV gets a pass/fail. The image used is the lowest-numbered beam the
map pass already read, so this costs no extra I/O.

The measurement returns NUMBERS as well as a verdict, because the thresholds
here came from the distribution actually observed across real sections rather
than from a specification - see the constants below. Keeping the numbers
visible is what makes them re-tunable later instead of a black box.

A skew measurement lived here too and has been removed (2026-09-16). It
responded to synthetic shear correctly (slope ~1.0, r = 0.99) but sat on a
content-dependent offset of several degrees - tissue grain is
indistinguishable from a sheared raster in a single image - so no honest
pass/fail could be built on it. Doing it properly needs the same physical
content seen twice, i.e. the overlap between adjacent sFoVs. See git history
for the implementation if it is revisited.

---- scan fault --------------------------------------------------------------
A failure in the raster of a single sFoV. Three signatures, all row-wise:

  dead rows   a scan line with collapsed contrast (std far below the image's
              own median row std) - a line that carried no signal
  torn rows   an abrupt row-to-row jump in mean intensity. Detected on the
              DIFFERENCE between consecutive row means, not on deviation from
              the image median: real tissue has smooth large-scale intensity
              gradients, and measuring against the median would flag every
              section whose top half is tissue and bottom half is resin.
  duplicate   a row bit-identical to the one above it - a held/repeated line

---- skew: MEASURED BUT NOT TRUSTWORTHY AS PASS/FAIL -------------------------
The raster sheared, so a feature that should be vertical leans. Measured by
splitting the image into horizontal bands, cross-correlating adjacent bands'
column-mean profiles for a lateral shift, and converting the median
incremental shift into an angle.

This responds to shear correctly - against synthetic shears applied to a real
sFoV it tracks the applied angle at slope ~1.0 with r = 0.99. But it sits on a
large, CONTENT-DEPENDENT offset: the same measurement on the unsheared
original reports +4.4 deg, and across 236 real sFoVs the apparent angle
ranged over tens of degrees. Tissue has its own directional grain, and that
grain is indistinguishable from raster shear when only one image is
available.

So `skew_deg` is reported as a number but `skew_pass` is left None - no
pass/fail is claimed. Asserting one would mean failing sections for having
diagonally-oriented tissue.

Measuring raster skew properly needs the same physical content seen twice:
the OVERLAP between adjacent sFoVs, where a relative displacement is
unambiguous. That is what the acquisition side's "sFoV stitch" angle
(-0.16 deg) came from. It needs adjacent sFoVs rather than one image per
mFoV, so it does not fit the current sampling.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional

import numpy as np

# Defaults. See the module docstring: these came from the observed spread
# across real sections, so they are starting points, not specification.
DEAD_ROW_STD_FRAC = 0.25     # row std below this fraction of the median = dead
TORN_ROW_SIGMA = 8.0         # robust sigma on consecutive row-mean jumps
SCANFAULT_MAX_BAD_FRAC = 0.0   # any implicated row is a fault - see below


@dataclass
class ScanCheck:
    """One sFoV's verdict. `passed` is None when not measurable."""
    scanfault_bad_rows: int = 0
    scanfault_bad_frac: float = 0.0
    scanfault_dead: int = 0
    scanfault_torn: int = 0
    scanfault_dup: int = 0
    scanfault_pass: Optional[bool] = None
    error: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {
            "scanfault_bad_rows": self.scanfault_bad_rows,
            "scanfault_bad_frac": self.scanfault_bad_frac,
            "scanfault_dead": self.scanfault_dead,
            "scanfault_torn": self.scanfault_torn,
            "scanfault_dup": self.scanfault_dup,
            "scanfault_pass": self.scanfault_pass,
            "error": self.error,
        }


def _robust_sigma(values: np.ndarray) -> float:
    """MAD scaled to a normal-equivalent sigma. Median-based so a handful of
    genuinely faulty rows cannot inflate the threshold meant to catch them."""
    med = float(np.median(values))
    mad = float(np.median(np.abs(values - med)))
    return 1.4826 * max(mad, 1e-9)


def measure_scan_faults(
    gray: np.ndarray,
    dead_frac: float = DEAD_ROW_STD_FRAC,
    torn_sigma: float = TORN_ROW_SIGMA,
    max_bad_frac: float = SCANFAULT_MAX_BAD_FRAC,
) -> Dict[str, Any]:
    g = gray.astype(np.float32)
    h = g.shape[0]
    if h < 8:
        return {"bad_rows": 0, "bad_frac": 0.0, "dead": 0, "torn": 0,
                "dup": 0, "passed": None}

    row_mean = g.mean(axis=1)
    row_std = g.std(axis=1)

    # Dead lines: contrast collapsed relative to this image's own typical row.
    med_std = float(np.median(row_std))
    dead = row_std < (dead_frac * med_std) if med_std > 1e-6 else np.zeros(h, bool)

    # Torn lines: an abrupt step between consecutive rows. Using the
    # difference rather than the absolute level is what keeps a smooth
    # tissue-to-resin gradient from being read as hundreds of faults.
    d = np.diff(row_mean)
    thr = torn_sigma * _robust_sigma(d)
    torn = np.zeros(h, bool)
    torn[1:] = np.abs(d - float(np.median(d))) > thr

    # Held/repeated lines: bit-identical neighbours.
    dup = np.zeros(h, bool)
    if h > 1:
        dup[1:] = np.all(gray[1:] == gray[:-1], axis=1)

    bad = dead | torn | dup
    n_bad = int(bad.sum())
    frac = n_bad / float(h)
    # Strict by default (max_bad_frac = 0): ANY implicated row fails.
    #
    # Justified by measurement, not taste - across 236 real sFoVs this
    # detector flagged exactly zero rows, so it has no false-positive budget
    # to spend. A tolerance of 1% instead let a 40-row torn band pass, because
    # a step detector reports the band's two EDGES rather than its interior:
    # 2 rows out of 1748 is 0.1%. Raise max_bad_frac if a real instrument
    # turns out to produce occasional benign flags.
    return {
        "bad_rows": n_bad,
        "bad_frac": float(frac),
        "dead": int(dead.sum()),
        "torn": int(torn.sum()),
        "dup": int(dup.sum()),
        "passed": bool(frac <= max_bad_frac),
    }


def check_image(path, cfg: Optional[Dict[str, Any]] = None) -> ScanCheck:
    """Scan-fault check on one beam image."""
    cfg = cfg or {}
    out = ScanCheck()
    try:
        from PIL import Image
        with Image.open(path) as im:
            gray = np.asarray(im.convert("L"), dtype=np.uint8)
    except Exception as exc:
        out.error = f"{type(exc).__name__}: {exc}"
        return out

    sf = measure_scan_faults(
        gray,
        dead_frac=float(cfg.get("scanfault_dead_row_std_frac", DEAD_ROW_STD_FRAC)),
        torn_sigma=float(cfg.get("scanfault_torn_row_sigma", TORN_ROW_SIGMA)),
        max_bad_frac=float(cfg.get("scanfault_max_bad_row_frac", SCANFAULT_MAX_BAD_FRAC)),
    )
    out.scanfault_bad_rows = sf["bad_rows"]
    out.scanfault_bad_frac = sf["bad_frac"]
    out.scanfault_dead = sf["dead"]
    out.scanfault_torn = sf["torn"]
    out.scanfault_dup = sf["dup"]
    out.scanfault_pass = sf["passed"]

    return out


def summarize(checks: Dict[int, Dict[str, Any]]) -> Dict[str, Any]:
    """Per-section rollup of the per-mFoV checks."""
    vals = list(checks.values())
    if not vals:
        return {}
    sf_judged = [v for v in vals if v.get("scanfault_pass") is not None]
    return {
        "n_checked": len(vals),
        "scanfault_n_judged": len(sf_judged),
        "scanfault_n_fail": sum(1 for v in sf_judged if not v["scanfault_pass"]),
        "scanfault_worst_frac": max(
            (float(v["scanfault_bad_frac"]) for v in vals), default=0.0
        ),
    }
