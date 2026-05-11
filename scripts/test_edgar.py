"""Quick sanity test for the EDGAR client. Fetches one day of Form 4s, parses one."""
import sys
from datetime import date
from scraper import edgar


def main():
    test_date = date(2026, 5, 8)  # Friday
    print(f"Fetching daily index for {test_date} ...")
    rows = edgar.fetch_daily_index_form4s(test_date)
    print(f"  Got {len(rows)} Form 4 / 4-A filings.")
    if not rows:
        print("  No filings — EDGAR may be 404 on this date. Trying 2024-05-08...")
        rows = edgar.fetch_daily_index_form4s(date(2024, 5, 8))
        print(f"  Got {len(rows)} filings on 2024-05-08.")
    if not rows:
        print("ERROR: could not fetch any Form 4s.")
        sys.exit(1)

    print(f"\nFirst filing: {rows[0]['company_name']} (CIK {rows[0]['cik']}, {rows[0]['accession']})")
    xml = edgar.fetch_form4_xml(rows[0]["cik"], rows[0]["accession_nodash"])
    if xml is None:
        print("ERROR: could not fetch XML.")
        sys.exit(1)
    print(f"  XML size: {len(xml)} bytes")

    parsed = edgar.parse_form4(xml)
    if parsed is None:
        print("ERROR: parse failed.")
        sys.exit(1)
    print(f"  Issuer: {parsed['issuer_name']} ({parsed['issuer_ticker']}, CIK {parsed['issuer_cik']})")
    print(f"  Reporter: {parsed['reporter_name']}")
    print(f"  Relationship: {parsed['relationship']}")
    print(f"  Transactions: {len(parsed['transactions'])}")
    for txn in parsed["transactions"][:3]:
        print(f"    [{txn['table'][:3]}] code={txn['code']} shares={txn['shares']:.0f} price=${txn['price']:.2f} title={txn['security_title']!r}")

    print("\n--- ticker → CIK lookup ---")
    cik = edgar.ticker_to_cik("AAPL")
    print(f"  AAPL → CIK {cik}")

    print("\n--- companyfacts probe ---")
    facts = edgar.fetch_companyfacts(cik)
    if facts is None:
        print("ERROR: companyfacts returned None.")
        sys.exit(1)
    us_gaap = facts.get("facts", {}).get("us-gaap", {})
    print(f"  Apple has {len(us_gaap)} us-gaap tags.")
    for tag in ["Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax",
                "CashAndCashEquivalentsAtCarryingValue", "LongTermDebt",
                "CommonStockSharesOutstanding"]:
        present = "✓" if tag in us_gaap else "✗"
        print(f"  {present} {tag}")
    print("\n[PASSED] EDGAR client works end-to-end.")


if __name__ == "__main__":
    main()
