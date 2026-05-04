# -*- coding: utf-8 -*-
"""
Test validate_symbol con Bybit mockato (403 / success) e cache su path temporaneo.
"""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(__file__))

import validators  # noqa: E402


def _http_error_403() -> validators.error.HTTPError:
    body = b'{"message":"Forbidden"}'
    fp = io.BytesIO(body)
    return validators.error.HTTPError(
        "https://api.bybit.com/v5/market/instruments-info",
        403,
        "Forbidden",
        {},
        fp,
    )


def _mock_urlopen_403(_req, timeout=None):
    raise _http_error_403()


def _mock_urlopen_ok_spot(_req, timeout=None):
    payload = {
        "retCode": 0,
        "retMsg": "OK",
        "result": {
            "list": [
                {"symbol": "BTCUSDT", "status": "Trading"},
                {"symbol": "SOLUSDT", "status": "Trading"},
            ],
            "nextPageCursor": "",
        },
    }
    raw = json.dumps(payload).encode("utf-8")
    resp = MagicMock()
    resp.status = 200
    resp.getcode = lambda: 200
    resp.read = lambda: raw
    resp.__enter__ = lambda s: s
    resp.__exit__ = lambda *a: None
    return resp


def _mock_urlopen_ok_futures(_req, timeout=None):
    payload = {
        "retCode": 0,
        "retMsg": "OK",
        "result": {
            "list": [
                {"symbol": "BTCUSDT", "status": "Trading"},
                {"symbol": "SOLUSDT", "status": "Trading"},
            ],
            "nextPageCursor": "",
        },
    }
    raw = json.dumps(payload).encode("utf-8")
    resp = MagicMock()
    resp.status = 200
    resp.getcode = lambda: 200
    resp.read = lambda: raw
    resp.__enter__ = lambda s: s
    resp.__exit__ = lambda *a: None
    return resp


class TestValidateSymbolOnline(unittest.TestCase):
    def setUp(self):
        validators.clear_cache()
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self._cache_path = Path(self._td.name) / "bybit_symbols_cache.json"
        self._path_patcher = patch.object(
            validators, "_SYMBOLS_CACHE_PATH", self._cache_path
        )
        self._path_patcher.start()
        self.addCleanup(self._path_patcher.stop)

    def test_bybit_403_uses_whitelist_spot_and_futures(self):
        with patch.object(validators.request, "urlopen", side_effect=_mock_urlopen_403):
            ok, err = validators.validate_symbol("BTCUSDT", "spot")
            self.assertTrue(ok, err)
            self.assertIsNone(err)

            ok, err = validators.validate_symbol("SOLUSDT", "spot")
            self.assertTrue(ok, err)

            ok, err = validators.validate_symbol("AAAUSDT", "spot")
            self.assertFalse(ok)
            self.assertEqual(err, validators._INVALID_PAIR_USER_MSG)

            ok, err = validators.validate_symbol("MATICUSDT", "spot")
            self.assertFalse(ok)
            self.assertEqual(err, validators._INVALID_PAIR_USER_MSG)

            validators.clear_cache()
            ok, err = validators.validate_symbol("BTCUSDT", "futures")
            self.assertTrue(ok, err)

            validators.clear_cache()
            ok, err = validators.validate_symbol("AAAUSDT", "futures")
            self.assertFalse(ok)
            self.assertEqual(err, validators._INVALID_PAIR_USER_MSG)

    def test_bybit_ok_list_membership(self):
        def urlopen_dispatch(req, timeout=None):
            url = getattr(req, "full_url", "") or ""
            if "category=spot" in url:
                return _mock_urlopen_ok_spot(req, timeout)
            if "category=linear" in url:
                return _mock_urlopen_ok_futures(req, timeout)
            raise AssertionError(f"unexpected url {url!r}")

        with patch.object(validators.request, "urlopen", side_effect=urlopen_dispatch):
            validators.clear_cache()
            ok, err = validators.validate_symbol("BTCUSDT", "spot")
            self.assertTrue(ok, err)
            ok, err = validators.validate_symbol("AAAUSDT", "spot")
            self.assertFalse(ok)
            self.assertEqual(err, validators._INVALID_PAIR_USER_MSG)

            validators.clear_cache()
            ok, err = validators.validate_symbol("BTCUSDT", "futures")
            self.assertTrue(ok, err)
            ok, err = validators.validate_symbol("AAAUSDT", "futures")
            self.assertFalse(ok)
            self.assertEqual(err, validators._INVALID_PAIR_USER_MSG)


if __name__ == "__main__":
    unittest.main()
