"""
Regression test for AutoStageCalibrator's per-marker/total timing report.

run_three_markers writes a timing_report_<ts>.txt into out_dir recording how
long each marker took and the overall total, via a `finally` block so it's
written even when a marker fails partway through the run (not just on a
clean, fully-successful run).

Run standalone (no pytest needed):   python tests/test_calib_timing.py
"""
import os
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, REPO)

import auto_stage_calib as asc  # noqa: E402


class _FakeConn:
    def execute(self, cmd):
        return 0


def _make_calibrator():
    cfg = asc.AutoCalibConfig(verbose=False)
    return asc.AutoStageCalibrator(conn=_FakeConn(), cfg=cfg)


def test_timing_report_written_on_early_failure():
    with tempfile.TemporaryDirectory() as td:
        outputs_dir = os.path.join(td, "outputs")
        calib = _make_calibrator()

        call_n = {"n": 0}

        def fake_find_one_marker(idx, expected, outputs_dir):
            call_n["n"] += 1
            success = call_n["n"] < 2  # fail on the 2nd marker
            return asc.FoundMarker(
                idx=idx, expected=expected, actual=expected,
                stitched_annotated_path="", elbow_px_mosaic_ds=(0, 0),
                success=success, error="" if success else "simulated failure",
            )
        calib.find_one_marker = fake_find_one_marker

        markers = [
            asc.LMarkerPosition(x=0, y=0, z=1, name="M3"),
            asc.LMarkerPosition(x=1, y=1, z=1, name="M2"),
            asc.LMarkerPosition(x=2, y=2, z=1, name="M1"),
        ]
        results, transform = calib.run_three_markers(markers, outputs_dir)
        assert len(results) == 2, "must stop right after the failing marker"
        assert transform is None

        timing_files = [f for f in os.listdir(outputs_dir) if f.startswith("timing_report_")]
        assert len(timing_files) == 1, f"expected exactly 1 timing report, got {timing_files}"
        content = open(os.path.join(outputs_dir, timing_files[0])).read()
        assert "Marker 1 (M3)" in content and "SUCCESS" in content
        assert "Marker 2 (M2)" in content and "FAILED" in content
        assert "Marker 3" not in content, "a marker never attempted must not appear in the report"
        assert "Total:" in content


def test_timing_report_written_on_full_success():
    with tempfile.TemporaryDirectory() as td:
        outputs_dir = os.path.join(td, "outputs")
        calib = _make_calibrator()

        def fake_find_one_marker(idx, expected, outputs_dir):
            time.sleep(0.01)
            return asc.FoundMarker(
                idx=idx, expected=expected, actual=expected,
                stitched_annotated_path="", elbow_px_mosaic_ds=(0, 0),
                success=True, error="",
            )
        calib.find_one_marker = fake_find_one_marker

        markers = [
            asc.LMarkerPosition(x=0, y=0, z=1, name="M3"),
            asc.LMarkerPosition(x=100, y=0, z=1, name="M2"),
            asc.LMarkerPosition(x=0, y=100, z=1, name="M1"),
        ]
        results, transform = calib.run_three_markers(markers, outputs_dir)
        assert len(results) == 3
        assert transform is not None

        timing_files = [f for f in os.listdir(outputs_dir) if f.startswith("timing_report_")]
        assert len(timing_files) == 1
        content = open(os.path.join(outputs_dir, timing_files[0])).read()
        for name in ("M3", "M2", "M1"):
            assert f"({name})" in content and "SUCCESS" in content
        assert "Total:" in content


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS  {name}")
