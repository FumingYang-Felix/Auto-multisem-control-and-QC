# standalone_qc

Image-quality QC for MultiSEM acquisitions, run on a **separate computer** from
acquisition. Watches an acquisition output root on a shared drive; once a region
has finished imaging, scores its beam images and writes its own report.

Real-time QC inside the acquisition process made acquisition itself
unacceptably slow. Per the operator (2026-09-05), image-quality scoring now
belongs to a dedicated machine: the acquisition side just writes tiles to disk,
and this package is the consumer.

## Quick start

```powershell
cd D:\mSEMAPI\himc_app
.venv\Scripts\Activate.ps1

# 1. what's out there, and what has already been scored (reads no images)
python -m standalone_qc --output-root Z:\path\to\wafer_root --list

# 2. score every finished region that hasn't been scored yet
python -m standalone_qc --output-root Z:\path\to\wafer_root --workers 24

# 3. one region, re-scoring even if already done, plus the stitched overview
python -m standalone_qc --output-root Z:\... --region S2R3 --force --overview
```

Start with `--list`: it reads only the small JSON marker files, so it's a safe
way to confirm the path is right before anything touches image data. Add
`--dry-run` to any scoring command to see what it *would* do.

> ### Drive letter: `Z:` here, `V:` on the acquisition PC
>
> Both machines mount the same share, `\\nwl-vast.rc.fas.harvard.edu\multisem`,
> but under **different letters** — `V:` on the acquisition computer, `Z:` on
> this one. So a path copied out of `msem_run_config.json`, the HI-MC GUI, or
> Claude 1's handoff documents needs `V:` swapped for `Z:`:
>
> | Acquisition PC | Here |
> |---|---|
> | `V:\Richard\Vlad\...` | `Z:\Richard\Vlad\...` |
>
> This is also why `layout.py` never trusts the `tile_folder` column in
> `live_qc_results.csv` verbatim — it holds the acquisition machine's own
> `V:\` paths — and derives `<region_dir>\mfov<N>` instead.

Only `numpy` and `Pillow` are actually needed. `matplotlib` and `PySide6` are
in the venv but this package never imports them (verified: it starts with
`matplotlib` blocked), so nothing here depends on a GUI stack.

### Measured on real data

227-tile region `S2R3` (91 beams/tile, 3.34 MB per beam image), read from the
share at `--workers 24`: **12 s per region**, 496 images / ~1.6 GB. All five
regions of a batch take about a minute. Scoring is not the bottleneck.

Output lands in `<batch>\<region>\StandaloneQC\`:

| File | Contents |
|---|---|
| `standalone_qc_report.html` | Self-contained page: tile map, scored tiles, per-beam quality, per-beam detail |
| `standalone_qc_results.csv` | Fully-scored tiles, in `live_qc_results.csv`'s own column order |
| `standalone_qc_results.jsonl` | Same rows with full nested per-beam detail |
| `standalone_qc_probe.csv` | The one-beam tissue probe, every tile |
| `standalone_qc_summary.json` | Machine-readable run summary |
| `standalone_qc_region_overview.png` | Only with `--overview` |

A ledger at `<output_root>\standalone_qc_state.json` records what's been scored,
so re-running is cheap and an interrupted pass resumes.

## How it decides a region is ready

Two signals; either is accepted, the per-region one preferred.

**1. `<region_folder>\region_complete.json`** (preferred) — a dedicated
per-region marker the acquisition side added on 2026-09-07:

```json
{"name": "S2R3", "status": "done", "detail": null, "completed_at": "2026-09-07 12:07:30"}
```

Preferred because it isn't rewritten every time some *other* region in the
batch changes state, and because it's written immediately *before* the
manifest — so it's marginally earlier as well as more specific. Skipped and
failed regions get one too, carrying that status, so it can withhold a region
as well as release one.

**2. `<batch_folder>\region_manifest.json`** — a region's entry flipping to
`"status": "done"`. Still required for **enumeration** (it's the only record
of what a batch *intends* to image, including regions that failed before their
folder existed), and it's the only signal any region imaged before 2026-09-07
has — i.e. all historical data on the share.

Both are safe for the same three reasons, verified in `focus_map.py` and
confirmed against the acquisition clone in `../../ACQUISITION_REPLY.md`:

- Written **atomically** (tmp + `os.replace`), explicitly so a concurrent
  reader never sees a half-written file.
- Written only after `run_one_region()` returns — after acquisition's final
  `CMD_WAIT_ACQUISITION_COMPLETE` and after both live-QC subprocesses are
  joined. So when the signal is visible, ZEN has flushed every BMP. Neither
  call site is gated on `lightweight_mode`, so this holds in both modes.
- Always written, single-region runs included, and distinguishing
  `failed` / `skipped` / `retake`.

`--list`'s `SRC` column shows which signal decided each region's status.

Note this machine's own `focus_map.py` does **not** write
`region_complete.json` — the acquisition clone added it and there's no shared
remote yet. That's fine: we're the consumer, so what matters is which files are
on the share, not what our local copy of the producer would write.

`verify_region_files()` additionally checks each tile folder has an
`image_coordinates.txt` and matches the region's modal beam count, to absorb
SMB metadata lag. That's belt-and-braces, not the primary mechanism.

## Regression canary

Each report passes through the acquisition side's own
`LiveQC/live_qc_timing.json`, and flags `qc_catchup_s` above 30 s. That number
is how long acquisition waited for its *own* live-QC subprocesses to drain
after imaging finished; it used to hit its full 300 s timeout on ~230-tile
regions, and driving it to near-zero is exactly what moving scoring to this
machine was for. A non-trivial value means a regression on the acquisition
side worth reporting back.

## Scoring policy: all beams, sampled tiles

At 2000×1748 8-bit, one beam image is ~3.5 MB, so a 91-beam tile is ~320 MB and
a 236-tile region is **~75 GB** of network reads. All beams on all tiles isn't
worth that. Instead, two passes:

1. **Tissue probe** — score *one* beam on every tile (~1/91 of the I/O) to
   classify tissue vs. blank, reusing `aggregate_mfov_quality`'s own
   saturation/flatness criteria. Blankness is a whole-tile property, so one
   beam genuinely settles it.
2. **Full scoring** — *all* beams on the first / middle / last tissue tile.

That's under 2 GB for the same region, and it's what makes the per-beam summary
meaningful: a beam that is consistently bad across several tiles is a hardware
problem, and a 5-beam live sample could never see it.

Change with `--sampling all` (every tile, the expensive complete option),
`--sampling every_n --every-n 25`, or `--no-probe`.

`--overview` is separate and expensive on purpose:
`RegionOverviewAccumulator.add_mfov()` reads every beam of every mfov, so an
overview costs the full region read no matter how few tiles were scored.

## Design contract

This package sits alongside acquisition code that lives in a **different git
clone with no shared remote**. Three rules keep that survivable:

1. **Never import `focus_map`** (comtypes / hardware). We import only
   `qc_algorithm`, `qc_overview` and `live_qc_support` — all hardware-free.
   `live_qc_support` imports matplotlib solely inside its viewer function, so
   importing it here costs nothing.
2. **Never edit those three files.** They're shared, actively-used-by-
   acquisition code and the highest merge-conflict surface between the two
   machines. We use their public API only: `build_beam_map`,
   `choose_beams_for_tile`, `CSV_COLUMNS`, `compute_single_image_quality`,
   `aggregate_mfov_quality`, `detect_outlier_beams`, `quality_tier`,
   `RegionOverviewAccumulator`. All new code lives in this folder.
3. **Read-only against batch folders**, except the deliberate two-way channels
   (`region_review_status.json` / `retake_regions.txt` at the wafer root — not
   yet wired up, see below). Our own output goes to `StandaloneQC/`.

Two acquisition-side facts this depends on, both from
`../../STANDALONE_QC_HANDOFF.md`:

- In the acquisition side's **lightweight mode**, only
  `tile_idx, x_um, y_um, tile_folder, ol_pred, stig_x_pred, stig_y_pred` are
  real in `live_qc_results.csv`; `quality` is `NaN` and the rest are zeros.
  We read *only* those columns (see `layout.py`) and recompute everything else.
- `is_blank_outside_roi` is therefore a placeholder, which is why we run our
  own tissue probe rather than reading it.

## Module map

| Module | Role |
|---|---|
| `config.py` | Defaults + JSON override; `policy_fingerprint()` for the ledger |
| `discover.py` | Batch/region enumeration, the `region_manifest.json` trigger, file verification |
| `layout.py` | Tile layout from `live_qc_results.csv`, falling back to `mfov*/metadata.txt` |
| `score.py` | The two passes, tile selection, cross-tile per-beam summary |
| `report.py` | CSV / JSONL / JSON / HTML output and the optional region overview |
| `ledger.py` | What's already been scored, atomically persisted |
| `cli.py` | `python -m standalone_qc` |

## Tests

No shared CI between the two machines, so the tests build their own miniature
output tree in a temp dir — no microscope, no `V:\` share, no real run:

```powershell
python standalone_qc\tests\test_standalone_qc.py   # or: pytest standalone_qc\tests
```

They cover discovery (only `done` regions), BOM tolerance, incomplete-tile
detection, both layout sources, NaN-placeholder rejection, tissue-probe
classification, tile selection, outlier-beam detection, report output, ledger
policy invalidation, and missing-image robustness.

## Not built yet

- **`--watch` mode.** Post-hoc per finished region was the deliberate first
  step. The trigger is already implemented in `discover.py`, so watch mode is a
  poll loop around `find_scorable_regions()`.
- **GUI.** CLI first. A thin PySide6 window (folder picker, Start/Stop, log
  tail) can drive `python -m standalone_qc` as a subprocess the same way
  `msem_control/engine/pipeline_runner.py` already drives the acquisition
  pipeline.
- **Feeding retake decisions back.** `live_qc_support.sync_retake_list()` /
  `write_wafer_qc_report()` are the existing channel; writing a region into
  `region_review_status.json` at the wafer root is all it takes. Left until the
  scoring output has been validated against real data.
