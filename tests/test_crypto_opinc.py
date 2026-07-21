"""Crypto-asset remeasurement is lifted out of Operating Income.

ZSTK (ZeroStack, formerly Flora Growth) is an ASU-2023-08 crypto-treasury filer
that folds its token fair-value remeasurement into `OperatingExpenses`, hence
into `OperatingIncomeLoss`. Left alone, a $58M unrealized token loss showed up
as a $65.8M operating "expense" on $7M of revenue (Q1'26), and FY25's $143M loss
became a $149M Q4 opex line. These pin the lift that moves the remeasurement
below the operating line so the operating rows describe the actual business.

The tags come in several shapes across periods (the filer uses a signed
`GainLoss` split for interim quarters and a `Gain` / `Loss` magnitude pair for
the full year), so the net-per-duration series must bridge them before the
FY − 9M walk can derive Q4.
"""
from scraper.xbrl_financials import (
    _combine_crypto_tags,
    _crypto_op_gainloss_series,
    _lift_crypto_from_opinc,
)


def _fact(start, end, val):
    return {"start": start, "end": end, "val": val, "accn": "a1", "form": "10-Q"}


def _usg(**tag_facts):
    return {tag: {"units": {"USD": facts}} for tag, facts in tag_facts.items()}


# ---- _combine_crypto_tags -------------------------------------------------

def test_combine_signed_gainloss_split():
    """Interim quarters: signed Unrealized + Realized GainLoss tags add as-is."""
    net = _combine_crypto_tags({
        "CryptoAssetUnrealizedGainLossOperatingAndNonoperating": -60_742_000,
        "CryptoAssetRealizedGainLossOperatingAndNonoperating": 2_771_000,
    })
    assert net == -57_971_000


def test_combine_gain_loss_magnitude_pair():
    """Full year: a positive Gain magnitude minus a positive Loss magnitude."""
    net = _combine_crypto_tags({
        "CryptoAssetRealizedAndUnrealizedGainOperatingAndNonoperating": 555_000,
        "CryptoAssetRealizedAndUnrealizedLossOperatingAndNonoperating": 143_552_000,
    })
    assert net == -142_997_000


def test_combine_prefers_full_total_over_components():
    """A period tagging both the RealizedAndUnrealized total and its split parts
    must not double-count — the full total wins."""
    net = _combine_crypto_tags({
        "CryptoAssetRealizedAndUnrealizedLossOperatingAndNonoperating": 143_552_000,
        "CryptoAssetUnrealizedGainLossOperatingAndNonoperating": -140_000_000,
        "CryptoAssetRealizedGainLossOperatingAndNonoperating": -3_552_000,
    })
    assert net == -143_552_000


# ---- _crypto_op_gainloss_series (Q4 derived across tag shapes) -------------

def _zstk_like_usg():
    return _usg(
        CryptoAssetUnrealizedGainLossOperatingAndNonoperating=[
            _fact("2025-01-01", "2025-09-30", 732_000),
            _fact("2026-01-01", "2026-03-31", -60_742_000),
        ],
        CryptoAssetRealizedGainLossOperatingAndNonoperating=[
            _fact("2025-01-01", "2025-09-30", 575_000),
            _fact("2026-01-01", "2026-03-31", 2_771_000),
        ],
        CryptoAssetRealizedAndUnrealizedGainOperatingAndNonoperating=[
            _fact("2025-01-01", "2025-12-31", 555_000),
        ],
        CryptoAssetRealizedAndUnrealizedLossOperatingAndNonoperating=[
            _fact("2025-01-01", "2025-12-31", 143_552_000),
        ],
    )


def test_quarterly_series_derives_q4_from_ytd_across_shapes():
    ser = _crypto_op_gainloss_series(_zstk_like_usg(), "quarterly")
    assert ser["2026-03-31"] == -57_971_000          # direct discrete quarter
    # Q4 = FY(-142,997,000) - 9M(+1,307,000)
    assert ser["2025-12-31"] == -144_304_000
    assert "2025-09-30" not in ser                   # 9M YTD is not a quarter


def test_annual_series_is_the_full_year_total():
    ser = _crypto_op_gainloss_series(_zstk_like_usg(), "annual")
    assert ser == {"2025-12-31": -142_997_000}


def test_no_crypto_tags_returns_empty():
    assert _crypto_op_gainloss_series(_usg(), "quarterly") == {}


# ---- _lift_crypto_from_opinc ----------------------------------------------

def _stmt(op_row, ends):
    return {"labels": ["Operating Income"], "data": [list(op_row)], "_ends": list(ends)}


def test_lift_removes_embedded_remeasurement():
    ends = ["2026-03-31", "2025-12-31", "2025-09-30", "2025-06-30"]
    stmt = _stmt([-62_653_000, -149_240_000, -3_266_000, -2_566_000], ends)
    out = _lift_crypto_from_opinc(stmt, _zstk_like_usg(), "quarterly")
    assert out["data"][0] == [-4_682_000, -4_936_000, -3_266_000, -2_566_000]


def test_lift_leaves_opinc_alone_when_remeasurement_is_below_the_line():
    """If Operating Income is already core (the filer booked the swing below the
    line), removing crypto would only push OpInc further from the operating
    scale, so the gate declines and OpInc is untouched — no double count."""
    ends = ["2026-03-31"]
    stmt = _stmt([-4_682_000], ends)          # already ex-crypto
    out = _lift_crypto_from_opinc(stmt, _zstk_like_usg(), "quarterly")
    assert out["data"][0] == [-4_682_000]


def test_lift_ignores_immaterial_remeasurement():
    """A sub-$1M swing isn't worth disturbing the operating line for."""
    usg = _usg(CryptoAssetUnrealizedGainLossOperatingAndNonoperating=[
        _fact("2026-01-01", "2026-03-31", -400_000),
    ])
    stmt = _stmt([-4_682_000], ["2026-03-31"])
    out = _lift_crypto_from_opinc(stmt, usg, "quarterly")
    assert out["data"][0] == [-4_682_000]
