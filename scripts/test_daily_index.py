"""Deterministic regression test for the daily master.idx dedupe.

EDGAR's daily index lists a Form 4 once for EACH associated CIK (the issuer plus
every reporting owner), so the same accession shows up on multiple lines.
``edgar._parse_master_idx`` must collapse those to one row per accession —
otherwise every insider's totals and transaction counts are inflated (2x in the
common single-owner case, 3x for a joint filing). This is the regression guard
for the 2026-06 daily-page doubling bug. No network: it parses a fixture.
"""
import sys

from scraper import edgar

# Real shape of master.YYYYMMDD.idx: a header block, a dashed rule, then
# CIK|Company|Form|DateFiled|Filename rows. The two real Form 4 accessions below
# each appear twice — once under the reporting owner's CIK, once under the
# issuer's CIK — exactly as SEC published them in the 2026-06-24 index (verified
# against the live filings for TXO and AMS). The trailing 4/A appears under three
# CIKs (a joint filing: two reporting owners + the issuer) to prove N-way dedupe.
SAMPLE_IDX = """\
Description:           Master Index of EDGAR Dissemination Feed
Last Data Received:    June 24, 2026
Comment:              For corrections, contact StructuredData@sec.gov
CIK|Company Name|Form Type|Date Filed|Filename
--------------------------------------------------------------------------------
1182684|SIMPSON BOB R|4|20260624|edgar/data/1182684/0001193125-26-281322.txt
1559432|TXO Partners, L.P.|4|20260624|edgar/data/1559432/0001193125-26-281322.txt
1609580|Stachowiak Raymond C|4|20260624|edgar/data/1609580/0001437749-26-021567.txt
744825|AMERICAN SHARED HOSPITAL SERVICES|4|20260624|edgar/data/744825/0001437749-26-021567.txt
1300514|SOME OTHER FILER INC|8-K|20260624|edgar/data/1300514/0001140361-26-000099.txt
1888001|JOINT OWNER A|4/A|20260624|edgar/data/1888001/0001999999-26-000777.txt
1888002|JOINT OWNER B|4/A|20260624|edgar/data/1888002/0001999999-26-000777.txt
1888003|JOINT ISSUER CO|4/A|20260624|edgar/data/1888003/0001999999-26-000777.txt
"""


def main():
    rows = edgar._parse_master_idx(SAMPLE_IDX)
    accs = [r["accession"] for r in rows]

    # One row per accession — the multi-CIK listing must not leak duplicates.
    assert len(accs) == len(set(accs)), f"duplicate accessions leaked: {accs}"

    # Exactly the three distinct Form 4 / 4-A accessions; the 8-K is excluded.
    assert set(accs) == {
        "0001193125-26-281322",  # TXO   (owner + issuer lines)
        "0001437749-26-021567",  # AMS   (owner + issuer lines)
        "0001999999-26-000777",  # joint (two owners + issuer lines)
    }, accs

    # Non-Form-4 forms never make it through.
    assert "0001140361-26-000099" not in accs, "non-Form-4 leaked in"

    # The joint 4/A spanning three CIK lines collapses to a single row.
    assert accs.count("0001999999-26-000777") == 1, "joint filing not deduped"

    # accession_nodash stays consistent with accession.
    for r in rows:
        assert r["accession_nodash"] == r["accession"].replace("-", "")

    print(f"[PASSED] 8 index lines -> {len(rows)} filings, one row per accession.")


if __name__ == "__main__":
    main()
