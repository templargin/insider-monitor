"""Contract tests for what happens to an issuer that reaches no verdict.

The daily page publishes only companies that met the criteria. That is only
honest if every candidate that did NOT reach a verdict is accounted for
somewhere, and the two ways of not reaching one are not the same thing:

- **Out of scope** — the screen does not apply. A registered fund publishes no
  XBRL statements, a 20-F filer publishes no us-gaap ones, a company whose first
  10-Q has not posted has none at all. The answer is identical tomorrow, so it is
  recorded and never shown.
- **Unresolved** — we could not read something we should have been able to read.
  That is a question still open, so it goes on a queue and is asked again.

Conflating them is what put 122 entries on 22 daily pages against 56 issuers
shown, 60% of which nobody could ever have checked.

Fixture-based: no network.
"""
import json

import pytest
import requests

from scraper import edgar, pipeline


# --- scope classification -------------------------------------------------------

@pytest.mark.parametrize("forms, expected", [
    (["10-Q", "10-K", "4", "8-K"], edgar.SCOPE_DOMESTIC),
    (["10-K/A", "S-1"], edgar.SCOPE_DOMESTIC),
    (["20-F", "6-K", "4"], edgar.SCOPE_FOREIGN),
    (["N-CSR", "N-CEN", "N-2"], edgar.SCOPE_FUND),
    (["S-1", "424B4", "8-K", "3", "4"], edgar.SCOPE_PRE_REPORT),
    ([], edgar.SCOPE_PRE_REPORT),
    # A listed BDC files both, and it IS screenable — 10-K wins over N-2.
    (["10-K", "N-2", "N-CSR"], edgar.SCOPE_DOMESTIC),
])
def test_filer_scope(monkeypatch, forms, expected):
    monkeypatch.setattr(edgar, "fetch_submissions",
                        lambda cik: {"filings": {"recent": {"form": forms}}})
    assert edgar.filer_scope("1") == expected


def test_transient_failure_is_never_a_scope_answer(monkeypatch):
    """A companyfacts 429 says nothing about what the issuer files. Writing it off
    as out-of-scope would silently retire an issuer over a throttle."""
    monkeypatch.setattr(edgar, "filer_scope", lambda cik: edgar.SCOPE_FUND)
    exc = pipeline.DataUnavailable("companyfacts fetch failed", transient=True)
    assert pipeline.classify_unscreenable("1", exc) is None


def test_unanswerable_scope_question_defers_rather_than_excludes(monkeypatch):
    """If we cannot even ask what the issuer files, we do not get to conclude
    anything about it — it goes on the queue."""
    def boom(cik):
        raise requests.ConnectionError("submissions unreachable")
    monkeypatch.setattr(edgar, "filer_scope", boom)
    assert pipeline.classify_unscreenable("1", pipeline.DataUnavailable("no companyfacts")) is None


@pytest.mark.parametrize("scope", [edgar.SCOPE_FUND, edgar.SCOPE_FOREIGN, edgar.SCOPE_PRE_REPORT])
def test_out_of_scope_categories_are_reportable(monkeypatch, scope):
    monkeypatch.setattr(edgar, "filer_scope", lambda cik: scope)
    category = pipeline.classify_unscreenable("1", pipeline.DataUnavailable("no us-gaap balance sheet"))
    assert category == scope
    assert pipeline.EXCLUSION_REASONS[category]


def test_a_domestic_filer_we_could_not_read_stays_a_question(monkeypatch):
    """The whole point of the split: a 10-Q filer that failed to screen is a bug
    or an outage, not a population. It must never be filed away as out of scope."""
    monkeypatch.setattr(edgar, "filer_scope", lambda cik: edgar.SCOPE_DOMESTIC)
    assert pipeline.classify_unscreenable("1", pipeline.DataUnavailable("no basic share count")) is None


# --- placeholder tickers --------------------------------------------------------

@pytest.mark.parametrize("field", ["NONE", "none", "N/A", "[NONE]", "(none)", "", "  ", None])
def test_placeholder_symbols_are_not_tickers(field):
    """`[NONE]` reached the screener as if it were a symbol and came back as a
    share-count failure — a placeholder wearing the costume of a data problem."""
    assert not pipeline._is_real_ticker(field)


