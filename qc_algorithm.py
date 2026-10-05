from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Sequence, Tuple

import numpy as np
from PIL import Image


@dataclass
class ImageQCResult:
    image_path: str
    beam_index: int | None
    quality: float
    range_val: float
    horz_qual: float
    vert_qual: float
    std_high: float
    std_all: float
    percent_saturation: float
    acquisition_fail: bool
    used_signal_points: int
    status: str
    jitter_score: float = 0.0
    pct_dark: float = 0.0
    pct_bright: float = 0.0
    mean_intensity: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class MFOVQCResult:
    tile_idx: int
    x_um: float
    y_um: float
    quality: float
    n_images_used: int
    selected_beams: List[int]
    status: str
    failed_images: int
    image_results: List[Dict[str, Any]]
    tile_folder: str
    stig_x_med: float = 0.0
    stig_y_med: float = 0.0
    stig_anisotropy: float = 0.0
    pct_saturation_med: float = 0.0
    pct_dark_med: float = 0.0
    pct_bright_med: float = 0.0
    std_all_med: float = 0.0
    jitter_score_med: float = 0.0
    mean_intensity_med: float = 0.0
    is_blank_outside_roi: bool = False
    is_jittery: bool = False
    status_category: str = "ok"
    # One (or a few) beams whose quality is a statistical outlier vs. the rest
    # of THIS mfov's own beams - see detect_outlier_beams(). Empty when there
    # weren't enough valid beams to judge, or none stood out.
    outlier_beams: List[Dict[str, Any]] = field(default_factory=list)
    has_beam_outlier: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def detect_outlier_beams(
    valid_results: Sequence[ImageQCResult],
    mad_sigma: float = 3.0,
    min_beams: int = 5,
) -> List[Dict[str, Any]]:
    """Find beams whose quality is a statistical outlier among the OTHER
    beams in this same mfov - i.e. "one beam went berserk while the rest of
    the mfov is fine", which a plain median/mean aggregate quality score
    would otherwise hide (a single bad beam barely moves a median over 90
    good ones).

    Same median + MAD*1.4826*sigma robust-outlier convention already used
    for cross-tile stig/jitter outliers in live_qc_support.py, applied here
    across beams within one tile instead of across tiles within a region.
    Needs at least `min_beams` valid beams to judge (an outlier vs. only 2-3
    others isn't statistically meaningful) - returns [] below that, same as
    a subset-sampled mfov would today.
    """
    if len(valid_results) < min_beams:
        return []

    quals = np.asarray([r.quality for r in valid_results], dtype=np.float64)
    med = float(np.median(quals))
    mad = float(np.median(np.abs(quals - med)))
    threshold = mad_sigma * 1.4826 * max(mad, 1e-9)

    outliers = []
    for r, q in zip(valid_results, quals):
        deviation = float(q) - med
        if abs(deviation) > threshold:
            outliers.append({
                "beam_index": r.beam_index,
                "quality": float(q),
                "group_median": med,
                "deviation": deviation,
            })
    return outliers


def quality_tier(quality: float, low_threshold: float, high_threshold: float) -> str:
    """Three-tier bucket for a quality score: 'red' below low_threshold,
    'green' at/above high_threshold, 'amber' in between. Used to drive the
    green/amber/red mfov coloring (see live_qc_support.py).

    Boundary convention matches two things already established elsewhere:
    strict '<' for the low cutoff matches "low_q" in live_qc_support.py
    (raw_q < low_quality_threshold), and both cutoffs match how
    matplotlib.colors.BoundaryNorm bins values (left-closed/right-open, so a
    value exactly AT low_threshold falls in the amber bin, not red) - used
    for the same three colors in the matplotlib/SVG focus-quality panel.
    """
    if quality < low_threshold:
        return "red"
    if quality >= high_threshold:
        return "green"
    return "amber"


