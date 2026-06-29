"""Structured balance-sheet debt extraction (moves 2 & 3 of the debt fix).

The screener's total-debt figure used to come from a fixed allow-list of XBRL
leaf tags (the "ladder"). That can never close the long tail of us-gaap debt
concepts: a filer reporting its revolver under ``LineOfCredit`` or its notes
under ``SeniorNotes`` read as $0, and the dollars vanished into a derived "Other"
plug — silently. This module replaces that with:

  Move 2 — classify by the us-gaap debt hierarchy, not a leaf allow-list.
  Borrowings are recognized by concept *family* (``LineOfCredit``,
  ``SeniorNotes``, ``ConvertibleNotesPayable``, finance leases, …) and deduped by
  *tier* — a reported subtotal subsumes the instruments beneath it — over only
  the current balance sheet (date-anchored, move 1). Whole families come along at
  once, so there is no per-leaf list to extend.

  Move 3 — flag, never plug. Reconcile recognized debt against the filer's own
  reported total liabilities. A material borrowing-shaped residual is surfaced
  (``flag``) instead of being absorbed into "Other" — including debt under the
  filer's custom namespace, which companyfacts doesn't carry and which therefore
  shows up as an unexplained slice of total liabilities.

Values are read from companyfacts at the balance-sheet date (move-1 anchoring),
so abandoned/stale tags can't leak in.
"""
import re

from . import xbrl_facts as XF

# ---- borrowing recognizer (substring patterns, not an exact allow-list) -------
# A concept is interest-bearing debt if its local name matches one of these
# families and none of the exclusions. Patterns capture whole families at once
# (every *LineOfCredit*, *SeniorNotes*, *ConvertibleNotesPayable*, custom or not)
# — the opposite of enumerating individual leaf tags.
_DEBT_PAT = re.compile(
    r"(longtermdebt|debtcurrent|debtnoncurrent|debtlongtermandshortterm"
    r"|lineofcredit|longtermline|shorttermborrow|longtermborrow|otherborrow"
    r"|notespayable|notesandloans|loanspayable|loanpayable"
    r"|seniornote|secured(note|debt)|unsecured(note|debt)|subordinat|debenture|conduit"
    r"|convertible(note|debt|debenture|subordinated|seniornote)"
    r"|commercialpaper|termloan|mediumtermnote|bridgeloan"
    r"|financelease(liability|obligation)|capitalleaseobligation"
    r"|federalhomeloanbankadvances|advancesfromfederalhomeloanbank"
    r"|securitiessoldunderagreementstorepurchase|warehouse)",
    re.I,
)
_DEBT_EXCL = re.compile(
    r"(availableforsale|heldtomaturity|debtsecur|tradingsecur|marketablesecur"
    r"|investment|maturit|proceed|repayment|issuance|amortiz|accretion"
    r"|interestexpense|interestpaid|accruedinterest|interestpayable|discount"
    r"|premium|faceamount|conversionprice|converted|covenant|weightedaverage"
    r"|effectiveinterest|statedinterest|maximum|remaining|unused|fairvalue"
    r"|guarantee|restrictedcash|collateral|threshold|percentage|numberof"
    r"|sharesissued|warrant|\brate\b|frequency|periodicpayment|prepayment"
    r"|paymentsdue|borrowingcapacity|capacity|rightofuse|asset|\bstock\b"
    r"|liabilitiesotherthan|onrealestate|instrumentcarryingamount|grantdate"
    r"|redemption|extinguishment|gainloss|netofdeferred"
    # asset-side look-alikes: notes/loans *receivable* and their allowances are
    # assets (a lender's loan book), not borrowings — DAVE, SEZL, BNPL/lenders.
    r"|receivable|allowance|heldforsale|heldforinvestment|duefrom"
    # `*Gross` duplicates the net carrying tag (FKYS repo, LongTermDebtGross).
    r"|gross)",
    re.I,
)