@pytest.mark.parametrize("field", ["BDSX", "wbhc", "BRK.B"])
def test_real_symbols_survive(field):
    assert pipeline._is_real_ticker(field)


# --- the retry queue ------------------------------------------------------------

@pytest.fixture
def queue(monkeypatch, tmp_path):
    monkeypatch.setattr(pipeline, "UNRESOLVED_PATH", tmp_path / "unresolved.json")
    monkeypatch.setattr(pipeline, "INSIDERS_DIR", tmp_path / "insiders")
    (tmp_path / "insiders").mkdir()
    monkeypatch.setattr(pipeline, "update_company_data", lambda *a, **k: None)
    return tmp_path


BUCKET = {"ticker": "SUJA", "name": "Suja Life", "by_reporter": {
    "0001": {"reporter_name": "Insider One", "relationship": "Director",
             "total_value": 250_000.0, "shares": 40_000.0, "txn_count": 2}}}
SNAP = {"ev_basic": 370e6, "mc_basic": 227e6, "ttm_revenue": 159e6}


def entry(**over):
    e = {"ticker": "SUJA", "name": "Suja Life", "cik": "1934114",
         "reason": "no basic share count for SUJA", "transient": False,
         "bucket_data": BUCKET}
    e.update(over)
    return e


def write_day(queue_dir, url_date, tickers=()):
    path = pipeline.INSIDERS_DIR / f"{url_date}.json"
    path.write_text(json.dumps({"url_date": url_date, "tickers": list(tickers),
                                "excluded": [], "unresolved": [entry()]}))
    return path


def test_requeueing_the_same_day_keeps_the_attempt_count(queue):
    """A re-run of a day is not a fresh start for an issuer that has already
    failed twice — otherwise nothing ever reaches the review flag."""
    from datetime import date
    d = date(2026, 8, 13)
    pipeline.queue_unresolved(d, [entry()])
    entries = pipeline._queue_load()
    entries[0]["attempts"] = 2
    pipeline._queue_save(entries)

    pipeline.queue_unresolved(d, [entry(reason="no share price for SUJA")])
    saved = pipeline._queue_load()
    assert len(saved) == 1, "same (day, issuer) must not queue twice"
    assert saved[0]["attempts"] == 2
    assert saved[0]["reason"] == "no share price for SUJA"


def test_a_recovered_issuer_lands_on_its_own_days_page(queue, monkeypatch):
    write_day(queue, "2026-08-13")
    pipeline.queue_unresolved(__import__("datetime").date(2026, 8, 13), [entry()])
    monkeypatch.setattr(pipeline, "screener_pass", lambda cik, t, b: (dict(SNAP), None))

    counts = pipeline.retry_unresolved()

    assert counts["recovered"] == 1
    day = json.loads((pipeline.INSIDERS_DIR / "2026-08-13.json").read_text())
    assert [t["ticker"] for t in day["tickers"]] == ["SUJA"]
    assert day["tickers"][0]["total_value"] == 250_000.0
    assert day["unresolved"] == [], "the page must stop carrying a question it answered"
    assert pipeline._queue_load() == [], "a resolved issuer leaves the queue"


def test_a_recovered_issuer_is_not_added_twice(queue, monkeypatch):
    write_day(queue, "2026-08-13", tickers=[{"ticker": "SUJA", "total_value": 250_000.0}])
    pipeline.queue_unresolved(__import__("datetime").date(2026, 8, 13), [entry()])
    monkeypatch.setattr(pipeline, "screener_pass", lambda cik, t, b: (dict(SNAP), None))

    pipeline.retry_unresolved()

    day = json.loads((pipeline.INSIDERS_DIR / "2026-08-13.json").read_text())
    assert len(day["tickers"]) == 1


