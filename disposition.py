from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import json
import os
import re
import tempfile
from typing import Iterable

import requests


TWSE_DISPOSITION_URL = "https://www.twse.com.tw/rwd/zh/announcement/punish?response=json"
TPEX_DISPOSITION_URL = "https://www.tpex.org.tw/www/zh-tw/bulletin/disposal?response=json"
OFFICIAL_SOURCES = {"twse", "tpex"}
TW_TZ = timezone(timedelta(hours=8))


def today_tw() -> date:
    return datetime.now(TW_TZ).date()


def parse_roc_date(value: str) -> date:
    match = re.search(r"(\d{2,3})/(\d{1,2})/(\d{1,2})", value)
    if not match:
        raise ValueError(f"invalid ROC date: {value}")
    year, month, day = (int(part) for part in match.groups())
    return date(year + 1911, month, day)


def parse_roc_date_range(value: str) -> tuple[date, date]:
    dates = re.findall(r"\d{2,3}/\d{1,2}/\d{1,2}", value or "")
    if len(dates) < 2:
        raise ValueError(f"invalid ROC date range: {value}")
    return parse_roc_date(dates[0]), parse_roc_date(dates[1])


def _table_rows(payload: dict) -> Iterable[tuple[list[str], list]]:
    fields = payload.get("fields")
    rows = payload.get("data")
    if fields and rows:
        yield fields, rows

    for table in payload.get("tables", []):
        fields = table.get("fields")
        rows = table.get("data")
        if fields and rows:
            yield fields, rows


def _clean_name(name: str) -> str:
    return re.sub(r"\(.*", "", str(name or "")).strip()


def records_from_payload(payload: dict, source: str, as_of: date) -> dict[str, dict]:
    records: dict[str, dict] = {}
    for fields, rows in _table_rows(payload):
        idx = {field: pos for pos, field in enumerate(fields)}
        symbol_i = idx.get("證券代號")
        name_i = idx.get("證券名稱")
        range_i = idx.get("處置起迄時間", idx.get("處置起訖時間"))
        if symbol_i is None or range_i is None:
            continue

        for row in rows:
            symbol = str(row[symbol_i]).strip()
            if not symbol:
                continue
            try:
                start_on, expires_on = parse_roc_date_range(str(row[range_i]))
            except ValueError:
                continue
            if not (start_on <= as_of <= expires_on):
                continue

            name = _clean_name(row[name_i]) if name_i is not None else ""
            records[symbol] = {
                "reason": "處置股，排除不能現沖標的",
                "source": source,
                "name": name,
                "start_on": start_on.isoformat(),
                "expires_on": expires_on.isoformat(),
            }
    return records


def fetch_official_dispositions(timeout: float = 8.0, as_of: date | None = None) -> dict[str, dict]:
    as_of = as_of or today_tw()
    records: dict[str, dict] = {}
    for source, url in (("twse", TWSE_DISPOSITION_URL), ("tpex", TPEX_DISPOSITION_URL)):
        response = requests.get(url, timeout=timeout)
        response.raise_for_status()
        records.update(records_from_payload(response.json(), source=source, as_of=as_of))
    return records


def _load_json(path: str) -> dict:
    try:
        with open(path) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}


def _atomic_write_json(path: str, data: dict) -> None:
    dir_name = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(dir=dir_name, prefix=".daytrade_exclusions_")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.write("\n")
        os.replace(tmp, path)
    except Exception:
        os.unlink(tmp)
        raise


def refresh_daytrade_exclusions(path: str, timeout: float = 8.0, as_of: date | None = None) -> dict:
    as_of = as_of or today_tw()
    existing = _load_json(path)
    existing_symbols = existing.get("symbols", {})
    if isinstance(existing_symbols, list):
        existing_symbols = {str(sym): {"reason": "daytrade_excluded"} for sym in existing_symbols}

    official = fetch_official_dispositions(timeout=timeout, as_of=as_of)
    merged = {
        str(symbol).strip(): meta
        for symbol, meta in existing_symbols.items()
        if str(symbol).strip() and not (isinstance(meta, dict) and meta.get("source") in OFFICIAL_SOURCES)
    }
    merged.update(official)

    output = {
        **existing,
        "updated_at": datetime.now(TW_TZ).isoformat(timespec="seconds"),
        "official_sources": [TWSE_DISPOSITION_URL, TPEX_DISPOSITION_URL],
        "symbols": dict(sorted(merged.items())),
    }
    _atomic_write_json(path, output)
    return output


if __name__ == "__main__":
    target = os.getenv("DAYTRADE_EXCLUDE_FILE", "daytrade_exclusions.json")
    data = refresh_daytrade_exclusions(target)
    print(f"updated {target}: {len(data.get('symbols', {}))} exclusions")
