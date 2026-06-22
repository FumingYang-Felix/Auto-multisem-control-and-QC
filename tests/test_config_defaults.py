"""
Guard: every HI-MC extra-param widget default must match focus_map.CONFIG.

The HI-MC page surfaces ~27 focus_map.CONFIG knobs as widgets (_EXTRA_PARAM_GROUPS).
If a widget default drifts from the real CONFIG default it would silently change
acquisition behaviour. This extracts focus_map.CONFIG WITHOUT importing focus_map
(its `from msem import ...` needs Windows-only comtypes) by parsing the literal
with ast, and compares.

Run:  python tests/test_config_defaults.py      (exit 0 = pass)
Also discoverable by pytest.
"""
import ast
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def _extract_config(path):
    src = open(path, "r", encoding="utf-8").read()
    tree = ast.parse(src)
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if getattr(t, "id", None) == "CONFIG":
                    return ast.literal_eval(node.value)
        if isinstance(node, ast.AnnAssign) and getattr(node.target, "id", None) == "CONFIG":
            return ast.literal_eval(node.value)
    raise AssertionError("CONFIG not found in focus_map.py")


def test_himc_extra_defaults_match_config():
    config = _extract_config(os.path.join(ROOT, "focus_map.py"))
    # import only the data table (no Qt needed at module import for the constant)
    from msem_control.ui.pages.himc_page import _EXTRA_PARAM_GROUPS

    mismatches = []
    n = 0
    for _title, specs in _EXTRA_PARAM_GROUPS:
        for key, _label, _kind, default, _opts in specs:
            n += 1
            if config.get(key, "<MISSING>") != default:
                mismatches.append((key, default, config.get(key, "<MISSING>")))
    assert not mismatches, f"widget defaults differ from focus_map.CONFIG: {mismatches}"
    assert n >= 20, f"expected the full extra-param set, only saw {n}"
    return n


if __name__ == "__main__":
    n = test_himc_extra_defaults_match_config()
    print(f"OK: all {n} HI-MC extra-param defaults match focus_map.CONFIG")
