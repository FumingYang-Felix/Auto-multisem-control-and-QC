"""
Regression tests for detect_l_marker_elbow_on_mosaic_ds:

1. The cv2.convexityDefects() shape change between OpenCV versions. OpenCV
   4.x returns convexityDefects() as shape (n, 1, 4); OpenCV 5.x returns
   (n, 4). The code used to assume the former and crashed with "TypeError:
   cannot unpack non-iterable numpy.int32 object" on OpenCV 5.x -- found by
   reproducing the stitch+detect pipeline against a real failed calibration
   attempt where the L-marker was clearly visible in the raw images but
   detection silently errored out, which the caller couldn't tell apart from
   "marker not present."

2. The is_complete flag: a real calibration run found the L-marker in all
   three markers, but each detection sat toward the mosaic's edge rather than
   centered (confirmed by reproducing the pipeline against the real raw
   images and inspecting the annotated output). The function didn't
   distinguish "found the true elbow" from "found a fragment/edge-adjacent
   piece," so the caller accepted an unreliable corner. is_complete is False
   whenever the CORNER POINT itself either touches the mosaic's true edge, or
   merely sits within the outer edge_margin_frac band.

3. is_complete must be judged by the corner point alone, NOT the whole
   contour's bounding box. An earlier version checked the full bounding box,
   which meant a marker with one arm longer than the mosaic's field of view
   could never be "complete" no matter how well-centered its corner was (the
   far arm tip always sat at the mosaic edge) -- confirmed against a real
   recentered attempt from the field (corner at (1921,1040) in a 2644x2295
   mosaic) that a subsequent run showed had actually converged to a clean,
   correctly-centered corner, but was rejected as "still partial" and the
   search exhausted all 24 points looking for something better that wasn't
   achievable. Only the corner matters for the affine calc, so only the
   corner's own position is checked here.

4. The elbow itself is now found via a line-fit intersection (fit each arm's
   centerline over its full length, intersect the two lines), not local
   corner curvature (convexity defects / corner detectors). A real run's
   raw images showed a threshold/stitching gap fragmenting the L-marker into
   3 disconnected contours; the old method picked the largest single
   fragment (just the horizontal arm, no real corner on it at all) and
   returned a confidently-wrong point nowhere near the true corner. The
   line-fit only needs enough intact pixels somewhere along each arm, so it
   tolerates the gap.

5. The line-fit's arm "band" must anchor to where the arm's content actually
   starts, not to the bounding box's own edge. The bbox used for the fit
   comes from a gap-bridging dilation (see gap_bridge_full) that pads it well
   beyond the marker's real content on every side to merge fragments -- a
   real M1 case showed the horizontal arm's true content starting ~64px
   inside the bbox edge (not at row 0), which silently truncated the band to
   only part of the arm's thickness and biased the fitted corner toward the
   empty, padded side (visibly off the true corner in a saved comparison
   image) instead of the true centerline intersection.

Run standalone (no pytest needed):   python tests/test_lmarker_detection.py
"""
import os
import sys

import cv2
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, REPO)

from auto_stage_calib import detect_l_marker_elbow_on_mosaic_ds  # noqa: E402


def _l_mosaic(width, height, corner_x, corner_y, arm_len=150, arm_thick=30):
    """A white mosaic with a black L-shaped marker: a vertical arm going down
    and a horizontal arm going right, meeting at (corner_x, corner_y)."""
    mosaic = np.full((height, width), 255, dtype=np.uint8)
    cv2.rectangle(mosaic, (corner_x, corner_y), (corner_x + arm_thick, corner_y + arm_len), 0, -1)
    cv2.rectangle(mosaic, (corner_x, corner_y), (corner_x + arm_len, corner_y + arm_thick), 0, -1)
    return mosaic


