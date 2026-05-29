# MultiSEM GUI ↔ acquisition pipeline integration

This branch revives the PySide6 control GUI (originally the first commit of
`Auto-multisem-control-and-QC`) and wires it to the **current** acquisition
pipeline (`focus_map.py` / `main_live_qc.py` / `live_qc_support.py` /
`qc_algorithm.py`) instead of the old, broken inline `msem.*` acquisition loop.

## Architecture

```
  msem_control/  (PySide6 GUI)
    main.py                     entry: WorkflowEngine(sim) + MainWindow
    engine/
      workflow.py               status polling + manual stage/AF/AS control
                                (pauses while a pipeline run owns the scope)
      pipeline_runner.py        NEW: QProcess driver — writes config JSON,
                                spawns `python main_live_qc.py`, tails stdout,
                                parses [Region]/[AcqTile]/[LiveQC] -> Qt signals
    ui/pages/acquisition_page.py  Start -> _run_acquisition_workflow ->
                                  build config from UI -> PipelineRunner
    ...
  focus_map.py / main_live_qc.py / live_qc_support.py / qc_algorithm.py
                                the real pipeline (run as a SUBPROCESS)
  msem/                         comtypes COM wrapper (MSEMConnection, Stage, ...)
```

### How acquisition runs now
1. User loads a Zeiss imaging JSON (Parameters panel → Load Config). The GUI
   parses regions/contours/support points **and remembers the file path**.
2. User sets scan/output params and clicks **Start Acquisition**.
3. `AcquisitionPage._run_acquisition_workflow` maps the UI → a config dict
   (`pipeline_runner.build_run_config`), writes it to `msem_run_config.json`,
   and launches `python main_live_qc.py` with `MSEM_CONFIG_JSON` pointing at it.
4. `focus_map.py` / `main_live_qc.py` read `MSEM_CONFIG_JSON` and override their
   `CONFIG` / `LIVE_QC_CONFIG` (no env var ⇒ hardcoded defaults, so terminal
   `python main_live_qc.py` is unchanged).
5. The GUI tails the subprocess stdout into the Log panel and drives the
   progress bar + map MFOV hexagons from the `[Region]`/`[AcqTile]` lines.
6. **Stop** sends SIGINT (graceful) so the in-flight tile flushes and the
   LiveQC subprocesses shut down. The GUI re-enables the engine when the
   subprocess exits.

### Hardware coexistence
While the subprocess owns the scope, `WorkflowEngine.pause_hardware()` stops
the GUI's status poll loop and blocks manual stage/AF/AS ops (Zeiss warns that
two software components must not drive the hardware at once). It resumes on the
runner's `finished` signal.

## What was removed
- **Single-live / full-live** (engine methods + ControlPage UI + `msem/live_scan.py`)
  — never worked through the API; live image only renders in ZEN.
- `SettingsPage` (never instantiated), `core/microscope.py`, `data/processor.py`
  (dead code, never imported).
- `TravelPattern.SPIRAL` combo option (no backend enum → crashed on select).

## Config JSON shape (`msem_run_config.json`, written by the GUI)
```json
{
  "focus_map": { "json_file": "...", "output_root": "...", "czi_name": "...",
                 "acq_pixel_size_nm": 8.0, "acq_scan_speed": 4,
                 "roi_hex_overlap": 0.05, "route_mode": "SERPENTINE", ... },
  "live_qc":   { "acquire_script_path": "<co-located focus_map.py>",
                 "low_quality_threshold": 10.0, ... }
}
```

## Prev focus-map: point at a folder
Instead of the hardcoded `focus_map.CONFIG['prev_focusmap_by_region']` dict, you
can point at a **folder** of per-section `*.txt` focus maps and let the pipeline
match each section by number (S25R1 → a file whose name contains `s25` as a
token, e.g. `w13_s25.txt`):
- In the GUI: Acquisition page → **Prev Focus-map Dir** (folder picker).
- Or in config: `focus_map.CONFIG['prev_focusmap_dir']` / the run-config JSON.
Resolution order: explicit `prev_focusmap_by_region` dict → `prev_focusmap_dir`
folder → legacy `prev_focusmap_path`. (`resolve_prev_focusmap_path()` in
focus_map.py.)

## One-click environment setup
```
python setup_env.py            # install deps into the current Python
python setup_env.py --venv     # ...or into an isolated ./.venv
python setup_env.py --dry-run  # show what it would install
```
Windows: double-click `setup_env.bat` (it also installs comtypes and prints the
COM-registration checklist). macOS/Linux: `./setup_env.sh`. Deps:
`requirements.txt` (PySide6, numpy, Pillow, matplotlib) + `requirements-windows.txt`
(comtypes). On the control PC, run the installer with the SAME Python that
already runs focus_map.py so the GUI shares its Zeiss-COM environment.

## Running

### Mac (development, simulation only)
```
cd msem_gui
python -m msem_control.main      # WorkflowEngine(simulation_mode=True)
```
The GUI runs; clicking Start will launch the subprocess but it fails at
`import comtypes` (COM is Windows-only) — expected. Everything UI-side works.

### Windows (real hardware)
1. Deploy the whole tree (GUI + the 4 pipeline scripts + `msem/`) to the
   control PC (e.g. `D:\mSEMAPI\mSEMAPI\`). `comtypes` + the Zeiss
   `ZeissmSEMWrapper.Commands` COM object must be registered.
2. In `msem_control/main.py`, set `WorkflowEngine(simulation_mode=False)`.
3. `python -m msem_control.main`
4. The subprocess uses the **co-located** `focus_map.py` (the GUI passes
   `acquire_script_path` = `<repo_root>/focus_map.py`, overriding the old
   hardcoded `D:\` default).

## Known limitations / TODO
- MFOV-hexagon progress recolour is best-effort: the subprocess visits regions
  in `SHORTEST_PATH` order, which may differ from the GUI's region indices.
- The GUI has no QC-threshold widgets yet; `live_qc` overrides use defaults.
  (Easy to add later — `build_run_config` already accepts `qc_*` keys.)
- `_calculate_tiles_for_region` (confirmation-dialog tile estimate) still uses
  the old `msem.roi_acquisition` math; it's only an estimate, the real tile
  count comes from `focus_map.py`.