# Non-debt liabilities we can name. Only used by the footing check (move 3) to
# decide whether an unexplained remainder is "shaped like" missing debt.
_NONDEBT_LIAB_PAT = re.compile(
    r"(accountspayable|accrued|deferredrevenue|contractwithcustomerliab"
    r"|operatingleaseliab|deferredtax|incometax|taxespayable|dividendspayable"
    r"|employeerelated|pensionandother|postretirement|assetretirementobligation"
    r"|deposits|policyholder|unearnedpremium|duetoaffiliat"
    r"|warrantliab|derivativeliab|otherliabilit)",
    re.I,
)

# Asset-side concepts that share a substring with a liability family (the pool
# carries both sides of the sheet): FinancingReceivableExcludingAccruedInterest
# matches `accrued`, DeferredTaxAssets matches `deferredtax`, etc. Excluded when
# summing non-debt *liabilities* so the footing residual isn't swamped.
_NONLIAB_HINT = re.compile(
    r"(receivable|prepaid|goodwill|intangible|propertyplant|\binvestment|inventory"
    r"|deferredtaxasset|marketablesecur|availableforsale|heldtomaturity"
    r"|capitalizedcomputer|loansheld|restrictedcash|\bcashand)",
    re.I,
)

# us-gaap debt classification hierarchy. The dedup is by *tier*, not a per-tag
# overlap map: a classification total subsumes the instruments beneath it. This
# is filer-agnostic and resolves the double-count that a flat tag list can't —
# e.g. AVD reports both `LongTermDebtNoncurrent` ($264M, the noncurrent total)
# and `LineOfCredit` ($283M, the instrument inside it); the total wins.
_AGG_INCL_LEASE = {"LongTermDebtAndCapitalLeaseObligations"}      # incl finance leases
_AGG_TOTAL = {"LongTermDebt", "DebtLongtermAndShorttermCombinedAmount"}  # current+noncurrent, excl leases
_NONCUR_TOTAL = {"LongTermDebtNoncurrent"}
_CUR_TOTAL = {"LongTermDebtCurrent", "DebtCurrent"}


def _is_debt(name):
    return bool(_DEBT_PAT.search(name)) and not _DEBT_EXCL.search(name)


def _is_lease(name):
    # Pure lease liability — NOT the combined `LongTermDebtAndCapitalLeaseObligations`
    # aggregate (which carries 'debt' in the name and is handled as a debt total).
    n = name.lower()
    return ("financelease" in n or "capitallease" in n) and "debt" not in n


def _lease_total(matched):
    """Finance/capital lease liability carrying value from matched tags."""
    for t in ("FinanceLeaseLiability", "CapitalLeaseObligations", "LongtermFinanceLeaseLiability"):
        if t in matched:
            return matched[t]
    cur = sum(v for n, v in matched.items() if _is_lease(n) and n.endswith("Current"))
    non = sum(v for n, v in matched.items() if _is_lease(n) and n.endswith("Noncurrent"))
    return cur + non


# ---- value lookup (anchored to the balance-sheet date) ------------------------

def _ns_facts(facts):
    return (facts or {}).get("facts", {})


def _instant_liability_concepts(facts, as_of):
    """All us-gaap USD instant facts on the balance-sheet date, as {name: val}.

    The single pool the recognizer and the footing check both draw from. USD-only
    (balance-sheet items are monetary) — avoids picking up a per-share or shares
    unit for the same concept."""
    out = {}
    for name, obj in _ns_facts(facts).get("us-gaap", {}).items():
        cands = [f for f in obj.get("units", {}).get("USD", [])
                 if f.get("end") == as_of and "start" not in f and isinstance(f.get("val"), (int, float))]
        if cands:    # same period-end can carry an original + a restated fact; take latest-filed
            out[name] = max(cands, key=lambda f: f.get("filed", ""))["val"]
    return out


# ---- recognizer path (no linkbase): pattern over the current sheet ------------

def _drop_family_subtotals(matched):
    """Drop component tags whose parent subtotal is also reported, beyond the
    fixed tier sets — e.g. ANGX reports `NotesPayable` $102M (the total) AND its
    parts `LongTermNotesPayable` $62M + `NotesPayableCurrent` $40M; summing all
    three double-counts to $205M. A tag P is a family subtotal when its local
    name is contained in >=2 other matched tags whose values sum to P's value
    (within 2%); those children are dropped. Filer-agnostic: no per-family list."""
    drop = set()
    for p, pv in matched.items():
        kids = [c for c in matched if c != p and p in c]
        if len(kids) >= 2 and abs(sum(matched[c] for c in kids) - pv) <= 0.02 * max(pv, 1):
            drop.update(kids)
    return {n: v for n, v in matched.items() if n not in drop}