def test_detects_elbow_without_crashing_on_this_opencv_version():
    mosaic = _l_mosaic(400, 400, corner_x=150, corner_y=100)
    result = detect_l_marker_elbow_on_mosaic_ds(
        mosaic_ds=mosaic, threshold=70, morph_kernel_full=11, min_area_full=1000.0, ds=1,
    )
    assert result is not None, "L-marker elbow must be detected on a clean synthetic L shape"
    (ex, ey), is_complete = result
    # elbow should land near the inner corner (~150, 100), not some arbitrary point
    assert 100 < ex < 220 and 60 < ey < 200, f"elbow ({ex},{ey}) not near the expected corner region"


def test_well_centered_marker_is_complete():
    # 1000x1000 mosaic, L-marker corner near the middle -> nowhere close to
    # any edge or the outer 25% periphery band.
    mosaic = _l_mosaic(1000, 1000, corner_x=450, corner_y=450)
    result = detect_l_marker_elbow_on_mosaic_ds(
        mosaic_ds=mosaic, threshold=70, morph_kernel_full=11, min_area_full=1000.0, ds=1,
    )
    assert result is not None
    _corner, is_complete = result
    assert is_complete is True, "a well-centered, fully-contained L-marker must be flagged complete"


def test_marker_toward_the_edge_is_incomplete():
    # Reproduces the real field scenario: the shape is fully inside the array
    # (not literally clipped) but sits within the outer periphery band --
    # exactly what the three real calibration mosaics showed.
    #
    # gap_bridge_full is overridden the same way the gap/padding tests below
    # already do: the default (800 full-res px) is tuned for real ds=8
    # mosaics roughly 2600px wide, where it's a small fraction of the frame.
    # Left at its default against this tiny 1000x1000 ds=1 canvas, it dilates
    # the 150px marker by ~400px per side -- ballooning the bbox to nearly
    # the whole canvas and starving the line-fit of any real anchor, which
    # the corner-near-real-foreground check (added after a real false
    # detection landed in blank space) correctly refuses to trust.
    mosaic = _l_mosaic(1000, 1000, corner_x=800, corner_y=450, arm_len=150, arm_thick=30)
    result = detect_l_marker_elbow_on_mosaic_ds(
        mosaic_ds=mosaic, threshold=70, morph_kernel_full=11, min_area_full=1000.0, ds=1,
        gap_bridge_full=200,
    )
    assert result is not None
    _corner, is_complete = result
    assert is_complete is False, "a marker sitting toward the mosaic edge must be flagged incomplete"


def test_centered_corner_with_long_arm_reaching_edge_is_complete():
    # Reproduces the actual root cause: the corner is dead-center, but one
    # arm is long enough that its far tip reaches the mosaic's true edge.
    # Only the corner's own position should matter -- the arm's reach must
    # NOT make this "incomplete."
    mosaic = _l_mosaic(1000, 1000, corner_x=500, corner_y=500, arm_len=490, arm_thick=30)
    result = detect_l_marker_elbow_on_mosaic_ds(
        mosaic_ds=mosaic, threshold=70, morph_kernel_full=11, min_area_full=1000.0, ds=1,
    )
    assert result is not None
    (cx, cy), is_complete = result
    assert 480 < cx < 560 and 480 < cy < 560, f"corner ({cx},{cy}) not near the expected (500,500)"
    assert is_complete is True, (
        "a centered corner must be complete even if a long arm reaches the mosaic edge -- "
        "the whole shape's extent must not be judged, only the corner's own position"
    )


def test_gap_in_one_arm_still_finds_correct_elbow():
    # Reproduces the real M2 field bug: a threshold/stitching gap fragments
    # the marker into disconnected pieces (3 separate contours in the real
    # case). The line-fit must still find the true corner using whatever
    # intact pixels remain along each arm, not latch onto a single fragment.
    mosaic = _l_mosaic(1000, 1000, corner_x=300, corner_y=300, arm_len=300, arm_thick=30)
    cv2.rectangle(mosaic, (300, 420), (330, 460), 255, -1)  # punch a gap in the vertical arm
    result = detect_l_marker_elbow_on_mosaic_ds(
        mosaic_ds=mosaic, threshold=70, morph_kernel_full=11, min_area_full=1000.0, ds=1,
        gap_bridge_full=200,
    )
    assert result is not None
    (cx, cy), _is_complete = result
    assert 280 < cx < 320 and 280 < cy < 320, (
        f"elbow ({cx},{cy}) should still land near the true corner (300,300) despite the gap"
    )


