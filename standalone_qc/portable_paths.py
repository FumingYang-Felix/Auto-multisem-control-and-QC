r"""Rewriting drive-letter paths in generated HTML so other machines can use it.

The reports are written onto the share, so any computer that can reach the
share can already open them. What is NOT portable is the ~950 absolute
references per dashboard that the click-an-mFoV-to-view-its-beams feature
needs: they are spelled with THIS machine's drive letter.

    file:///Z:/Richard/Vlad/...

Z: here is \\nwl-vast.rc.fas.harvard.edu\multisem. The acquisition PC mounts
the same share as V:, so those links resolve to nothing there - every other
part of the page works, and only the image click-through is dead. Rewriting
them to UNC removes the dependency on any particular letter:

    file://nwl-vast.rc.fas.harvard.edu/multisem/Richard/Vlad/...

Verified in Chromium on this machine: both the Z: and the UNC form of the
same beam image load (2000x1748). UNC additionally works on a machine with
no mapping for the share at all.

Only paths on MAPPED drives are touched. A local path (D:\mSEMAPI\...) is
left exactly as it is: it is already unreachable from another machine, and
inventing a UNC for it would turn a visibly broken link into a silently
wrong one.

Applied to the HTML only, never to the JSON/CSV data files. Those are records
this package reads back on this machine, and rewriting stored paths risks
breaking our own readers for no viewing benefit.
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as _wt
import re
from typing import Dict, Optional, Tuple

# Every distinct spelling a Windows path takes in the generated HTML:
#   file:///Z:/a/b     URL form, forward slashes  (TILE_BMPS, <a href>)
#   Z:\a\b           JSON-escaped backslashes   (inside script payloads)
#   Z:\a\b             plain Windows path         (data-path, note text)
_DRIVE_RE = re.compile(r"(?:file:///)?([A-Za-z]):[\\/]")

_cache: Dict[str, Optional[str]] = {}


def unc_for_drive(letter: str) -> Optional[str]:
    r"""r"\\server\share" behind a mapped drive, or None if it is local,
    unmapped, or this is not Windows."""
    letter = letter.rstrip(":").upper() + ":"
    if letter in _cache:
        return _cache[letter]
    value: Optional[str] = None
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = _wt.DWORD(len(buf))
        rc = ctypes.windll.mpr.WNetGetConnectionW(  # type: ignore[attr-defined]
            letter, buf, ctypes.byref(size))
        if rc == 0 and buf.value.startswith("\\\\"):
            value = buf.value
    except Exception:
        value = None
    _cache[letter] = value
    return value


def _host_share(unc: str) -> Tuple[str, str]:
    r"""r"\\host\share" -> ("host", "share")."""
    parts = unc.lstrip("\\").split("\\", 1)
    return parts[0], (parts[1] if len(parts) > 1 else "")


def make_portable(html: str, resolver=unc_for_drive) -> Tuple[str, Dict[str, str]]:
    """(rewritten html, {drive: unc}) for every mapped drive it replaced.

    Idempotent: once rewritten there are no drive letters left to match, so
    running it again changes nothing.
    """
    drives = {m.group(1).upper() for m in _DRIVE_RE.finditer(html)}
    replaced: Dict[str, str] = {}
    for d in sorted(drives):
        unc = resolver(d + ":")
        if not unc:
            continue                      # local or unmapped - leave alone
        host, share = _host_share(unc)
        url_base = f"file://{host}/{share}/"
        # Built from an explicit backslash rather than inline escapes:
        # these strings are mostly backslashes, and getting one wrong
        # here silently produces links that look right and resolve to
        # nothing.
        b = chr(92)
        plain = b * 2 + host + b + share + b
        jsonesc = b * 4 + host + b * 2 + share + b * 2
        for old, new in (
            (f"file:///{d}:/", url_base),
            (f"file:///{d.lower()}:/", url_base),
            # JSON-escaped first: it CONTAINS the plain form, so doing
            # the plain one first would corrupt it.
            (f"{d}:" + b * 2, jsonesc),
            (f"{d.lower()}:" + b * 2, jsonesc),
            (f"{d}:" + b, plain),
            (f"{d.lower()}:" + b, plain),
        ):
            html = html.replace(old, new)
        replaced[f"{d}:"] = unc
    return html, replaced


def apply(html: str, cfg) -> str:
    """make_portable, honouring the `portable_unc_paths` config key."""
    if not (cfg or {}).get("portable_unc_paths", True):
        return html
    out, _ = make_portable(html)
    return out