def _debt_from_facts(pool):
    """(debt, [(name, val)], largest) from the borrowing tags in `pool` (the
    USD instant facts on the balance-sheet date).

    Dedupes by the classification hierarchy: a present subtotal subsumes the
    instruments beneath it, so an aggregate + its parts are never summed twice.
    Debt under a filer's *custom namespace* is invisible here (companyfacts only
    carries standard taxonomies) — caught by the footing flag, not dropped."""
    matched = {n: v for n, v in pool.items() if _is_debt(n) and v > 0}
    if not matched:
        return 0, [], 0
    matched = _drop_family_subtotals(matched)

    leases = _lease_total(matched)
    nonlease = {n: v for n, v in matched.items() if not _is_lease(n)}

    def total_of(names):
        # The largest present subtotal in the tier. Deterministic (the tier sets
        # are unordered) and correct when a filer reports two overlapping totals
        # — e.g. `DebtCurrent` (all current debt) alongside `LongTermDebtCurrent`
        # (current portion of LT only): the broader one wins, not set-hash order.
        present = [nonlease[n] for n in names if n in nonlease]
        return max(present) if present else None

    agg_lease = total_of(_AGG_INCL_LEASE)
    if agg_lease is not None:
        core = agg_lease                      # already includes leases
        leases = 0
    else:
        agg = total_of(_AGG_TOTAL)
        if agg is not None:
            core = agg                        # current + noncurrent debt, ex leases
        else:
            non_total = total_of(_NONCUR_TOTAL)
            cur_total = total_of(_CUR_TOTAL)
            # noncurrent: the subtotal if reported, else the sum of noncurrent +
            # bare instrument tags (bare = no current/noncurrent suffix → long-term)
            if non_total is not None:
                noncur = non_total
            else:
                noncur = sum(v for n, v in nonlease.items()
                             if n not in _CUR_TOTAL and not n.endswith("Current"))
            # current: the subtotal if reported, else the sum of *Current instruments
            if cur_total is not None:
                cur = cur_total
            else:
                cur = sum(v for n, v in nonlease.items()
                          if n not in _NONCUR_TOTAL and n.endswith("Current"))
            core = noncur + cur

    total = core + leases
    # detail + the single largest instrument (a hard floor used when an
    # intra-family overlap forces a clamp).
    detail = sorted(matched.items(), key=lambda kv: -kv[1])
    largest = detail[0][1] if detail else 0
    return total, detail, largest


# ---- public entry point -------------------------------------------------------

def _is_financial_institution(facts, as_of, liab):
    """True for deposit-funded banks / thrifts — where deposits are a large share
    of the balance sheet, EV is not a meaningful metric, so the debt figure is
    flagged low-confidence rather than trusted. Anchored to the balance-sheet date
    and gated on deposit *share*, so an operating company with an incidental
    customer-deposit liability is not mislabeled (and a former bank that long ago
    divested isn't flagged forever off a stale tag)."""
    dep, _ = XF.instant_value_at(facts, ["Deposits", "InterestBearingDepositLiabilities"], as_of)
    if not dep or dep <= 0:
        return False
    # Without a total-liabilities figure we can't confirm the deposit *share*, so
    # don't assert "bank" off a bare deposits tag (which an operating company can
    # also carry as customer/security deposits).
    return bool(liab and liab > 0 and dep > 0.25 * liab)


def _reported_liabilities(facts, as_of):
    v, _ = XF.instant_value_at(facts, ["Liabilities"], as_of)
    if v is not None:
        return v
    cur, _ = XF.instant_value_at(facts, ["LiabilitiesCurrent"], as_of)
    non, _ = XF.instant_value_at(facts, ["LiabilitiesNoncurrent"], as_of)
    if cur is None and non is None:
        return None
    return (cur or 0) + (non or 0)


