#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Email / text-message failure alerts, shared by the SFN scripts
(auto_stage_calib.py, stage_init.py) and the HI-MC page (himc_page.py, for
multi-region-failure / unexpected-pipeline-exit notifications).

Configure via alert_config.json in the repo root (see that file for the
fields and for common carrier email-to-SMS gateway addresses to use as a
"to_addrs" entry for a text alert). The SMTP password is never stored in
that file -- set it in the environment variable it names (default
MSEM_ALERT_SMTP_PASSWORD) before launching the SFN app. "enabled" and the
SMTP settings are shared by every caller (one on/off switch + one mail
relay for the whole app); a caller passing its own to_addrs (see
send_alert's parameter below) only overrides WHO receives that particular
alert, never whether alerting is turned on at all.

send_alert() is best-effort and never raises: a broken/missing config
degrades to a printed warning rather than failing the calibration run that
was trying to report the original failure.
"""

from __future__ import annotations

import json
import os
import smtplib
from email.mime.text import MIMEText
from pathlib import Path
from typing import Optional

_CONFIG_PATH = Path(__file__).resolve().parent / "alert_config.json"


def _load_config() -> Optional[dict]:
    try:
        with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        print(f"[Alerts] No {_CONFIG_PATH.name} found next to alerts.py -- failure alerts disabled.")
        return None
    except Exception as e:
        print(f"[Alerts] Failed to read {_CONFIG_PATH.name}: {e} -- failure alerts disabled.")
        return None


def send_alert(subject: str, body: str, to_addrs: Optional[list] = None) -> bool:
    """Send a failure-notification email (or, via a carrier's email-to-SMS
    gateway address in to_addrs, a text message). Returns True if sent to at
    least one recipient, False otherwise -- never raises.

    If to_addrs is given (non-empty), it overrides alert_config.json's
    to_addrs for this call only -- e.g. a page-specific recipient list.
    The "enabled" flag and SMTP settings still come from alert_config.json
    either way."""
    try:
        cfg = _load_config()
        if not cfg:
            return False

        if not cfg.get("enabled", False):
            print(f"[Alerts] Alerts disabled in {_CONFIG_PATH.name} (\"enabled\": false) -- not sending: {subject}")
            return False

        to_addrs = [a for a in (to_addrs if to_addrs is not None else cfg.get("to_addrs", [])) if a]
        if not to_addrs:
            print(f"[Alerts] No to_addrs configured -- not sending: {subject}")
            return False

        smtp_host = cfg.get("smtp_host", "")
        smtp_port = int(cfg.get("smtp_port", 587))
        use_tls = bool(cfg.get("use_tls", True))
        username = cfg.get("smtp_username", "")
        from_addr = cfg.get("from_addr") or username
        password_env = cfg.get("smtp_password_env", "MSEM_ALERT_SMTP_PASSWORD")
        password = os.environ.get(password_env, "")

        if not smtp_host or not from_addr:
            print(f"[Alerts] {_CONFIG_PATH.name} is missing smtp_host/from_addr -- not sending: {subject}")
            return False
        if username and not password:
            print(f"[Alerts] SMTP username set but env var {password_env} is empty -- not sending: {subject}")
            return False

        subject_prefix = cfg.get("subject_prefix", "[HIMC SFN]")
        msg = MIMEText(body)
        msg["Subject"] = f"{subject_prefix} {subject}"
        msg["From"] = from_addr
        msg["To"] = ", ".join(to_addrs)

        if use_tls and smtp_port != 465:
            with smtplib.SMTP(smtp_host, smtp_port, timeout=20) as server:
                server.starttls()
                if username:
                    server.login(username, password)
                server.sendmail(from_addr, to_addrs, msg.as_string())
        else:
            with smtplib.SMTP_SSL(smtp_host, smtp_port, timeout=20) as server:
                if username:
                    server.login(username, password)
                server.sendmail(from_addr, to_addrs, msg.as_string())

        print(f"[Alerts] Sent failure alert to {', '.join(to_addrs)}: {subject}")
        return True
    except Exception as e:
        print(f"[Alerts] ERROR: failed to send alert ({subject!r}): {e}")
        return False


if __name__ == "__main__":
    # Manual smoke test: python alerts.py
    ok = send_alert("Test alert", "This is a test alert from alerts.py.")
    print(f"[Alerts] send_alert() returned {ok}")
