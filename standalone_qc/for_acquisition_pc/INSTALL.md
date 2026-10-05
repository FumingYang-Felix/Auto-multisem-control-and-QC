# Adding the Standalone QC tab to msem_control (acquisition computer)

Analysis runs on the QC computer. This page only writes a small request file
onto the share and reads a status file back, so it adds no measurable load to
acquisition — which is the whole point of the split.

## 1. Copy one file

    standalone_qc_page.py  ->  msem_control/ui/pages/standalone_qc_page.py

It needs only PySide6, the standard library, and msem_control's own
`BasePage` and `LogPanel`. It imports nothing from the `standalone_qc`
package, which does not exist on this machine.

## 2. Register it — all in `main_window.py`

Nothing to change in `pages/__init__.py`: `main_window.py` imports each page
straight from its module (`from .pages.himc_page import HiMcPage`), and
neither HiMcPage nor SfnPage is listed in `__init__.py` either. Adding it
there would work, but would not match how the others are done.

In `msem_control/ui/main_window.py`, beside the existing pages:

```python
from .pages.standalone_qc_page import StandaloneQcPage  # with the other imports

self.page_qc = StandaloneQcPage(engine)                 # near page_sfn
self.pages.addWidget(self.page_qc)                      # after the others
```

and in the nav bar, beside `tab_sfn`:

```python
self.tab_qc = QPushButton("Standalone QC")
self.tab_qc.setCheckable(True)
self.tab_qc.setObjectName("NavTab")
bar_layout.addWidget(self.tab_qc)
self.tabs = [self.tab_acq, self.tab_himc, self.tab_sfn, self.tab_qc]
self.tab_qc.clicked.connect(lambda: self.switch_page(3))
```

Three ordering constraints in that nav-bar function, all easy to trip over:

- `self.tabs` is assigned BEFORE the `for i, tab in enumerate(self.tabs)`
  loop that styles each button and adds it to the layout. Either put
  `tab_qc` in that list before the loop, or (as above) style and add it by
  hand afterwards - but do reassign `self.tabs`, because `switch_page()`
  iterates it to set the checked state.
- Insert before the existing `bar_layout.addStretch()`, or the new tab lands
  at the far right of the bar instead of beside the others.
- The number in `switch_page(3)` indexes `self.pages`, so it must match the
  order of the `addWidget` calls. If your `main_window.py` already has a
  fourth page, use the next number up - in both places.

## 3. Set it up once, on both machines

**QC computer** — open the Standalone QC window, choose
`Service — the acquisition computer asks, this computer analyzes`, set the
Control folder, press **Start Service**. Leave the window open: closing it
stops the service.

**This computer** — point the page's Control folder at the same folder.

The two fields do NOT have to read identically. Each machine only ever uses
its own path to reach that folder, so this is fine:

    QC computer      Z:\Richard\Vlad\StandaloneQC_control
    this computer    V:\Richard\Vlad\StandaloneQC_control

Both resolve to one folder on the share, and nothing compares the two
strings. A UNC path works too and is handy if you want one string you can
paste on either machine:

    \\nwl-vast.rc.fas.harvard.edu\multisem\Richard\Vlad\StandaloneQC_control

What DOES have to be readable by the other machine is the **image
directory**, because this computer writes that path into the request and the
QC computer opens it. `V:\...` means nothing over there. The page converts
mapped drives to UNC automatically before sending, so this is handled for
you - it is only worth knowing if you ever hand-write a request file.

## 4. Use it

Press **Use the HI-MC storage location** to copy the batch path from the
HI-MC page, then **Analyze on the QC computer**. Leave Sections blank to
analyze everything that has finished imaging and is not yet analyzed, or name
sections (`S3R5, S4R5`) to run exactly those.

The status line shows the QC computer's state and live progress. If it reads
OFFLINE, the service is not running over there — the request will simply wait
until it is.

Consider clearing **Stitch the section overview** during a run: the overview
reads every beam of every mFoV on the QC computer (~36,000 images for a
400-mFoV section), and those reads come off the same share this acquisition is
writing to.

## What the two sides agree on

Only the JSON in the control folder. The constants at the top of
`standalone_qc_page.py` must match `standalone_qc/control.py` on the QC
computer; a test over there (`tests/test_acq_page.py`) asserts they do, and
drives this exact file against the real agent.
