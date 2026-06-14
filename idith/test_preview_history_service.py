"""Test unitari per preview_history_service (detection, formattazione, timezone)."""
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from preview_history_service import (
    format_created_at_rome,
    format_preview_history,
    format_pnl_pct,
    is_preview_history_request,
)


def test_history_detection_positive():
    assert is_preview_history_request("storico preview")
    assert is_preview_history_request("mostra preview")
    assert is_preview_history_request("ultime preview")
    assert is_preview_history_request("cronologia preview")
    assert is_preview_history_request("mie preview")
    assert is_preview_history_request("le mie preview")
    assert is_preview_history_request("mostra le preview")
    assert is_preview_history_request("mostrami le preview")
    assert is_preview_history_request("Mostrami le mie preview salvate")


def test_history_detection_negative():
    assert not is_preview_history_request("fammi una preview")
    assert not is_preview_history_request("preview ultimi 30 giorni")
    assert not is_preview_history_request("backtest")
    assert not is_preview_history_request("")
    assert not is_preview_history_request("simula")


def test_format_pnl_pct():
    assert format_pnl_pct(8.4) == "+8.4%"
    assert format_pnl_pct(-20.0) == "-20.0%"
    assert format_pnl_pct(0) == "+0.0%"
    assert format_pnl_pct(None) == "N/D"


def test_format_created_at_rome():
    # 2026-06-06 21:41 UTC → 23:41 Europe/Rome (CEST, UTC+2)
    iso = "2026-06-06T21:41:00+00:00"
    assert format_created_at_rome(iso) == "06/06/2026 23:41"

    dt_utc = datetime(2026, 6, 6, 21, 41, tzinfo=timezone.utc)
    assert format_created_at_rome(dt_utc) == "06/06/2026 23:41"

    # Inverno: UTC+1
    iso_winter = "2026-01-15T10:30:00Z"
    expected = (
        datetime.fromisoformat("2026-01-15T10:30:00+00:00")
        .astimezone(ZoneInfo("Europe/Rome"))
        .strftime("%d/%m/%Y %H:%M")
    )
    assert format_created_at_rome(iso_winter) == expected


def test_format_preview_history_empty():
    assert format_preview_history([]) == "Non hai ancora generato nessuna preview."


def test_format_preview_history_multiple_rows():
    rows = [
        {
            "created_at": "2026-06-06T21:41:00+00:00",
            "symbol": "BTCUSDT",
            "timeframe": "3m",
            "operating_mode": "aggressiva",
            "pnl_pct": -20.0,
        },
        {
            "created_at": "2026-06-06T20:57:00+00:00",
            "symbol": "ETHUSDT",
            "timeframe": "15m",
            "operating_mode": "selettiva",
            "pnl_pct": 8.4,
        },
    ]
    text = format_preview_history(rows)
    assert "📊 Storico Preview" in text
    assert "06/06/2026 23:41 • BTCUSDT • 3m • aggressiva" in text
    assert "Risultato: -20.0%" in text
    assert "06/06/2026 22:57 • ETHUSDT • 15m • selettiva" in text
    assert "Risultato: +8.4%" in text
    assert "preview_payload" not in text
    assert "drawdown" not in text.lower()


if __name__ == "__main__":
    test_history_detection_positive()
    test_history_detection_negative()
    test_format_pnl_pct()
    test_format_created_at_rome()
    test_format_preview_history_empty()
    test_format_preview_history_multiple_rows()
    print("test_preview_history_service: OK")
