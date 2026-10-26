from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

import requests
from monitor import circular_is_recent, parse_date

BUCKET_DEFAULT = "circulars"
OBJECT_PATH_RE = re.compile(r"^circulars/[a-f0-9]{64}\.pdf$")
RECENT_WINDOW = timedelta(hours=1)


def supabase_url(raw: str) -> str:
    value = raw.strip().rstrip("/")
    if value.startswith("https://"):
        return value
    if value.startswith("http://"):
        raise ValueError("insecure_url")
    if re.fullmatch(r"[a-z0-9]{20}", value, re.IGNORECASE):
        return f"https://{value.lower()}.supabase.co"
    return f"https://{value}"


def auth_headers(key: str) -> dict[str, str]:
    headers = {"apikey": key, "content-type": "application/json"}
    if len(key.split(".")) == 3:
        headers["authorization"] = f"Bearer {key}"
    return headers


def parse_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def storage_objects(base_url: str, key: str, bucket: str) -> list[dict[str, object]]:
    response = requests.post(
        f"{base_url}/storage/v1/object/list/{bucket}",
        headers=auth_headers(key),
        json={
            "prefix": "circulars",
            "limit": 1000,
            "offset": 0,
            "sortBy": {"column": "created_at", "order": "desc"},
        },
        timeout=30,
    )
    if not response.ok:
        print(f"storage_list_http={response.status_code}")
        raise RuntimeError("storage_list_failed")
    payload = response.json()
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict) and isinstance(payload.get("data"), list):
        return [item for item in payload["data"] if isinstance(item, dict)]
    raise RuntimeError("storage_list_invalid_response")


def normalized_object_path(name: object) -> str | None:
    if not isinstance(name, str) or not name:
        return None
    path = name if name.startswith("circulars/") else f"circulars/{name.lstrip('/')}"
    return path if OBJECT_PATH_RE.fullmatch(path) else None


def main() -> int:
    raw_url = os.environ.get("SUPABASE_URL", "").strip()
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "").strip()
    bucket = os.environ.get("SUPABASE_STORAGE_BUCKET", BUCKET_DEFAULT).strip() or BUCKET_DEFAULT
    if not raw_url or not key:
        print("repair_status=missing_configuration")
        return 1
    try:
        base_url = supabase_url(raw_url)
        if urlsplit(base_url).scheme != "https":
            raise ValueError("insecure_url")
        objects = storage_objects(base_url, key, bucket)
    except Exception as exc:
        category = "insecure_url" if str(exc) == "insecure_url" else type(exc).__name__
        print(f"repair_status=storage_check_failed category={category}")
        return 1

    now = datetime.now(timezone.utc)
    recent_objects: list[str] = []
    for item in objects:
        path = normalized_object_path(item.get("name"))
        if not path:
            continue
        timestamps = [
            timestamp
            for timestamp in (
                parse_timestamp(item.get("created_at")),
                parse_timestamp(item.get("updated_at")),
            )
            if timestamp is not None
        ]
        if timestamps and timedelta(0) <= now - max(timestamps) <= RECENT_WINDOW:
            recent_objects.append(path)

    if len(recent_objects) != 1:
        print(f"repair_status=stopped recent_pdf_objects={len(recent_objects)}")
        return 1

    grades_path = Path("grades.json")
    try:
        data = json.loads(grades_path.read_text(encoding="utf-8"))
    except Exception:
        print("repair_status=stopped grades_json_invalid")
        return 1
    notifications = data.get("notifications") if isinstance(data, dict) else None
    if not isinstance(notifications, list):
        print("repair_status=stopped notifications_missing")
        return 1

    candidates = [
        item
        for item in notifications
        if isinstance(item, dict)
        and item.get("type") == "notice"
        and not item.get("attachmentPath")
        and item.get("attachmentUrl")
        and item.get("attachmentName")
        and circular_is_recent(item.get("date"))
    ]
    if not candidates:
        print("repair_status=stopped no_notice_candidate")
        return 1

    # The monitor picks the newest Bacheca date and preserves source-list order for ties.
    newest_date = max((parse_date(item.get("date")) or datetime.min.date()) for item in candidates)
    candidate = next(
        item
        for item in candidates
        if (parse_date(item.get("date")) or datetime.min.date()) == newest_date
    )
    candidate["attachmentPath"] = recent_objects[0]

    temporary = grades_path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    temporary.replace(grades_path)
    print("repair_status=applied recent_pdf_objects=1 candidate_selected=1")
    return 0


if __name__ == "__main__":
    sys.exit(main())
