# Standalone QC tab — what these files are, and what needs doing

Two files were just copied into `ui/pages/` on the acquisition computer:

    standalone_qc_page.py   the new page (code)
    INSTALL.md              how to register it (documentation, not code)

## Context

QC for the MultiSEM now runs on a SECOND computer. It was moved off the
acquisition machine because real-time QC there made acquisition
unacceptably slow — one run spent 3000s of 5502s waiting on its own QC. That
split must not be undone.

The operator still wants QC to be part of the acquisition workflow, so this
page is a **thin remote control**: it writes a small JSON request onto the
shared drive and polls a small JSON status file back. The QC computer does
all the work.

    this page ──request_<id>.json──▶ <control folder on the share>
              ◀──status.json / response_<id>.json ──── QC computer's agent

## What the page must keep being

- **No analysis.** No image reads, no scoring, no stitching. If a change
  would make this page open a `.bmp`, it is the wrong change.
- **No import from `standalone_qc`.** That package does not exist on this
  machine. The whole protocol is ~40 lines of `json` at the top of the file.
- Its only dependencies are PySide6, the stdlib, and this app's own
  `BasePage` (`ui/pages/base_page.py`) and `LogPanel` (`ui/widgets.py`), via
  `from .base_page import ...` and `from ..widgets import ...`. If this
  clone's tree is laid out differently, fixing those two import lines is
  expected; anything more is not.

The file is already tested on the QC computer: it is loaded there with those
two imports stubbed and driven against the real agent, and its protocol
constants are asserted equal to the QC side's. **Please do not refactor it,
rename its fields, or "improve" the protocol constants** — a silent mismatch
shows up as a request the QC computer never answers, with nothing in any log
to say why.

## What needs doing

One file: `ui/main_window.py`. Add the import, construct the page, add it to
the page stack, and add a nav tab. INSTALL.md section 2 has the exact
snippet plus three ordering constraints that are easy to trip over
(`self.tabs` is assigned before the loop that adds the buttons; the insert
must precede `bar_layout.addStretch()`; the number in `switch_page(N)`
indexes `self.pages` and must match the `addWidget` order).

That snippet was written against the OTHER clone's `main_window.py`. Read
the real file here first and adapt — that is the reason this was handed to
you rather than applied blind.

## How to verify

Launch msem_control and open the new tab. With no control folder set, the
status line should read "QC computer: no control folder set". That alone
proves the page constructed, its imports resolved, and its poll timer runs.

The rest is configuration the operator does, not code:

1. On the QC computer: Standalone QC window → Service mode → set the control
   folder → Start Service (the window stays open to keep it running).
2. On this computer: the same folder in the page's Control folder field.
   The two paths need NOT be the identical string — each machine uses its own
   drive letter for the share.

Then "Use the HI-MC storage location" → "Analyze on the QC computer". The
status line should go from OFFLINE to analyzing, and finished sections should
appear in the page's log with their median quality and report paths.

## One thing worth knowing

The image directory IS sent between machines, so the page converts mapped
drives to UNC before writing a request (`_to_unc`). The acquisition computer
mounts the share as `V:` and the QC computer as `Z:`, so an un-converted
`V:\...` path names nothing on the far side. Leave that conversion in place.
