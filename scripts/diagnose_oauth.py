#!/usr/bin/env python3
"""Verifica il login DidUP usato dal monitor senza scaricare o modificare dati."""
from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import monitor


def main() -> int:
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    school = os.environ.get("DIDUP_SCHOOL")
    username = os.environ.get("DIDUP_USER")
    password = os.environ.get("DIDUP_PASSWORD")
    if not (school and username and password):
        print(
            json.dumps(
                {
                    "event": "didup_oauth_diagnostic",
                    "ok": False,
                    "reason": "missing_credentials",
                }
            )
        )
        return 2

    original_sections = monitor.SECTIONS
    original_browser_fallback = monitor.browser_oauth_access_token
    fallback_calls = 0

    def observed_fallback(school_arg: str, user_arg: str, password_arg: str) -> str:
        nonlocal fallback_calls
        fallback_calls += 1
        return original_browser_fallback(school_arg, user_arg, password_arg)

    try:
        # Nessun metodo di download viene chiamato e non si toccano file o notifiche.
        monitor.SECTIONS = {}
        monitor.browser_oauth_access_token = observed_fallback
        monitor.fetch_all_once(school, username, password, apply_filters=True, auto=False)
        print(
            json.dumps(
                {
                    "event": "didup_oauth_diagnostic",
                    "ok": True,
                    "stage": "production_login_and_api_session",
                    "browser_fallback_used": fallback_calls > 0,
                    "data_sections_requested": 0,
                },
                sort_keys=True,
            )
        )
        return 0
    except Exception as exc:
        print(
            json.dumps(
                {
                    "event": "didup_oauth_diagnostic",
                    "ok": False,
                    "stage": "production_login_and_api_session",
                    "browser_fallback_used": fallback_calls > 0,
                    "error_type": type(exc).__name__,
                    "reason": monitor.safe_error_summary(exc),
                },
                sort_keys=True,
            ),
            file=sys.stderr,
            flush=True,
        )
        return 1
    finally:
        monitor.SECTIONS = original_sections
        monitor.browser_oauth_access_token = original_browser_fallback


if __name__ == "__main__":
    raise SystemExit(main())