def test_band_anchors_to_real_content_not_padded_bbox_edge():
    # The bbox used for the line-fit comes from a gap-bridging dilation that
    # pads it well beyond the marker's real content on every side (needed to
    # merge disconnected fragments) -- the arm band must anchor to where
    # content actually starts, not to the bbox's own edge, or the fit is
    # biased toward the padded/empty side.
    mosaic = _l_mosaic(1200, 1200, corner_x=500, corner_y=500, arm_len=250, arm_thick=30)
    result = detect_l_marker_elbow_on_mosaic_ds(
        mosaic_ds=mosaic, threshold=70, morph_kernel_full=11, min_area_full=1000.0, ds=1,
        gap_bridge_full=400,  # heavy padding relative to arm_thick=30
    )
    assert result is not None
    (cx, cy), _is_complete = result
    assert 480 < cx < 520 and 480 < cy < 520, (
        f"elbow ({cx},{cy}) biased away from the true corner (500,500) by bbox padding"
    )


def test_no_marker_present_returns_none_not_a_crash():
    blank = np.full((400, 400), 255, dtype=np.uint8)
    result = detect_l_marker_elbow_on_mosaic_ds(
        mosaic_ds=blank, threshold=70, morph_kernel_full=11, min_area_full=1000.0, ds=1,
    )
    assert result is None


def test_gap_bridge_does_not_merge_a_distant_unrelated_dark_region():
    # Reproduces a real failed run (M2, 2026-08-11): a genuine L-marker sat
    # ~10um from an unrelated dark scratch on the same wafer/tissue surface.
    # gap_bridge_full's old default (800 full-res px = 6.4um at 8nm/px) was
    # big enough to bridge the two into one bbox spanning the empty space
    # between them; the line-fit intersection then landed in that blank gap,
    # nowhere near either shape, yet still reported a confident "complete"
    # corner. The unrelated blob here sits 200px away -- well inside the old
    # 800px default's reach but outside the new, much smaller default.
    mosaic = _l_mosaic(1000, 1000, corner_x=100, corner_y=100, arm_len=150, arm_thick=30)
    cv2.rectangle(mosaic, (450, 450), (500, 500), 0, -1)  # unrelated dark blob, ~200px from the L
    result = detect_l_marker_elbow_on_mosaic_ds(
        mosaic_ds=mosaic, threshold=70, morph_kernel_full=11, min_area_full=1000.0, ds=1,
    )
    assert result is not None, "the real L-marker must still be found despite the nearby unrelated blob"
    (cx, cy), _is_complete = result
    assert 80 < cx < 130 and 80 < cy < 130, (
        f"elbow ({cx},{cy}) should land near the true corner (100,100), not be dragged toward "
        f"the unrelated blob or land in the blank space between them"
    )


def test_large_near_uniform_blob_is_rejected_not_fit_as_an_l():
    # Reproduces a real failed run (M3/M1, 2026-08-11): on a real, densely
    # textured wafer/tissue surface, plenty of area can be dark enough to
    # clear the threshold purely from natural texture, with no true L-marker
    # anywhere in view. A large blob whose density is roughly uniform across
    # all four bbox quadrants (unlike a real L, which has one quadrant its
    # arms never reach at all) must be rejected rather than fit a bogus
    # corner onto it.
    rng = np.random.default_rng(7)
    mosaic = np.full((400, 400), 255, dtype=np.uint8)
    region = mosaic[100:300, 100:300]
    region[rng.random(region.shape) < 0.35] = 0  # dense, spatially-uniform speckle -- no L shape
    result = detect_l_marker_elbow_on_mosaic_ds(
        mosaic_ds=mosaic, threshold=70, morph_kernel_full=11, min_area_full=1000.0, ds=1,
    )
    assert result is None, "a dense, near-uniform blob with no true corner must not be reported as found"


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS  {name}")
    print(f"\n(running against cv2 {cv2.__version__})")
