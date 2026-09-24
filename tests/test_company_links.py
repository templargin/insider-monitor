"""Every ticker a daily page lists must have a company page to link to.

Two ways the link broke in September 2026, both live 404s on the public site:
- SPRU (2026-09-24): the company refresh hit a SEC 503 that outlived `_get`'s
  retries. The failure was caught (correctly — it must not cost the day's page)
  but nothing asked again, so /companies/SPRU/ was never built.
- CV (2026-09-21): CapsoVision typed its Form 4 symbol as "cv". The company JSON
  is written upper-case and Pages paths are case-sensitive.

Fixture-based: no network.
"""
import json

import pytest

from scraper import edgar, pipeline


@pytest.fixture
def dirs(tmp_path, monkeypatch):
    monkeypatch.setattr(pipeline, "INSIDERS_DIR", tmp_path / "insiders")
    monkeypatch.setattr(pipeline, "COMPANIES_DIR", tmp_path / "companies")
    pipeline.INSIDERS_DIR.mkdir()
    pipeline.COMPANIES_DIR.mkdir()
    return tmp_path


def _day(name, *tickers):
    (pipeline.INSIDERS_DIR / f"{name}.json").write_text(
        json.dumps({"tickers": [{"ticker": t} for t in tickers]}))


def _fake_refresh(ticker, cik, snap):
    (pipeline.COMPANIES_DIR / f"{ticker.upper()}.json").write_text("{}")


def test_a_linked_ticker_with_no_company_page_is_built(dirs, monkeypatch):
    _day("2026-09-24", "SPRU", "CV")
    (pipeline.COMPANIES_DIR / "CV.json").write_text("{}")
    monkeypatch.setattr(edgar, "ticker_to_cik", lambda t: "1772720")
    monkeypatch.setattr(pipeline, "screener_pass", lambda cik, t, b: ({}, "EV too big"))
    monkeypatch.setattr(pipeline, "update_company_data", _fake_refresh)

    assert pipeline.heal_missing_companies() == (["SPRU"], [])
    assert (pipeline.COMPANIES_DIR / "SPRU.json").exists()
    assert pipeline.heal_missing_companies() == ([], []), "idempotent once built"


def test_a_heal_that_fails_again_is_reported_not_raised(dirs, monkeypatch):
    _day("2026-09-24", "SPRU")
    monkeypatch.setattr(edgar, "ticker_to_cik", lambda t: "1772720")

    def boom(*a):
        raise RuntimeError("503 for https://www.sec.gov/...")
    monkeypatch.setattr(pipeline, "screener_pass", boom)

    assert pipeline.heal_missing_companies() == ([], ["SPRU"])


def test_a_lowercase_form4_symbol_is_normalised():
    xml = b"""<ownershipDocument><issuer><issuerCik>0002031561</issuerCik>
    <issuerName>CapsoVision, Inc</issuerName><issuerTradingSymbol>cv</issuerTradingSymbol>
    </issuer></ownershipDocument>"""
    parsed = edgar.parse_form4(xml)
    assert parsed["issuer_ticker"] == "CV"
