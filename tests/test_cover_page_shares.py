"""Contract tests for the cover-page share count.

companyfacts drops every fact carrying a dimension, and a multi-class filer tags
its cover-page share count once per class — so the count that is plainly on the
front page of the 10-Q is absent from the API the screener reads. Between
2026-07-15 and 2026-08-13 that dropped 21 issuers with a qualifying insider
purchase, 7 of them inside the EV cap (SUJA, OPFI, PTLO, BTMD, RAIN, ABTC, DGICA).

`edgar.parse_cover_page_shares` reads the filing's own XBRL instance instead.
These tests pin the three rules that keep its sum from inventing shares.

Fixture-based: no network.
"""
import pytest

from scraper import edgar


def instance(facts, contexts):
    """Minimal XBRL instance: `facts` are (contextRef, value), `contexts` are
    (id, identifier, instant, [(dimension, member)])."""
    ctx_xml = "".join(
        f'<xbrli:context id="{cid}"><xbrli:entity>'
        f'<xbrli:identifier scheme="http://www.sec.gov/CIK">{ident}</xbrli:identifier>'
        + ("<xbrli:segment>" + "".join(
            f'<xbrldi:explicitMember dimension="{d}">{m}</xbrldi:explicitMember>'
            for d, m in dims) + "</xbrli:segment>" if dims else "")
        + f'</xbrli:entity><xbrli:period><xbrli:instant>{instant}</xbrli:instant>'
        f'</xbrli:period></xbrli:context>'
        for cid, ident, instant, dims in contexts)
    fact_xml = "".join(
        f'<dei:EntityCommonStockSharesOutstanding contextRef="{ref}" unitRef="shares" '
        f'decimals="0">{val}</dei:EntityCommonStockSharesOutstanding>'
        for ref, val in facts)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<xbrli:xbrl xmlns:xbrli="http://www.xbrl.org/2003/instance" '
        'xmlns:xbrldi="http://xbrl.org/2006/xbrldi" '
        'xmlns:dei="http://xbrl.sec.gov/dei/2024">'
        f"{ctx_xml}{fact_xml}</xbrli:xbrl>"
    ).encode()


CLASS_AXIS = "us-gaap:StatementClassOfStockAxis"


def test_multi_class_counts_are_summed():
    """SUJA: Class A 23,788,700 + Class V 14,836,312. companyfacts has neither —
    its `dei` namespace is empty, because both facts carry a class dimension."""
    xml = instance(
        [("c-2", "23788700"), ("c-3", "14836312")],
        [("c-2", "0001934114", "2026-07-31", [(CLASS_AXIS, "us-gaap:CommonClassAMember")]),
         ("c-3", "0001934114", "2026-07-31", [(CLASS_AXIS, "suja:CommonClassVMember")])])
    total, as_of = edgar.parse_cover_page_shares(xml, 1934114)
    assert total == 38_625_012
    assert as_of == "2026-07-31"


def test_a_concept_tagged_twice_in_one_context_counts_once():
    """SUJA tags the same concept under two fact ids in the same context. Summing
    facts rather than classes would double the company."""
    xml = instance(
        [("c-2", "23788700"), ("c-2", "23788700"), ("c-3", "14836312")],
        [("c-2", "0001934114", "2026-07-31", [(CLASS_AXIS, "us-gaap:CommonClassAMember")]),
         ("c-3", "0001934114", "2026-07-31", [(CLASS_AXIS, "suja:CommonClassVMember")])])
    assert edgar.parse_cover_page_shares(xml, 1934114)[0] == 38_625_012


def test_co_registrant_shares_are_not_added_to_the_parent():
    """Subsidiary guarantors file under the same accession and tag their own cover
    count. Adding it to the parent's would invent shares out of the filing's own
    structure — the context's entity identifier is what tells them apart."""
    xml = instance(
        [("c-1", "75000000"), ("c-sub", "1000")],
        [("c-1", "0001871509", "2026-07-29", []),
         ("c-sub", "0009999999", "2026-07-29", [])])
    assert edgar.parse_cover_page_shares(xml, 1871509)[0] == 75_000_000


def test_zero_share_class_is_ignored():
    """OPFI's cover lists Class B and Class V at 0 alongside 85,208,247 Class A."""
    xml = instance(
        [("c-2", "85208247"), ("c-3", "0"), ("c-4", "0")],
        [("c-2", "0001818502", "2026-08-06", [(CLASS_AXIS, "us-gaap:CommonClassAMember")]),
         ("c-3", "0001818502", "2026-08-06", [(CLASS_AXIS, "us-gaap:CommonClassBMember")]),
         ("c-4", "0001818502", "2026-08-06", [(CLASS_AXIS, "opfi:ClassVMember")])])
    assert edgar.parse_cover_page_shares(xml, 1818502)[0] == 85_208_247


def test_newest_instant_wins_within_a_class():
    xml = instance(
        [("c-old", "100"), ("c-new", "175")],
        [("c-old", "0000000001", "2025-08-05", [(CLASS_AXIS, "us-gaap:CommonClassAMember")]),
         ("c-new", "0000000001", "2026-08-06", [(CLASS_AXIS, "us-gaap:CommonClassAMember")])])
    total, as_of = edgar.parse_cover_page_shares(xml, 1)
    assert (total, as_of) == (175, "2026-08-06")


def test_single_undimensioned_count_still_works():
    """The ordinary filer, where companyfacts would have answered anyway. The
    fallback must agree with it, not diverge — verified against BDSX, ALTO, CHCT,
    ANIK, ASPS and AUID, which match companyfacts to the share."""
    xml = instance([("c-1", "10551118")], [("c-1", "0001439725", "2026-07-30", [])])
    assert edgar.parse_cover_page_shares(xml, 1439725) == (10_551_118, "2026-07-30")


@pytest.mark.parametrize("xml", [
    b"<not-xml",
    instance([], [("c-1", "0000000001", "2026-07-30", [])]),
])
def test_nothing_readable_is_none_not_zero(xml):
    """The rule this repo keeps relearning: an unknown is never a number. A
    filing with no cover-page fact must return None, never 0 shares — 0 would
    price the company at nothing and pass it straight through the EV cap."""
    assert edgar.parse_cover_page_shares(xml, 1) == (None, None)


def test_instance_document_is_preferred_over_the_rendered_report():
    """`R1.htm` renders LILA's 156,500,000 shares as `156.5` because the filer
    reports in millions; the instance carries the absolute value. Pick the
    instance, and never a linkbase."""
    names = ["FilingSummary.xml", "lila-20260630_cal.xml", "lila-20260630_lab.xml",
             "lila-20260630_htm.xml", "R1.htm"]
    assert edgar._instance_name(names) == "lila-20260630_htm.xml"
    assert edgar._instance_name(["abc-20260630_pre.xml", "abc-20260630.xml"]) == "abc-20260630.xml"
    assert edgar._instance_name(["FilingSummary.xml", "R1.htm"]) is None
