from __future__ import annotations

import importlib.util
import json
import multiprocessing as mp
import os
import re
import signal
import sys
import time
from pathlib import Path
from statistics import mean, median
from typing import Any, Dict, List, Optional, Tuple

# Keep local imports available when this file is launched from Explorer / cmd.
_THIS_DIR = Path(__file__).resolve().parent
if str(_THIS_DIR) not in sys.path:
    sys.path.insert(0, str(_THIS_DIR))

from live_qc_support import (
    emit_completed_tile,
    prepare_run_selected_beams,
    qc_viewer_main,
    qc_worker_main,
)


LIVE_QC_CONFIG: Dict[str, Any] = {
    # Point this to YOUR CURRENT acquire script.
    "acquire_script_path": r"D:\mSEMAPI\mSEMAPI\focus_map.py",

    # Beam selection.
    # Recommended default: choose beam 1 + 4 random beams ONCE per run, then use the same 5 beams for all MFOVs.
    "beam_selection_mode": "FIXED_PER_RUN_RANDOM",  # FIXED_PER_RUN_RANDOM / FIXED_SET / PER_MFOV_RANDOM
    "include_beam1": True,
    "num_extra_beams": 4,
    "beam_universe": list(range(1, 92)),
    "random_seed": 20260316,
    "fixed_beams": [1, 10, 28, 55, 82],  # only used if beam_selection_mode == FIXED_SET

    # File discovery.
    "image_suffix_globs": ["*.bmp", "*.BMP"],
    "beam_index_regexes": [
        r"beam[_\- ]?(\d{1,3})",
        r"mfov[_\- ]?\d+[_\- ]?beam[_\- ]?(\d{1,3})",
        r"[_\- ]b(\d{1,3})(?:\D|$)",
        r"[_\- ](\d{1,3})(?:\.[^.]+)$",
    ],

    # QC algorithm.
    "dsamp": 200,
    "sig_thresh": 0.01,
    "top_fraction": 0.50,
    "aggregate_mode": "median",  # median / mean / min / trimmed_mean
    "low_quality_threshold": 10.0,

    # Viewer.
    "display_min": -100.0,
    "display_max": 100.0,
    "hex_radius_um": 0.0,  # 0 => auto estimate from tile centers
    "colormap": "RdYlGn",
    "save_every": 5,

    # Dashboard thresholds.
    "outside_roi_sat_threshold": 30.0,   # MFOV flagged outside-ROI when median(percent_saturation) > this
    "outside_roi_std_threshold": 3.0,    # ... or when median(std_all) < this (very flat = blank)
    "jitter_smooth_size": 8,             # boxcar smooth (px) along scan dir before row-diff; suppresses pixel noise
    "jitter_top_band_fraction": 0.05,    # fraction of rows that form the suspect band (top of image, where mSEM jitter lives)
    "jitter_band_position": "top",       # "top" (default mSEM) or "bottom"
    "jitter_warn_threshold": 1.5,        # absolute warn threshold for the new top-vs-rest ratio; flag tiles with score >= this
    "jitter_display_min": 1.0,           # lower bound of jitter color scale (1.0 = no jitter)
    "jitter_display_max": 2.5,           # upper bound of jitter color scale
    "stig_display_max": 50.0,            # absolute-magnitude clip for the quiver arrow length
    "stig_centered_display_max": 30.0,   # ± range for the stig hex color = (value − running_median); cancels constant AS offset

    # Downsampled overview images (qc_overview.py).
    "overview_enabled": True,            # per-MFOV overview PNG in each tile folder
    "overview_region_enabled": True,     # per-region stitched overview PNG in LiveQC dir
    "overview_ds": 16,                   # per-MFOV downsample factor (beam px / ds)
    "overview_padding": 80,              # full-res stitch padding (when coords present)
    "overview_beam_layout": "auto",      # auto | coords_file (use image_coordinates.txt) — else grid montage
    "overview_region_thumb_px": 64,      # max dim of each cached MFOV thumbnail
    "overview_region_canvas_px": 2000,   # target size of the region overview canvas
    "overview_mfov_name": "overview_mfov.png",
    "overview_region_name": "live_qc_region_overview.png",
}


