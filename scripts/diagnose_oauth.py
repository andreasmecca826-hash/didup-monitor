#!/usr/bin/env python3
"""Diagnostica il login OAuth DidUP senza scaricare o modificare dati del registro.

Stampa solo metadati strutturali della pagina di consenso. Non stampa URL query,
contenuto HTML, valori dei campi, credenziali, token o codici OAuth.
"""
from __future__ import annotations

import json
import logging
import os
import sys
from html.parser import HTMLParser
from urllib.parse import urlsplit

import httpx
from didupwrapper import DiDUPClientSync


class ConsentMetadataParser(HTMLParser):
    """Estrae solo metadati non sensibili da una pagina HTML di consenso."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.forms: list[dict[str, object]] = []
        self._form: dict[str, object] | None = None
        self._button_depth = 0
        self._button_text: list[str] = []
        self.button_keywords: set[str] = set()
        self._text_tag: str | None = None
        self._title: list[str] = []
        self._headings: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag == "form":
            action = values.get("action") or ""
            self._form = {
                "method": (values.get("method") or "get").upper(),
                "action_path": urlsplit(action).path or "(current page)",
                "fields": [],
            }
            self.forms.append(self._form)
        elif tag == "input" and self._form is not None:
            name = values.get("name")
            if name:
                fields = self._form["fields"]
                assert isinstance(fields, list)
                fields.append({"name": name[:80], "type": (values.get("type") or "text")[:24]})
        elif tag == "button":
            self._button_depth += 1
            self._button_text = []
        elif tag == "title":
            self._text_tag = "title"
        elif tag in ("h1", "h2"):
            self._text_tag = tag

    def handle_endtag(self, tag: str) -> None:
        if tag == "form":
            self._form = None
        elif tag in ("title", "h1", "h2"):
            self._text_tag = None
        elif tag == "button" and self._button_depth:
            text = " ".join("".join(self._button_text).casefold().split())
            for keyword in (
                "consent",
                "accept",
                "authorize",
                "continue",
                "allow",
                "deny",
                "consenti",
                "accetta",
                "autorizza",
                "continua",
                "rifiuta",
                "nega",
            ):
                if keyword in text:
                    self.button_keywords.add(keyword)
            self._button_depth -= 1
            self._button_text = []

    def handle_data(self, data: str) -> None:
        if self._button_depth:
            self._button_text.append(data[:120])
        if self._text_tag == "title":
            self._title.append(data[:200])
        elif self._text_tag in ("h1", "h2"):
            self._headings.append(data[:200])

    @staticmethod
    def classify_page_text(value: str) -> str:
        text = " ".join(value.casefold().split())
        categories = (
            ("access_denied", ("access denied", "accesso negato", "forbidden", "unauthorized")),
            ("rate_limited", ("too many requests", "rate limit", "temporarily blocked")),
            ("service_error", ("internal server error", "service unavailable", "errore interno")),
            ("invalid_request", ("invalid request", "richiesta non valida", "invalid_client", "invalid_grant")),
            ("login_rejected", ("login failed", "accesso non riuscito", "credenziali non valide")),
            ("consent", ("consent", "consenso")),
            ("login_page", ("login", "accedi", "accesso")),
            ("generic_error", ("error", "errore")),
        )
        for category, markers in categories:
            if any(marker in text for marker in markers):
                return category
        return "unclassified"

    def page_categories(self) -> dict[str, object]:
        return {
            "title": self.classify_page_text(" ".join(self._title)),
            "headings": sorted({self.classify_page_text(text) for text in self._headings}),
        }


def _safe_consent_metadata(response: httpx.Response) -> dict[str, object]:
    parser = ConsentMetadataParser()
    try:
        parser.feed(response.text)
        html = response.text[:200_000].casefold()
    except Exception:
        html = ""
    return {
        "event": "didup_oauth_consent_page",
        "status": response.status_code,
        "content_type": response.headers.get("content-type", "").split(";", 1)[0],
        "body_bytes": len(response.content),
        "location_present": bool(response.headers.get("location")),
        "page_markers": {
            key: key in html for key in ("consent", "login", "captcha", "otp", "error")
        },
        "forms": parser.forms[:8],
        "button_keywords": sorted(parser.button_keywords),
        "page_categories": parser.page_categories(),
    }


def main() -> int:
    # httpx logga URL con query OAuth (state/nonce/verifier): qui li silenziamo.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    required = ("DIDUP_SCHOOL", "DIDUP_USER", "DIDUP_PASSWORD")
    if not all(os.environ.get(key) for key in required):
        print(json.dumps({"event": "didup_oauth_diagnostic", "ok": False, "reason": "missing_credentials"}))
        return 2

    original_request = httpx.AsyncClient.request

    async def inspect_request(
        self: httpx.AsyncClient,
        method: str,
        url: object,
        *args: object,
        **kwargs: object,
    ) -> httpx.Response:
        response = await original_request(self, method, url, *args, **kwargs)
        parsed = urlsplit(str(url))
        is_portal = parsed.hostname and parsed.hostname.lower().endswith("portaleargo.it")
        is_consent_page = "/auth/sso/consent" in parsed.path
        if is_portal and is_consent_page and response.status_code == 200:
            print(json.dumps(_safe_consent_metadata(response), ensure_ascii=False, sort_keys=True), flush=True)
        return response

    httpx.AsyncClient.request = inspect_request  # type: ignore[method-assign]
    client: DiDUPClientSync | None = None
    try:
        client = DiDUPClientSync(
            os.environ["DIDUP_SCHOOL"],
            os.environ["DIDUP_USER"],
            os.environ["DIDUP_PASSWORD"],
        )
        client.login()
        print(json.dumps({"event": "didup_oauth_diagnostic", "ok": True, "stage": "oauth_login_completed"}))
        return 0
    except Exception as exc:
        text = str(exc).casefold()
        if "redirect 2" in text:
            reason = "oauth_redirect_2_missing"
        elif "redirect 3" in text:
            reason = "oauth_consent_response_has_no_redirect"
        elif "redirect finale" in text or "redirect final" in text:
            reason = "oauth_final_redirect_missing"
        elif "credenziali" in text or "login sso" in text:
            reason = "sso_login_rejected_or_flow_changed"
        else:
            reason = "oauth_login_failed"
        code = getattr(exc, "status_code", None)
        result: dict[str, object] = {
            "event": "didup_oauth_diagnostic",
            "ok": False,
            "reason": reason,
            "error_type": type(exc).__name__,
        }
        if isinstance(code, int):
            result["status_code"] = code
        print(json.dumps(result, ensure_ascii=False, sort_keys=True), file=sys.stderr, flush=True)
        return 1
    finally:
        if client is not None:
            try:
                client.close()
            except Exception:
                pass
        httpx.AsyncClient.request = original_request  # type: ignore[method-assign]


if __name__ == "__main__":
    raise SystemExit(main())
