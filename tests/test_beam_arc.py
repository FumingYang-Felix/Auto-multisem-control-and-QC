"""
Tests for beam_arc_monitor — verified off-hardware against a small committed
fixture (tests/fixtures/sample_arc_log.xml) distilled from a real service log:
one genuine beam-arc episode (the operator's ids 4431-4434) plus a couple of
unrelated AutoFocus/Matlab ERRORs to check for false positives.

Run standalone (no pytest needed):   python tests/test_beam_arc.py
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, REPO)

from beam_arc_monitor import ArcDetector, HV_OUTPUTS, scan_log_file  # noqa: E402

SAMPLE_LOG = os.path.join(HERE, "fixtures", "sample_arc_log.xml")

# The four clearance-failure ERRORs in the sample log (timestamps in epoch-ms).
ARC_FIRST_TS = 1782395056060   # Tip_HV_BE, the first of the burst


def test_detects_the_single_arc():
    arcs = scan_log_file(SAMPLE_LOG)                 # default min_outputs=2
    assert len(arcs) == 1, f"expected exactly 1 arc, got {len(arcs)}"
    a = arcs[0]
    assert a.ts_ms == ARC_FIRST_TS, f"arc ts {a.ts_ms} != {ARC_FIRST_TS}"
    assert len(a.outputs) >= 2


def test_captures_all_four_hv_outputs_including_ol_extr():
    # require all four so the coalesced event reflects the full 4431-4434 burst
    arcs = scan_log_file(SAMPLE_LOG, min_outputs=4)
    assert len(arcs) == 1
    a = arcs[0]
    assert set(a.outputs) == set(HV_OUTPUTS), f"outputs={a.outputs}"
    assert "OL_HV_Extr" in a.outputs
    assert a.n_events == 4


def test_no_false_positive_on_autofocus_errors():
    # the log has ~57 other ERRORs (AutoFocus / Matlab failures); none is an arc
    arcs = scan_log_file(SAMPLE_LOG)
    assert len(arcs) == 1, "an autofocus/matlab ERROR was misclassified as a beam arc"


def test_window_and_cooldown_semantics():
    fired = []
    det = ArcDetector(on_arc=fired.append, window_ms=1000, min_outputs=2, cooldown_ms=5000)
    # two HV outputs within the window -> one arc
    det.feed_event(1000, "ERROR", "Tip_HV_BE_OutputStatusDetails: Output off (beam clearance failure)")
    det.feed_event(1005, "ERROR", "OL_HV_Extr_OutputStatusDetails: Output off (beam clearance failure)")
    assert len(fired) == 1
    # more failures inside cooldown -> still the same (one) arc
    det.feed_event(1010, "ERROR", "Stage_HV_BE_OutputStatusDetails: Output off (beam clearance failure)")
    assert len(fired) == 1
    # a fresh episode well after cooldown -> a second arc
    det.feed_event(50000, "ERROR", "Tip_HV_BE_OutputStatusDetails: Output off (beam clearance failure)")
    det.feed_event(50005, "ERROR", "OL_HV_Extr_OutputStatusDetails: Output off (beam clearance failure)")
    assert len(fired) == 2


def test_ignores_non_error_and_unrelated_messages():
    fired = []
    det = ArcDetector(on_arc=fired.append, min_outputs=2)
    # right string but INFO level -> ignore
    det.feed_event(1, "INFO", "OL_HV_Extr_OutputStatusDetails: Output off (beam clearance failure)")
    # an autofocus ERROR -> ignore
    det.feed_event(2, "ERROR", "Failed to execute matlab highlevel command AutoFocus")
    # a single clearance failure (below min_outputs=2) -> ignore
    det.feed_event(3, "ERROR", "Tip_HV_BE_OutputStatusDetails: Output off (beam clearance failure)")
    assert fired == []


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS  {name}")
    # show what a real detection looks like
    a = scan_log_file(SAMPLE_LOG, min_outputs=4)[0]
    print("\nDetected on the sample log:\n  " + a.describe())
