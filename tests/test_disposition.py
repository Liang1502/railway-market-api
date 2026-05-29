from datetime import date
import json

import disposition


def test_parse_roc_date_range_accepts_twse_and_tpex_separators():
    assert disposition.parse_roc_date_range("115/05/20～115/06/02") == (
        date(2026, 5, 20),
        date(2026, 6, 2),
    )
    assert disposition.parse_roc_date_range("115/05/21~115/06/03") == (
        date(2026, 5, 21),
        date(2026, 6, 3),
    )


def test_records_from_payload_keeps_only_active_symbols_and_skips_empty_rows():
    payload = {
        "fields": ["證券代號", "證券名稱", "處置起迄時間"],
        "data": [
            ["3665", "貿聯-KY", "115/05/20～115/06/02"],
            ["", "本日無處置資料", ""],
            ["2454", "聯發科", "115/05/01～115/05/20"],
        ],
    }

    records = disposition.records_from_payload(payload, source="twse", as_of=date(2026, 5, 22))

    assert list(records) == ["3665"]
    assert records["3665"]["expires_on"] == "2026-06-02"
    assert records["3665"]["reason"] == "處置股，排除不能現沖標的"


def test_refresh_daytrade_exclusions_preserves_manual_entries_and_replaces_old_official(
    tmp_path, monkeypatch
):
    path = tmp_path / "daytrade_exclusions.json"
    path.write_text(
        json.dumps(
            {
                "symbols": {
                    "3498": {"reason": "manual"},
                    "9999": {"source": "twse", "expires_on": "2026-05-21"},
                }
            }
        )
    )

    monkeypatch.setattr(
        disposition,
        "fetch_official_dispositions",
        lambda timeout, as_of: {
            "3665": {
                "reason": "處置股，排除不能現沖標的",
                "source": "twse",
                "expires_on": "2026-06-02",
            }
        },
    )

    result = disposition.refresh_daytrade_exclusions(str(path), as_of=date(2026, 5, 22))

    assert set(result["symbols"]) == {"3498", "3665"}
    assert result["symbols"]["3498"]["reason"] == "manual"
    assert result["symbols"]["3665"]["source"] == "twse"
