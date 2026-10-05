Last updated: 2026-08-30

This document describes the code as of the date above. It is a living document — revise it whenever the modules change materially, rather than treating it as a one-time snapshot. (If you're reading this much later, check the git log for the files it references before trusting the details.)

# himc_app — Module Overview

## What this application is

`himc_app` is the control software for a Zeiss MultiSEM 91-beam electron microscope. It is a PySide6 desktop GUI (`msem_control/`) that wraps the Zeiss MultiSEM COM API (the `msem/` package: `MSEMConnection`, `Stage`, `Acquisition`, `Autofocus`, `Autostig`, etc.) and drives a set of standalone acquisition/calibration scripts that live at the repo root (`focus_map.py`, `main_live_qc.py`, `auto_stage_calib.py`, `stage_init.py`, and friends).

The GUI is launched via `msem_control/main.py`, which creates a single `WorkflowEngine` (`msem_control/engine/workflow.py`) and a `MainWindow` (`msem_control/ui/main_window.py`). `MainWindow` presents three tabs, each a `QWidget` page:

- **Acquisition** (`msem_control/ui/pages/acquisition_page.py`) — the original, full-featured page: manual stage control, an interactive drag-and-drop tile map for drawing ROIs by hand, and a "Start Acquisition" button that can either use a loaded Zeiss imaging JSON or generate one itself from hand-drawn regions ("standalone mode").
- **HI-MC** (`msem_control/ui/pages/himc_page.py`) — a lightweight, form-driven launcher for the same underlying acquisition + Live-QC pipeline. This is the page used for day-to-day production runs.
- **Shuttle & Find / SFN** (`msem_control/ui/pages/sfn_page.py`) — stage re-registration: finds physical L-shaped markers on a wafer, computes an affine transform between the old and new stage coordinate systems, and applies it to an imaging JSON so a previously-planned wafer map can be re-used after the wafer is reloaded.

All three pages share one `WorkflowEngine` instance (passed into each page's constructor) and are all children of the same `MainWindow`. Under the hood, all three ultimately drive the microscope not by calling `msem.*` APIs directly from the GUI process, but by launching the same pipeline scripts as **subprocesses** and tailing their output — this is discussed in detail in "Interactions between modules" below.

---

## Acquisition page

**File:** `msem_control/ui/pages/acquisition_page.py` (the largest UI file in the app, ~4,800 lines)
**Engine code it drives:** `msem_control/engine/pipeline_runner.py` (`PipelineRunner`, `build_run_config`), plus direct engine calls (`msem_control/engine/workflow.py`) for manual stage/AF/AS control.

### What problem it solves

This is "the whole workflow" page — the one place where an operator can work purely from an optical image of a wafer (no Zeiss ZEN project required) and hand-draw the regions to image. It's also where manual, ad-hoc control of the stage/autofocus/autostig lives (left over from before HI-MC existed as a separate, streamlined page).

### Workflow, conceptually

1. **Image Setup** — load an optical overview image (CZI or a plain image file) into the large center map widget (`TileMapView`, an `InteractiveMap` subclass). The image is centered at scene origin (0,0) and calibrated to real stage micrometers, either from CZI metadata or a manual pixel-size override.
2. **Draw or load ROIs** — either draw polygon regions directly on the map (click-to-add-point, click-near-start-to-close), or load them from a Zeiss imaging JSON (via the shared Parameters panel's "Load Config", or a sections/ROI CSV). Each ROI becomes a "region"; the map can also overlay reference-only section outlines and per-region autofocus "support points."
3. **Set scan/output parameters** — pixel size, scan rate, tile size, MFOV/SFOV overlap, travel pattern/direction/start position, output folder naming — via the left settings panel.
4. **Start Acquisition** — this has two modes:
   - **Normal mode**: requires a loaded imaging JSON (the pipeline reads its regions/contours/support points from that file, not from the hand-drawn ROIs).
   - **Standalone mode** (`chk_standalone_mode`): generates its own imaging-JSON-equivalent config directly from the page's own drawn regions and support points, so an operator can run a full acquisition without ever touching ZEN.
5. Acquisition itself is **not** run in-process — `_run_acquisition_workflow` builds a config dict and launches the same `main_live_qc.py` / `focus_map.py` pipeline as a subprocess (see below), then mirrors its progress into the log panel, progress bar, and the map's MFOV hexagon coloring (blue = pending, green = acquired).
6. Manual controls (move stage, single acquire, run autofocus/autostig, "beams to fibers") are also available directly on this page for ad-hoc hardware interaction outside of a full run — these go straight through `WorkflowEngine`, not through the subprocess.

### Note

Despite the "Acquisition" vs. "HI-MC" naming, both pages now drive the exact same subprocess pipeline for a real run (`PipelineRunner` + `main_live_qc.py`/`focus_map.py`). The real distinction is upstream: Acquisition is the manual/interactive, map-based, ZEN-optional front end; HI-MC is the fast, form-based front end for a ZEN-JSON-driven production run with the full set of focus/QC tuning knobs exposed.

---

## HI-MC page

**File:** `msem_control/ui/pages/himc_page.py` (~1,740 lines)
**Engine code it drives:** `msem_control/engine/pipeline_runner.py` (same `PipelineRunner`/`build_run_config` as Acquisition), plus `msem_control/engine/screenshot_fiducial.py` and `msem_control/engine/focus_map_collector.py` for its two auxiliary tools, and `alerts.py` for failure notifications.

### What problem it solves

This is the day-to-day production page. Before it existed, running a real acquisition meant hand-editing `focus_map.CONFIG` in a text editor and running `python main_live_qc.py` from a terminal. HI-MC surfaces every knob an operator actually tunes as a proper form, keeps no heavy map widget (so it stays fast even for very large wafers), and drives the identical subprocess — "no microscope code runs in the GUI process" for this page either.

### Workflow, conceptually

1. **Input/Output**: point at the Zeiss imaging JSON (regions + support points — everything downstream comes from this one file), a "raw focus-map dir" (per-section `FocusMap.txt` files from a prior run, matched to each region by section number), and an output root folder.
2. **Region selection**: process every region in the JSON (optionally narrowed by a substring filter) or just one named region; optionally skip regions already successfully imaged in a prior batch under the same output root, or force a retake list.
3. **Scan settings / Route**: pixel size, scan rate, tile dimensions, MFOV/SFOV overlap, and the tile travel pattern/direction/start position — mirrors the Acquisition page's scan controls but as a flatter form.
4. **Focus Method** ("Plane update method"): chooses how each region's focus/stig plane is built each run. The dropdown shows a friendly display name; internally it still writes the original `focus_map.CONFIG` string (translated via `_PLANE_METHOD_DISPLAY_TO_INTERNAL`/`_INTERNAL_TO_DISPLAY` in `himc_page.py`, the same pattern `pipeline_runner.py` already used for travel-pattern/direction/start-position), so saved configs, logs, and `focus_map.py` itself are unaffected by the rename. Five methods are shown:
   - `ALL_POINTS_PLANE` (internal: `CURRENT_ALL_POINTS_PLANE`, default) — live-measure all of a region's own support points and fit a fresh plane.
   - `ONE_REGION_FSP` (internal: `ONE_POINT_OFFSET`) — remeasure a few of the best prior points and apply a robust offset; the only mode that works with just 1–2 points per region.
   - `WAFER_FIDUCIAL_DIRECT` — skips AF/AS measurement entirely; every mFOV's OL/stig comes straight from evaluating the wafer-fiducial plane at that mFOV's location.
   - `SAMPLE_TOPOGRAPHY_CORRECTION` (EXPERIMENTAL, new) — zero AF/AS/measurement; every mFOV's OL, StigX, StigY, *and* predicted ProjX/ProjY come from planes fit to the wafer-fiducial file (built from ZEN screenshots — see point 6). One Beam-to-Fiber still runs at region start, since B2F is what actually establishes the real projective alignment in ZEN; the plane-predicted ProjX/ProjY are recorded alongside the real B2F reading for comparison, never sent to hardware.
   - `FOCUSMAP_TOPOGRAPHY` (EXPERIMENTAL, new) — zero AF/AS; OL/StigX/StigY come from a plane fit to the region's *own* `FocusMap.txt` instead of the wafer-fiducial file (this per-region plane fit was already being computed for every method, so this mode just uses it directly instead of refining it with a live measurement). `FocusMap.txt` has no Z column, so Z still comes from the wafer-fiducial file's Z-plane. B2F runs once per region; its real ProjX/ProjY reading is logged, but predicting it from a plane fit across already-imaged regions ("assume all regions lie on one tilted plane") is a known follow-up, not yet implemented — the bootstrapping details (how many regions before that plane is fittable, this-run-only vs. prior-runs' data too) are still being worked out.

   `THREE_POINT_DELTA`, `CURRENT_ALL_POINTS_QUADRATIC_RESIDUAL_IDW`, and `MANUAL_POINTS` are hidden from this dropdown until further notice — `focus_map.py` still fully supports them, selectable via the Advanced overrides box (point 9).
5. **Wafer fiducial mechanism**: a "Wafer fiducial OL file" (x, y, z, ol[, stigx, stigy, projx, projy] points, in `focus_map.py`'s `_parse_wafer_fiducial_points`/`_get_wafer_fiducial_planes`) gives a focus/stig/projective estimate at *any* (x, y) on the wafer, not just previously-measured points — needed to anchor `ONE_REGION_FSP` or any region with fewer than 3 local points, and is what `WAFER_FIDUCIAL_DIRECT` and `SAMPLE_TOPOGRAPHY_CORRECTION` rely on entirely (the latter also needs this file's ProjX/ProjY planes). It also feeds a one-time-per-region coarse Z sanity check (catching the stage sitting at the wrong absolute height, e.g. left at Z=0 after a fresh connection) and, via that same Z-plane, `FOCUSMAP_TOPOGRAPHY`'s Z source. This file is normally auto-discovered from the imaging JSON's `w<N>h<N>` name, or can be built directly inside the page from screenshots (next point).
6. **Screenshot-to-CSV fiducial builder**: an operator can add one or more screenshots of ZEN's "Verify Positions" table, tell the page which columns are visible left-to-right, and click "Process Screenshots → CSV" — this OCRs the screenshots (`msem_control/engine/screenshot_fiducial.py`: `parse_screenshot_to_rows`/`merge_rows`/`write_fiducial_csv`), logs every parsed row and warning for manual cross-checking (OCR is explicitly not trusted blindly — a plausible-range check also catches the single worst failure mode found in practice, a dropped decimal point that still parses as a valid-looking number), and writes/auto-fills the wafer fiducial file above.
7. **Collect Focus Maps**: pulls every region's `FocusMap.txt` out of a *completed* run's per-region subfolders into one flat `S<N>_FocusMap.txt` folder (`msem_control/engine/focus_map_collector.py`), which can then be pointed at directly as the "Raw focus-map dir" for a follow-up run (e.g. re-imaging the next tissue section using the previous section's converged focus as a starting point, or as the direct data source for `FOCUSMAP_TOPOGRAPHY` above).
8. **Live QC settings**: thresholds and beam-sampling strategy for the real-time quality-control dashboard that runs alongside acquisition (low/high quality thresholds, beam selection mode, single-beam-outlier detection, jitter/saturation thresholds).
9. **Advanced overrides**: two free-form "key = value" text boxes ("Acquisition pipeline settings"/"Live QC settings") for any `focus_map.CONFIG` / `LIVE_QC_CONFIG` key not otherwise exposed as a widget — including the three plane-update methods hidden from the dropdown above. The group's checkbox actually gates this: unchecked, neither box's contents are applied (checked automatically on restore if a saved config had a leftover override, so it never silently stops applying just because the page was reopened).
10. **Start / Stop / region progress**: Start launches the pipeline subprocess exactly as Acquisition's standalone flow does; a "Region imaging order & progress" list shows the actual order regions will be imaged in (computed by the pipeline itself, which may differ from the JSON's listed order) and live per-region status (pending/running/done/failed/skipped), restored from the last run's `region_manifest.json` even before Start is clicked again (so a relaunch after a crash shows "where did we leave off").
11. **Safety nets running in the background** (no dedicated settings panel — deliberately fixed thresholds): a stage-Z safety alert (banner + log entry if the live stage Z readback drops below a threshold, catching drift toward a parked/low Z between moves that never explicitly command a Z target), and email/SMS failure notifications via `alerts.py` (sent when multiple regions fail in one run, or the pipeline exits unexpectedly) to recipients listed in a plain-text file (`himc_notify_emails.txt`) the page seeds on first run.
12. The page also restores its own settings from `msem_run_config.json` (the same file `PipelineRunner` writes right before launching a run) on load, so relaunching after a crash requires minimal re-entry.

---

## Shuttle & Find (SFN) page

**File:** `msem_control/ui/pages/sfn_page.py` (~950 lines, see its module docstring for the two-stage design)
**Engine code it drives:** `PipelineRunner` (same class again) launching `stage_init.py`, `auto_stage_calib.py`, and `apply_affine_to_section_json.py` as subprocesses.

### What problem it solves

When a wafer is removed and reloaded (or moved to a different holder), the microscope's stage coordinate system no longer lines up with the coordinates recorded in a previously-planned imaging JSON. SFN re-establishes that correspondence by finding three physical L-shaped markers on the wafer, computing the affine transform between their expected and actual positions, and reapplying that transform to every region in the old imaging JSON — producing a new JSON that HI-MC can image directly.

### Workflow, conceptually

1. **Stage 0 — Calibrate Stage** (optional, `stage_init.py`): re-homes the stage (drives to the lower limits, re-calibrates its coordinate system, returns to (0,0,0)). Run once if the stage's own coordinate system needs re-initializing; the mSEM service log is tailed live for stage/calibration errors during this step.
2. **Stage A — Calibration** (`auto_stage_calib.py`, launched via `PipelineRunner` with an argv list rather than the JSON-config mechanism, since it's argparse-driven): given a `markers_prev.json` describing the 3 L-markers' expected positions, drives the stage to each in turn (order M3→M2→M1), runs beams-to-fibers, autofocuses, stitches a downsampled mosaic per marker, detects the marker via contour thresholding, and computes the offset. From the three markers' expected vs. detected positions it fits an affine transform (`new = A·old + t`) and writes it to an `affine_*.json` (`{a11,a12,a21,a22,tx,ty}`).
3. **Stage B — Apply** (`apply_affine_to_section_json.py`, run synchronously since it's pure file I/O with no hardware): applies that affine to every region of the old imaging JSON, writing a new, re-registered JSON.
4. **Send new JSON → HI-MC**: a single button pushes the newly-written JSON straight into the HI-MC page's "Imaging JSON" field (see `set_himc_page`/`set_imaging_json_path` below), so the operator can go directly from "wafer just re-registered" to "start imaging" without manually locating the output file.
5. **"Start New Wafer Procedure"** ties this all together as a one-click sequence, auto-triggered by detecting a new `wafer_transfer_*.txt` file in a watched folder (polled every 15 minutes): turn off Simulation Mode, connect to the scope, ensure the beam is on (escalating to an automated arc-recovery sequence if a plain `CMD_BEAM_ON` doesn't take), look up and load the correct holder's `markers_prev.json` (by parsing the wafer holder number out of the transfer file and finding `h<N>_markers_prev.json` under a holder-JSON directory), run stage-init, and — on success — automatically chain into Stage A calibration. Any failure along the way pops an alert dialog rather than silently stopping partway.
6. A prominent simulation-mode banner blocks Beam ON / Calibrate Stage / Run Calibration whenever Simulation Mode is checked, since (unlike HI-MC) this page's actions are explicitly gated on that flag rather than always assuming real hardware.

---

## Interactions between modules

- **Shared `WorkflowEngine`**: `MainWindow` constructs one `WorkflowEngine` and passes it into all three pages. It owns the actual MSEM connection, stage position polling, and manual stage/AF/AS operations, and broadcasts connection/position/status changes that the shared Status panel displays.
- **Shared subprocess mechanism, not shared engine calls, for real runs**: Acquisition, HI-MC, and SFN all launch their respective real work (`main_live_qc.py`/`focus_map.py`, `auto_stage_calib.py`, `stage_init.py`, `apply_affine_to_section_json.py`) as child processes through the same `PipelineRunner` class (`msem_control/engine/pipeline_runner.py`), rather than calling `msem.*` from inside the GUI process. This exists because the Zeiss API doesn't tolerate two software components driving the hardware at once, and because the pipeline scripts are standalone and build their own connections/subprocesses (LiveQC's own matplotlib child) that would fight the GUI's Qt event loop if imported directly.
- **Hardware ownership handoff**: before launching any of those subprocesses, the launching page calls `engine.pause_hardware()`, which stops the engine's background poll loop and makes it refuse manual stage/AF/AS/acquire calls; the page calls `engine.resume_hardware()` (in both its success and its error-handling paths) once the subprocess finishes. `engine.is_paused()` is checked by the *other* pages before letting the operator start a competing run, so e.g. HI-MC and SFN can't both try to own the scope at once.
- **Config JSON hand-off, mechanically identical across pages**: Acquisition's `_run_acquisition_workflow` and HI-MC's `_on_start` both build a UI dict and pass it through the exact same `pipeline_runner.build_run_config()` / `config_to_ui()` pair, then write it to the same `msem_run_config.json` at the repo root before launching `main_live_qc.py`. This means either page can pick up and "resume" a run's settings the other page (or the terminal, running the script directly) last wrote.
- **SFN → HI-MC**: SFN's "Send new JSON → HI-MC" button (`_on_send_himc`) calls `self._himc_page.set_imaging_json_path(...)` directly — `MainWindow` wires this up at construction time via `self.page_sfn.set_himc_page(self.page_himc)`. This is the one direct page-to-page object reference in the app (every other cross-page interaction goes through the shared engine, a shared panel, or a shared file on disk).
- **Shared imaging JSON auto-fill via the Parameters panel**: the (hidden-by-default) Parameters dock's "Load JSON" emits `imaging_json_selected(path)`, which `MainWindow` connects to `set_imaging_json_path` on **all three** pages simultaneously (Acquisition, HI-MC, and SFN's "old imaging JSON" field). So loading a JSON once from that panel can seed all three pages, though each page's own imaging-JSON field can also be Browse'd independently and takes precedence from then on.
- **Shared Status panel**: `MainWindow` builds one `StatusPanel` (left dock) and wires the SFN page to it directly (`self.page_sfn.set_status_panel(self.status_panel)`, used e.g. to uncheck Simulation Mode programmatically during "Start New Wafer Procedure"). All three pages' pipeline runners also feed a shared "Beam Arc" indicator on this panel (`status_panel.set_beam_arc`) whenever the subprocess reports an arc-detection/recovery event.
- **The Parameters panel is a legacy, mostly Acquisition-only sibling, not a hub**: despite living in a dock alongside everything else, every field in the Parameters panel (Beam/Stage/Scan/ROI/Output/Autofocus/Autostig, "Update All Parameters") only feeds the *Acquisition* page (`update_from_params_panel`/`load_regions_from_json`) — it has no effect on HI-MC or SFN beyond the imaging-JSON auto-fill above, and its own "Save JSON" toolbar button isn't wired to anything. It is hidden by default (toggle via View → Parameters panel) precisely because it's not part of the current HI-MC-centric workflow; don't read its docked presence as evidence of deeper integration than this.
- **`alerts.py` is shared infrastructure, not a per-page feature**: both HI-MC's failure notifications and SFN's/`auto_stage_calib.py`'s calibration-failure alerts go through the same `alerts.send_alert()`, configured by one shared `alert_config.json` (SMTP host/credentials/enabled flag) at the repo root — one on/off switch and one mail relay for the whole app, even though each caller can supply its own recipient list.
- **Focus-map continuity across runs, not across pages**: HI-MC's "Collect Focus Maps" tool and the "Raw focus-map dir"/wafer-fiducial mechanisms exist to carry per-section focus results *forward from one completed run into the next* (e.g. section N's converged focus seeding section N+1, or one wafer's fiducial plane seeding the next). This is a data hand-off through files on disk (`FocusMap.txt`, the wafer-fiducial CSV/txt), not a live in-app connection between pages.
