"""The share price has two independent providers and one boundary.

2026-09-23: Yahoo returned "No data found" for every candidate at 06:30 and again
on the afternoon rerun, so the page-write guard (correctly) refused to write and
the site carried yesterday's page all day. Polygon priced all six the whole time.
These tests pin the fallback order and the "an unknown is never a number" rule
on both providers. Fixture-based: no network.
"""
import math

import pytest

from scraper import financials


class _Hist:
    def __init__(self, closes):
        import types
        self.empty = not closes
        self._closes = list(closes)
        self.__dict__["Close"] = types.SimpleNamespace(tolist=lambda: list(self._closes))

    def __getitem__(self, k):
        assert k == "Close"
        return self.__dict__["Close"]


class _YF:
    def __init__(self, closes):
        self._closes = closes

    def history(self, period):
        return _Hist(self._closes)


class _Resp:
    def __init__(self, status, payload=None):
        self.status_code = status
        self._payload = payload or {}

    def json(self):
        return self._payload


def _polygon(monkeypatch, status, close=None, key="k"):
    calls = []

    def get(url, params=None, timeout=None):
        calls.append(url)
        payload = {"results": [{"c": close}]} if close is not None else {"results": []}
        return _Resp(status, payload)

    import requests
    monkeypatch.setattr(requests, "get", get)
    monkeypatch.setattr(financials.time if hasattr(financials, "time") else __import__("time"),
                        "sleep", lambda s: None)
    if key is None:
        monkeypatch.delenv(financials.POLYGON_ENV, raising=False)
    else:
        monkeypatch.setenv(financials.POLYGON_ENV, key)
    return calls


def test_yahoo_price_wins_when_present(monkeypatch):
    monkeypatch.setattr(financials, "_safe_yf_ticker", lambda t: _YF([9.5, 10.0]))
    calls = _polygon(monkeypatch, 200, close=99.0)
    assert financials.fetch_share_price("X") == 10.0
    assert calls == []                      # Polygon never consulted


def test_yahoo_nan_last_bar_uses_prior_real_close(monkeypatch):
    # Yahoo's null bar for a thin name: the last close is NaN but earlier ones are real.
    monkeypatch.setattr(financials, "_safe_yf_ticker", lambda t: _YF([9.5, float("nan")]))
    _polygon(monkeypatch, 200, close=99.0)
    assert financials.fetch_share_price("X") == 9.5


def test_yahoo_empty_falls_through_to_polygon(monkeypatch):
    monkeypatch.setattr(financials, "_safe_yf_ticker", lambda t: _YF([]))
    calls = _polygon(monkeypatch, 200, close=2.98)
    assert financials.fetch_share_price("VFF", retries=1) == 2.98
    assert calls and calls[0].endswith("/v2/aggs/ticker/VFF/prev")


def test_yahoo_all_nan_falls_through_to_polygon(monkeypatch):
    monkeypatch.setattr(financials, "_safe_yf_ticker", lambda t: _YF([float("nan")]))
    _polygon(monkeypatch, 200, close=8.67)
    assert financials.fetch_share_price("BCBP", retries=1) == 8.67


def test_both_fail_is_none_not_a_number(monkeypatch):
    monkeypatch.setattr(financials, "_safe_yf_ticker", lambda t: _YF([]))
    _polygon(monkeypatch, 200, close=None)
    assert financials.fetch_share_price("X", retries=1) is None


def test_polygon_nan_or_zero_close_is_none(monkeypatch):
    monkeypatch.setattr(financials, "_safe_yf_ticker", lambda t: _YF([]))
    _polygon(monkeypatch, 200, close=0)
    assert financials.fetch_share_price("X", retries=1) is None
    _polygon(monkeypatch, 200, close=float("nan"))
    assert financials.fetch_share_price("X", retries=1) is None


def test_polygon_skipped_without_key(monkeypatch):
    monkeypatch.setattr(financials, "_safe_yf_ticker", lambda t: _YF([]))
    calls = _polygon(monkeypatch, 200, close=5.0, key=None)
    assert financials.fetch_share_price("X", retries=1) is None
    assert calls == []


def test_polygon_non_200_is_none(monkeypatch):
    monkeypatch.setattr(financials, "_safe_yf_ticker", lambda t: _YF([]))
    _polygon(monkeypatch, 403, close=5.0)
    assert financials.fetch_share_price("X", retries=1) is None


def test_polygon_429_waits_then_retries(monkeypatch):
    monkeypatch.setattr(financials, "_safe_yf_ticker", lambda t: _YF([]))
    statuses = iter([429, 200])
    slept = []

    def get(url, params=None, timeout=None):
        return _Resp(next(statuses), {"results": [{"c": 4.91}]})

    import requests, time
    monkeypatch.setattr(requests, "get", get)
    monkeypatch.setattr(time, "sleep", lambda s: slept.append(s))
    monkeypatch.setenv(financials.POLYGON_ENV, "k")
    assert financials.fetch_share_price("TPVG", retries=1) == 4.91
    assert slept and slept[-1] == financials._POLYGON_RATE_SLEEP
