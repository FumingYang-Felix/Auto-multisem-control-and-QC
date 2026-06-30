# MultiSEM Acquisition + Live QC Control

Hardware control + parallel **Live QC** for Zeiss multi-SEM (mSEM) acquisition, with a
PySide6 desktop GUI. Drives focus-map / autofocus / autostig per support-point, tiles
each region, acquires through the Zeiss COM API, and scores image quality in real time.

> **Operators:** start with **[OPERATOR_GUIDE.md](OPERATOR_GUIDE.md)** — an illustrated,
> no-coding walkthrough (sync → launch → connect → SFN → HI-MC → Start).

---

## Quick start (Windows control PC)

```powershell
# one-time clone
cd D:\mSEMAPI
git clone https://github.com/FumingYang-Felix/Auto-multisem-control-and-QC.git himc_app

# each session: update + launch (uses the 3.11 venv)
cd D:\mSEMAPI\himc_app
git pull
D:\mSEMAPI\mSEMAPI\venv\Scripts\Activate.ps1
python -m msem_control.main
```

> **Environment:** the control PC is Windows 10 / Server 2016 (build 14393), which can
> only run **Qt 6.4.x**, so it needs **Python 3.11 + PySide6 6.4.3** + the VC++ x64
> redistributable + `opencv-python` (for SFN). See `setup_env.py` and OPERATOR_GUIDE.md.

---

## The GUI (`python -m msem_control.main`)

Four tabs:

| Tab | What it does |
|---|---|
| **Control & Map** | Manual stage / AF / AS control + status. |
| **Acquisition** | Full workflow with the interactive tile map. |
| **HI-MC** | Lightweight form-driven launcher: pick the imaging JSON + raw focus-map folder + output, tune parameters, **Start**. Runs the real pipeline and streams Live QC. |
| **SFN** | Shuttle-and-Find: re-find the 3 L-markers on the scope → compute the affine → re-register every region of the imaging JSON. |

## Pipeline (runs as a subprocess, also usable from a terminal)

- **`focus_map.py`** — acquisition driver. Reads the imaging JSON, fits the OL/stig plane
  from the previous `slice_<N>_FocusMap.txt`, runs AF / minimize-OL / AS per support point,
  tiles each region, drives the scope. Owns `CONFIG`.
- **`main_live_qc.py`** — wraps `focus_map`'s tile loop to run the Live QC worker + viewer
  in parallel. Owns `LIVE_QC_CONFIG`.
- **`live_qc_support.py`** / **`qc_algorithm.py`** — QC worker + dashboard, and the
  per-image scoring (focus, stig anisotropy, jitter, saturation, blank detection).
- **`qc_overview.py`** — downsampled per-MFOV + per-region overview images.
- **`auto_stage_calib.py`** / **`apply_affine_to_section_json.py`** — SFN: live L-marker
  calibration → affine, then apply it to the imaging JSON.

## Output layout

```
<output_root>\w13_h25_Slice1_8NM_<timestamp>\S25R1\mfov42\mfov42_<timestamp>__001.bmp
                └ block (from JSON name+pixel+time)  └ region  └ MFOV  └ beam image
            \S25R1\LiveQC\  live_qc_dashboard.(png|html), live_qc_results.csv,
                           live_qc_region_overview.png
```

---

## Developers

Architecture + integration details: **[INTEGRATION_NOTES.md](INTEGRATION_NOTES.md)**.
The GUI (`msem_control/`) launches the pipeline as a subprocess and bridges UI → config
via the `MSEM_CONFIG_JSON` env var (`engine/pipeline_runner.py`). The `msem/` package is
the comtypes COM wrapper. Run `python tests/test_config_defaults.py` for the config guard.
