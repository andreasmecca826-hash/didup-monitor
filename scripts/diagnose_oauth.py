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
import asyncio
from http.cookies import SimpleCookie
from html.parser import HTMLParser
from urllib.parse import parse_qs, urlsplit

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


def _test_browser_login() -> dict[str, object]:
    """Verifica il login web, il callback PKCE e il login API in fasi separate."""
    browser = None
    stage = "browser_import"
    code: str | None = None
    authenticator = None
    link = None
    page_info: dict[str, object] = {}
    try:
        from playwright.sync_api import sync_playwright
        from didupwrapper import DiDUPClientSync
        from didupwrapper.auth import Autenticatore, Credenziali

        with sync_playwright() as playwright:
            stage = "portal_web_login"
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page()
            callback_urls: list[str] = []
            callback_uri = "it.argosoft.didup.famiglia.new://login-callback"

            def capture_callback(request: object) -> None:
                request_url = getattr(request, "url", "")
                if isinstance(request_url, str) and request_url.startswith(callback_uri):
                    callback_urls.append(request_url)

            page.on("request", capture_callback)
            page.goto(
                "https://www.portaleargo.it/famiglia",
                wait_until="domcontentloaded",
                timeout=45_000,
            )
            page.wait_for_selector("#codiceScuola", timeout=30_000)
            page.locator("#codiceScuola").fill(os.environ["DIDUP_SCHOOL"])
            page.locator("#username").fill(os.environ["DIDUP_USER"])
            page.locator("#password").fill(os.environ["DIDUP_PASSWORD"])
            page.locator("#accediBtn").click()
            page.wait_for_timeout(5_000)
            password_field = page.locator("#password")
            if password_field.count() and password_field.is_visible():
                return {"ok": False, "stage": stage, "reason": "web_login_not_completed"}

            stage = "browser_oauth"
            authenticator = Autenticatore(
                Credenziali(
                    os.environ["DIDUP_SCHOOL"],
                    os.environ["DIDUP_USER"],
                    os.environ["DIDUP_PASSWORD"],
                )
            )
            link = authenticator._genera_login_link()
            try:
                page.goto(link.url, wait_until="domcontentloaded", timeout=45_000)
            except Exception:
                # Il callback mobile può causare ERR_UNKNOWN_URL_SCHEME in Chromium.
                pass
            page.wait_for_timeout(3_000)

            if page.locator("#codiceScuola").count():
                stage = "browser_oauth_relogin"
                page.locator("#codiceScuola").fill(os.environ["DIDUP_SCHOOL"])
                page.locator("#username").fill(os.environ["DIDUP_USER"])
                page.locator("#password").fill(os.environ["DIDUP_PASSWORD"])
                page.locator("#accediBtn").click()
                page.wait_for_timeout(5_000)

            candidate_urls = callback_urls + [page.url]
            for candidate in candidate_urls:
                parsed = urlsplit(candidate)
                if parsed.scheme == "it.argosoft.didup.famiglia.new":
                    code = parse_qs(parsed.query).get("code", [None])[0]
                    if code:
                        break

            if not code:
                page_parser = ConsentMetadataParser()
                try:
                    page_parser.feed(page.content())
                except Exception:
                    pass
                page_info = {
                    "oauth_browser_callback": False,
                    "oauth_page_categories": page_parser.page_categories(),
                    "oauth_forms": page_parser.forms[:8],
                    "oauth_button_keywords": sorted(page_parser.button_keywords),
                }

        if not code:
            return {"ok": True, "stage": "portal_web_login", **page_info}

        stage = "oauth_token_exchange"

        async def exchange_code() -> str:
            async with httpx.AsyncClient(follow_redirects=False, timeout=30.0) as http:
                token = await authenticator._ottieni_token(http, link, code)
                return token.access_token

        access_token = asyncio.run(exchange_code())
        stage = "didup_api_login"
        api_client = DiDUPClientSync(
            os.environ["DIDUP_SCHOOL"],
            os.environ["DIDUP_USER"],
            os.environ["DIDUP_PASSWORD"],
            access_token=access_token,
            auto_versione=True,
        )
        try:
            api_client.login()
        finally:
            api_client.close()
        return {
            "ok": True,
            "stage": "didup_api_login",
            "oauth_browser_callback": True,
            "token_exchange": True,
        }
    except Exception as exc:
        reason = "timeout" if "timeout" in type(exc).__name__.casefold() else "stage_failed"
        return {
            "ok": False,
            "stage": stage,
            "reason": reason,
            "error_type": type(exc).__name__,
        }
    finally:
        if browser is not None:
            try:
                browser.close()
            except Exception:
                pass


def main() -> int:
    # httpx logga URL con query OAuth (state/nonce/verifier): qui li silenziamo.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    required = ("DIDUP_SCHOOL", "DIDUP_USER", "DIDUP_PASSWORD")
    if not all(os.environ.get(key) for key in required):
        print(json.dumps({"event": "didup_oauth_diagnostic", "ok": False, "reason": "missing_credentials"}))
        return 2

    original_request = httpx.AsyncClient.request
    session_cookies: dict[str, str] = {}

    def remember_cookies(response: httpx.Response) -> None:
        for header in response.headers.get_list("set-cookie"):
            parsed_cookies = SimpleCookie()
            try:
                parsed_cookies.load(header)
            except Exception:
                continue
            for name, morsel in parsed_cookies.items():
                session_cookies[name] = morsel.value

    async def inspect_request(
        self: httpx.AsyncClient,
        method: str,
        url: object,
        *args: object,
        **kwargs: object,
    ) -> httpx.Response:
        response = await original_request(self, method, url, *args, **kwargs)
        remember_cookies(response)
        parsed = urlsplit(str(url))
        is_portal = parsed.hostname and parsed.hostname.lower().endswith("portaleargo.it")
        is_consent_page = "/auth/sso/consent" in parsed.path
        if is_portal and is_consent_page and response.status_code == 200:
            cookie_header = "; ".join(f"{name}={value}" for name, value in session_cookies.items())
            if cookie_header:
                retry_kwargs = dict(kwargs)
                retry_headers = dict(retry_kwargs.get("headers") or {})
                retry_headers["cookie"] = cookie_header
                retry_kwargs["headers"] = retry_headers
                retry = await original_request(self, method, url, *args, **retry_kwargs)
                remember_cookies(retry)
                metadata = _safe_consent_metadata(retry)
                metadata["retry_with_session_cookies"] = True
                metadata["cookie_names"] = sorted(session_cookies)
                print(json.dumps(metadata, ensure_ascii=False, sort_keys=True), flush=True)
                if retry.status_code in (301, 302, 303, 307, 308) and retry.headers.get("location"):
                    return retry
            else:
                metadata = _safe_consent_metadata(response)
                metadata["retry_with_session_cookies"] = False
                metadata["cookie_names"] = []
                print(json.dumps(metadata, ensure_ascii=False, sort_keys=True), flush=True)
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
            "browser_portal_login": _test_browser_login(),
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
