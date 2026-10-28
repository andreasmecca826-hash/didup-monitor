#!/usr/bin/env python3
"""Verifica il login e le API DidUP in sola lettura, senza persistenza o notifiche."""
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

    original_browser_fallback = monitor.browser_oauth_access_token
    original_download_circulars = monitor.download_circulars
    original_log_disabled = monitor.log.disabled
    fallback_calls = 0

    def observed_fallback(school_arg: str, user_arg: str, password_arg: str) -> str:
        nonlocal fallback_calls
        fallback_calls += 1
        return original_browser_fallback(school_arg, user_arg, password_arg)

    try:
        # I dati restano in memoria; non si scaricano PDF, notificano o salvano file.
        monitor.download_circulars = lambda didup, items: []
        monitor.log.disabled = True
        monitor.browser_oauth_access_token = observed_fallback
        data = monitor.fetch_all_once(school, username, password, apply_filters=True, auto=False)
        failed_sections = [name for name in monitor.SECTIONS if data.get(name) is None]
        ok = not failed_sections
        print(
            json.dumps(
                {
                    "event": "didup_oauth_diagnostic",
                    "ok": ok,
                    "stage": "production_readonly_data_fetch",
                    "browser_fallback_used": fallback_calls > 0,
                    "data_sections_requested": len(monitor.SECTIONS),
                    "failed_sections": failed_sections,
                    "data_persisted": False,
                    "notifications_sent": False,
                    "circular_files_downloaded": False,
                },
                sort_keys=True,
            )
        )
        return 0 if ok else 1
    except Exception as exc:
        print(
            json.dumps(
                {
                    "event": "didup_oauth_diagnostic",
                    "ok": False,
                    "stage": "production_readonly_data_fetch",
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
        monitor.browser_oauth_access_token = original_browser_fallback
        monitor.download_circulars = original_download_circulars
        monitor.log.disabled = original_log_disabled


if __name__ == "__main__":
    raise SystemExit(main())
