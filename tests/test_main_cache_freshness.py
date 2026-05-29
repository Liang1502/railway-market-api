from datetime import datetime, timedelta
import asyncio
import json
import os

import pytest

import main


@pytest.fixture(autouse=True)
def clean_cache(monkeypatch):
    monkeypatch.setattr(main, "_refresh_daytrade_exclusions_if_needed", lambda: None)
    main.market_data.clear()
    main.wishlist.clear()
    main.watch_set.clear()
    yield
    main.market_data.clear()
    main.wishlist.clear()
    main.watch_set.clear()


async def _no_sleep(_seconds):
    return None


def _payload(symbol="2330", seconds_old=0, price=100.0):
    return {
        "symbol": symbol,
        "current_price": price,
        "score_price": price,
        "_server_ts": (datetime.utcnow() - timedelta(seconds=seconds_old)).isoformat(),
    }


def test_is_stale_false_for_fresh_payload():
    assert main.is_stale(_payload(seconds_old=main.STALE_SECS - 1)) is False


def test_is_stale_true_for_old_payload():
    assert main.is_stale(_payload(seconds_old=main.STALE_SECS + 1)) is True


def test_single_symbol_stale_cache_triggers_refresh_and_marks_response(monkeypatch):
    monkeypatch.setattr(main.asyncio, "sleep", _no_sleep)
    main.market_data["2330"] = _payload(seconds_old=main.STALE_SECS + 10, price=100.0)

    result = asyncio.run(main.get_analysis("2330"))

    assert "2330" in main.wishlist
    assert result["_stale"] is True
    assert result["_age_secs"] >= main.STALE_SECS
    assert result["current_price"] == 100.0


def test_batch_waits_on_stale_symbols_instead_of_returning_as_fresh(monkeypatch):
    monkeypatch.setattr(main.asyncio, "sleep", _no_sleep)
    main.market_data["2330"] = _payload("2330", seconds_old=main.STALE_SECS + 10, price=100.0)
    main.market_data["2454"] = _payload("2454", seconds_old=1, price=200.0)

    result = asyncio.run(main.get_analysis_batch("2330,2454"))

    assert result["2330"]["_stale"] is True
    assert result["2454"].get("_stale") is None
    assert "2330" in main.wishlist


def test_scan_market_sorts_candidates_by_v6_score_first():
    main.market_data["A"] = _payload("A") | {
        "symbol": "A",
        "decision": "long_possible",
        "score": 90,
        "v6_score": 10,
        "entry_signal": {"long_trigger": False, "short_trigger": False},
    }
    main.market_data["B"] = _payload("B") | {
        "symbol": "B",
        "decision": "long_possible",
        "score": 60,
        "v6_score": 80,
        "entry_signal": {"long_trigger": False, "short_trigger": False},
    }
    main.market_data["C"] = _payload("C") | {
        "symbol": "C",
        "decision": "short_possible",
        "score": 20,
        "v6_score": -50,
        "structure": {"pressure_ratio": 1.3},
        "entry_signal": {"long_trigger": False, "short_trigger": False},
    }
    main.market_data["D"] = _payload("D") | {
        "symbol": "D",
        "decision": "short_possible",
        "score": 5,
        "v6_score": -10,
        "entry_signal": {"long_trigger": False, "short_trigger": False},
    }

    result = main.scan_market()

    assert result["top_long"][0]["symbol"] == "B"
    assert result["top_short"][0]["symbol"] == "C"
    assert [item["symbol"] for item in result["top_short"]] == ["C"]


def test_scan_market_applies_spec_candidate_gates():
    main.market_data["SHORT_OK"] = _payload("SHORT_OK") | {
        "symbol": "SHORT_OK",
        "decision": "short_possible",
        "score": 20,
        "v6_score": -30,
        "structure": {"pressure_ratio": 1.3},
        "entry_signal": {"long_trigger": False, "short_trigger": True},
    }
    main.market_data["SHORT_WEAK_V6"] = _payload("SHORT_WEAK_V6") | {
        "symbol": "SHORT_WEAK_V6",
        "decision": "short_possible",
        "score": 20,
        "v6_score": -10,
        "structure": {"pressure_ratio": 1.3},
        "entry_signal": {"long_trigger": False, "short_trigger": True},
    }
    main.market_data["SHORT_WEAK_PRESSURE"] = _payload("SHORT_WEAK_PRESSURE") | {
        "symbol": "SHORT_WEAK_PRESSURE",
        "decision": "short_possible",
        "score": 20,
        "v6_score": -30,
        "structure": {"pressure_ratio": 1.1},
        "entry_signal": {"long_trigger": False, "short_trigger": True},
    }
    main.market_data["LONG_OK"] = _payload("LONG_OK") | {
        "symbol": "LONG_OK",
        "decision": "long_possible",
        "score": 80,
        "v6_score": 80,
        "price": {"change_percent": 6.4},
        "entry_signal": {"long_trigger": True, "short_trigger": False},
    }
    main.market_data["LONG_FOMO"] = _payload("LONG_FOMO") | {
        "symbol": "LONG_FOMO",
        "decision": "long_possible",
        "score": 90,
        "v6_score": 90,
        "price": {"change_percent": 6.5},
        "entry_signal": {"long_trigger": True, "short_trigger": False},
    }

    result = main.scan_market()

    assert [item["symbol"] for item in result["top_short"]] == ["SHORT_OK"]
    assert [item["symbol"] for item in result["triggered_short"]] == ["SHORT_OK"]
    assert [item["symbol"] for item in result["top_long"]] == ["LONG_OK"]
    assert [item["symbol"] for item in result["triggered_long"]] == ["LONG_OK"]