def _named_nondebt(pool, liab):
    """Sum of named non-debt liabilities in `pool`, bounded by total liabilities.
    Asset-side look-alikes (_NONLIAB_HINT) are excluded; the pool carries both
    sides of the sheet."""
    nd = sum(v for n, v in pool.items()
             if _NONDEBT_LIAB_PAT.search(n) and not _is_debt(n) and not _NONLIAB_HINT.search(n))
    return min(nd, liab) if (liab and liab > 0) else nd


def get_structured_debt(facts):
    """Total interest-bearing debt on the current balance sheet.

    Returns (debt, as_of, flag) where flag is None or a dict
    {reason, amount, concept} describing residual uncertainty (move 3). `debt`
    is None only when the filer reports no debt-shaped liability at all.

    Classify borrowings on the current balance sheet by the us-gaap debt
    hierarchy (move 2), bound the result by the filer's reported total
    liabilities, then reconcile and flag — never plug — any residual (move 3).
    """
    as_of = XF.balance_sheet_date(facts)
    if not as_of:
        return None, None, None

    pool = _instant_liability_concepts(facts, as_of)        # one scan, reused below
    liab = _reported_liabilities(facts, as_of)
    raw_debt, detail, largest = _debt_from_facts(pool)

    if not detail:
        # No face-of-balance-sheet debt line. Before declaring the filer debt-free
        # (the path that used to silently return $0 for EPSN), fall back to the
        # debt-footnote total carrying amount, which is the only place EPSN and
        # peers tag their $45.5M. Filers genuinely without any debt tag return
        # None; the negative case is covered by `scripts.audit_debt_free`, not a
        # per-page flag (large non-debt liabilities — custodial, insurance — would
        # otherwise false-alarm).
        dica = (pool.get("DebtInstrumentCarryingAmount")
                or pool.get("DebtInstrumentCarryingAmountNoncurrent"))
        if dica and dica > 0:
            return (min(dica, liab) if (liab and liab > 0) else dica), as_of, \
                {"reason": "debt_from_footnote_total", "amount": None, "concept": None}
        return None, as_of, None

    named_nondebt = _named_nondebt(pool, liab)

    # Hard bound: total debt cannot exceed total liabilities. When the raw tier
    # sum violates that *and* exceeds its single biggest component, overlapping
    # instrument tags were double-counted (OPAD: NotesAndLoansPayable + *Current
    # + OtherNotesPayableCurrent = $209M vs $104M liabilities). The `raw >
    # largest` guard is essential: a single clean tag that merely exceeds an
    # under-captured `Liabilities` total (TXO) must NOT be clamped. Fall back to
    # the largest single instrument that fits inside total liabilities — a real
    # borrowing, never a plug of unclassified liabilities — and flag (move 3). If
    # even the largest instrument exceeds liabilities (the total is itself
    # under-reported), keep that instrument rather than plugging debt = liab.
    debt, ambiguous = raw_debt, False
    if liab and liab > 0 and raw_debt > liab and raw_debt > largest:
        debt = next((v for _, v in detail if v <= liab), largest)
        ambiguous = True

    is_financial = _is_financial_institution(facts, as_of, liab)
    flag = _reconcile_flag(liab, debt, named_nondebt, ambiguous, raw_debt, is_financial)
    return debt, as_of, flag


def _reconcile_flag(liab, debt, named_nondebt, ambiguous, raw_debt, is_financial):
    """Move 3: surface residual uncertainty instead of plugging it into 'Other'."""
    if ambiguous:
        return {"reason": "debt_tags_overlap_clamped", "amount": raw_debt - debt,
                "concept": None}
    if is_financial:
        # Deposit-funded balance sheet with overlapping wholesale-funding tags;
        # EV is not meaningful for a bank/thrift regardless.
        return {"reason": "financial_institution", "amount": None, "concept": None}
    if liab and liab > 0:
        remainder = liab - (debt or 0) - named_nondebt
        # A chunky slice of liabilities that is neither recognized debt nor a
        # named non-debt liability — could be debt under the filer's custom
        # namespace (unvaluable from companyfacts). Report the amount, not a
        # guessed concept: every standard debt tag is already counted.
        if remainder > max(5_000_000, 0.10 * liab):
            return {"reason": "unexplained_liabilities", "amount": remainder, "concept": None}
    return None
