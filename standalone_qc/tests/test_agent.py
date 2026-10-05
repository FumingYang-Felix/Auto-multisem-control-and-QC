"""The cross-machine control channel, driven end to end.

The acquisition computer writes a request file onto the share; the agent on
the QC computer claims it, analyzes, and writes status and a response back.
These tests stand in for the acquisition side by writing the request JSON
DIRECTLY - no import of control.submit_request in the round-trip test - so
they also prove the protocol is reproducible by a separate clone that does
not have this package.

    pytest standalone_qc/tests/test_agent.py
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import pytest  # noqa: E402

from standalone_qc import control  # noqa: E402
from standalone_qc.agent import Agent  # noqa: E402
from standalone_qc.config import load_config  # noqa: E402
from standalone_qc.tests.test_standalone_qc import (  # noqa: E402
    BATCH,
    DSAMP,
    build_fixture,
)


def _cfg(**kw):
    base = {"dsamp": DSAMP, "max_workers": 4,
            "region_overview_enabled": False}
    base.update(kw)
    return load_config(None, **base)


def _agent(control_root: Path, **cfgkw):
    lines = []
    a = Agent(control_root, _cfg(**cfgkw), poll_s=1.0, log=lines.append)
    return a, lines


def _raw_request(control_root: Path, **fields) -> str:
    """Write a request the way a foreign process would: plain JSON, no
    helper from this package."""
    rid = fields.pop("id", time.strftime("%Y%m%d_%H%M%S") + "_test")
    payload = {"protocol": 1, "id": rid, "action": "analyze_pending",
               "sections": [], "options": {}}
    payload.update(fields)
    control_root.mkdir(parents=True, exist_ok=True)
    (control_root / f"request_{rid}.json").write_text(
        json.dumps(payload, indent=2), encoding="utf-8")
    return rid


# ---------------------------------------------------------------------------
def test_round_trip_analyzes_and_reports_back(tmp_path: Path) -> None:
    """The whole point: a request written by another machine results in a
    section analyzed here, with a response the requester can read."""
    build_fixture(tmp_path / "data")
    ctrl = tmp_path / "ctrl"
    rid = _raw_request(ctrl, image_dir=str(tmp_path / "data" / BATCH))

    agent, lines = _agent(ctrl)
    agent.run(once=True)

    resp = control.read_response(ctrl, rid)
    assert resp is not None, "no response file written"
    assert resp["ok"] is True, resp
    assert resp["id"] == rid
    assert len(resp["sections"]) == 1
    sec = resp["sections"][0]
    assert sec["ok"] and sec["section"] == "S1R1"
    assert sec["n_tiles"] >= 1
    assert Path(sec["report"]).is_file()

    # The request is archived, not left to be run again.
    assert not list(ctrl.glob("request_*.json"))
    assert (ctrl / control.HANDLED_DIR).is_dir()
    assert list((ctrl / control.HANDLED_DIR).glob("request_*"))


def test_status_reports_liveness_and_progress(tmp_path: Path) -> None:
    """The acquisition page shows a button that must not lie. If the agent
    is not running, its status has to read as offline rather than leaving a
    request nobody will ever collect."""
    ctrl = tmp_path / "ctrl"
    agent, _ = _agent(ctrl)
    agent.run(once=True)                     # writes status, then stops

    st = control.read_status(ctrl)
    assert st is not None
    assert st["protocol"] == control.PROTOCOL_VERSION
    assert st["host"] and st["pid"] == os.getpid()
    # A --once pass that finished its work is idle, NOT stopped: "stopped"
    # is reserved for an agent that was actually asked to shut down, so the
    # acquisition page can tell "done for now" from "gone".
    assert st["state"] == "idle"
    # Just written, so it counts as online...
    assert control.is_online(st) is True
    # ...and a stale one does not.
    st_old = dict(st, heartbeat_epoch=time.time() - 10 * control.OFFLINE_AFTER_S)
    assert control.is_online(st_old) is False
    assert "OFFLINE" in control.status_line(st_old)
    assert control.is_online(None) is False
    assert "no status file" in control.status_line(None)


def test_heartbeat_keeps_writing_during_a_long_section(tmp_path: Path) -> None:
    """A 400-mFoV section spends ~15 minutes inside one overview stitch. If
    status.json only advanced between sections, the acquisition GUI would
    call the agent dead in the middle of every large section."""
    ctrl = tmp_path / "ctrl"
    agent, _ = _agent(ctrl)
    agent.state.update(state="analyzing")
    agent._write_status()
    first = control.read_status(ctrl)["heartbeat_epoch"]

    hb = threading.Thread(target=agent._heartbeat_loop, daemon=True)
    agent._stop.clear()
    hb.start()
    try:
        # Stand in for the worker being busy: do not touch status ourselves.
        deadline = time.time() + control.HEARTBEAT_S * 2.5
        latest = first
        while time.time() < deadline:
            time.sleep(0.5)
            st = control.read_status(ctrl)
            if st:
                latest = st["heartbeat_epoch"]
        assert latest > first, "heartbeat did not advance while busy"
    finally:
        agent._stop.set()
        hb.join(timeout=5)


def test_a_bad_request_is_answered_not_fatal(tmp_path: Path) -> None:
    """An agent that exits on one malformed request is one the operator
    finds stopped hours later with a run unanalyzed."""
    ctrl = tmp_path / "ctrl"
    ctrl.mkdir()
    (ctrl / "request_broken.json").write_text("{not json", encoding="utf-8")
    rid = _raw_request(ctrl, id="nodir", image_dir=str(tmp_path / "nope"))

    agent, _ = _agent(ctrl)
    agent.run(once=True)                     # must return, not raise

    bad = control.read_json(ctrl / "response_broken.json")
    assert bad and bad["ok"] is False and "unreadable" in bad["error"]
    missing = control.read_response(ctrl, rid)
    assert missing and missing["ok"] is False
    assert "not a folder" in missing["error"]
    # Both archived, so neither is retried forever.
    assert not list(ctrl.glob("request_*.json"))


def test_named_sections_are_analyzed_even_if_already_done(tmp_path: Path) -> None:
    """Naming a section IS the instruction - the same rule the GUI's
    Analyze Selected follows, for the same reason."""
    build_fixture(tmp_path / "data")
    ctrl = tmp_path / "ctrl"
    batch = tmp_path / "data" / BATCH

    a1, _ = _agent(ctrl)
    r1 = _raw_request(ctrl, id="first", image_dir=str(batch))
    a1.run(once=True)
    assert control.read_response(ctrl, r1)["ok"]

    # Second request with no sections: nothing left pending.
    r2 = _raw_request(ctrl, id="second", image_dir=str(batch))
    a1.run(once=True)
    assert control.read_response(ctrl, r2).get("note") == "nothing to analyze"

    # Naming it explicitly runs it again.
    r3 = _raw_request(ctrl, id="third", image_dir=str(batch),
                      sections=["S1R1"])
    a1.run(once=True)
    resp = control.read_response(ctrl, r3)
    assert resp["ok"] and len(resp["sections"]) == 1


def test_only_known_options_are_honoured(tmp_path: Path) -> None:
    """A future acquisition-side page must not be able to change scoring
    policy by sending a key we never agreed on."""
    ctrl = tmp_path / "ctrl"
    agent, _ = _agent(ctrl)
    cfg = agent._cfg_for({"options": {"max_workers": 3,
                                      "beam_selection_mode": "NONSENSE",
                                      "dsamp": 1}})
    assert cfg["max_workers"] == 3
    assert cfg["beam_selection_mode"] == "ALL", "unknown key leaked into cfg"
    assert cfg["dsamp"] != 1


def test_two_agents_cannot_claim_the_same_request(tmp_path: Path) -> None:
    """Claim-by-rename: os.replace is atomic, so exactly one agent wins."""
    ctrl = tmp_path / "ctrl"
    rid = _raw_request(ctrl, image_dir=str(tmp_path))
    a, _ = _agent(ctrl)
    b, _ = _agent(ctrl)

    path = ctrl / f"request_{rid}.json"
    first = a._claim(path)
    second = b._claim(path)
    assert first is not None and first.name.endswith(control.CLAIMED_SUFFIX)
    assert second is None, "both agents claimed the same request"


def test_stop_action_stops_the_agent(tmp_path: Path) -> None:
    ctrl = tmp_path / "ctrl"
    rid = _raw_request(ctrl, action="stop", image_dir=str(tmp_path))
    agent, _ = _agent(ctrl)
    agent.run(once=True)
    assert agent._stop.is_set()
    assert control.read_response(ctrl, rid)["ok"] is True
    # And that is the one case status must read "stopped".
    assert control.read_status(ctrl)["state"] == "stopped"


def test_run_can_be_called_again(tmp_path: Path) -> None:
    """run() used to be single-shot: its cleanup set the same event the
    loop checked, so a second call returned instantly having done nothing.
    Any caller that polls with --once depends on this."""
    build_fixture(tmp_path / "data")
    ctrl = tmp_path / "ctrl"
    agent, _ = _agent(ctrl)

    agent.run(once=True)                  # nothing waiting
    rid = _raw_request(ctrl, image_dir=str(tmp_path / "data" / BATCH))
    agent.run(once=True)                  # must still pick this up

    resp = control.read_response(ctrl, rid)
    assert resp is not None and resp["ok"] is True


def test_submit_request_matches_what_the_agent_expects(tmp_path: Path) -> None:
    """control.submit_request is what the acquisition page will use (or
    copy). Its output must be exactly what the agent consumes."""
    build_fixture(tmp_path / "data")
    ctrl = tmp_path / "ctrl"
    req = control.submit_request(
        ctrl, str(tmp_path / "data" / BATCH),
        action="analyze", sections=["S1R1"],
        options={"max_workers": 2}, requested_by="acq-pc")
    assert (ctrl / f"request_{req['id']}.json").is_file()

    agent, _ = _agent(ctrl)
    agent.run(once=True)
    resp = control.read_response(ctrl, req["id"])
    assert resp and resp["ok"] and resp["sections"][0]["section"] == "S1R1"


def test_rejects_an_unknown_action() -> None:
    with pytest.raises(ValueError):
        control.submit_request("/tmp/x", "/tmp/y", action="rm -rf")