# MATLAB logic translated as closely as practical, with one intentional fix:
# the original code likely meant Id = 255 - uint8_image, not 255 - im2double(image).
# Using 0..255 intensities makes the saturation / empty-bottom-row checks meaningful.
_YSHIFT = np.array([0, 0, 0, 1, 1, 1, 2, 2, 2], dtype=np.int32)
_XSHIFT = np.array([0, 1, 2, 0, 1, 2, 0, 1, 2], dtype=np.int32)

# 1-based MATLAB indices converted to 0-based Python indices below.
_SUR = [
    [2, 4, 6],
    [1, 2, 3],
    [4, 8, 6],
    [4, 5, 6],
    [1, 5, 7],
    [1, 4, 7],
    [2, 6, 8],
    [2, 5, 8],
]
_CENT = [
    [1, 5, 3],
    [4, 5, 6],
    [7, 5, 9],
    [7, 8, 9],
    [2, 4, 8],
    [2, 5, 8],
    [3, 5, 9],
    [3, 6, 9],
]
_SUR0 = [[i - 1 for i in row] for row in _SUR]
_CENT0 = [[i - 1 for i in row] for row in _CENT]
_GROUPS = [(0, 2), (1, 3), (4, 6), (5, 7)]


def _read_gray_u8(image_path: str | Path) -> np.ndarray:
    with Image.open(image_path) as img:
        gray = img.convert("L")
        arr = np.asarray(gray, dtype=np.uint8)
    return arr


