r"""Making generated HTML usable from another computer.

The reports already live on the share, so any machine that can reach it can
open them. What was not portable is the ~950 absolute references per
dashboard that the click-an-mFoV-to-view-its-beams feature needs: they were
spelled with THIS machine's drive letter. The acquisition PC mounts the same
share as V:, so every one of those links was dead there.

Verified in Chromium on this machine: the same beam image loads from both
file:///Z:/... and file://nwl-vast.rc.fas.harvard.edu/multisem/...

    pytest standalone_qc/tests/test_portable_paths.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import pytest  # noqa: E402

from standalone_qc.portable_paths import apply, make_portable  # noqa: E402

B = chr(92)
UNC = B * 2 + "srv.example.edu" + B + "multisem"


def _res(drive: str):
    """Z: is a mapped share; everything else is local or unmapped."""
    return UNC if drive.upper() == "Z:" else None


def test_every_spelling_of_a_path_is_converted() -> None:
    """Three forms appear in the generated HTML and all three matter.

    The JSON-escaped one is the trap: it CONTAINS the plain form, so
    replacing the plain form first corrupts it into a half-converted path.
    """
    url = "file:///Z:/Richard/a.bmp"
    plain = "Z:" + B + "Richard" + B + "a.bmp"
    jsonesc = "Z:" + B * 2 + "Richard" + B * 2 + "a.bmp"

    assert make_portable(url, _res)[0] == \
        "file://srv.example.edu/multisem/Richard/a.bmp"
    assert make_portable(plain, _res)[0] == \
        B * 2 + "srv.example.edu" + B + "multisem" + B + "Richard" + B + "a.bmp"
    assert make_portable(jsonesc, _res)[0] == \
        B * 4 + "srv.example.edu" + B * 2 + "multisem" + B * 2 + "Richard" \
        + B * 2 + "a.bmp"


def test_all_three_forms_together_in_one_page() -> None:
    """A real dashboard carries all three at once; converting one must not
    damage another."""
    page = (
        '<a href="file:///Z:/Richard/a.bmp" data-path="Z:' + B + 'Richard'
        + B + 'a.bmp">x</a>'
        '<script>{"f":["file:///Z:/Richard/b.bmp"],"d":"Z:' + B * 2
        + 'Richard' + B * 2 + 'b.bmp"}</script>'
    )
    out, replaced = make_portable(page, _res)
    assert "Z:" not in out, out
    assert replaced == {"Z:": UNC}
    assert out.count("file://srv.example.edu/multisem/") == 2
    # The JSON string is still valid JSON-escaped, i.e. doubled.
    assert B * 4 + "srv.example.edu" + B * 2 + "multisem" in out


def test_local_paths_are_left_alone() -> None:
    """A local path is already unreachable from another machine. Inventing a
    UNC for it would turn a visibly broken link into a silently wrong one."""
    page = "D:" + B + "mSEMAPI" + B + "himc_app  and  C:/Windows"
    out, replaced = make_portable(page, _res)
    assert out == page
    assert replaced == {}


def test_unmapped_letter_is_left_alone() -> None:
    page = "file:///Q:/somewhere/else.bmp"
    assert make_portable(page, _res)[0] == page


def test_lowercase_drive_letters_are_handled() -> None:
    out, _ = make_portable("file:///z:/Richard/a.bmp", _res)
    assert out == "file://srv.example.edu/multisem/Richard/a.bmp"


def test_idempotent() -> None:
    """Rewriting an already-rewritten page must change nothing - the
    retrofit tool may be run over a mix of old and new pages."""
    for src in ("file:///Z:/a/b.bmp",
                "Z:" + B + "a" + B + "b.bmp",
                "Z:" + B * 2 + "a" + B * 2 + "b.bmp"):
        once, _ = make_portable(src, _res)
        twice, _ = make_portable(once, _res)
        assert once == twice


def test_config_switch_turns_it_off() -> None:
    page = "file:///Z:/Richard/a.bmp"
    assert apply(page, {"portable_unc_paths": False}) == page
    # Default is on; with no mapped drive on this machine it is a no-op,
    # so just assert it does not raise and returns a string.
    assert isinstance(apply(page, {}), str)


def test_real_drive_resolution_is_consistent() -> None:
    """Against the live machine: whatever unc_for_drive says about Z:, the
    rewrite must agree with it."""
    from standalone_qc.portable_paths import unc_for_drive

    unc = unc_for_drive("Z:")
    if not unc:
        pytest.skip("no Z: mapping on this machine")
    assert unc.startswith(B * 2)
    out, replaced = make_portable("file:///Z:/x.bmp")
    host_share = unc.lstrip(B).replace(B, "/")
    assert out == f"file://{host_share}/x.bmp"
    assert replaced["Z:"] == unc