@pytest.fixture()
def tmp_daytrade_exclude_file(tmp_path, monkeypatch):
    path = str(tmp_path / "daytrade_exclusions.json")
    monkeypatch.setattr(main, "DAYTRADE_EXCLUDE_FILE", path)
    return path


def test_scan_market_excludes_daytrade_blocked_symbols(tmp_daytrade_exclude_file):
    with open(tmp_daytrade_exclude_file, "w") as f:
        json.dump({"symbols": {"2454": {"reason": "處置股"}}}, f)

    main.market_data["2454"] = _payload("2454") | {
        "symbol": "2454",
        "decision": "long_possible",
        "score": 90,
        "v6_score": 100,
        "entry_signal": {"long_trigger": True, "short_trigger": False},
    }
    main.market_data["2330"] = _payload("2330") | {
        "symbol": "2330",
        "decision": "long_possible",
        "score": 80,
        "v6_score": 80,
        "entry_signal": {"long_trigger": False, "short_trigger": False},
    }

    result = main.scan_market()

    assert result["top_long"][0]["symbol"] == "2330"
    assert result["excluded_symbols"] == ["2454"]


def test_scan_market_ignores_expired_daytrade_exclusions(tmp_daytrade_exclude_file):
    with open(tmp_daytrade_exclude_file, "w") as f:
        json.dump({"symbols": {"2454": {"expires_on": "2000-01-01"}}}, f)

    main.market_data["2454"] = _payload("2454") | {
        "symbol": "2454",
        "decision": "long_possible",
        "score": 90,
        "v6_score": 100,
        "entry_signal": {"long_trigger": True, "short_trigger": False},
    }

    result = main.scan_market()

    assert result["top_long"][0]["symbol"] == "2454"
    assert result["excluded_symbols"] == []


def test_scan_market_returns_all_triggered_candidates_beyond_top_three():
    for idx, score in enumerate([100, 90, 80], start=1):
        symbol = f"L{idx}"
        main.market_data[symbol] = _payload(symbol) | {
            "symbol": symbol,
            "decision": "long_possible",
            "score": score,
            "v6_score": score,
            "entry_signal": {"long_trigger": False, "short_trigger": False},
        }
    main.market_data["TRIG"] = _payload("TRIG") | {
        "symbol": "TRIG",
        "decision": "observe",
        "score": 10,
        "v6_score": 10,
        "entry_signal": {"long_trigger": True, "short_trigger": False},
    }

    result = main.scan_market()

    assert [item["symbol"] for item in result["top_long"]] == ["L1", "L2", "L3"]
    assert [item["symbol"] for item in result["triggered_long"]] == ["TRIG"]
    assert result["triggered_long_count"] == 1


# =============================
# watch_set persistence
# =============================

@pytest.fixture()
def tmp_watch_file(tmp_path, monkeypatch):
    path = str(tmp_path / "watch_set.json")
    monkeypatch.setattr(main, "WATCH_SET_FILE", path)
    return path


def test_load_watch_set_returns_empty_when_file_missing(tmp_watch_file):
    result = main._load_watch_set()
    assert result == set()


def test_load_watch_set_returns_symbols_from_file(tmp_watch_file):
    with open(tmp_watch_file, "w") as f:
        json.dump({"symbols": ["2330", "2454"]}, f)
    result = main._load_watch_set()
    assert result == {"2330", "2454"}


def test_save_watch_set_writes_current_set(tmp_watch_file):
    main.watch_set.update({"2330", "2454"})
    asyncio.run(main._save_watch_set())
    with open(tmp_watch_file) as f:
        data = json.load(f)
    assert set(data["symbols"]) == {"2330", "2454"}


def test_batch_persists_new_symbol_to_file(monkeypatch, tmp_watch_file):
    monkeypatch.setattr(main.asyncio, "sleep", _no_sleep)
    main.market_data["2330"] = _payload("2330", seconds_old=1)

    asyncio.run(main.get_analysis_batch("2330"))

    assert os.path.exists(tmp_watch_file)
    with open(tmp_watch_file) as f:
        data = json.load(f)
    assert "2330" in data["symbols"]


def test_batch_skips_save_when_already_tracked(monkeypatch, tmp_watch_file):
    monkeypatch.setattr(main.asyncio, "sleep", _no_sleep)
    main.watch_set.add("2330")
    main.market_data["2330"] = _payload("2330", seconds_old=1)

    asyncio.run(main.get_analysis_batch("2330"))

    assert not os.path.exists(tmp_watch_file)


def test_remove_from_watch_list_persists_removal(tmp_watch_file):
    main.watch_set.update({"2330", "2454"})

    asyncio.run(main.remove_from_watch_list("2330", authorization=f"Bearer {main.MY_SECRET_TOKEN}"))

    with open(tmp_watch_file) as f:
        data = json.load(f)
    assert "2330" not in data["symbols"]
    assert "2454" in data["symbols"]


def test_remove_from_watch_list_skips_save_when_not_present(tmp_watch_file):
    main.watch_set.add("2454")

    asyncio.run(main.remove_from_watch_list("2330", authorization=f"Bearer {main.MY_SECRET_TOKEN}"))

    assert not os.path.exists(tmp_watch_file)
