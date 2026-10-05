"""Does the generated HTML actually WORK in a browser?

These load a real page into Chromium (via QtWebEngine, offscreen) and ask the
page itself. Every other test in this package matches strings in the HTML,
which is necessary but has already proved insufficient twice:

  - Panels 6 and 7 were injected into a JavaScript string literal. The text
    was present in the file, so a substring assertion passed, but the browser
    never rendered it - and the stray quotes broke the page's click handler,
    which is what made clicking an mFoV open a directory listing.
  - A 3D plot whose JS throws, whose payload is malformed, or whose
    projection collapses all points to one spot still emits perfectly
    plausible HTML. Only a real canvas can tell you it drew nothing.

So: no console errors, canvas has painted pixels, and dragging changes the
picture. Skipped when QtWebEngine is unavailable.

    pytest standalone_qc/tests/test_render.py
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault(
    "QTWEBENGINE_CHROMIUM_FLAGS",
    "--disable-gpu --allow-file-access-from-files",
)

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import pytest  # noqa: E402

pytest.importorskip(
    "PySide6.QtWebEngineWidgets",
    reason="QtWebEngine not installed; HTML is only string-checked",
)

from PySide6.QtCore import QTimer, QUrl  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402
from PySide6.QtWebEngineWidgets import QWebEngineView  # noqa: E402

from standalone_qc import dashboard, plot3d  # noqa: E402

# Ask the page about itself: JS errors, what the canvases actually painted,
# and whether a drag changes the rendering.
_PROBE = r"""
(function () {
  var out = {errors: window.__p3dErrors || [], plots: 0, drawn: []};
  document.querySelectorAll('.p3d').forEach(function (r) {
    out.plots++;
    var c = r.querySelector('canvas');
    if (!c) { out.drawn.push(null); return; }
    var d = c.getContext('2d').getImageData(0, 0, c.width, c.height).data;
    var painted = 0;
    for (var i = 3; i < d.length; i += 4) { if (d[i] !== 0) { painted++; } }
    out.drawn.push({w: c.width, h: c.height,
                    frac: painted / (d.length / 4)});
  });
  var c0 = document.querySelector('.p3d canvas');
  if (c0) {
    var before = c0.toDataURL();
    var b = c0.getBoundingClientRect();
    function ev(t, dx, dy, target) {
      target.dispatchEvent(new MouseEvent(t, {
        clientX: b.left + b.width / 2 + dx,
        clientY: b.top + b.height / 2 + dy,
        bubbles: true, cancelable: true, view: window}));
    }
    ev('mousedown', 0, 0, c0);
    ev('mousemove', 70, 25, window);
    ev('mouseup', 70, 25, window);
    out.rotated = (c0.toDataURL() !== before);
    var zin = document.querySelector('.p3d [data-p3d="in"]');
    if (zin) { var b2 = c0.toDataURL(); zin.click();
               out.zoomed = (c0.toDataURL() !== b2); }
  }
  out.svgs = document.querySelectorAll('svg').length;
  return JSON.stringify(out);
})()
"""


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture(scope="module")
def view(qapp):
    """One view for the module, using its OWN default page.

    Deliberately NOT a QWebEnginePage subclass with setPage(): a Python
    subclass gets garbage-collected on Python's schedule, and when that lands
    after the QApplication is gone QtWebEngine tears down through freed memory
    and the whole process exits 0xC0000005 - every test passing and the runner
    still reporting failure. The default page is owned by the view on the C++
    side, and JS errors are collected inside the page instead (see
    _write_page), so the subclass bought nothing.
    """
    v = QWebEngineView()
    v.resize(1200, 900)
    yield v
    # Ordered teardown while the app is still alive.
    v.page().setHtml("")
    qapp.processEvents()
    v.close()
    qapp.processEvents()


def _probe(qapp, view, page_path: Path) -> dict:
    page = view.page()
    result: dict = {}

    def on_load(ok):
        if not ok:
            result["load_failed"] = True
            qapp.quit()
            return
        QTimer.singleShot(900, lambda: page.runJavaScript(_PROBE, got))

    def got(res):
        try:
            result.update(json.loads(res))
        except Exception as exc:                       # pragma: no cover
            result["probe_error"] = f"{exc}: {res!r}"
        qapp.quit()

    page.loadFinished.connect(on_load)
    try:
        page.setUrl(QUrl.fromLocalFile(str(page_path.resolve())))
        QTimer.singleShot(30000, qapp.quit)
        qapp.exec()
    finally:
        page.loadFinished.disconnect(on_load)
    return result


def _write_page(tmp_path: Path) -> Path:
    """A page shaped like the real dashboard: two 3D plots, one engine, and
    a JS-embedded second document to keep the injection honest."""
    pts = [(0.0, 0.0, 2.0), (100.0, 0.0, 2.4), (0.0, 120.0, 2.9),
           (100.0, 120.0, 3.4), (50.0, 60.0, 2.7)]
    body = (
        plot3d.scatter3d_html(pts, "OL", "uA", plane=(0.004, 0.0058, 2.0),
                              point_labels=[f"mFoV {i}" for i in range(5)],
                              plot_id="a")
        + plot3d.scatter3d_html(pts, "StigX", "", plot_id="b")
        + plot3d.engine_html()
    )
    page = tmp_path / "page.html"
    # Errors are captured IN the page - both uncaught exceptions and anything
    # written to console.error/warn - so the test needs no page subclass to
    # see them. See the `view` fixture for why that matters.
    page.write_text(
        "<html><head><meta charset='utf-8'></head><body>"
        "<script>window.__p3dErrors=[];"
        "window.onerror=function(m){window.__p3dErrors.push('onerror: '+m);};"
        "['error','warn'].forEach(function(k){var o=console[k].bind(console);"
        "console[k]=function(){window.__p3dErrors.push(k+': '+"
        "Array.prototype.join.call(arguments,' '));o.apply(null,arguments);};"
        "});</script>" + body + "</body></html>",
        encoding="utf-8",
    )
    return page


def test_3d_plots_render_and_respond(qapp, view, tmp_path) -> None:
    res = _probe(qapp, view, _write_page(tmp_path))

    assert not res.get("load_failed"), "page failed to load"
    assert not res.get("probe_error"), res.get("probe_error")
    assert res.get("errors") == [], f"JS errors: {res.get('errors')}"

    assert res["plots"] == 2, res
    for d in res["drawn"]:
        assert d, "a plot has no canvas"
        assert d["w"] > 50 and d["h"] > 50, d
        # A blank canvas is ~0; a correctly drawn one with axes, a plane
        # wireframe and points lands in the percent range.
        assert d["frac"] > 0.01, f"canvas looks blank: {d}"

    assert res.get("rotated") is True, "dragging did not change the render"
    assert res.get("zoomed") is True, "the zoom button did nothing"


def test_document_body_end_picks_the_real_close() -> None:
    """The unit behind the injection bug, checked directly."""
    page = ("<html><body>real"
            "<script>w.document.write('<html><body>x</body></html>');</script>"
            "</body></html>")
    at = dashboard._document_body_end(page)
    assert at == page.rindex("</body>")
    assert page[:at].count("<script") == page[:at].count("</script>")

    assert dashboard._document_body_end("<div>none</div>") == -1
    plain = "<html><body>x</body></html>"
    assert dashboard._document_body_end(plain) == plain.index("</body>")


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
