"""Analyzing one section: the single implementation, two callers.

cli.main() had this inline. The agent needs exactly the same work done, and a
second copy would drift - so it lives here and both call it.

The log lines are emitted through a callback but their WORDING IS FIXED: the
GUI's QCRunner parses them with regexes (progress %, section banner,
"mfovs=... analyzed=... median_q=...", "OUTLIER BEAM(S)", "report:"), and
test_gui asserts those against lines copied from real runs. Changing the text
here silently kills the GUI's progress bar.
"""
from __future__ import annotations

import sys
import time
import traceback
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from . import ledger
from .discover import RegionRef
from .report import render_region_overview, write_region_report
from .score import score_region

Log = Callable[[str], None]
Progress = Callable[[int, int, str], None]


def _noop_progress(done: int, total: int, label: str) -> None:
    pass


def analyze_one(
    region: RegionRef,
    root: Path,
    cfg: Dict[str, Any],
    fingerprint: str,
    log: Optional[Log] = None,
    on_progress: Optional[Progress] = None,
    write_dashboard: bool = True,
) -> Dict[str, Any]:
    """Score one section, write its outputs, and record it in the ledger.

    Returns a result dict; never raises. One section failing must not take
    down a batch of 400, so every failure path marks the ledger and returns
    ok=False instead.
    """
    from .dashboard import write_section_dashboard

    say: Log = log or (lambda s: print(s, flush=True))

    # Phase announcements: each names what is starting and times the one
    # before it. Without these an operator saw a section begin and then
    # nothing at all for the length of an overview stitch - minutes,
    # with nothing on screen to say it was alive.
    phase_state = {"name": None, "t0": time.perf_counter()}

    def phase(name: str, detail: str = "") -> None:
        now = time.perf_counter()
        if phase_state["name"]:
            say(f"[Phase] {phase_state['name']} done in "
                f"{now - phase_state['t0']:.1f}s")
        phase_state["name"] = name
        phase_state["t0"] = now
        say(f"[Phase] {name}" + (f" — {detail}" if detail else ""))

    def phase_end() -> None:
        if phase_state["name"]:
            say(f"[Phase] {phase_state['name']} done in "
                f"{time.perf_counter() - phase_state['t0']:.1f}s")
            phase_state["name"] = None
    progress = on_progress or _noop_progress
    out: Dict[str, Any] = {"key": region.key, "section": region.name, "ok": False}

    say(f"\n[StandaloneQC] === {region.key} ===")
    phase("reading and scoring mFoVs",
          f"{cfg.get('map_beams_per_mfov', 5)} beams on every mFoV, "
          f"then all beams on the sampled ones")
    try:
        score = score_region(region, cfg, on_progress=progress)
    except Exception as exc:                      # never kill the batch
        traceback.print_exc()
        ledger.mark(root, region.key, fingerprint, status="error",
                    detail={"error": f"{type(exc).__name__}: {exc}"})
        print(f"[StandaloneQC] {region.key}: FAILED - {exc}", file=sys.stderr)
        out["error"] = f"{type(exc).__name__}: {exc}"
        return out

    if not score.tiles:
        for e in score.errors:
            say(f"    ! {e}")
        ledger.mark(root, region.key, fingerprint, status="no_data",
                    detail={"errors": score.errors})
        say(f"[StandaloneQC] {region.key}: no mFoV layout - skipped.")
        out["error"] = "no mFoV layout"
        out["errors"] = list(score.errors)
        return out

    if bool(cfg.get("region_overview_enabled")):
        phase("placing mFoVs into the section overview",
              f"reads every beam of all {score.n_tiles} mFoVs and places "
              f"each at its recorded coordinate - no alignment is done, "
              f"and this is the slow part")
        score.overview = render_region_overview(score, cfg,
                                                on_progress=progress)

    dash = None
    if write_dashboard and bool(cfg.get("dashboard_enabled", True)):
        phase("building the interactive dashboard")
        dash = write_section_dashboard(score, cfg)

    phase("writing the section report")
    written = write_region_report(score, cfg, fingerprint)
    phase_end()
    if dash is not None:
        say(f"    dashboard: {dash}")

    q = [float(x.get("quality", -100.0)) for x in score.scored
         if x.get("status") == "ok"]
    q_txt = f"{sorted(q)[len(q) // 2]:.2f}" if q else "n/a"
    bad_beams = [b["beam_index"] for b in score.beam_summary if b["is_outlier"]]
    say(f"    mfovs={score.n_tiles} blank={score.n_blank} "
        f"analyzed={len(score.scored)} images_read={score.n_images_read} "
        f"median_q={q_txt} in {score.elapsed_s:.1f}s")
    if bad_beams:
        say(f"    OUTLIER BEAM(S): {bad_beams}")
    for e in score.errors:
        say(f"    ! {e}")
    for n in score.notes:
        say(f"    note: {n}")
    say(f"    report: {written['report_html']}")

    detail = {
        "n_tiles": score.n_tiles,
        "n_scored": len(score.scored),
        "n_images_read": score.n_images_read,
        "elapsed_s": round(score.elapsed_s, 2),
        "outlier_beams": bad_beams,
        "report": str(written["report_html"]),
    }
    ledger.mark(root, region.key, fingerprint, status="scored", detail=detail)

    out.update(ok=True, median_q=q_txt, outlier_beams=bad_beams,
               n_tiles=score.n_tiles, n_scored=len(score.scored),
               elapsed_s=round(score.elapsed_s, 2),
               report=str(written["report_html"]),
               dashboard=str(dash) if dash else None,
               errors=list(score.errors), notes=list(score.notes))
    return out