def _jitter_score(
    gray_u8: np.ndarray,
    smooth_size: int = 8,
    band_fraction: float = 0.05,
    band_position: str = "top",
) -> float:
    """Localized line-jitter score, self-referencing per image.

    mSEM line jitter typically appears in only the first ~5% of rows (beam still
    settling at scan start). Averaging row-diff over the whole image dilutes the
    signal; comparing the suspect band against a clean reference band right next
    to it does not. Returns:

        score = std(row-diff in suspect band) / std(row-diff in reference band)

    Interpretation:
      ~1.0     suspect band looks like the rest of the image (no jitter)
      ~1.5–2.0 mild jitter localized to the band
      > 2.5    severe jitter

    Args:
        smooth_size:    boxcar smoothing window along scan dir, suppresses pixel noise
        band_fraction:  fraction of total rows that form the suspect band (0.05 = 5%)
        band_position:  "top" (default, mSEM convention) or "bottom"
    """
    H = gray_u8.shape[0]
    if H < 10:
        return 1.0
    g = gray_u8.astype(np.float32)
    if smooth_size > 1 and g.shape[1] > smooth_size:
        cs = np.cumsum(g, axis=1)
        cs = np.concatenate([np.zeros((g.shape[0], 1), dtype=g.dtype), cs], axis=1)
        g = (cs[:, smooth_size:] - cs[:, :-smooth_size]) / float(smooth_size)
    band_h = max(2, int(round(H * float(band_fraction))))
    band_h = min(band_h, H // 3)
    if str(band_position).lower() == "bottom":
        suspect = g[-band_h:]
        ref_end = H - band_h
        ref_start = max(0, ref_end - 5 * band_h)
        reference = g[ref_start:ref_end]
    else:
        suspect = g[:band_h]
        ref_start = band_h
        ref_end = min(H, ref_start + 5 * band_h)
        reference = g[ref_start:ref_end]
    if suspect.shape[0] < 2 or reference.shape[0] < 2:
        return 1.0
    sd_suspect = float(np.std(np.diff(suspect, axis=0)))
    sd_ref = float(np.std(np.diff(reference, axis=0)))
    return sd_suspect / max(sd_ref, 1e-3)


def _intensity_stats(gray_u8: np.ndarray) -> Tuple[float, float, float]:
    """Return (mean_intensity, pct_dark, pct_bright) on the raw grayscale image.
    pct_dark = % pixels <= 5; pct_bright = % pixels >= 250."""
    total = float(gray_u8.size)
    if total == 0:
        return 0.0, 0.0, 0.0
    mean_i = float(gray_u8.mean())
    pct_dark = float((gray_u8 <= 5).sum()) / total * 100.0
    pct_bright = float((gray_u8 >= 250).sum()) / total * 100.0
    return mean_i, pct_dark, pct_bright


def _sample_shifted_stack_u8(gray_u8: np.ndarray, dsamp: int) -> np.ndarray:
    """Return Id with shape [dsamp, dsamp, 9], where Id = 255 - sampled_intensity."""
    ys, xs = gray_u8.shape
    if ys < dsamp + 5 or xs < dsamp + 5:
        raise ValueError(f"image too small for dsamp={dsamp}: shape={gray_u8.shape}")

    dsampy = max(1, int(math.floor((ys - 5) / dsamp)))
    dsampx = max(1, int(math.floor((xs - 5) / dsamp)))

    out = np.zeros((dsamp, dsamp, len(_YSHIFT)), dtype=np.float64)
    for j, (dy, dx) in enumerate(zip(_YSHIFT, _XSHIFT)):
        rows = np.arange(3 + dy, ys, dsampy, dtype=np.int32)[:dsamp]
        cols = np.arange(3 + dx, xs, dsampx, dtype=np.int32)[:dsamp]
        if rows.size < dsamp or cols.size < dsamp:
            raise ValueError(
                f"sampling under-filled for shift ({dy},{dx}): rows={rows.size}, cols={cols.size}, shape={gray_u8.shape}"
            )
        patch = gray_u8[np.ix_(rows, cols)].astype(np.float64)
        out[:, :, j] = 255.0 - patch
    return out


def compute_single_image_quality(
    image_path: str | Path,
    beam_index: int | None = None,
    dsamp: int = 200,
    sig_thresh: float = 0.01,
    top_fraction: float = 0.50,
    jitter_smooth_size: int = 8,
    jitter_band_fraction: float = 0.05,
    jitter_band_position: str = "top",
) -> ImageQCResult:
    image_path = str(image_path)
    try:
        gray = _read_gray_u8(image_path)
        mean_i, pct_dark, pct_bright = _intensity_stats(gray)
        jitter_score = _jitter_score(
            gray,
            smooth_size=jitter_smooth_size,
            band_fraction=jitter_band_fraction,
            band_position=jitter_band_position,
        )
        Id = _sample_shifted_stack_u8(gray, dsamp=dsamp)

        maxId = np.mean(Id, axis=2)
        devI = np.std(Id, axis=2, ddof=0)

        # Acquisition failure heuristic from MATLAB: if sampled bottom row is all zero signal.
        last_values = 255.0 - Id[-1, :, :]
        acquisition_fail = bool(np.sum(last_values) == 0)

        sats = (Id >= 255.0) | (Id <= 1.0)
        percent_sat = float(np.mean(sats) * 100.0)
        sum_sats = np.sum(sats, axis=2)

        use_vals = devI[sum_sats < 1]
        if use_vals.size > 5 and not acquisition_fail:
            thresh_val = float(np.quantile(use_vals, 1.0 - sig_thresh, method="nearest"))
            use_sig = np.where((devI >= thresh_val) & (sum_sats == 0))
            used_signal_points = int(use_sig[0].size)

            use_difs: List[np.ndarray] = []
            for cent_idx, sur_idx in zip(_CENT0, _SUR0):
                dif = np.mean(Id[:, :, cent_idx], axis=2) - np.mean(Id[:, :, sur_idx], axis=2)
                use_difs.append(dif[use_sig])

            top_con = np.zeros(4, dtype=np.float64)
            for g, (i0, i1) in enumerate(_GROUPS):
                vals = np.abs(np.concatenate([use_difs[i0], use_difs[i1]]))
                if vals.size == 0:
                    top_con[g] = 0.0
                else:
                    thresh = float(np.quantile(vals, 1.0 - top_fraction, method="nearest"))
                    top_con[g] = float(np.mean(vals[vals >= thresh])) if np.any(vals >= thresh) else 0.0

            sample_size = used_signal_points
            mean_vals = np.zeros((sample_size, 4), dtype=np.float64)
            for g, (i0, i1) in enumerate(_GROUPS):
                vals = np.stack([use_difs[i0], use_difs[i1]], axis=1)
                mean_vals[:, g] = np.abs(np.mean(vals, axis=1))

            top_max = np.zeros(2, dtype=np.float64)
            for f in range(2):
                max_vals = np.maximum(mean_vals[:, f], mean_vals[:, f + 2])
                thresh = float(np.quantile(max_vals, 1.0 - top_fraction, method="nearest"))
                top_max[f] = float(np.mean(max_vals[max_vals >= thresh])) if np.any(max_vals >= thresh) else 0.0

            glob = np.mean(Id, axis=2)
            glob_vals = np.sort(glob.reshape(-1))
            back_ground = float(np.median(glob_vals[:dsamp])) if glob_vals.size >= dsamp else float(np.median(glob_vals))
            top_glob_m = float(np.mean(glob[use_sig])) if used_signal_points > 0 else 0.0
            range_val = top_glob_m - back_ground

            eps = 1e-12
            vert_qual = (top_con[1] / max(top_con[0], eps) - 1.0) * 100.0
            horz_qual = (top_con[3] / max(top_con[2], eps) - 1.0) * 100.0
            quality = (top_max[1] / max(top_max[0], eps) - 1.0) * 100.0
            std_high = float(np.mean(devI[use_sig]) / max(range_val, eps)) if used_signal_points > 0 else 0.0
            std_all = float(np.std(Id.reshape(-1), ddof=0))

            return ImageQCResult(
                image_path=image_path,
                beam_index=beam_index,
                quality=float(quality),
                range_val=float(range_val),
                horz_qual=float(horz_qual),
                vert_qual=float(vert_qual),
                std_high=std_high,
                std_all=std_all,
                percent_saturation=percent_sat,
                acquisition_fail=acquisition_fail,
                used_signal_points=used_signal_points,
                status="ok",
                jitter_score=float(jitter_score),
                pct_dark=float(pct_dark),
                pct_bright=float(pct_bright),
                mean_intensity=float(mean_i),
            )

        return ImageQCResult(
            image_path=image_path,
            beam_index=beam_index,
            quality=-100.0,
            range_val=0.0,
            horz_qual=0.0,
            vert_qual=0.0,
            std_high=0.0,
            std_all=float(np.std(Id.reshape(-1), ddof=0)),
            percent_saturation=percent_sat,
            acquisition_fail=acquisition_fail,
            used_signal_points=0,
            status="insufficient_signal" if not acquisition_fail else "acquisition_fail",
            jitter_score=float(jitter_score),
            pct_dark=float(pct_dark),
            pct_bright=float(pct_bright),
            mean_intensity=float(mean_i),
        )
    except Exception as exc:
        return ImageQCResult(
            image_path=image_path,
            beam_index=beam_index,
            quality=-100.0,
            range_val=0.0,
            horz_qual=0.0,
            vert_qual=0.0,
            std_high=0.0,
            std_all=0.0,
            percent_saturation=0.0,
            acquisition_fail=True,
            used_signal_points=0,
            status=f"error: {exc}",
            jitter_score=0.0,
            pct_dark=0.0,
            pct_bright=0.0,
            mean_intensity=0.0,
        )


def aggregate_mfov_quality(
    tile_idx: int,
    x_um: float,
    y_um: float,
    tile_folder: str | Path,
    image_results: Sequence[ImageQCResult],
    selected_beams: Sequence[int],
    aggregate_mode: str = "median",
    outside_roi_sat_threshold: float = 30.0,
    outside_roi_std_threshold: float = 3.0,
    jitter_warn_threshold: float = 0.25,
    beam_outlier_mad_sigma: float = 3.0,
    beam_outlier_min_beams: int = 5,
) -> MFOVQCResult:
    image_results = list(image_results)
    valid_results = [r for r in image_results if r.status == "ok" and not r.acquisition_fail]
    valid_q = [r.quality for r in valid_results]
    failed = sum(1 for r in image_results if r.acquisition_fail or r.status != "ok")

    outlier_beams = detect_outlier_beams(
        valid_results, mad_sigma=beam_outlier_mad_sigma, min_beams=beam_outlier_min_beams
    )

    if not valid_q:
        quality = -100.0
        status = "no_valid_images"
    else:
        mode = aggregate_mode.strip().lower()
        arr = np.asarray(valid_q, dtype=np.float64)
        if mode == "mean":
            quality = float(np.mean(arr))
        elif mode == "min":
            quality = float(np.min(arr))
        elif mode == "trimmed_mean":
            if arr.size >= 3:
                arr = np.sort(arr)
                arr = arr[1:-1]
            quality = float(np.mean(arr))
        else:
            quality = float(np.median(arr))
        status = "ok"

    def _med(vals: List[float], default: float = 0.0) -> float:
        if not vals:
            return default
        return float(np.median(np.asarray(vals, dtype=np.float64)))

    pct_sat_med = _med([r.percent_saturation for r in image_results])
    pct_dark_med = _med([r.pct_dark for r in image_results])
    pct_bright_med = _med([r.pct_bright for r in image_results])
    std_all_med = _med([r.std_all for r in image_results])
    jitter_med = _med([r.jitter_score for r in image_results])
    mean_i_med = _med([r.mean_intensity for r in image_results])

    stig_pool = valid_results if valid_results else image_results
    stig_x_med = _med([r.horz_qual for r in stig_pool])
    stig_y_med = _med([r.vert_qual for r in stig_pool])
    stig_anisotropy = stig_x_med - stig_y_med

    is_blank = (
        pct_sat_med > float(outside_roi_sat_threshold)
        or std_all_med < float(outside_roi_std_threshold)
    )
    is_jittery = jitter_med >= float(jitter_warn_threshold)

    if is_blank:
        status_category = "outside_roi"
    elif status == "ok":
        status_category = "ok"
    elif image_results and all(r.acquisition_fail for r in image_results):
        status_category = "acquisition_fail"
    elif image_results and all(r.status == "insufficient_signal" for r in image_results):
        status_category = "insufficient_signal"
    elif image_results and any(r.status.startswith("error") for r in image_results):
        status_category = "error"
    else:
        status_category = "no_valid_images"

    return MFOVQCResult(
        tile_idx=int(tile_idx),
        x_um=float(x_um),
        y_um=float(y_um),
        quality=quality,
        n_images_used=int(len(valid_q)),
        selected_beams=[int(b) for b in selected_beams],
        status=status,
        failed_images=int(failed),
        image_results=[r.to_dict() for r in image_results],
        tile_folder=str(tile_folder),
        stig_x_med=float(stig_x_med),
        stig_y_med=float(stig_y_med),
        stig_anisotropy=float(stig_anisotropy),
        pct_saturation_med=float(pct_sat_med),
        pct_dark_med=float(pct_dark_med),
        pct_bright_med=float(pct_bright_med),
        std_all_med=float(std_all_med),
        jitter_score_med=float(jitter_med),
        mean_intensity_med=float(mean_i_med),
        is_blank_outside_roi=bool(is_blank),
        is_jittery=bool(is_jittery),
        status_category=str(status_category),
        outlier_beams=outlier_beams,
        has_beam_outlier=bool(outlier_beams),
    )


def dumps_json(obj: Dict[str, Any]) -> str:
    return json.dumps(obj, ensure_ascii=False)