def _apply_live_qc_config_override(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """Merge overrides from $MSEM_CONFIG_JSON['live_qc'] into LIVE_QC_CONFIG.
    No env var / bad file => no-op (terminal runs unchanged). See the matching
    apply_external_config_override() in focus_map.py for the focus_map section."""
    path = os.environ.get("MSEM_CONFIG_JSON", "").strip()
    if not path:
        return cfg
    if not os.path.isfile(path):
        print(f"[ExtConfig] MSEM_CONFIG_JSON set but file not found: {path} (using LiveQC defaults)")
        return cfg
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as exc:
        print(f"[ExtConfig] failed to parse {path}: {exc} (using LiveQC defaults)")
        return cfg
    overrides = data.get("live_qc", {})
    if not isinstance(overrides, dict):
        print(f"[ExtConfig] {path}['live_qc'] is not an object; ignoring")
        return cfg
    applied = []
    for k, v in overrides.items():
        old = cfg.get(k, "<unset>")
        cfg[k] = v
        applied.append(f"{k}: {old!r} -> {v!r}")
    if applied:
        print(f"[ExtConfig] applied {len(applied)} LiveQC override(s) from {path}:")
        for line in applied:
            print(f"    {line}")
    return cfg


_apply_live_qc_config_override(LIVE_QC_CONFIG)


def _sanitize_label(label: Optional[str], fallback: str) -> str:
    if not label:
        return fallback
    cleaned = re.sub(r'[\\/:*?"<>|]+', "_", str(label)).strip().rstrip(".")
    return cleaned or fallback


def load_module_from_path(script_path: str):
    script_path = os.path.abspath(script_path)
    if not os.path.exists(script_path):
        raise FileNotFoundError(f"acquire_script_path not found: {script_path}")

    module_name = f"acquire_module_{int(time.time())}"
    spec = importlib.util.spec_from_file_location(module_name, script_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot create import spec for: {script_path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = mod
    spec.loader.exec_module(mod)
    return mod


def start_live_qc_runtime(layout: List[Dict[str, Any]], results_dir: Path, cfg: Dict[str, Any]):
    ctx = mp.get_context("spawn")
    task_queue = ctx.Queue()
    viewer_queue = ctx.Queue()

    run_selected_beams = prepare_run_selected_beams(cfg)
    worker_cfg = dict(cfg)
    worker_cfg["results_dir"] = str(results_dir)
    worker_cfg["run_selected_beams"] = run_selected_beams

    viewer_cfg = dict(cfg)
    viewer_cfg["results_dir"] = str(results_dir)

    worker_p = ctx.Process(target=qc_worker_main, args=(task_queue, viewer_queue, worker_cfg), daemon=True)
    viewer_p = ctx.Process(target=qc_viewer_main, args=(layout, viewer_queue, viewer_cfg), daemon=True)

    viewer_p.start()
    worker_p.start()

    print(f"[LiveQC] worker pid={worker_p.pid} viewer pid={viewer_p.pid}")
    print(f"[LiveQC] run-selected beams: {run_selected_beams}")
    print(f"[LiveQC] results_dir: {results_dir}")
    return task_queue, viewer_queue, worker_p, viewer_p, run_selected_beams


def stop_live_qc_runtime(task_queue: Any, worker_p: Any, viewer_p: Any) -> None:
    try:
        task_queue.put(None)
    except Exception:
        pass

    try:
        worker_p.join(timeout=300)
    except Exception:
        pass

    try:
        viewer_p.join(timeout=300)
    except Exception:
        pass

    if worker_p.is_alive():
        print("[LiveQC] WARNING: worker still alive, terminating.")
        worker_p.terminate()
    if viewer_p.is_alive():
        print("[LiveQC] WARNING: viewer still alive, terminating.")
        viewer_p.terminate()


def make_live_qc_acquire_tiles(acq, live_qc_cfg: Dict[str, Any]):
    def acquire_tiles(
        conn,
        tiles: List[Tuple[float, float]],
        plans: List[Any],
        block_folder: Optional[str] = None,
    ):
        route = acq.build_tile_route(tiles)
        print(
            f"[Route] tiles={len(tiles)} route_len={len(route)} mode={acq.CONFIG['route_mode']} "
            f"direction={acq.CONFIG['travel_direction']} start={acq.CONFIG['start_position']}"
        )

        tile_times: List[float] = []
        move_times: List[float] = []
        acq_times: List[float] = []

        max_tiles = int(acq.CONFIG.get("speed_test_max_tiles", 0) or 0)
        warmup_tiles = int(acq.CONFIG.get("speed_test_warmup_tiles", 0) or 0)

        if block_folder is None:
            ts = time.strftime("%Y%m%d_%H%M%S")
            try:
                block_name = acq.build_block_name(
                    acq.CONFIG.get("json_file"), acq.CONFIG.get("acq_pixel_size_nm"), ts)
            except Exception:
                block_name = f"{acq.CONFIG['czi_name']}_{ts}"
            block_folder = os.path.join(acq.CONFIG["output_root"], block_name)

        os.makedirs(block_folder, exist_ok=True)
        print(f"[Output] Block folder: {block_folder}")

        region_idx = int(acq.CONFIG.get("region_idx", 1))
        region_label = _sanitize_label(acq.CONFIG.get("region_name"), f"Region_{region_idx}")
        block_label = Path(block_folder).name

        results_dir = Path(block_folder) / region_label / "LiveQC"
        layout = [{"tile_idx": int(p.tile_idx), "x_um": float(p.x), "y_um": float(p.y)} for p in plans]
        run_cfg = dict(live_qc_cfg)
        run_cfg["region_label"] = region_label
        run_cfg["region_name"] = acq.CONFIG.get("region_name") or region_label
        run_cfg["block_label"] = block_label
        run_cfg["block_folder"] = str(block_folder)
        run_cfg["imaging_json_path"] = acq.CONFIG.get("imaging_json_path")
        run_cfg["hex_radius_um_for_retake"] = float(live_qc_cfg.get("hex_radius_um") or 0.0)
        run_cfg["section_console_log_path"] = acq.CONFIG.get("section_console_log_path")
        task_queue, viewer_queue, worker_p, viewer_p, run_selected_beams = start_live_qc_runtime(
            layout, results_dir, run_cfg
        )

        # --- Graceful Ctrl+C handling ---------------------------------------
        # Without this, Ctrl+C during a long `conn.execute("CMD_WAIT_...")` C
        # call is invisible to Python (native call holds GIL), the loop never
        # checks, and on Windows/VSCode the integrated terminal often eats the
        # signal entirely. We install a flag-based handler:
        #   1st Ctrl+C → set flag, print msg, let the in-flight tile finish
        #   2nd Ctrl+C → restore default handler → KeyboardInterrupt unwinds
        # The loop body checks the flag at each iteration's top and breaks
        # cleanly; the existing finally block tears down worker/viewer.
        abort_state = {"requested": False}
        prev_sigint_handler = None

        def _sigint_handler(signum, frame):
            if abort_state["requested"]:
                print("\n[LiveQC] Second Ctrl+C — restoring default handler, will hard-exit.",
                      flush=True)
                try:
                    signal.signal(signal.SIGINT, signal.SIG_DFL)
                except Exception:
                    pass
                raise KeyboardInterrupt
            abort_state["requested"] = True
            print("\n[LiveQC] Ctrl+C received — finishing in-flight tile then exiting "
                  "cleanly. Press Ctrl+C again to force.", flush=True)

        try:
            prev_sigint_handler = signal.signal(signal.SIGINT, _sigint_handler)
        except (ValueError, OSError):
            # signal.signal can fail outside the main thread
            prev_sigint_handler = None

        pending_tile_meta: Optional[Dict[str, Any]] = None

        try:
            rc_off = conn.execute("CMD_SCAN_OFF")
            print(f"[Scan] CMD_SCAN_OFF rc={rc_off}")

            acq.safe_set_double(conn, "AP_SCAN_TIMEOUT", 60000)

            scan_set = acq.apply_acq_scan_settings(conn)
            print(
                f"[AcqSettings] scanrate ok={scan_set['scanrate']['ok']} key={scan_set['scanrate']['key']} "
                f"val={acq.CONFIG['acq_scan_speed']}"
            )
            print(
                f"[AcqSettings] pixelsize ok={scan_set['pixelsize']['ok']} key={scan_set['pixelsize']['key']} "
                f"val={acq.CONFIG['acq_pixel_size_nm']}nm"
            )
            print(
                f"[AcqSettings] prepared readback: "
                f"W={acq.safe_get_double(conn,'DP_PREPARED_WIDTH')} "
                f"H={acq.safe_get_double(conn,'DP_PREPARED_HEIGHT')} "
                f"SR={acq.safe_get_double(conn,'DP_PREPARED_SCANRATE')}"
            )

            col_refs: List[float] = []
            horizontal = str(acq.CONFIG["travel_direction"]).upper().startswith("HORIZONTAL")
            tol = float(acq.CONFIG["line_group_tol_um"] if horizontal else acq.CONFIG.get("vert_group_tol_um", 55.0))
            last_col_id = None

            prev_need_wait2 = False

            first_i0 = route[0]
            first_plan = plans[first_i0]
            t0 = time.perf_counter()
            acq.stage_move_xy_z(conn, first_plan.x, first_plan.y, first_plan.z_pred)
            t_premove = time.perf_counter() - t0
            acq.safe_set_double(conn, "AP_OLENS_CURRENT", float(first_plan.ol_pred))
            acq.safe_set_double(conn, "AP_STIG_X", float(getattr(first_plan, 'stig_x_pred', 0.0)))
            acq.safe_set_double(conn, "AP_STIG_Y", float(getattr(first_plan, 'stig_y_pred', 0.0)))
            print(f"[Pipeline] pre-move to first tile: {t_premove:.3f}s")
            print(f"[LiveQC] selected beams for this run: {run_selected_beams}")

            for order_i, tile_i0 in enumerate(route, start=1):
                if abort_state["requested"]:
                    print(f"[LiveQC] aborting before tile {order_i}/{len(route)} "
                          f"(Ctrl+C requested)", flush=True)
                    break

                t_tile0 = time.perf_counter()

                plan = plans[tile_i0]
                tile_idx = plan.tile_idx
                x, y = plan.x, plan.y

                group_val = y if horizontal else x
                line_id = acq.compute_line_id_for_tile(group_val, col_refs, tol)
                new_line = (last_col_id is None) or (line_id != last_col_id)

                print("\n" + "-" * 78)
                print(f"[AcqTile] order={order_i}/{len(route)} tile_idx={tile_idx} line_id={line_id} new_line={new_line}")
                print(f"          XY=({x:.3f},{y:.3f}) Z_pred={plan.z_pred} OL_pred={plan.ol_pred:.3f} "
                      f"StigX={getattr(plan, 'stig_x_pred', 0.0):.6f} StigY={getattr(plan, 'stig_y_pred', 0.0):.6f}")

                t_flush_prev = 0.0
                if prev_need_wait2:
                    tF0 = time.perf_counter()
                    rc_w2 = conn.execute("CMD_WAIT_ACQUISITION_COMPLETE")
                    t_flush_prev = time.perf_counter() - tF0
                    if rc_w2 != 0:
                        raise RuntimeError(f"[Acq] wait prev acq failed rc={rc_w2} err={acq.safe_get_last_error(conn)}")
                    prev_need_wait2 = False
                    if pending_tile_meta is not None:
                        emit_completed_tile(task_queue, pending_tile_meta)
                        print(
                            f"[LiveQC] queued tile {pending_tile_meta['tile_idx']} "
                            f"folder={pending_tile_meta['tile_folder']}"
                        )
                        pending_tile_meta = None

                t_sanity = 0.0
                do_sanity = False
                n = int(acq.CONFIG.get("sanity_every_n_tiles", 0) or 0)
                if n > 0 and (order_i % n == 0):
                    do_sanity = True
                if acq.CONFIG.get("sanity_on_new_line", False) and new_line:
                    do_sanity = True

                if do_sanity:
                    tS = time.perf_counter()
                    rc_s = conn.execute("CMD_CHECK_SYSTEM_SANITY")
                    t_sanity = time.perf_counter() - tS
                    if rc_s != 0:
                        raise RuntimeError(f"[Sanity] failed rc={rc_s} err={acq.safe_get_last_error(conn)}")

                rb_px = acq.safe_get_double(conn, "AP_PIXELSIZE", default=float("nan"))
                print(f"[PreTrigger] AP_PIXELSIZE readback = {rb_px} nm")

                out = acq.configure_output_for_tile(
                    conn,
                    acq.CONFIG["output_root"],
                    acq.CONFIG["czi_name"],
                    acq.CONFIG["region_idx"],
                    tile_idx,
                    acq.CONFIG["prefix_base"],
                    acq.CONFIG["combo_format"],
                    block_folder=block_folder,
                    region_label=acq.CONFIG.get("region_name"),
                )
                print(f"[Output] tile_folder={out['tile_folder']} prefix={out['prefix_value']}")

                current_tile_meta = {
                    "tile_idx": int(tile_idx),
                    "x_um": float(x),
                    "y_um": float(y),
                    "tile_folder": out["tile_folder"],
                    "prefix": out["prefix_value"],
                }

                t_trig0 = time.perf_counter()
                rc_t = conn.execute("CMD_TRIGGER_ACQUISITION")
                if rc_t != 0:
                    raise RuntimeError(f"[Acq] trigger failed rc={rc_t} err={acq.safe_get_last_error(conn)}")
                t_trig = time.perf_counter() - t_trig0

                t_scan = 0.0
                if acq.CONFIG.get("use_wait_scan_complete", True):
                    t_scan0 = time.perf_counter()
                    rc_w1 = conn.execute("CMD_WAIT_SCAN_COMPLETE")
                    t_scan = time.perf_counter() - t_scan0
                    if rc_w1 != 0:
                        raise RuntimeError(f"[Acq] wait scan failed rc={rc_w1} err={acq.safe_get_last_error(conn)}")

                t_move_next = 0.0
                if order_i < len(route):
                    next_plan = plans[route[order_i]]
                    tM0 = time.perf_counter()
                    acq.stage_move_xy_z(conn, next_plan.x, next_plan.y, next_plan.z_pred)
                    t_move_next = time.perf_counter() - tM0
                    acq.safe_set_double(conn, "AP_OLENS_CURRENT", float(next_plan.ol_pred))
                    acq.safe_set_double(conn, "AP_STIG_X", float(getattr(next_plan, 'stig_x_pred', 0.0)))
                    acq.safe_set_double(conn, "AP_STIG_Y", float(getattr(next_plan, 'stig_y_pred', 0.0)))

                t_finish_now = 0.0
                defer = bool(acq.CONFIG.get("defer_finish_write", False))
                use_wait2 = bool(acq.CONFIG.get("use_wait_acq_complete", True))

                if defer:
                    prev_need_wait2 = True
                    pending_tile_meta = current_tile_meta

                    force_n = int(acq.CONFIG.get("force_finish_every_n", 0) or 0)
                    if force_n > 0 and (order_i % force_n == 0):
                        tF0 = time.perf_counter()
                        rc_w2 = conn.execute("CMD_WAIT_ACQUISITION_COMPLETE")
                        t_finish_now = time.perf_counter() - tF0
                        if rc_w2 != 0:
                            raise RuntimeError(f"[Acq] wait acq failed rc={rc_w2} err={acq.safe_get_last_error(conn)}")
                        prev_need_wait2 = False
                        if pending_tile_meta is not None:
                            emit_completed_tile(task_queue, pending_tile_meta)
                            print(
                                f"[LiveQC] queued tile {pending_tile_meta['tile_idx']} "
                                f"folder={pending_tile_meta['tile_folder']}"
                            )
                            pending_tile_meta = None
                else:
                    if use_wait2:
                        tF0 = time.perf_counter()
                        rc_w2 = conn.execute("CMD_WAIT_ACQUISITION_COMPLETE")
                        t_finish_now = time.perf_counter() - tF0
                        if rc_w2 != 0:
                            raise RuntimeError(f"[Acq] wait acq failed rc={rc_w2} err={acq.safe_get_last_error(conn)}")
                        emit_completed_tile(task_queue, current_tile_meta)
                        print(f"[LiveQC] queued tile {current_tile_meta['tile_idx']} folder={current_tile_meta['tile_folder']}")

                tiny_ms = int(acq.CONFIG.get("pipeline_tiny_delay_ms", 0) or 0)
                if tiny_ms > 0:
                    time.sleep(tiny_ms / 1000.0)

                t_tile_wall = time.perf_counter() - t_tile0
                scan_chain = t_trig + t_scan
                acq_wall = scan_chain + t_flush_prev
                move_wall = t_move_next
                total_wall = acq_wall + move_wall

                if order_i > warmup_tiles:
                    move_times.append(move_wall)
                    acq_times.append(acq_wall)
                    tile_times.append(total_wall)

                print(
                    f"[TileWall] acq_wall={acq_wall:.3f}s (scan={scan_chain:.3f}s + flush_prev={t_flush_prev:.3f}s) "
                    f"move_next={move_wall:.3f}s total(move+acq)={total_wall:.3f}s  "
                    f"(measured_loop_wall={t_tile_wall:.3f}s, sanity={t_sanity:.3f}s, finish_now={t_finish_now:.3f}s)"
                )

                if max_tiles and order_i >= max_tiles:
                    print(f"[Timing] Reached max_tiles={max_tiles}, stopping early.")
                    break

                last_col_id = line_id

            if prev_need_wait2:
                print("[Pipeline] final flush last tile...")
                rc_w2 = conn.execute("CMD_WAIT_ACQUISITION_COMPLETE")
                if rc_w2 != 0:
                    raise RuntimeError(f"[Acq] final wait acq failed rc={rc_w2} err={acq.safe_get_last_error(conn)}")
                prev_need_wait2 = False
                if pending_tile_meta is not None:
                    emit_completed_tile(task_queue, pending_tile_meta)
                    print(f"[LiveQC] queued tile {pending_tile_meta['tile_idx']} folder={pending_tile_meta['tile_folder']}")
                    pending_tile_meta = None

            def _summ(xs: List[float]) -> str:
                if not xs:
                    return "n/a"
                return f"n={len(xs)} mean={mean(xs):.3f}s med={median(xs):.3f}s min={min(xs):.3f}s max={max(xs):.3f}s"

            print("\n" + "=" * 78)
            print("[Timing] SUMMARY (excluding focus)")
            print("  move :", _summ(move_times))
            print("  acq  :", _summ(acq_times))
            print("  total:", _summ(tile_times))
            print("=" * 78 + "\n")
            print(f"[LiveQC] QC outputs will be written to: {results_dir}")
        finally:
            stop_live_qc_runtime(task_queue, worker_p, viewer_p)
            if prev_sigint_handler is not None:
                try:
                    signal.signal(signal.SIGINT, prev_sigint_handler)
                except (ValueError, OSError):
                    pass

    return acquire_tiles


def main() -> None:
    acquire_script_path = LIVE_QC_CONFIG["acquire_script_path"]
    acq = load_module_from_path(acquire_script_path)
    acq.acquire_tiles = make_live_qc_acquire_tiles(acq, LIVE_QC_CONFIG)
    acq.main()


if __name__ == "__main__":
    mp.freeze_support()
    main()
