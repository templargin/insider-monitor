"""Phase 2 sanity test: run end-to-end on Fri 2026-05-08 bucket.

For /may/8 (Fri read), the bucket is filings posted on Thu 2026-05-07.
We aggregate code-P, filter to ≥$100k aggregate per issuer, then compute EV and revenue
for survivors and check filter logic.
"""
import sys
from datetime import date
from scraper import edgar, filters, xbrl_facts, buckets


def main():
    url_date = date(2026, 5, 8)  # Fri
    filing_dates = buckets.filing_dates_for_url(url_date)
    print(f"URL date: {url_date} ({url_date.strftime('%A')})")
    print(f"Filing dates in bucket: {filing_dates}")

    # Just process one day for the test
    fd = filing_dates[0]
    print(f"\nFetching daily index for {fd} ...")
    rows = edgar.fetch_daily_index_form4s(fd)
    print(f"  {len(rows)} Form 4 filings.")

    # Quick scan: parse first 30 to find some Ps
    print("\nParsing first 50 filings to find code-P transactions...")
    parsed = []
    for i, row in enumerate(rows[:50]):
        xml = edgar.fetch_form4_xml(row["cik"], row["accession_nodash"])
        if xml is None:
            continue
        p = edgar.parse_form4(xml)
        if p is None:
            continue
        parsed.append(p)
        if i % 10 == 0:
            print(f"  ...{i + 1}/{50}", flush=True)

    print(f"\nParsed {len(parsed)} Form 4s.")
    p_count = sum(1 for p in parsed if any(t["code"] == "P" for t in p["transactions"]))
    print(f"  Of those, {p_count} have at least one code-P transaction.")

    aggregated = filters.aggregate_p_purchases(parsed)
    print(f"\nAggregated to {len(aggregated)} issuers with any P-purchases.")
    threshold_passers = [(cik, b) for cik, b in aggregated.items()
                         if filters.passes_threshold(b)]
    print(f"  ≥${filters.PURCHASE_THRESHOLD_USD:,} aggregate: {len(threshold_passers)} issuers.")

    for cik, b in threshold_passers[:5]:
        qualifying = filters.qualifying_reporters(b)
        total_q = sum(r['total_value'] for r in qualifying)
        print(f"\n  Issuer: {b['name']} ({b['ticker']}) — qualifying ${total_q:,.0f} across {len(qualifying)} insider(s)")
        for r in qualifying:
            print(f"    Reporter: {r['reporter_name']} ({r['relationship']}) — ${r['total_value']:,.0f}")

    # If we got at least one, test EV/revenue filter on it
    if threshold_passers:
        test_cik = threshold_passers[0][0]
        test_ticker = threshold_passers[0][1]["ticker"]
        test_name = threshold_passers[0][1]["name"]
        print(f"\n\n--- EV / revenue probe for {test_name} (CIK {test_cik}) ---")
        facts = edgar.fetch_companyfacts(test_cik)
        if facts is None:
            print("  No companyfacts (CIK may not have XBRL filings)")
        else:
            shares, sh_end = xbrl_facts.get_basic_shares(facts)
            cash, c_end = xbrl_facts.get_cash(facts)
            debt, d_end = xbrl_facts.get_total_debt(facts)
            ttm_rev, r_end = xbrl_facts.get_ttm_revenue(facts)
            print(f"  Basic shares: {shares} (as of {sh_end})")
            print(f"  Cash:         {cash} (as of {c_end})")
            print(f"  Total debt:   {debt} (as of {d_end})")
            print(f"  TTM revenue:  {ttm_rev} (as of {r_end})")

    print("\n[PASSED] Phase 2 basics work.")


if __name__ == "__main__":
    main()