def test_a_merits_rejection_closes_the_question_quietly(queue, monkeypatch):
    write_day(queue, "2026-08-13")
    pipeline.queue_unresolved(__import__("datetime").date(2026, 8, 13), [entry()])
    monkeypatch.setattr(pipeline, "screener_pass",
                        lambda cik, t, b: (dict(SNAP), "EV=$2,000.0M ≥ $1,000M cap"))

    counts = pipeline.retry_unresolved()

    assert (counts["rejected"], counts["recovered"]) == (1, 0)
    assert pipeline._queue_load() == []
    day = json.loads((pipeline.INSIDERS_DIR / "2026-08-13.json").read_text())
    assert day["tickers"] == []


def test_repeated_failure_is_flagged_for_review_not_retried_forever(queue, monkeypatch):
    write_day(queue, "2026-08-13")
    pipeline.queue_unresolved(__import__("datetime").date(2026, 8, 13), [entry()])

    def unavailable(cik, t, b):
        raise pipeline.DataUnavailable("no share price for SUJA", transient=True)
    monkeypatch.setattr(pipeline, "screener_pass", unavailable)

    day = ["2026-08-13"]
    monkeypatch.setattr(pipeline, "_today_iso", lambda: day[0])

    for attempt in range(1, pipeline.MAX_RETRY_ATTEMPTS):
        counts = pipeline.retry_unresolved()
        assert counts["still_open"] == 1
        assert pipeline._queue_load()[0]["attempts"] == attempt
        # A second run the same day retries — two cover each weekday — but must
        # not spend a second of the issuer's three days.
        pipeline.retry_unresolved()
        assert pipeline._queue_load()[0]["attempts"] == attempt
        day[0] = f"2026-08-{13 + attempt}"

    counts = pipeline.retry_unresolved()
    assert counts["abandoned"] == 1
    assert pipeline._queue_load()[0]["abandoned"] is True

    # ...and an abandoned entry is kept as a record, but never screened again.
    def must_not_run(cik, t, b):
        raise AssertionError("abandoned entries must not be retried")
    monkeypatch.setattr(pipeline, "screener_pass", must_not_run)
    assert pipeline.retry_unresolved()["retried"] == 0


def test_an_entry_with_nothing_to_rescreen_is_flagged_not_dropped(queue, monkeypatch):
    monkeypatch.setattr(pipeline.edgar, "cik_to_ticker", lambda cik: None)
    pipeline._queue_save([entry(ticker=None, bucket_data={})])
    counts = pipeline.retry_unresolved()
    assert counts["abandoned"] == 1
    assert pipeline._queue_load()[0]["abandoned"] is True


def test_a_symbol_that_was_unlookupable_is_looked_up_again(queue, monkeypatch):
    """An issuer queued because SEC's ticker file was unreachable must have that
    question re-asked, not be written off for want of the answer it was missing."""
    write_day(queue, "2026-08-13")
    pipeline._queue_save([entry(ticker=None, url_date="2026-08-13",
                                first_seen="2026-08-13T00:00:00+00:00", attempts=0)])
    monkeypatch.setattr(pipeline.edgar, "cik_to_ticker", lambda cik: "SUJA")
    monkeypatch.setattr(pipeline, "screener_pass", lambda cik, t, b: (dict(SNAP), None))

    assert pipeline.retry_unresolved()["recovered"] == 1
    day = json.loads((pipeline.INSIDERS_DIR / "2026-08-13.json").read_text())
    assert [t["ticker"] for t in day["tickers"]] == ["SUJA"]


def test_a_recovery_with_no_page_to_land_on_stays_queued(queue, monkeypatch):
    """A day whose write was skipped has not carried this issuer. Counting that as
    recovered would drop it from the queue and lose it for good."""
    pipeline.queue_unresolved(__import__("datetime").date(2026, 8, 13), [entry()])
    monkeypatch.setattr(pipeline, "screener_pass", lambda cik, t, b: (dict(SNAP), None))

    counts = pipeline.retry_unresolved()

    assert (counts["recovered"], counts["still_open"]) == (0, 1)
    assert pipeline._queue_load()[0]["attempts"] == 0, "nothing was learned; no day spent"


def test_an_unreadable_queue_file_does_not_take_the_run_down(queue):
    pipeline.UNRESOLVED_PATH.write_text("{ not json")
    assert pipeline._queue_load() == []
