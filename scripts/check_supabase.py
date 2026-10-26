#!/usr/bin/env python3
"""Read-only Supabase preflight for GitHub Actions; never prints secrets or hostnames."""
from __future__ import annotations

import os
import re
import sys
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

try:
    response = requests.get(
        f"{candidate.rstrip('/')}/storage/v1/bucket",
        headers={"apikey": service_key, "Authorization": f"Bearer {service_key}"},
        timeout=15,
    )
except requests.RequestException as exc:
    print("storage_api_reachable=false", "error_type=" + type(exc).__name__)
    raise SystemExit(3)

print("storage_api_reachable=true", "http_status=" + str(response.status_code))
if response.status_code != 200:
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
print("supabase_preflight=passed")
