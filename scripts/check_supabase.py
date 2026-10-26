#!/usr/bin/env python3
"""Read-only Supabase preflight for GitHub Actions; never prints secrets or hostnames."""
from __future__ import annotations

import os
import json
import re
import sys
from pathlib import Path
from urllib.parse import urlsplit

import requests

raw_url = os.environ.get("SUPABASE_URL", "").strip().rstrip("/")
service_key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
bucket_name = os.environ.get("SUPABASE_STORAGE_BUCKET", "circulars")

if not raw_url:
    print("supabase_url_present=false")
    raise SystemExit(2)

candidate = raw_url if urlsplit(raw_url).scheme else f"https://{raw_url}"
parsed = urlsplit(candidate)
if parsed.scheme == "https" and re.fullmatch(r"[a-z0-9]{20}", parsed.netloc.lower()):
    candidate = f"https://{parsed.netloc.lower()}.supabase.co"
parsed = urlsplit(candidate)
if parsed.scheme != "https" or not parsed.hostname:
    print("supabase_url_valid_https=false")
    raise SystemExit(2)
print("supabase_url_valid_https=true")

if not service_key:
    print("service_role_key_present=false")
    raise SystemExit(2)
print("service_role_key_present=true")
if service_key.startswith("sb_secret_"):
    print("service_role_key_format=new_secret")
elif service_key.count(".") == 2:
    print("service_role_key_format=legacy_jwt")
else:
    print("service_role_key_format=unrecognized")

auth_headers = {"apikey": service_key}
if service_key.count(".") == 2 and not service_key.startswith(("sb_secret_", "sb_publishable_")):
    auth_headers["Authorization"] = f"Bearer {service_key}"

try:
    response = requests.get(
        f"{candidate.rstrip('/')}/storage/v1/bucket",
        headers=auth_headers,
        timeout=15,
    )
except requests.RequestException as exc:
    print("storage_api_reachable=false", "error_type=" + type(exc).__name__)
    raise SystemExit(3)

print("storage_api_reachable=true", "http_status=" + str(response.status_code))
if response.status_code != 200:
    try:
        error_text = response.text.lower()
    except Exception:
        error_text = ""
    if "jwt" in error_text or "token" in error_text or "authorization" in error_text:
        category = "auth_token_rejected"
    elif "api key" in error_text or "apikey" in error_text:
        category = "api_key_rejected"
    elif "bucket" in error_text:
        category = "bucket_request_error"
    elif response.status_code == 400:
        category = "bad_request"
    else:
        category = "storage_http_error"
    print("storage_error_category=" + category)
    raise SystemExit(4)

try:
    buckets = response.json()
except ValueError:
    print("bucket_list_valid_json=false")
    raise SystemExit(5)
if not isinstance(buckets, list):
    print("bucket_list_valid_json=false")
    raise SystemExit(5)

bucket = next((item for item in buckets if item.get("name") == bucket_name), None)
print("circular_bucket_exists=" + str(bucket is not None).lower())
if bucket is None:
    raise SystemExit(6)
private = bucket.get("public") is False
print("circular_bucket_private=" + str(private).lower())
if not private:
    raise SystemExit(7)

try:
    grades = json.loads(Path("grades.json").read_text(encoding="utf-8"))
except (OSError, json.JSONDecodeError):
    print("internal_pdf_metadata=unavailable")
    raise SystemExit(8)
notifications = grades.get("notifications") if isinstance(grades, dict) else None
paths = [
    item.get("attachmentPath")
    for item in notifications or []
    if isinstance(item, dict)
    and isinstance(item.get("attachmentPath"), str)
    and re.fullmatch(r"circulars/[a-f0-9]{64}\.pdf", item["attachmentPath"])
]
print("internal_pdf_path_present=" + str(bool(paths)).lower())
if not paths:
    raise SystemExit(9)

try:
    with requests.get(
        f"{candidate.rstrip('/')}/storage/v1/object/{bucket_name}/{paths[0]}",
        headers=auth_headers,
        timeout=20,
        stream=True,
    ) as pdf_response:
        print("private_pdf_http_status=" + str(pdf_response.status_code))
        if pdf_response.status_code != 200:
            raise SystemExit(10)
        signature_valid = pdf_response.raw.read(5) == b"%PDF-"
        print("private_pdf_signature_valid=" + str(signature_valid).lower())
        if not signature_valid:
            raise SystemExit(11)
except requests.RequestException as exc:
    print("private_pdf_request_failed error_type=" + type(exc).__name__)
    raise SystemExit(12)
print("supabase_preflight=passed")
