"""
Department Mapping engine — decides what Department a UPC gets when the
top-priority source (NWG/"Scan Advantage", aka P1) has no usable Department
for it, instead of Merge silently falling through to a lower-priority
source's raw, messy Department/Category/Subcategory text.

Ported from the legacy script.py's bucket-inference engine. A "combo" is a
(source, raw Department, raw Category, raw Subcategory) triple from a
non-P1 source, built only from UPCs whose *current* merged item does not
already get its Department from P1. "Evidence" for a combo is the subset of
its UPCs that P1 also carries with a real Department — if enough of that
evidence agrees on one Department (high "purity", enough "sample"), the
combo can be auto-decided; otherwise it needs a human ("Crosswalk"), or, if
there's no evidence at all, "Unmatched".

Two entry points:
- `compute_combos(...)` is a pure function (no DB) over plain data
  structures — this is what's unit-tested with synthetic data.
- `run_engine(engine, ...)` does the DB I/O: pulls narrow filtered data,
  calls `compute_combos`, then writes the incremental delta back without
  ever clobbering a genuine human decision (see `_write_back`).
"""

import json
import re
from collections import Counter
from datetime import datetime, timezone

import pandas as pd
from sqlalchemy import bindparam, text

BLANK = ""  # matches ingest.py's convention: blanks are "", never NULL/NaN, in raw_items.

MAX_DISTINCT_SUGGESTIONS = 5  # cap on distinct departments a single combo/UPC dispute can accumulate; see upsert_combo_suggestion.

DEFAULT_CONFIG = {
    "min_purity": 0.90,
    "min_sample": 5,
    "max_chain_rounds": 3,
    "cat_match2_min_siblings": 2,
    "brand_combo_min_sample": 5,
    "brand_combo_min_purity": 0.90,
    "brand_item_min_sample": 1,
    "brand_item_min_purity": 0.75,
    "brand_category_consensus_min_sample": 15,
    "brand_keyword_conflict_max_fraction": 0.20,
    # Per-UPC passes (script.py Passes 4/5/6) — these run automatically on
    # every engine run against UPCs whose combo never reached tier=="auto",
    # using an evidence table built ONLY from combos that DID reach
    # tier=="auto" this run. Confirmed against script.py's real source.
    "upc_brand_match_min_sample": 1,
    "upc_brand_match_min_purity": 0.75,
    "upc_root_match_min_sample": 1,
    "upc_root_match_min_purity": 0.80,
    "description_match_min_sample": 3,
    "description_match_min_purity": 0.90,
    "description_match_min_vote_share": 0.90,
}

# script.py's GENERIC_SUBCATEGORY_BLOCKLIST — checked against BOTH Category
# and Subcategory (confirmed against the real source, all 4 call sites use
# an identical `_blocked(cat, subcat)`), everywhere a combo or candidate UPC
# could otherwise be promoted: the combo-level review/Category-Match/Brand-
# Match passes, and all three per-UPC passes. A catch-all bucket like "MISC"/
# "OTHER" is too heterogeneous for any of these signals to safely generalize
# across.
GENERIC_SUBCATEGORY_BLOCKLIST = {"", "OTHER", "MISC", "MISCELLANEOUS", "N/A", "NA", "NOT FOR RESALE", "SUPPLIES NOT FOR RESALE"}


def _is_blocked_bucket(category, subcategory) -> bool:
    return _norm(category) in GENERIC_SUBCATEGORY_BLOCKLIST or _norm(subcategory) in GENERIC_SUBCATEGORY_BLOCKLIST


# script.py's BRAND_MATCH_CONFLICT_KEYWORDS — a description mentioning one of
# these patterns is treated as real evidence AGAINST any department other
# than the one it's keyed to (e.g. "beef" in a description is evidence
# against putting that item somewhere other than MEAT), independent of what
# Brand/UPC-root/word signal might otherwise suggest. Used two ways: a hard
# per-item skip in the three per-UPC passes (line refs 1926/2026/2179 in
# script.py), and a whole-combo fractional gate in combo-level Brand Match
# (script.py lines 1674-1683).
BRAND_MATCH_CONFLICT_KEYWORDS = {
    "FROZEN": [r"\bfrozen\b", r"\bfrz\b", r"\bgelato\b", r"\bice cream\b"],
    "DAIRY": [r"\bmilk\b(?! chocolate)", r"\byogurt\b"],
    "MEAT": [r"\bbeef\b", r"\bpork\b", r"\bchicken\b", r"\bsausage\b", r"\bbacon\b", r"\bpatty\b", r"\bpatties\b"],
    "ALCOHOL": [r"\bwine\b", r"\bbeer\b", r"\bvodka\b", r"\bwhiskey\b", r"\bgin\b"],
    "HEALTH & BEAUTY CARE": [r"\bshampoo\b", r"\blotion\b", r"\bvitamin\b(?!\s*[a-e]\b(?:\s*&\s*[a-e]\b)?)", r"\bsunscreen\b", r"\bsoap\b"],
    "HOUSEHOLD CARE": [r"\bdetergent\b", r"\bcleaner\b", r"\bair freshener\b"],
    "BABY CARE": [r"\bdiaper\b"],
    "PET CARE": [r"\bpet\b", r"\bcanine\b", r"\bfeline\b"],
}


def _has_keyword_conflict(description: str, target_dept: str) -> bool:
    """True if `description` matches a conflict keyword belonging to some
    OTHER department than target_dept — script.py's per-item hard skip for
    the three per-UPC passes."""
    desc_l = (description or "").lower()
    for conflict_dept, patterns in BRAND_MATCH_CONFLICT_KEYWORDS.items():
        if conflict_dept == target_dept:
            continue
        if any(re.search(p, desc_l) for p in patterns):
            return True
    return False


def _keyword_conflict_fraction(descriptions: list, target_dept: str) -> float:
    """Fraction of `descriptions` matching a conflict keyword belonging to
    some OTHER department than target_dept — combo-level Brand Match's
    whole-combo gate (script.py's kw_conflict_frac)."""
    if not descriptions:
        return 0.0
    hits = 0
    for desc in descriptions:
        desc_l = (desc or "").lower()
        for conflict_dept, patterns in BRAND_MATCH_CONFLICT_KEYWORDS.items():
            if conflict_dept == target_dept:
                continue
            if any(re.search(p, desc_l) for p in patterns):
                hits += 1
                break
    return hits / len(descriptions)


# script.py's CATEGORY_MAPPING_DESCRIPTION_WORD_EXCLUDE default (the live
# workbook value can differ if a user has customized the "Department
# Matching Keywords" sheet — this is the shipped default).
DESCRIPTION_WORD_EXCLUDE = {"salmon", "shrimp", "caviar", "curved", "fossil"}


def _norm(v) -> str:
    """Normalizes raw text for combo-key/lookup purposes: strip+upper. The
    same source frequently spells the same real department inconsistently
    across rows (e.g. KEHE has both "Health Body Care" and "HEALTH BODY
    CARE" in its own raw file) — left un-normalized, Python would treat
    these as two different combos that then collide on SQL Server's UNIQUE
    constraint anyway (its default collation is case-insensitive), and,
    worse, would silently split one real bucket's evidence across two
    combos. Matches this app's convention of canonical Department values
    being ALL-CAPS."""
    return (v or BLANK).strip().upper()


def _purity_and_majority(values: list) -> tuple:
    """Given a list of Department values (one per evidence UPC), returns
    (majority_department, purity, runner_up_department, runner_up_share) —
    purity/runner_up_share are each department's share of the list. Empty
    list -> (None, None, None, None). Mirrors script.py's real
    _department_mapping_evidence, which reports the runner-up alongside
    the winner so a person reviewing a Crosswalk row can see how close
    the evidence actually was (e.g. 82% vs a real 6% runner-up is a much
    safer-looking suggestion than 82% vs a real 18% runner-up)."""
    if not values:
        return None, None, None, None
    counts = Counter(values)
    ranked = counts.most_common(2)
    majority, count = ranked[0]
    runner_up, runner_count = ranked[1] if len(ranked) > 1 else (None, 0)
    n = len(values)
    return majority, count / n, runner_up, (runner_count / n if runner_up else None)


def compute_combos(
    member_rows: list,
    evidence_rows: list,
    p1_department: dict,
    strict_map: dict,
    config: dict = None,
    unmatched_defaults: dict = None,
) -> list:
    """Pure computation, no DB. Returns a list of combo dicts.

    member_rows: list of dicts with keys upc, source_key, department,
        category, subcategory, brand (raw text from non-P1 sources —
        brand is optional, used only by the Brand Match pass). PRE-
        FILTERED to only the UPCs each source actually WON in the merge
        (upc_to_winner_key[upc] == source_key) — these are the UPCs that
        actually need a decided Department, and they become each combo's
        "upcs"/"brands"/"descriptions"/n_upcs_total (what per-UPC passes
        and the review UI operate on).
    evidence_rows: list of dicts, same shape, but the FULL, UNFILTERED
        non-P1 raw data — every row each non-P1 source carries under a
        given (source, Department, Category, Subcategory) label,
        regardless of merge-winner status. Used ONLY to vote on what that
        raw label "really means" against P1's real Department map.
        member_rows and evidence_rows are structurally disjoint by UPC
        when P1's raw data is "all-or-nothing" (every P1 row fully
        complete, hence P1 always wins what it carries): a UPC P1 also
        carries can never be a merge winner for a non-P1 source (so it's
        evidence, never a member), and a UPC a non-P1 source actually won
        was, by definition, not covered by P1 (so it's a member, never
        evidence) — confirmed true of this app's real NWG data (zero
        blank cells across all 175,307 rows). Conflating the two
        (computing both from the same winner-restricted or same
        unfiltered set) either starves evidence to zero or massively
        fragments combos — both were real bugs found during validation.
    p1_department: {upc: department} — P1's own non-blank Departments.
        This is the round-1 evidence pool ("known").
    strict_map: {(source_key, raw_department): trust_direct_evidence(bool)}
        — a department listed here opts out of chaining/promotion; only
        direct P1 evidence can decide it, and only if trust_direct is True.
    config: threshold overrides; falls back to DEFAULT_CONFIG per key.
    unmatched_defaults: {(source_key, raw_department): default_department}
        — a curated fallback suggestion for a combo with ZERO evidence
        anywhere (nothing for the algorithm to infer on its own), keyed
        by exact raw Department text; ("any", raw_department) matches
        regardless of source, an exact-source entry wins over "any" for
        the same text if both exist (that precedence is resolved by the
        caller — see _load_reference_data — this dict is already flat).
        Confirmed against script.py's real source: applied once, BEFORE
        any promotion pass runs, as that combo's starting suggestion —
        Category Match/Brand Match can still overwrite it later in the
        same run if they find real evidence-backed agreement; it's only
        the FINAL suggestion when nothing better ever replaces it.
    """
    cfg = {**DEFAULT_CONFIG, **(config or {})}

    # --- Build the combo universe (membership only) -------------------
    combos = {}  # key -> combo dict, accumulated in place
    for row in member_rows:
        upc = row["upc"]
        key = (row["source_key"], _norm(row["department"]), _norm(row["category"]), _norm(row["subcategory"]))
        combo = combos.get(key)
        if combo is None:
            combo = {
                "source_key": key[0], "raw_department": key[1], "raw_category": key[2], "raw_subcategory": key[3],
                "upcs": [], "brands": [], "descriptions": [], "evidence_upcs": [],
                "is_strict": (key[0], key[1]) in strict_map,
                "trust_direct_evidence": strict_map.get((key[0], key[1]), False),
            }
            combos[key] = combo
        combo["upcs"].append(upc)

    for combo in combos.values():
        combo["n_upcs_total"] = len(combo["upcs"])

    # --- Evidence pool: full, unfiltered non-P1 data, keyed the same way,
    # completely independent of merge-winner status. Only keys that also
    # have at least one member (i.e. something that actually needs a
    # decision) get a combo object, so evidence for labels nobody ever
    # won under is simply unused.
    # brands/descriptions are ALSO populated here, from this same full
    # unfiltered pool — confirmed against script.py's real source (the
    # Brand Match pass and the per-UPC Brand/Root/Description passes all
    # build their donor tables from `cleaned_dfs[source_key]` masked to a
    # combo's raw label, i.e. the combo's FULL raw item group, not just
    # its winner-restricted member subset). A combo's "own real items"
    # for signal-building purposes is always this full group.
    for row in evidence_rows:
        key = (row["source_key"], _norm(row["department"]), _norm(row["category"]), _norm(row["subcategory"]))
        combo = combos.get(key)
        if combo is not None:
            combo["evidence_upcs"].append(row["upc"])
            if row.get("brand"):
                combo["brands"].append(_norm(row["brand"]))
            combo["descriptions"].append(row.get("description") or "")

    # --- Round 1: direct evidence against P1 -------------------------
    known = {u: _norm(d) for u, d in p1_department.items()}  # upc -> department, grows each chain round

    def _tier_combo(combo, known_map, round_num, direct_only_map):
        if combo["is_strict"] and not combo["trust_direct_evidence"]:
            # Suggestion-only — computed from the FULL (possibly chain-
            # grown) known_map purely so a human reviewing this combo sees
            # an honest "here's what the evidence points to" (confirmed
            # against the real workbook: KEHE Bulk/BULK SNACKS/SNACKS shows
            # New Department = GROCERY at Confidence "Needs Review -
            # Ambiguous (100.0%)", "partly inferred through another
            # distributor's own data") — never used to decide tier=="auto".
            evidence_upcs = [u for u in combo["evidence_upcs"] if u in known_map]
            n_evidence = len(evidence_upcs)
            majority, purity, runner_up, runner_share = _purity_and_majority([known_map[u] for u in evidence_upcs])
            combo["n_evidence"] = n_evidence
            combo["purity"] = purity
            combo["majority_department"] = majority
            combo["runner_up_department"] = runner_up
            combo["runner_up_share"] = runner_share
            combo["tier"] = "review" if n_evidence else "unmatched"
            combo["resolved_via"] = None
            combo["chain_round"] = None
            combo["suggested_department"] = majority
            return

        # Confirmed against the real workbook's own "Strict Departments"
        # sheet documentation: "Chained ... never counts as evidence here
        # [for a Strict Department]" — "Either way" (regardless of Trust
        # Direct Evidence). A Strict combo with Trust Direct Evidence
        # checked therefore ALWAYS evaluates against direct_only_map (P1's
        # own literal Departments), never the chain-grown known_map a
        # non-strict combo uses — so it can only ever tier=="auto" via a
        # literal direct match, never via another combo's inferred
        # evidence donated through chaining.
        effective_map = direct_only_map if combo["is_strict"] else known_map
        evidence_upcs = [u for u in combo["evidence_upcs"] if u in effective_map]
        n_evidence = len(evidence_upcs)
        majority, purity, runner_up, runner_share = _purity_and_majority([effective_map[u] for u in evidence_upcs])
        combo["n_evidence"] = n_evidence
        combo["purity"] = purity
        combo["majority_department"] = majority
        combo["runner_up_department"] = runner_up
        combo["runner_up_share"] = runner_share

        clears_bar = n_evidence >= cfg["min_sample"] and (purity or 0) >= cfg["min_purity"]
        if clears_bar:
            combo["tier"] = "auto"
            combo["suggested_department"] = majority
            used_only_direct = all(u in direct_only_map for u in evidence_upcs)
            combo["resolved_via"] = "Direct" if used_only_direct else f"Chained (via {n_evidence} known UPCs)"
            combo["chain_round"] = 1 if used_only_direct else round_num
        else:
            combo["tier"] = "review" if n_evidence else "unmatched"
            combo["resolved_via"] = None
            combo["chain_round"] = None
            combo["suggested_department"] = majority

    for combo in combos.values():
        _tier_combo(combo, known, round_num=1, direct_only_map=p1_department)

    # --- Chaining rounds 2..max_chain_rounds --------------------------
    for round_num in range(2, cfg["max_chain_rounds"] + 1):
        newly_known = {}
        for combo in combos.values():
            # Confirmed against script.py's real source: the chaining
            # donation loop has NO is_strict exclusion — a Strict
            # Department combo can still donate its UPCs to help OTHER
            # combos chain, even though it can never become tier=="auto"
            # itself. (Currently a no-op with an empty Strict Departments
            # table, but matters once one is configured.)
            if combo["tier"] == "auto":
                for u in combo["upcs"]:
                    newly_known.setdefault(u, combo["suggested_department"])
        if not newly_known:
            break
        grown = {k: v for k, v in newly_known.items() if k not in known}
        if not grown:
            break
        known.update(grown)
        for combo in combos.values():
            if combo["tier"] != "auto":
                _tier_combo(combo, known, round_num=round_num, direct_only_map=p1_department)

    # Label combos that gained evidence via chaining but never cleared the bar.
    for combo in combos.values():
        if combo["tier"] != "auto" and combo["resolved_via"] is None:
            evidence_upcs = [u for u in combo["evidence_upcs"] if u in known]
            direct_evidence_n = len([u for u in combo["evidence_upcs"] if u in p1_department])
            if len(evidence_upcs) > direct_evidence_n:
                combo["resolved_via"] = "Chained - Insufficient" if direct_evidence_n == 0 else "Partially Chained"

    # --- Unmatched Department Defaults: a curated starting suggestion for
    # a combo with literally zero evidence anywhere, applied BEFORE the
    # promotion passes so real evidence-backed agreement (Category
    # Match/Brand Match) can still override it this same run.
    if unmatched_defaults:
        for combo in combos.values():
            if combo["tier"] == "unmatched" and combo["n_evidence"] == 0:
                default_dept = unmatched_defaults.get((combo["source_key"], combo["raw_department"]))
                if default_dept is None:
                    default_dept = unmatched_defaults.get(("any", combo["raw_department"]))
                if default_dept:
                    combo["suggested_department"] = default_dept
                    combo["resolved_via"] = "Default from Key"

    # --- Promotion passes (frozen snapshot each pass) -----------------
    _promote_category_match_pass1(combos, cfg)
    _promote_category_match_pass2(combos, cfg)
    _promote_brand_match(combos, cfg)

    for combo in combos.values():
        combo.pop("_skip_brand_match", None)
    # brands/descriptions/evidence_upcs are deliberately NOT stripped here
    # — apply_upc_level_overrides (the per-UPC Brand/Root/Description
    # passes) needs this same full-raw-group data to build its own donor
    # tables from auto-tier combos, per script.py's real source. The
    # caller (run_engine) strips them after that call, right before
    # writing combos back to the DB.
    return list(combos.values())


def _category_match_eligible(combo: dict, cfg: dict) -> bool:
    """Confirmed against script.py's real source (this is the single
    biggest fidelity gap found during validation): Category Match 1/2
    apply ONLY to a combo that is tier=="unmatched", OR tier=="review"
    with genuinely thin evidence (n_evidence < min_sample). A review
    combo that already has substantial evidence (n_evidence >= min_sample)
    but simply failed the purity bar — real, split evidence — is NOT
    eligible, no matter how unanimous its Category/Subcategory siblings
    look. Omitting this gate was previously inflating promotions by
    roughly 9x against script.py's real output.

    Also confirmed against the real source (build_department_category_mapping's
    row loop, the `_is_strict(row["source"], row["department"])` check at
    its very top): a Strict Department combo is excluded as a CANDIDATE
    from every promotion pass (Category Match 1/2, Brand Match, keyword),
    not just as a donor — regardless of its own trust_direct_evidence
    setting, which only ever affects Direct evidence, never these passes.
    This shares the same eligibility gate used by _promote_brand_match."""
    if combo["is_strict"]:
        return False
    if combo["tier"] == "unmatched":
        return True
    return combo["tier"] == "review" and combo["n_evidence"] < cfg["min_sample"]


def _promote_category_match_pass1(combos: dict, cfg: dict):
    """Same (source, Category, Subcategory), different Department: if the
    frozen auto-tier siblings agree on exactly one suggested Department,
    promote an ELIGIBLE (see _category_match_eligible) review/unmatched
    sibling to auto."""
    snapshot = {k: dict(v) for k, v in combos.items()}
    by_cat = {}
    for key, combo in snapshot.items():
        if combo["tier"] == "auto" and not combo["is_strict"] and not _is_blocked_bucket(combo["raw_category"], combo["raw_subcategory"]):
            by_cat.setdefault((combo["source_key"], combo["raw_category"], combo["raw_subcategory"]), []).append(combo)

    for key, combo in combos.items():
        if combo["tier"] == "auto" or _is_blocked_bucket(combo["raw_category"], combo["raw_subcategory"]):
            continue
        if not _category_match_eligible(combo, cfg):
            continue
        siblings = by_cat.get((combo["source_key"], combo["raw_category"], combo["raw_subcategory"]), [])
        suggestions = {s["suggested_department"] for s in siblings}
        if len(suggestions) == 1:
            dept = next(iter(suggestions))
            n = sum(s["n_evidence"] for s in siblings)
            combo["tier"] = "auto"
            combo["suggested_department"] = dept
            combo["resolved_via"] = f"Category Match (via {dept}, {n} known UPCs)"


def _promote_category_match_pass2(combos: dict, cfg: dict):
    """Same (source, Department, Category), different Subcategory: needs
    ALL frozen siblings (regardless of count) to agree on exactly one
    distinct suggested Department (mirrors Pass 1's own "exactly one
    distinct suggestion" rule, confirmed against script.py's real
    source — NOT "the first Department group with enough siblings",
    which could wrongly promote even when a second, competing
    Department also had enough agreeing siblings). If that single
    distinct Department's sibling count clears cat_match2_min_siblings,
    the combo auto-promotes; if there IS unanimous single-Department
    agreement but the count falls short, it's suggestion-only.

    Also confirmed against script.py's real source: a combo that reaches
    this unanimous-single-Department branch (auto-applied OR
    suggestion-only) is marked ineligible for the later Brand Match pass
    — script.py's per-row cascade `continue`s out in both cases, so
    Brand Match never even sees that row. Only a combo with NO siblings
    at all, or siblings that disagree across >1 distinct Department,
    falls through to Brand Match."""
    snapshot = {k: dict(v) for k, v in combos.items()}
    by_dept_cat = {}
    for key, combo in snapshot.items():
        if combo["tier"] == "auto" and not combo["is_strict"] and not _is_blocked_bucket(combo["raw_category"], combo["raw_subcategory"]):
            by_dept_cat.setdefault((combo["source_key"], combo["raw_department"], combo["raw_category"]), []).append(combo)

    for key, combo in combos.items():
        if combo["tier"] == "auto" or _is_blocked_bucket(combo["raw_category"], combo["raw_subcategory"]):
            continue
        if not _category_match_eligible(combo, cfg):
            continue
        siblings = by_dept_cat.get((combo["source_key"], combo["raw_department"], combo["raw_category"]), [])
        siblings = [s for s in siblings if s["raw_subcategory"] != combo["raw_subcategory"]]
        if not siblings:
            continue
        distinct = {s["suggested_department"] for s in siblings}
        if len(distinct) != 1:
            continue
        dept = next(iter(distinct))
        n = sum(s["n_evidence"] for s in siblings)
        combo["_skip_brand_match"] = True
        if len(siblings) >= cfg["cat_match2_min_siblings"]:
            combo["tier"] = "auto"
            combo["suggested_department"] = dept
            combo["resolved_via"] = f"Category Match (via {combo['raw_category']}, {n} known UPCs)"
        else:
            combo["suggested_department"] = dept
            combo["resolved_via"] = "Category Match (single sibling, not auto-applied)"


def _promote_brand_match(combos: dict, cfg: dict):
    """Builds a 'strong brand -> department' table from frozen auto combos'
    member brands, then promotes a review/unmatched combo whose own items
    carry a strong brand agreeing well enough — gated by THREE conditions,
    all required (confirmed against script.py's real source): brand purity
    itself, no conflicting Category consensus (`cat_conflict`, computed
    from real UPC counts across all auto combos sharing this combo's own
    (source, Category) — not the brand-vote fraction, a genuinely separate
    check), and a low keyword-conflict fraction (candidate items whose own
    Description mentions something that contradicts the brand-suggested
    department, e.g. "beef" mentioned in an item Brand Match wants to call
    GROCERY)."""
    snapshot = {k: dict(v) for k, v in combos.items()}

    brand_dept_evidence = {}  # brand -> {department: count}
    for combo in snapshot.values():
        if combo["tier"] == "auto" and not combo["is_strict"]:
            for b in combo.get("brands", []):
                brand_dept_evidence.setdefault(b, Counter())[combo["suggested_department"]] += 1

    strong_brands = {}  # brand -> (department, purity, n)
    for brand, counter in brand_dept_evidence.items():
        n = sum(counter.values())
        dept, count = counter.most_common(1)[0]
        purity = count / n
        if n >= cfg["brand_combo_min_sample"] and purity >= cfg["brand_combo_min_purity"]:
            strong_brands[brand] = (dept, purity, n)

    if not strong_brands:
        return

    # cat_majority: (source, Category) -> established majority department,
    # computed from REAL UPC counts (n_upcs_total) across all auto combos
    # sharing that (source, Category) regardless of Subcategory — needs
    # >=brand_category_consensus_min_sample total UPCs and >=min_purity
    # agreement to count as "established."
    cat_totals = {}
    for combo in snapshot.values():
        if combo["tier"] == "auto" and not combo["is_strict"]:
            cat_key = (combo["source_key"], combo["raw_category"])
            cat_totals.setdefault(cat_key, Counter())[combo["suggested_department"]] += combo["n_upcs_total"]
    cat_majority = {}
    for cat_key, counter in cat_totals.items():
        total = sum(counter.values())
        if total >= cfg["brand_category_consensus_min_sample"]:
            dept, count = counter.most_common(1)[0]
            if count / total >= cfg["min_purity"]:
                cat_majority[cat_key] = dept

    for key, combo in combos.items():
        if combo["tier"] == "auto" or combo.get("_skip_brand_match") or _is_blocked_bucket(combo["raw_category"], combo["raw_subcategory"]):
            continue
        if not _category_match_eligible(combo, cfg):
            continue
        brands = combo.get("brands", [])
        if not brands:
            continue
        matching = [strong_brands[b] for b in brands if b in strong_brands]
        if not matching:
            continue
        dept_counts = Counter(m[0] for m in matching)
        dept, n_matching = dept_counts.most_common(1)[0]
        # Purity is the top department's share of the MATCHED (strong-
        # brand) votes only (len(matching), confirmed against script.py's
        # real `brand_n = sum(brand_votes.values())` /
        # `brand_purity = brand_top_n / brand_n`) — NOT of every branded
        # item in the combo (len(brands)), which would wrongly dilute
        # purity with items whose brand never even qualified as "strong".
        item_purity = n_matching / len(matching)
        if len(matching) < cfg["brand_item_min_sample"] or item_purity < cfg["brand_item_min_purity"]:
            continue

        cat_conflict = cat_majority.get((combo["source_key"], combo["raw_category"])) not in (None, dept)
        kw_conflict_frac = _keyword_conflict_fraction(combo.get("descriptions", []), dept)
        if not cat_conflict and kw_conflict_frac < cfg["brand_keyword_conflict_max_fraction"]:
            n_known = sum(m[2] for m in matching if m[0] == dept)
            combo["tier"] = "auto"
            combo["suggested_department"] = dept
            combo["resolved_via"] = f"Brand Match ({n_known} known UPCs via Brand)"


# ---------------------------------------------------------------------
# Per-UPC passes (script.py Passes 4/5/6) — Brand Match, UPC Root Match,
# Description Word Match. These run on EVERY engine run, automatically,
# against individual UPCs whose combo never reached tier=="auto" — not
# gated behind a human manually breaking out the combo first. Any combo
# that picks up >=1 hit here gets auto-broken-out to UPC-level (see
# _write_back), which is what closes most of the remaining gap between
# combo-level auto-decide and script.py's real "Decided" percentage.
#
# All three share the same shape, confirmed against script.py's real
# source: build a {signal: department} majority table from the members of
# combos that already reached tier=="auto" this run (never from P1 directly,
# never from a prior run's manual decisions), then apply it to individual
# candidate UPCs, in strict precedence order (Brand > UPC Root >
# Description) — each later pass excludes UPCs the earlier ones already
# claimed, so a UPC is never decided by more than one pass.
# ---------------------------------------------------------------------

_DESCRIPTION_TOKEN_RE = re.compile(r"[a-z]{3,}")


def _tokenize_description(desc: str) -> set:
    return {t for t in _DESCRIPTION_TOKEN_RE.findall((desc or "").lower()) if t not in DESCRIPTION_WORD_EXCLUDE}


def apply_upc_level_overrides(combos: list, member_rows: list, already_covered_upcs: set, config: dict = None) -> dict:
    """Returns {upc: {"department": ..., "decided_via": ...}} for UPCs
    decided by the three per-UPC passes. Only ever touches a UPC whose
    combo has tier != 'auto', and never one already in
    already_covered_upcs (a standing human decision).

    member_rows: the SAME winner-restricted rows used to build combo
    membership in compute_combos — CANDIDATES for these per-UPC passes
    are always winner-restricted (only UPCs that actually need a
    decision), confirmed against script.py's real source
    (`mask = df["UPC"].isin(winner_upcs)` in each of
    build_department_brand_match_upc_overrides/_upc_root_match_/
    _description_match_). The DONOR tables (which brand/root/word maps to
    which Department), however, are built from each auto-tier combo's
    `evidence_upcs`/`brands`/`descriptions` fields on the combo dict
    itself — the FULL unfiltered raw group for that combo, not just its
    member subset (same real-source confirmation: those functions mask
    the FULL `cleaned_dfs[source]`, not a winner-restricted one, when
    building brand_evidence/root_evidence/word_evidence)."""
    cfg = {**DEFAULT_CONFIG, **(config or {})}
    combo_by_key = {
        (c["source_key"], c["raw_department"], c["raw_category"], c["raw_subcategory"]): c for c in combos
    }
    auto_combos = [c for c in combos if c["tier"] == "auto"]

    def _row_key(row):
        return (row["source_key"], _norm(row["department"]), _norm(row["category"]), _norm(row["subcategory"]))

    def _candidates(covered):
        for row in member_rows:
            upc = row["upc"]
            if upc in covered or _is_blocked_bucket(row["category"], row["subcategory"]):
                continue
            combo = combo_by_key.get(_row_key(row))
            # Confirmed against script.py's real source: per-UPC passes do
            # NOT exclude a Strict Department candidate the way the
            # combo-level promotion passes do — only tier == "auto" (which
            # already means "nothing left to decide here") is excluded.
            if combo is None or combo["tier"] == "auto":
                continue
            yield row

    decisions = {}
    covered = set(already_covered_upcs)

    # --- Pass 4: Brand Match (per-UPC) --------------------------------
    brand_evidence = {}
    for combo in auto_combos:
        dept = combo["suggested_department"]
        for brand in combo.get("brands", []):
            brand_evidence.setdefault(brand, Counter())[dept] += 1
    strong_brands = {}
    for brand, counter in brand_evidence.items():
        n = sum(counter.values())
        if n < cfg["upc_brand_match_min_sample"]:
            continue
        dept, count = counter.most_common(1)[0]
        purity = count / n
        if purity >= cfg["upc_brand_match_min_purity"]:
            strong_brands[brand] = (dept, purity)

    for row in _candidates(covered):
        brand = _norm(row.get("brand"))
        hit = strong_brands.get(brand) if brand else None
        if hit:
            dept, purity = hit
            if _has_keyword_conflict(row.get("description"), dept):
                continue
            decisions[row["upc"]] = {"department": dept, "decided_via": f"Auto-Applied (Brand Match, {brand}, {purity:.1%})"}
            covered.add(row["upc"])

    # --- Pass 5: UPC Root Match (first 7 characters of the UPC) -------
    root_evidence = {}
    for combo in auto_combos:
        dept = combo["suggested_department"]
        for upc in combo.get("evidence_upcs", []):
            root = str(upc)[:7]
            root_evidence.setdefault(root, Counter())[dept] += 1
    strong_roots = {}
    for root, counter in root_evidence.items():
        n = sum(counter.values())
        if n < cfg["upc_root_match_min_sample"]:
            continue
        dept, count = counter.most_common(1)[0]
        purity = count / n
        if purity >= cfg["upc_root_match_min_purity"]:
            strong_roots[root] = (dept, purity)

    for row in _candidates(covered):
        hit = strong_roots.get(str(row["upc"])[:7])
        if hit:
            dept, purity = hit
            if _has_keyword_conflict(row.get("description"), dept):
                continue
            decisions[row["upc"]] = {"department": dept, "decided_via": f"Auto-Applied (UPC Root Match, {purity:.1%})"}
            covered.add(row["upc"])

    # --- Pass 6: Description Word Match -------------------------------
    # One vote per ITEM containing a word (deduped via set()), not per
    # occurrence — a word repeated twice in one description still only
    # counts once for that item.
    word_evidence = {}
    for combo in auto_combos:
        dept = combo["suggested_department"]
        for desc in combo.get("descriptions", []):
            for tok in _tokenize_description(desc):
                word_evidence.setdefault(tok, Counter())[dept] += 1
    strong_words = {}
    for tok, counter in word_evidence.items():
        n = sum(counter.values())
        if n < cfg["description_match_min_sample"]:
            continue
        dept, count = counter.most_common(1)[0]
        purity = count / n
        if purity >= cfg["description_match_min_purity"]:
            strong_words[tok] = (dept, n)  # weight = word's own sample size

    for row in _candidates(covered):
        votes = Counter()
        for tok in _tokenize_description(row.get("description")):
            hit = strong_words.get(tok)
            if hit:
                dept, weight = hit
                votes[dept] += weight
        if not votes:
            continue
        total_votes = sum(votes.values())
        dept, top_votes = votes.most_common(1)[0]
        vote_share = top_votes / total_votes
        if vote_share >= cfg["description_match_min_vote_share"]:
            if _has_keyword_conflict(row.get("description"), dept):
                continue
            decisions[row["upc"]] = {"department": dept, "decided_via": f"Auto-Applied (Description Match, {vote_share:.1%})"}
            covered.add(row["upc"])

    # Confirmed against the real workbook's own "Strict Departments" sheet
    # documentation (not just the generic candidate-eligibility code path):
    # "NOTHING from Pass 4/5/6 (Brand/UPC Root/Description Match) - or
    # Chained/Category Match - ever counts as evidence here [for a Strict
    # Department] ... the ONLY thing that can auto-decide an INDIVIDUAL UPC
    # is a literal Exact UPC Match against Scan Advantage's own catalog" —
    # "Either way" (regardless of Trust Direct Evidence), so every Brand/
    # Root/Description hit computed above for a Strict combo's candidates
    # must be discarded here, never applied or used to trigger auto-break-
    # out. (Exact UPC Match itself isn't implemented as a fourth pass —
    # confirmed structurally disjoint-by-construction on this app's real
    # data: a member UPC, by definition, is never also covered by P1, so
    # it could never literally match P1's own catalog anyway.)
    if decisions:
        strict_upcs = {
            row["upc"] for row in member_rows
            if combo_by_key.get(_row_key(row), {}).get("is_strict")
        }
        decisions = {upc: v for upc, v in decisions.items() if upc not in strict_upcs}

    return decisions


# ---------------------------------------------------------------------
# DB I/O
# ---------------------------------------------------------------------

def _load_reference_data(engine):
    with engine.connect() as conn:
        config_row = conn.execute(text("SELECT * FROM dbo.dept_mapping_config WHERE config_id = 1")).mappings().first()
        strict_rows = conn.execute(text("SELECT source_key, old_department, trust_direct_evidence FROM dbo.dept_mapping_strict_departments")).mappings().all()
        default_rows = conn.execute(text("SELECT source_key, old_department, new_department FROM dbo.dept_mapping_unmatched_defaults")).mappings().all()
    config = dict(config_row) if config_row else {}
    config.pop("config_id", None)
    strict_map = {
        (r["source_key"], (r["old_department"] or "").strip().upper()): bool(r["trust_direct_evidence"])
        for r in strict_rows
    }
    unmatched_defaults = {
        (r["source_key"], (r["old_department"] or "").strip().upper()): r["new_department"]
        for r in default_rows
    }
    return config, strict_map, unmatched_defaults


def _load_engine_inputs(engine):
    """Narrow, filtered pulls only — see dept_mapping design notes for why
    this is safe to do in pandas rather than SQL at this data volume."""
    with engine.connect() as conn:
        # ISNULL, not just relying on ingest.py's "blanks are always ''"
        # convention — a NULL department/category/subcategory/brand reads
        # back from pyodbc as NaN (a float), and `NaN or ""` evaluates to
        # NaN (NaN is truthy), silently poisoning the combo key downstream.
        non_p1 = pd.read_sql(
            text(
                "SELECT upc, source_key, ISNULL(department, '') AS department, "
                "ISNULL(category, '') AS category, ISNULL(subcategory, '') AS subcategory, "
                "ISNULL(brand, '') AS brand, ISNULL(description, '') AS description "
                "FROM dbo.raw_items WHERE source_key <> 'nwg'"
            ),
            conn,
        )
        p1 = pd.read_sql(
            text("SELECT upc, department FROM dbo.raw_items WHERE source_key = 'nwg' AND ISNULL(department, '') <> ''"),
            conn,
        )
        # Confirmed against script.py's real source: a combo only ever
        # includes a UPC under the ONE source that actually WINS that UPC
        # in the merge (upc_to_winner_key[upc] == source_key) — never every
        # source whose raw file happens to also carry that UPC. Skipping
        # this filter is what made low-priority sources (C&S CA/PNW, last
        # in priority order) produce vastly more, much smaller/noisier
        # combos than script.py's real output — most of their raw catalog
        # is never actually the merge winner for any UPC, since a
        # higher-priority source usually already covers it.
        # dbo.items.source_key already holds this exact winner-per-UPC
        # from the last Merge run, so it's reused directly here rather
        # than re-deriving the whole priority-walk.
        winners = pd.read_sql(text("SELECT upc, source_key FROM dbo.items"), conn)
    winner_key = dict(zip(winners["upc"], winners["source_key"]))
    return non_p1, p1, winner_key


def run_engine(engine, config_overrides: dict = None) -> dict:
    """Runs the full engine against live data and writes the incremental
    delta back to dbo.dept_mapping_combos / dept_mapping_combo_upcs.
    Returns a summary dict for display: new_combos, auto_decided,
    needs_review, unmatched, demoted.
    """
    config, strict_map, unmatched_defaults = _load_reference_data(engine)
    if config_overrides:
        config.update(config_overrides)

    non_p1, p1, winner_key = _load_engine_inputs(engine)
    p1_department = dict(zip(p1["upc"], p1["department"]))
    # Two DIFFERENT populations of the same non-P1 raw data (see
    # compute_combos' docstring): evidence_rows is the FULL, unfiltered
    # set (every row each non-P1 source carries, used only to vote on
    # what a raw label means against P1's real Department map);
    # member_rows is restricted to UPCs each source actually WON in the
    # merge (the ones that actually need a decided Department).
    evidence_rows = non_p1.to_dict("records")
    member_non_p1 = non_p1[non_p1["upc"].map(winner_key) == non_p1["source_key"]]
    member_rows = member_non_p1.to_dict("records")

    fresh_combos = compute_combos(member_rows, evidence_rows, p1_department, strict_map, config, unmatched_defaults)

    with engine.connect() as conn:
        already_covered_rows = conn.execute(
            text("SELECT upc FROM dbo.dept_mapping_upc_overrides WHERE decided_via <> 'not_reviewed'")
        ).fetchall()
    already_covered_upcs = {r[0] for r in already_covered_rows}
    upc_decisions = apply_upc_level_overrides(fresh_combos, member_rows, already_covered_upcs, config)

    summary = _write_back(engine, fresh_combos, upc_decisions)
    summary["upc_level_auto_decided"] = len(upc_decisions)
    summary["upc_level_by_pass"] = {
        "Brand Match": sum(1 for v in upc_decisions.values() if "Brand Match" in v["decided_via"]),
        "UPC Root Match": sum(1 for v in upc_decisions.values() if "UPC Root Match" in v["decided_via"]),
        "Description Match": sum(1 for v in upc_decisions.values() if "Description Match" in v["decided_via"]),
    }
    _refresh_auto_departments(engine, p1_department)
    return summary


def _refresh_auto_departments(engine, p1_department: dict) -> None:
    """Keeps dbo.dept_mapping_departments' auto-sourced rows in sync with
    whatever Departments Scan Advantage's (NWG's) own raw data actually
    uses this run — confirmed against script.py's real "Departments"
    sheet ("The 16 Department values Scan Advantage's own data uses this
    run (auto-refreshed every run, always listed first) plus any extra
    rows typed in below"). A manually-added Department (source_type=
    'manual', e.g. a genuinely new one like "BULK" that doesn't exist in
    Scan Advantage's own catalog yet) is never touched here — only the
    'auto' rows are dropped and rebuilt fresh each run."""
    real_departments = sorted({d.strip().upper() for d in p1_department.values() if d and d.strip()})
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM dbo.dept_mapping_departments WHERE source_type = 'auto'"))
        manual_departments = {
            r[0] for r in conn.execute(text("SELECT department FROM dbo.dept_mapping_departments WHERE source_type = 'manual'")).fetchall()
        }
        # department is the table's own PRIMARY KEY — skip anything a
        # manual row already covers (e.g. a manually-added Department
        # that Scan Advantage's own catalog has since started using too)
        # rather than colliding with it.
        to_insert = [d for d in real_departments if d not in manual_departments]
        if to_insert:
            conn.execute(
                text("INSERT INTO dbo.dept_mapping_departments (department, source_type) VALUES (:department, 'auto')"),
                [{"department": d} for d in to_insert],
            )


def _write_back(engine, fresh_combos: list, upc_decisions: dict) -> dict:
    """Merges fresh tier/evidence results with standing human decisions —
    a genuine manual override (manual_department set) or an in-progress
    Broken Out combo is never overwritten by fresh evidence. See the
    module docstring / project plan for the exact rule.

    upc_decisions: {upc: {"department", "decided_via"}} from
    apply_upc_level_overrides — a combo whose fresh evidence still can't
    decide it as a whole, but that has >=1 member UPC individually
    decided by a per-UPC pass, auto-breaks-out to "broken_out" this same
    run. Confirmed against script.py's real source (build_final_dataset):
    `auto_break_out_keys` is computed fresh every run purely from Pass
    4/5/6 per-UPC evidence, unioned with (not replacing) whatever a human
    separately broke out by hand — the code's own comment calls this
    "the same mechanism as a person setting 'Break Out to UPC-Level?' =
    Yes by hand, just triggered by script.py itself." An earlier version
    of this function removed this entirely based on a since-corrected,
    incomplete research pass that only found the human-driven half of
    the real mechanism."""
    with engine.connect() as conn:
        existing = pd.read_sql(
            text(
                "SELECT combo_id, source_key, raw_department, raw_category, raw_subcategory, "
                "manual_department, approved, rejected, decision_state, decided_department, decided_via "
                "FROM dbo.dept_mapping_combos"
            ),
            conn,
        )
    # pd.read_sql hands back SQL NULL as NaN (a float) for a nullable
    # string column — and NaN is truthy in Python, which would make
    # `elif prior["manual_department"]:` below wrongly treat EVERY combo
    # as manually decided the moment this ran against a real, already-
    # populated table (never triggered by earlier runs, which always hit
    # the `prior is None` "new combo" branch instead on a freshly-cleared
    # table). Normalize every nullable object column's NaN to a real
    # None right after reading, once, instead of guarding each read site.
    existing = existing.astype(object).where(existing.notna(), None)
    existing_map = {
        (r["source_key"], r["raw_department"], r["raw_category"], r["raw_subcategory"]): r
        for r in existing.to_dict("records")
    }

    summary = {"new_combos": 0, "auto_decided": 0, "needs_review": 0, "unmatched": 0, "demoted": 0}
    to_upsert = []
    combo_upcs_rows = []

    for combo in fresh_combos:
        key = (combo["source_key"], combo["raw_department"], combo["raw_category"], combo["raw_subcategory"])
        prior = existing_map.get(key)
        row = {
            "source_key": combo["source_key"],
            "raw_department": combo["raw_department"],
            "raw_category": combo["raw_category"],
            "raw_subcategory": combo["raw_subcategory"],
            "n_upcs_total": combo["n_upcs_total"],
            "n_evidence": combo["n_evidence"],
            "purity": combo["purity"],
            "majority_department": combo["majority_department"],
            "runner_up_department": combo.get("runner_up_department"),
            "runner_up_share": combo.get("runner_up_share"),
            "suggested_department": combo.get("suggested_department"),
            "tier": combo["tier"],
            "resolved_via": combo["resolved_via"],
            "chain_round": combo["chain_round"],
            "is_strict": combo["is_strict"],
        }

        if prior is None:
            summary["new_combos"] += 1
            row["decision_state"] = "not_reviewed"
            row["decided_department"] = combo.get("suggested_department") if combo["tier"] == "auto" else None
            row["decided_via"] = "Auto" if combo["tier"] == "auto" else None
            row["manual_department"] = None
            row["approved"] = False
            row["rejected"] = False
            row["combo_id"] = None
        else:
            row["combo_id"] = prior["combo_id"]
            row["manual_department"] = prior["manual_department"]
            state = prior["decision_state"]
            if state in ("broken_out", "decided_broken_out"):
                row["decision_state"] = state
                row["decided_department"] = prior["decided_department"]
                row["decided_via"] = prior["decided_via"]
                row["approved"] = prior["approved"]
                row["rejected"] = prior["rejected"]
            elif prior["manual_department"]:
                row["decision_state"] = "decided"
                row["decided_department"] = prior["manual_department"]
                row["decided_via"] = prior["decided_via"] or "Manually Reviewed"
                row["approved"] = prior["approved"]
                row["rejected"] = prior["rejected"]
            elif prior["approved"] and prior["decision_state"] == "decided":
                if combo["tier"] == "auto":
                    row["decision_state"] = "decided"
                    row["decided_department"] = prior["decided_department"]  # pin, don't drift
                    row["decided_via"] = "Auto"
                    row["approved"] = True
                    row["rejected"] = False
                else:
                    row["decision_state"] = "not_reviewed"
                    row["decided_department"] = None
                    row["decided_via"] = None
                    row["approved"] = False
                    row["rejected"] = False
                    summary["demoted"] += 1
            elif prior["rejected"]:
                if combo["tier"] == "auto":
                    row["decision_state"] = "not_reviewed"
                    row["decided_department"] = None
                    row["decided_via"] = None
                    row["approved"] = False
                    row["rejected"] = True
                else:
                    row["decision_state"] = "not_reviewed"
                    row["decided_department"] = None
                    row["decided_via"] = None
                    row["approved"] = False
                    row["rejected"] = False
            else:
                row["decision_state"] = "not_reviewed"
                row["decided_department"] = combo.get("suggested_department") if combo["tier"] == "auto" else None
                row["decided_via"] = "Auto" if combo["tier"] == "auto" else None
                row["approved"] = False
                row["rejected"] = False

        # Auto-break-out: confirmed against script.py's real source
        # (build_final_dataset's `auto_break_out_keys`) — a combo that
        # never reached tier=="auto" itself, but has >=1 member UPC
        # individually decided by a per-UPC pass (Brand/UPC Root/
        # Description Match), moves to UPC-level review automatically
        # this same run — no human has to click "Break Out to UPC-Level"
        # first. Only applies when there's no standing human decision on
        # the combo already (a genuine manual_department/approved/rejected
        # state, handled above, always wins).
        combo_has_upc_hits = any(u in upc_decisions for u in combo["upcs"])
        if row["decision_state"] == "not_reviewed" and combo_has_upc_hits:
            row["decision_state"] = "broken_out"

        if row["tier"] == "auto":
            summary["auto_decided"] += 1
        elif row["tier"] == "review":
            summary["needs_review"] += 1
        else:
            summary["unmatched"] += 1

        to_upsert.append((key, row, combo["upcs"]))

    # Batched, not one-row-at-a-time: with thousands of combos, a Python
    # loop issuing one execute() per combo (as an earlier version of this
    # function did) means thousands of individual network round trips to
    # Azure SQL — this app is explicitly trying to minimize DB load/cost.
    # Passing a whole list of param dicts to one conn.execute(text(...))
    # call lets pyodbc's fast_executemany (already enabled in db.py's
    # get_engine()) send it as one batched round trip instead.
    new_rows = [row for _, row, _ in to_upsert if row["combo_id"] is None]
    existing_rows = [row for _, row, _ in to_upsert if row["combo_id"] is not None]

    with engine.begin() as conn:
        if new_rows:
            new_df = pd.DataFrame(new_rows).drop(columns=["combo_id"])
            # See the identical comment on the upc_overrides write below —
            # fast_executemany sizes its buffer off each chunk's first row,
            # so sort resolved_via longest-first to avoid a same-chunk
            # truncation error on a later, longer value.
            new_df["_sort_len"] = new_df["resolved_via"].str.len().fillna(0)
            new_df = new_df.sort_values("_sort_len", ascending=False).drop(columns=["_sort_len"])
            new_df.to_sql("dept_mapping_combos", conn, schema="dbo", if_exists="append", index=False, chunksize=5000)

        # Bulk insert doesn't hand back generated identities, and a
        # composite-key IN-list isn't valid T-SQL — simplest reliable fix
        # is one full re-select (this table is a few thousand rows at
        # most, cheap) to resolve every combo_id, new and existing alike.
        id_rows = conn.execute(
            text("SELECT combo_id, source_key, raw_department, raw_category, raw_subcategory FROM dbo.dept_mapping_combos")
        ).mappings().all()
        id_lookup = {
            (r["source_key"], r["raw_department"], r["raw_category"], r["raw_subcategory"]): r["combo_id"]
            for r in id_rows
        }

        if existing_rows:
            # Same fast_executemany buffer-sizing quirk as the inserts below.
            existing_rows_sorted = sorted(existing_rows, key=lambda r: len(r["resolved_via"] or ""), reverse=True)
            conn.execute(
                text(
                    """
                    UPDATE dbo.dept_mapping_combos SET
                        n_upcs_total = :n_upcs_total, n_evidence = :n_evidence, purity = :purity,
                        majority_department = :majority_department,
                        runner_up_department = :runner_up_department, runner_up_share = :runner_up_share,
                        suggested_department = :suggested_department,
                        tier = :tier, resolved_via = :resolved_via, chain_round = :chain_round, is_strict = :is_strict,
                        decision_state = :decision_state, decided_department = :decided_department,
                        decided_via = :decided_via, approved = :approved, rejected = :rejected,
                        last_computed_at = SYSUTCDATETIME()
                    WHERE combo_id = :combo_id
                    """
                ),
                existing_rows_sorted,
            )

        # Resolve every combo_id (new + existing) and rebuild combo_upcs in
        # two bulk statements total, regardless of how many combos there are.
        all_combo_ids = []
        combo_upcs_rows = []
        for key, row, upcs in to_upsert:
            combo_id = row["combo_id"] if row["combo_id"] is not None else id_lookup[key]
            all_combo_ids.append(combo_id)
            combo_upcs_rows.extend({"combo_id": combo_id, "upc": u} for u in upcs)

        if all_combo_ids:
            id_list = ", ".join(str(int(i)) for i in all_combo_ids)
            conn.execute(text(f"DELETE FROM dbo.dept_mapping_combo_upcs WHERE combo_id IN ({id_list})"))
        if combo_upcs_rows:
            pd.DataFrame(combo_upcs_rows).to_sql(
                "dept_mapping_combo_upcs", conn, schema="dbo", if_exists="append", index=False, chunksize=5000
            )

        # Populate dept_mapping_upc_overrides for every combo now sitting
        # broken_out (freshly this run via the auto-break-out logic above,
        # a human's explicit break_out_combo click, or carried forward
        # from a prior run) — a per-UPC pass hit becomes an approved
        # decision; every other member UPC gets the combo's own
        # suggested_department as an unreviewed pre-filled default,
        # matching script.py's real "combo_default" behavior. Never
        # touches a UPC that already has a real (non-"not_reviewed")
        # decided_via on file — a standing human or prior-run decision is
        # always left alone.
        broken_out_ids = {
            (row["combo_id"] if row["combo_id"] is not None else id_lookup[key])
            for key, row, _ in to_upsert
            if row["decision_state"] == "broken_out"
        }
        if broken_out_ids:
            sticky_upcs = set()
            if broken_out_ids:
                id_list = ", ".join(str(int(i)) for i in broken_out_ids)
                sticky_rows = conn.execute(
                    text(
                        f"SELECT upc FROM dbo.dept_mapping_upc_overrides "
                        f"WHERE combo_id IN ({id_list}) AND decided_via <> 'not_reviewed'"
                    )
                ).fetchall()
                sticky_upcs = {r[0] for r in sticky_rows}

            # A UPC can be carried by more than one source at once, so it
            # can legitimately belong to two different combos in the same
            # run (one per source) — but the table's PK is UPC alone (it
            # holds ONE decision per UPC, matching the single Department
            # Merge ultimately needs). Keep only the first combo touching
            # a given UPC each run, preferring one with an actual per-UPC
            # pass hit over a bare pending suggestion.
            override_by_upc = {}
            for key, row, upcs in to_upsert:
                combo_id = row["combo_id"] if row["combo_id"] is not None else id_lookup[key]
                if combo_id not in broken_out_ids:
                    continue
                for upc in upcs:
                    if upc in sticky_upcs:
                        continue
                    hit = upc_decisions.get(upc)
                    if hit:
                        candidate = {
                            "upc": upc, "combo_id": combo_id,
                            "suggested_department": hit["department"], "suggested_via": hit["decided_via"],
                            "department": hit["department"], "decided_via": hit["decided_via"],
                        }
                    else:
                        candidate = {
                            "upc": upc, "combo_id": combo_id,
                            "suggested_department": row.get("suggested_department"), "suggested_via": None,
                            "department": None, "decided_via": "not_reviewed",
                        }
                    existing_candidate = override_by_upc.get(upc)
                    if existing_candidate is None or (hit and existing_candidate["decided_via"] == "not_reviewed"):
                        override_by_upc[upc] = candidate
            override_rows = list(override_by_upc.values())

            if override_rows:
                touched_upcs = [r["upc"] for r in override_rows]
                # chunked IN-list delete keeps any single statement to a
                # sane size regardless of how many UPCs are touched.
                # Parameterized (expanding bindparam) rather than spliced
                # into the SQL string, since these UPCs originate from
                # ingested distributor files, not code-controlled values.
                delete_stmt = text("DELETE FROM dbo.dept_mapping_upc_overrides WHERE upc IN :upcs").bindparams(
                    bindparam("upcs", expanding=True)
                )
                for i in range(0, len(touched_upcs), 1000):
                    chunk = touched_upcs[i:i + 1000]
                    conn.execute(delete_stmt, {"upcs": chunk})
                # pyodbc's fast_executemany (enabled in db.py's get_engine())
                # sizes its parameter buffer off the FIRST row of each
                # executemany batch, not the actual column width — a longer
                # string later in the same 1000-row chunk than the first
                # row can overflow that buffer even though the real NVARCHAR
                # column is plenty wide. Sorting longest-first guarantees
                # each contiguous 1000-row chunk's own max length lands in
                # its first row, since chunking a globally-sorted list
                # preserves that per-slice.
                override_df = pd.DataFrame(override_rows)
                override_df["_sort_len"] = override_df["decided_via"].str.len().fillna(0)
                override_df = override_df.sort_values("_sort_len", ascending=False).drop(columns=["_sort_len"])
                override_df.to_sql(
                    "dept_mapping_upc_overrides", conn, schema="dbo", if_exists="append", index=False, chunksize=5000
                )

            # Promote any combo whose EVERY member UPC now has a real
            # decision (this run's fresh per-UPC hits, plus whatever was
            # already sticky from a prior run) straight to
            # decided_broken_out — mirrors apply_upc_decisions' own
            # graduation check (used by the Broken Out tab's "Stage Item
            # Decisions" button), applied here too so a combo that gets
            # 100% covered in the SAME run it's auto-broken-out doesn't
            # sit showing "0 pending" under decision_state=="broken_out"
            # forever. Confirmed as a real, common case: on this app's own
            # real data, 433 of 850 auto-broken-out combos were already
            # fully covered the moment they were broken out.
            id_list = ", ".join(str(int(i)) for i in broken_out_ids)
            pending_combo_ids = {
                r[0] for r in conn.execute(
                    text(
                        f"SELECT DISTINCT combo_id FROM dbo.dept_mapping_upc_overrides "
                        f"WHERE combo_id IN ({id_list}) AND decided_via = 'not_reviewed'"
                    )
                ).fetchall()
            }
            fully_decided_ids = broken_out_ids - pending_combo_ids
            if fully_decided_ids:
                fd_list = ", ".join(str(int(i)) for i in fully_decided_ids)
                conn.execute(
                    text(f"UPDATE dbo.dept_mapping_combos SET decision_state = 'decided_broken_out' WHERE combo_id IN ({fd_list})")
                )

        # Combos that no longer exist in fresh data (e.g. source removed/re-uploaded
        # without those rows) are left in place with their last-known state rather
        # than deleted — a human decision shouldn't vanish just because this run's
        # raw data happened not to include it.

    return summary


def get_upc_department_overrides(engine) -> dict:
    """upc -> decided Department, from both combo-level decisions and
    per-UPC Broken Out overrides. This is what Merge substitutes in for a
    non-P1-winning UPC's Department. Not cached here — the caller (app.py)
    owns caching via its existing @st.cache_data + .clear() convention."""
    with engine.connect() as conn:
        combo_rows = conn.execute(
            text(
                """
                SELECT cu.upc, c.decided_department
                FROM dbo.dept_mapping_combo_upcs cu
                JOIN dbo.dept_mapping_combos c ON c.combo_id = cu.combo_id
                WHERE c.decided_department IS NOT NULL
                """
            )
        ).fetchall()
        override_rows = conn.execute(
            text("SELECT upc, department FROM dbo.dept_mapping_upc_overrides WHERE department IS NOT NULL")
        ).fetchall()

    result = {r[0]: r[1] for r in combo_rows}
    for upc, department in override_rows:
        result[upc] = department  # per-UPC override always wins over its combo's own decision
    return result


# ---------------------------------------------------------------------
# Review UI support — Crosswalk ("review" tier) / Unmatched ("unmatched"
# tier). A combo only ever shows up here while decision_state is still
# "not_reviewed" — one already sent to Broken Out or Decided lives in its
# own tab instead, even if its tier still reads review/unmatched.
# ---------------------------------------------------------------------

def get_review_queue(engine, tier: str) -> pd.DataFrame:
    """Combos genuinely waiting on a human whole-combo decision for the
    given tier ("review" for Crosswalk, "unmatched" for Unmatched)."""
    with engine.connect() as conn:
        return pd.read_sql(
            text(
                "SELECT combo_id, source_key, raw_department, raw_category, raw_subcategory, "
                "n_upcs_total, n_evidence, purity, majority_department, "
                "runner_up_department, runner_up_share, suggested_department, "
                "resolved_via FROM dbo.dept_mapping_combos "
                "WHERE tier = :tier AND decision_state = 'not_reviewed' "
                "ORDER BY n_upcs_total DESC"
            ),
            conn,
            params={"tier": tier},
        )


def get_combo_member_items(engine, combo_id: int) -> pd.DataFrame:
    """The actual items a Crosswalk/Unmatched/Pending-Change combo would
    affect — for a person to sanity-check "is this really the group I
    think it is" before (or after) staging a decision, not just trust the
    aggregate count. Not capped — dept_mapping_combo_upcs is indexed on
    both combo_id and upc, and upc is dbo.items' own primary key, so this
    stays cheap even for a combo running into the thousands; the expander
    that renders it is collapsed by default and st.dataframe scrolls
    internally, so showing every row costs nothing extra until it's
    actually opened."""
    with engine.connect() as conn:
        return pd.read_sql(
            text(
                "SELECT i.upc, i.description, i.brand, i.pack, i.size, i.uom, "
                # Same manual_overrides signal Item Master surfaces — a
                # combo's raw evidence includes whatever a human has
                # already directly corrected on that item, which is worth
                # knowing before trusting the combo's own suggestion.
                "mo.updated_by AS manually_edited_by "
                "FROM dbo.dept_mapping_combo_upcs cu "
                "JOIN dbo.items i ON i.upc = cu.upc "
                "LEFT JOIN dbo.manual_overrides mo ON mo.upc = i.upc "
                "WHERE cu.combo_id = :combo_id "
                "ORDER BY i.description"
            ),
            conn,
            params={"combo_id": combo_id},
        )


def get_broken_out_combos(engine) -> pd.DataFrame:
    """Combos currently sitting decision_state == "broken_out" — a human
    already decided the combo as a whole can't be trusted, and needs its
    member UPCs decided one at a time instead. decided_count/override_count
    let the UI show "N of M items decided" per combo without a second
    round trip per row."""
    with engine.connect() as conn:
        return pd.read_sql(
            text(
                """
                SELECT c.combo_id, c.source_key, c.raw_department, c.raw_category, c.raw_subcategory,
                    c.n_upcs_total, c.tier,
                    (SELECT COUNT(*) FROM dbo.dept_mapping_upc_overrides o
                     WHERE o.combo_id = c.combo_id AND o.decided_via <> 'not_reviewed') AS decided_count,
                    (SELECT COUNT(*) FROM dbo.dept_mapping_upc_overrides o
                     WHERE o.combo_id = c.combo_id) AS override_count
                FROM dbo.dept_mapping_combos c
                WHERE c.decision_state = 'broken_out'
                ORDER BY c.n_upcs_total DESC
                """
            ),
            conn,
        )


def get_pending_upc_overrides(engine, combo_id: int) -> pd.DataFrame:
    """The still-undecided member UPCs of a Broken Out combo, for the
    per-item review grid — each pre-filled with its own suggested_department
    (from the per-UPC Brand/Root/Description passes, or the combo's own
    default) as a starting point, never as a silent auto-decision. Not
    capped, same reasoning as get_combo_member_items — cheap even for a
    large combo, and the caller's own expander/grid is what actually
    limits how much renders at once."""
    with engine.connect() as conn:
        return pd.read_sql(
            text(
                """
                SELECT o.upc, i.description, i.brand, i.pack, i.size, i.uom, o.suggested_department, o.suggested_via,
                    mo.updated_by AS manually_edited_by
                FROM dbo.dept_mapping_upc_overrides o
                LEFT JOIN dbo.items i ON i.upc = o.upc
                LEFT JOIN dbo.manual_overrides mo ON mo.upc = o.upc
                WHERE o.combo_id = :combo_id AND o.decided_via = 'not_reviewed'
                ORDER BY i.description
                """
            ),
            conn,
            params={"combo_id": combo_id},
        )


def get_auto_decided_upc_overrides(engine, combo_id: int) -> pd.DataFrame:
    """The member UPCs of an in-progress Broken Out combo that an automatic
    pass (Brand/UPC Root/Description Match) already decided — these don't
    show up in get_pending_upc_overrides (that's only the still-undecided
    ones), but a reviewer working through the rest of the combo has no way
    today to see, let alone sign off on, the ones the engine already
    filled in for them. Confirming one doesn't change its department —
    only its decided_via, from an automatic-pass label to "Manually
    Reviewed", as a record that a person actually looked at it."""
    with engine.connect() as conn:
        return pd.read_sql(
            text(
                """
                SELECT o.upc, i.description, i.brand, i.pack, i.size, i.uom, o.department, o.decided_via,
                    mo.updated_by AS manually_edited_by
                FROM dbo.dept_mapping_upc_overrides o
                LEFT JOIN dbo.items i ON i.upc = o.upc
                LEFT JOIN dbo.manual_overrides mo ON mo.upc = o.upc
                WHERE o.combo_id = :combo_id AND o.decided_via LIKE 'Auto-Applied%'
                ORDER BY i.description
                """
            ),
            conn,
            params={"combo_id": combo_id},
        )


def confirm_upc_decisions(engine, upcs: list, actor: str) -> None:
    """Marks already auto-decided UPCs (Broken Out's Brand/UPC Root/
    Description Match passes) as reviewed by a person — same department,
    just a decided_via change from the automatic-pass label to "Manually
    Reviewed", so a reviewer can track which auto-decisions they've
    actually looked at and confirmed versus ones nobody has checked yet."""
    if not upcs:
        return
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE dbo.dept_mapping_upc_overrides SET decided_via = 'Manually Reviewed' WHERE upc IN :upcs").bindparams(
                bindparam("upcs", expanding=True)
            ),
            {"upcs": upcs},
        )


def confirm_combo_decision(engine, combo_id: int, actor: str) -> None:
    """Same idea as confirm_upc_decisions, at the whole-combo level — marks
    an Auto-decided Whole Group combo as reviewed (decided_via: 'Auto' ->
    'Manually Reviewed'), without touching decided_department. This is
    purely an audit signal for now (see get_decided_combos' "Whole Group —
    Manual" vs "— Auto" status, which this flips), not a hard pin against
    future engine drift — that already exists separately via manual_department."""
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE dbo.dept_mapping_combos SET decided_via = 'Manually Reviewed' WHERE combo_id = :combo_id"),
            {"combo_id": combo_id},
        )


def get_combo_upc_decisions(engine, combo_id: int, limit: int = 250) -> pd.DataFrame:
    """Every member UPC of a Broken Out combo, in progress OR fully
    graduated to Decided, with its OWN final Department and how it got
    there — the per-item breakdown a single combo-level "Decided
    Department" column can't represent, since a Broken Out combo's whole
    point is that its items didn't all end up the same. Unlike
    get_pending_upc_overrides (only the ones STILL needing a decision),
    this returns every member row regardless of decided_via, so a
    finished (decided_broken_out) combo can be reviewed item by item
    after the fact from the Decided tab."""
    with engine.connect() as conn:
        return pd.read_sql(
            text(
                """
                SELECT TOP (:limit) o.upc, i.description, i.brand, o.department, o.decided_via,
                    o.updated_by AS decided_by, o.pushed_by
                FROM dbo.dept_mapping_upc_overrides o
                LEFT JOIN dbo.items i ON i.upc = o.upc
                WHERE o.combo_id = :combo_id
                ORDER BY i.description
                """
            ),
            conn,
            params={"combo_id": combo_id, "limit": limit},
        )


def apply_upc_decisions(engine, decisions: dict, pushed_by: str) -> None:
    """decisions: {upc: {"department", "staged_by"}} (staged_by falls back
    to pushed_by when not supplied). Writes each UPC's real decision —
    updated_by preserves who originally decided it (not necessarily
    whoever pushes), pushed_by/pushed_at record the push action
    separately — then promotes any combo whose EVERY member UPC now has a
    real (non-"not_reviewed") decision to decision_state ==
    "decided_broken_out", matching script.py's real per-combo graduation
    behavior."""
    if not decisions:
        return
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE dbo.dept_mapping_upc_overrides SET department = :department, "
                "decided_via = 'Manually Reviewed', updated_by = :staged_by, updated_at = SYSUTCDATETIME(), "
                "pushed_by = :pushed_by, pushed_at = SYSUTCDATETIME() "
                "WHERE upc = :upc"
            ),
            [
                {"upc": u, "department": d["department"], "staged_by": d.get("staged_by") or pushed_by, "pushed_by": pushed_by}
                for u, d in decisions.items()
            ],
        )
        combo_ids = [
            r[0] for r in conn.execute(
                text("SELECT DISTINCT combo_id FROM dbo.dept_mapping_upc_overrides WHERE upc IN :upcs").bindparams(
                    bindparam("upcs", expanding=True)
                ),
                {"upcs": list(decisions)},
            ).fetchall()
        ]
        for combo_id in combo_ids:
            remaining = conn.execute(
                text(
                    "SELECT COUNT(*) FROM dbo.dept_mapping_upc_overrides "
                    "WHERE combo_id = :combo_id AND decided_via = 'not_reviewed'"
                ),
                {"combo_id": combo_id},
            ).scalar()
            if remaining == 0:
                conn.execute(
                    text("UPDATE dbo.dept_mapping_combos SET decision_state = 'decided_broken_out' WHERE combo_id = :combo_id"),
                    {"combo_id": combo_id},
                )


def get_decided_combos(engine) -> pd.DataFrame:
    """Every group that's ACTUALLY finished — whole-group auto/manual
    decisions (a single decided_department for the whole combo) AND
    Broken Out groups that have graduated (decision_state ==
    "decided_broken_out", every one of their member UPCs individually
    decided). A group still IN PROGRESS (decision_state == "broken_out",
    some items decided, some not) never appears here, no matter how close
    to done it is — "in progress" only ever lives on the Broken Out tab,
    "finished" only ever lives here.

    "status" is one of 5 plain-English categories: "Whole Group — Auto",
    "Whole Group — Manual", "Broken Out — Fully Auto" (every one of its
    items was decided by an automatic pass), "Broken Out — Partially
    Auto" (a genuine mix), or "Broken Out — Manually Decided" (every item
    was typed in by hand, none from an automatic pass) — a per-UPC
    composition a single combo-level "Decided Via" value could never
    represent on its own."""
    with engine.connect() as conn:
        whole_group = pd.read_sql(
            text(
                "SELECT combo_id, source_key, raw_department, raw_category, raw_subcategory, "
                "n_upcs_total, decided_department, decided_via, approved, decision_state, "
                "last_decided_by, last_decided_at, pushed_by, pushed_at "
                "FROM dbo.dept_mapping_combos "
                "WHERE decided_department IS NOT NULL AND decision_state IN ('not_reviewed', 'decided')"
            ),
            conn,
        )
        broken = pd.read_sql(
            text(
                "SELECT combo_id, source_key, raw_department, raw_category, raw_subcategory, "
                "n_upcs_total, decided_department, decided_via, approved, decision_state, "
                "last_decided_by, last_decided_at, pushed_by, pushed_at "
                "FROM dbo.dept_mapping_combos WHERE decision_state = 'decided_broken_out'"
            ),
            conn,
        )
        composition = {}
        if not broken.empty:
            id_list = ", ".join(str(int(i)) for i in broken["combo_id"])
            comp_rows = conn.execute(
                text(
                    f"SELECT combo_id, "
                    f"SUM(CASE WHEN decided_via LIKE 'Auto-Applied%' THEN 1 ELSE 0 END) AS auto_n, "
                    f"COUNT(*) AS total_n "
                    f"FROM dbo.dept_mapping_upc_overrides WHERE combo_id IN ({id_list}) GROUP BY combo_id"
                )
            ).fetchall()
            composition = {r[0]: (int(r[1]), int(r[2])) for r in comp_rows}

    whole_group["status"] = whole_group["decided_via"].apply(
        lambda v: "Whole Group — Auto" if v == "Auto" else "Whole Group — Manual"
    )

    def _broken_status(combo_id):
        auto_n, total_n = composition.get(combo_id, (0, 0))
        if not total_n or auto_n == total_n:
            return "Broken Out — Fully Auto"
        if auto_n == 0:
            return "Broken Out — Manually Decided"
        return "Broken Out — Partially Auto"

    if not broken.empty:
        broken["status"] = broken["combo_id"].apply(_broken_status)

    combined = pd.concat([whole_group, broken], ignore_index=True) if not broken.empty else whole_group
    return combined.sort_values("n_upcs_total", ascending=False).reset_index(drop=True)


def get_combo_snapshot(engine, combo_id: int) -> dict:
    """Full current-state snapshot of one combo (its own row's decision
    fields + every one of its dept_mapping_upc_overrides rows) — captured
    right before an immediate, no-push action (Break Out / Send Back)
    changes it, so the UI's own "Undo" can restore the EXACT prior state
    afterward rather than approximating it. Break Out/Send Back aren't
    department decisions themselves (nothing gets decided by clicking
    them), so they apply immediately instead of sitting in Pending
    Changes — but a person should still be able to walk one back with a
    single click if it was a mis-click, the same way anything else here
    can be undone."""
    with engine.connect() as conn:
        combo_row = conn.execute(
            text(
                "SELECT decision_state, decided_department, decided_via, approved, rejected, manual_department "
                "FROM dbo.dept_mapping_combos WHERE combo_id = :combo_id"
            ),
            {"combo_id": combo_id},
        ).mappings().first()
        override_rows = conn.execute(
            text(
                "SELECT upc, department, decided_via, suggested_department, suggested_via "
                "FROM dbo.dept_mapping_upc_overrides WHERE combo_id = :combo_id"
            ),
            {"combo_id": combo_id},
        ).mappings().all()
    return {
        "combo": dict(combo_row) if combo_row else None,
        "overrides": [dict(r) for r in override_rows],
    }


def restore_combo_snapshot(engine, combo_id: int, snapshot: dict, actor: str) -> None:
    """Undoes an immediate Break Out / Send Back action by restoring the
    exact combo-row + per-UPC-override state get_combo_snapshot captured
    right before it ran — a real Undo of that specific action, not a
    generic "send it back to review" that would lose whatever item-level
    progress had been made."""
    combo = snapshot.get("combo")
    if combo is None:
        return
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                UPDATE dbo.dept_mapping_combos
                SET decision_state = :decision_state, decided_department = :decided_department,
                    decided_via = :decided_via, approved = :approved, rejected = :rejected,
                    manual_department = :manual_department,
                    last_decided_at = SYSUTCDATETIME(), last_decided_by = :actor
                WHERE combo_id = :combo_id
                """
            ),
            {**combo, "actor": actor, "combo_id": combo_id},
        )
        conn.execute(text("DELETE FROM dbo.dept_mapping_upc_overrides WHERE combo_id = :combo_id"), {"combo_id": combo_id})
        overrides = snapshot.get("overrides") or []
        if overrides:
            conn.execute(
                text(
                    "INSERT INTO dbo.dept_mapping_upc_overrides "
                    "(upc, combo_id, department, decided_via, suggested_department, suggested_via) "
                    "VALUES (:upc, :combo_id, :department, :decided_via, :suggested_department, :suggested_via)"
                ),
                [{**o, "combo_id": combo_id} for o in overrides],
            )


def revert_broken_out_combo(engine, combo_id: int, actor: str) -> None:
    """Sends a Broken Out group (in progress OR fully complete) back for
    fresh whole-group review — discards every one of its individual item
    decisions and resets it to "not_reviewed", so it reverts to whatever
    its own already-computed tier says (review/unmatched), immediately
    reachable again from Crosswalk/Unmatched. A genuinely different,
    more consequential action than revert_combo (which never touches
    per-UPC data, because a whole-group combo never has any) — the
    caller is responsible for warning that per-item work is discarded."""
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM dbo.dept_mapping_upc_overrides WHERE combo_id = :combo_id"), {"combo_id": combo_id})
        conn.execute(
            text(
                """
                UPDATE dbo.dept_mapping_combos
                SET manual_department = NULL, decision_state = 'not_reviewed',
                    decided_department = NULL, decided_via = NULL, approved = 0, rejected = 0,
                    last_decided_at = SYSUTCDATETIME(), last_decided_by = :actor
                WHERE combo_id = :combo_id
                """
            ),
            {"actor": actor, "combo_id": combo_id},
        )


def revert_combo(engine, combo_id: int, actor: str) -> None:
    """Sends a Decided combo back for fresh review — clears the manual
    override and decision entirely, so it reverts to whatever its own
    already-computed tier says (auto/review/unmatched), immediately
    reachable again from Crosswalk/Unmatched without needing an engine
    re-run. The reversal path a genuine manual override always has — an
    auto-decision that turns out to be wrong is never permanent."""
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                UPDATE dbo.dept_mapping_combos
                SET manual_department = NULL, decision_state = 'not_reviewed',
                    decided_department = NULL, decided_via = NULL, approved = 0, rejected = 0,
                    last_decided_at = SYSUTCDATETIME(), last_decided_by = :actor
                WHERE combo_id = :combo_id
                """
            ),
            {"actor": actor, "combo_id": combo_id},
        )


def confidence_label(tier: str, resolved_via, purity) -> str:
    """Plain-English "Confidence" text matching script.py's own real
    Crosswalk/Unmatched wording exactly (e.g. "Needs Review - Ambiguous
    (82.1%)", "Needs Review - Ambiguous (Partially Chained, 82.1%)",
    "Needs Review - Category Match (Unverified - Only 1 Sibling)",
    "Needs Review - No Evidence") — never raw jargon like "tier"/"purity"
    on their own."""
    # pd.read_sql hands back SQL NULL as NaN (a float) for a nullable
    # string column, not None — NaN is truthy AND not iterable, so a bare
    # `if resolved_via` / `in` check crashes here instead of just being
    # falsy. pandas isna() only exists on real pandas objects, so a Python
    # None (a synthetic default like the caller's tier/purity/None trio in
    # our own tests) needs its own check too.
    resolved_via = None if resolved_via is None or (isinstance(resolved_via, float) and pd.isna(resolved_via)) else resolved_via
    purity = None if purity is None or (isinstance(purity, float) and pd.isna(purity)) else purity
    if resolved_via and "single sibling" in resolved_via:
        return "Needs Review - Category Match (Unverified - Only 1 Sibling)"
    if resolved_via == "Default from Key":
        return "Needs Review - Default from Key"
    if tier == "unmatched" and not resolved_via:
        return "Needs Review - No Evidence"
    pct = f"{purity:.1%}" if purity is not None else "0.0%"
    if resolved_via in ("Chained - Insufficient", "Partially Chained"):
        return f"Needs Review - Ambiguous ({resolved_via}, {pct})"
    return f"Needs Review - Ambiguous ({pct})"


def get_departments(engine) -> pd.DataFrame:
    """The full canonical Department list for dropdowns app-wide (Manual
    Override Department, New Department on Broken Out UPCs, etc.) —
    Scan Advantage's OWN Departments (source_type='auto', refreshed every
    engine run from its real raw data) plus any manually-added extras
    (source_type='manual', e.g. a genuinely new Department that doesn't
    exist in Scan Advantage's own catalog yet) — confirmed against
    script.py's real "Departments" sheet. Auto rows sort first, matching
    that sheet's own "always listed first" ordering."""
    with engine.connect() as conn:
        return pd.read_sql(
            text(
                "SELECT department, source_type FROM dbo.dept_mapping_departments "
                "ORDER BY CASE source_type WHEN 'auto' THEN 0 ELSE 1 END, department"
            ),
            conn,
        )


def get_manual_override_details(engine, upcs: list) -> dict:
    """Full manual_overrides rows for a set of UPCs, keyed by upc — for the
    "you're about to overwrite someone's manual correction" guardrail on
    Item Master edits: shows who made the existing correction, when, and
    what it actually was, not just the fact that one exists."""
    if not upcs:
        return {}
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT upc, description, department, category, subcategory, brand, pack, size, uom, "
                "updated_by, updated_at FROM dbo.manual_overrides WHERE upc IN :upcs"
            ).bindparams(bindparam("upcs", expanding=True)),
            {"upcs": upcs},
        ).mappings().all()
    return {r["upc"]: dict(r) for r in rows}


def add_department(engine, department: str) -> None:
    """Manually adds a Department not yet in Scan Advantage's own data —
    e.g. a genuinely new one like "BULK" — immediately selectable
    everywhere a Department dropdown appears, no engine run needed.
    A no-op if it already exists (either as an auto or a prior manual
    row) — department is the table's own PRIMARY KEY."""
    department = department.strip().upper()
    if not department:
        return
    with engine.begin() as conn:
        exists = conn.execute(
            text("SELECT 1 FROM dbo.dept_mapping_departments WHERE department = :department"),
            {"department": department},
        ).first()
        if not exists:
            conn.execute(
                text("INSERT INTO dbo.dept_mapping_departments (department, source_type) VALUES (:department, 'manual')"),
                {"department": department},
            )


def remove_department(engine, department: str) -> None:
    """Removes a Department from the dropdown list. Only ever removes a
    MANUAL row — an 'auto' row (one of Scan Advantage's own real current
    Departments) can't be removed here since it would just reappear on
    the next engine run anyway; the underlying data would need to change
    first."""
    with engine.begin() as conn:
        conn.execute(
            text("DELETE FROM dbo.dept_mapping_departments WHERE department = :department AND source_type = 'manual'"),
            {"department": department.strip().upper()},
        )


def get_unmatched_defaults(engine) -> pd.DataFrame:
    with engine.connect() as conn:
        return pd.read_sql(
            text(
                "SELECT source_key, old_department, new_department FROM dbo.dept_mapping_unmatched_defaults "
                "ORDER BY source_key, old_department"
            ),
            conn,
        )


def save_unmatched_defaults(engine, rows: list, actor: str) -> None:
    """Full replace, matching the small-reference-table pattern already
    used for Strict Departments — rows: [{"source_key", "old_department",
    "new_department"}, ...]."""
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM dbo.dept_mapping_unmatched_defaults"))
        if rows:
            conn.execute(
                text(
                    "INSERT INTO dbo.dept_mapping_unmatched_defaults "
                    "(source_key, old_department, new_department, updated_by) "
                    "VALUES (:source_key, :old_department, :new_department, :updated_by)"
                ),
                [{**r, "updated_by": actor} for r in rows],
            )


def approve_combo(engine, combo_id: int, department: str, actor: str, pushed_by: str | None = None) -> None:
    """A human confirms `department` for this combo's whole membership —
    manual_department is the durable record (never silently overwritten
    by a future engine run's fresh evidence; see _write_back). `actor` is
    who actually decided the department — normally whoever staged the
    Approve, preserved through to push rather than overwritten by
    whoever happens to click Push; `pushed_by` records that separately,
    when known, so both survive independently."""
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                UPDATE dbo.dept_mapping_combos
                SET manual_department = :department, decision_state = 'decided',
                    decided_department = :department, decided_via = 'Manually Reviewed',
                    approved = 1, rejected = 0,
                    last_decided_at = SYSUTCDATETIME(), last_decided_by = :actor,
                    pushed_by = :pushed_by, pushed_at = SYSUTCDATETIME()
                WHERE combo_id = :combo_id
                """
            ),
            {"department": department, "actor": actor, "combo_id": combo_id, "pushed_by": pushed_by or actor},
        )


def get_decision_counts_for_combo(engine, combo_id: int) -> list:
    """{decided_via -> count} for every member UPC of a combo that
    already has a REAL decision (not the "not_reviewed" placeholder) —
    used to warn a person, before an immediate Send Back throws it away,
    exactly how much item-level work (auto or manual) is about to be
    lost."""
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT decided_via, COUNT(*) AS n FROM dbo.dept_mapping_upc_overrides "
                "WHERE combo_id = :combo_id AND decided_via <> 'not_reviewed' GROUP BY decided_via"
            ),
            {"combo_id": combo_id},
        ).fetchall()
    return [(r[0], int(r[1])) for r in rows]


def compute_upc_decisions_for_combo(engine, combo_id: int, config: dict = None) -> dict:
    """Recomputes the SAME per-UPC Brand/UPC Root/Description Word Match
    decisions the engine's own automatic passes would produce for one
    specific combo's member UPCs, using this run's current auto-tier
    evidence — without writing anything back or touching any other combo.
    Used to offer a human manually breaking a combo out a real choice:
    start with whatever auto-matching could decide right now, or a
    completely blank slate. Re-derives the full fresh combo set the same
    way run_engine does (see its own docstring for why this is safe to do
    in pandas at this data volume) since evidence tables (brands/
    descriptions/root maps) aren't persisted anywhere — they only exist
    as a byproduct of one engine pass."""
    cfg = {**DEFAULT_CONFIG, **(config or {})}
    _, strict_map, unmatched_defaults = _load_reference_data(engine)
    non_p1, p1, winner_key = _load_engine_inputs(engine)
    p1_department = dict(zip(p1["upc"], p1["department"]))
    evidence_rows = non_p1.to_dict("records")
    member_non_p1 = non_p1[non_p1["upc"].map(winner_key) == non_p1["source_key"]]
    member_rows = member_non_p1.to_dict("records")
    fresh_combos = compute_combos(member_rows, evidence_rows, p1_department, strict_map, cfg, unmatched_defaults)

    with engine.connect() as conn:
        already_covered_rows = conn.execute(
            text("SELECT upc FROM dbo.dept_mapping_upc_overrides WHERE decided_via <> 'not_reviewed'")
        ).fetchall()
        combo_upcs = {
            r[0] for r in conn.execute(
                text("SELECT upc FROM dbo.dept_mapping_combo_upcs WHERE combo_id = :combo_id"),
                {"combo_id": combo_id},
            ).fetchall()
        }
    already_covered_upcs = {r[0] for r in already_covered_rows}
    upc_decisions = apply_upc_level_overrides(fresh_combos, member_rows, already_covered_upcs, cfg)
    return {upc: v for upc, v in upc_decisions.items() if upc in combo_upcs}


def break_out_combo(engine, combo_id: int, actor: str, upc_decisions: dict = None) -> None:
    """Sends a combo to UPC-level review: flips decision_state to
    "broken_out" and seeds dept_mapping_upc_overrides with one row per
    member UPC — the same shape _write_back produces for an auto-broken-
    out combo, so the Broken Out tab can pick this up immediately rather
    than waiting for the next engine run. Never touches a UPC that
    already has an override row (e.g. from a prior run) — a standing
    decision is always left alone.

    upc_decisions: optional {upc: {"department", "decided_via"}} — from
    compute_upc_decisions_for_combo, when a human chose to start with
    whatever auto-matching can decide right now instead of a blank slate.
    Any member UPC not in this dict (or when upc_decisions is None) gets
    the combo's own suggested_department as an unreviewed pre-filled
    default, exactly as before."""
    with engine.begin() as conn:
        combo = conn.execute(
            text("SELECT suggested_department FROM dbo.dept_mapping_combos WHERE combo_id = :combo_id"),
            {"combo_id": combo_id},
        ).mappings().first()
        conn.execute(
            text(
                """
                UPDATE dbo.dept_mapping_combos
                SET decision_state = 'broken_out', last_decided_at = SYSUTCDATETIME(), last_decided_by = :actor
                WHERE combo_id = :combo_id
                """
            ),
            {"actor": actor, "combo_id": combo_id},
        )
        member_upcs = [
            r[0] for r in conn.execute(
                text("SELECT upc FROM dbo.dept_mapping_combo_upcs WHERE combo_id = :combo_id"),
                {"combo_id": combo_id},
            ).fetchall()
        ]
        if not member_upcs:
            return
        existing_upcs = {
            r[0] for r in conn.execute(
                text("SELECT upc FROM dbo.dept_mapping_upc_overrides WHERE upc IN :upcs").bindparams(
                    bindparam("upcs", expanding=True)
                ),
                {"upcs": member_upcs},
            ).fetchall()
        }
        new_upcs = [u for u in member_upcs if u not in existing_upcs]
        if new_upcs:
            default_suggestion = combo["suggested_department"] if combo else None
            rows = []
            for u in new_upcs:
                hit = (upc_decisions or {}).get(u)
                if hit:
                    rows.append({
                        "upc": u, "combo_id": combo_id,
                        "suggested_department": hit["department"], "suggested_via": hit["decided_via"],
                        "department": hit["department"], "decided_via": hit["decided_via"],
                    })
                else:
                    rows.append({
                        "upc": u, "combo_id": combo_id,
                        "suggested_department": default_suggestion, "suggested_via": None,
                        "department": None, "decided_via": "not_reviewed",
                    })
            conn.execute(
                text(
                    "INSERT INTO dbo.dept_mapping_upc_overrides "
                    "(upc, combo_id, suggested_department, suggested_via, department, decided_via) "
                    "VALUES (:upc, :combo_id, :suggested_department, :suggested_via, :department, :decided_via)"
                ),
                rows,
            )


# ---------------------------------------------------------------------
# Persisted Recent Moves / Pending Changes — shared across every editor's
# browser session instead of living only in one session's st.session_state
# (see migrations/add_dept_mapping_pending_tables.py for the full
# rationale: a chain of immediate actions, or a staged-but-unpushed
# decision, used to vanish with no trace the moment that one browser tab
# closed). Read through app.py's existing st.cache_data(no ttl) +
# explicit .clear()-after-write convention, so this costs one small read
# per change, shared by everyone, not a query per rerun.
# ---------------------------------------------------------------------

def get_recent_moves(engine) -> list:
    """Every immediate-action undo entry (Break Out / Send Back), most
    recent first — a real per-combo STACK, not one dedup'd slot per combo
    (confirmed as a real, previously-shipped bug: a second action on the
    same combo silently discarded the first snapshot, permanently losing
    whatever richer state it captured)."""
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT move_id, combo_id, source_key, label, n_upcs_total, description, "
                "snapshot_json, created_by, created_at "
                "FROM dbo.dept_mapping_recent_moves ORDER BY move_id DESC"
            )
        ).mappings().all()
    return [
        {
            "move_id": r["move_id"], "combo_id": r["combo_id"], "source_key": r["source_key"],
            "label": r["label"], "n_upcs_total": r["n_upcs_total"], "description": r["description"],
            "snapshot": json.loads(r["snapshot_json"]), "created_by": r["created_by"], "created_at": r["created_at"],
        }
        for r in rows
    ]


MAX_RECENT_MOVES = 30


def record_recent_move(
    engine, combo_id: int, source_key: str, label: str, n_upcs_total: int,
    description: str, snapshot: dict, actor: str,
) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO dbo.dept_mapping_recent_moves "
                "(combo_id, source_key, label, n_upcs_total, description, snapshot_json, created_by) "
                "VALUES (:combo_id, :source_key, :label, :n_upcs_total, :description, :snapshot_json, :created_by)"
            ),
            {
                "combo_id": combo_id, "source_key": source_key, "label": label,
                "n_upcs_total": n_upcs_total, "description": description,
                "snapshot_json": json.dumps(snapshot), "created_by": actor,
            },
        )
        # Keep only the most recent MAX_RECENT_MOVES rows overall (across
        # every combo/editor) so this doesn't grow forever.
        conn.execute(
            text(
                f"""
                DELETE FROM dbo.dept_mapping_recent_moves WHERE move_id IN (
                    SELECT move_id FROM dbo.dept_mapping_recent_moves
                    ORDER BY move_id DESC
                    OFFSET {MAX_RECENT_MOVES} ROWS FETCH NEXT 1000 ROWS ONLY
                )
                """
            )
        )


def delete_recent_move(engine, move_id: int) -> None:
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM dbo.dept_mapping_recent_moves WHERE move_id = :move_id"), {"move_id": move_id})


def clear_recent_moves_for_combo(engine, combo_id: int) -> None:
    """Called whenever a REAL decision for this combo gets pushed (its
    whole-group Approve, or its per-item Department choices) — that
    decision IS the new database state, so any earlier Break Out/Send
    Back snapshots for this same combo are no longer meaningful undo
    targets (there is nothing earlier left worth walking back to)."""
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM dbo.dept_mapping_recent_moves WHERE combo_id = :combo_id"), {"combo_id": combo_id})


def get_pending_changes(engine) -> dict:
    """Staged-but-not-pushed, RESOLVED whole-combo decisions, keyed by
    combo_id — visible to every editor the moment they're decided. A
    combo currently in dispute (see get_combo_suggestions) never appears
    here — it has no decided row until someone agrees with one of its
    suggestions."""
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT combo_id, tier, department, source_key, label, n_upcs_total, staged_by, staged_at, "
                "agreed_by, agreed_at, overridden_by, overridden_at FROM dbo.dept_mapping_pending_changes"
            ),
        ).mappings().all()
    return {
        r["combo_id"]: {
            "tier": r["tier"], "action": "approve", "department": r["department"],
            "source_key": r["source_key"], "label": r["label"], "n_upcs_total": r["n_upcs_total"],
            "staged_by": r["staged_by"], "staged_at": r["staged_at"],
            "agreed_by": r["agreed_by"], "agreed_at": r["agreed_at"],
            "overridden_by": r["overridden_by"], "overridden_at": r["overridden_at"],
        }
        for r in rows
    }


def get_combo_suggestions(engine, combo_ids=None) -> dict:
    """{combo_id: [{"staged_by", "department", "tier", "source_key",
    "label", "n_upcs_total", "suggested_at"}, ...]} — only combos
    currently DISPUTED (2+ distinct departments among their suggestions).
    A combo with exactly one distinct department is resolved (it's the
    decided row in dept_mapping_pending_changes) — its backers' rows are
    still sitting in dept_mapping_combo_suggestions (kept so a later
    divergent vote is still detected), just not returned here; use
    get_combo_backers for those."""
    query = "SELECT combo_id, staged_by, department, tier, source_key, label, n_upcs_total, suggested_at FROM dbo.dept_mapping_combo_suggestions"
    with engine.connect() as conn:
        if combo_ids is not None:
            if not combo_ids:
                return {}
            rows = conn.execute(
                text(f"{query} WHERE combo_id IN :combo_ids").bindparams(bindparam("combo_ids", expanding=True)),
                {"combo_ids": list(combo_ids)},
            ).mappings().all()
        else:
            rows = conn.execute(text(query)).mappings().all()
    by_combo = {}
    for r in rows:
        by_combo.setdefault(r["combo_id"], []).append(dict(r))
    return {combo_id: group for combo_id, group in by_combo.items() if len({g["department"] for g in group}) > 1}


def get_combo_backers(engine, combo_ids) -> dict:
    """{combo_id: set(staged_by)} for RESOLVED combos — who currently
    backs the decided department, whether they staged it, agreed with it
    later, or suggested it independently. Used to hide "I also agree"
    from someone who's already one of these, and to know who's still
    eligible to add a new suggestion under the MAX_DISTINCT_SUGGESTIONS
    cap."""
    if not combo_ids:
        return {}
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT combo_id, staged_by FROM dbo.dept_mapping_combo_suggestions "
                "WHERE combo_id IN :combo_ids"
            ).bindparams(bindparam("combo_ids", expanding=True)),
            {"combo_ids": list(combo_ids)},
        ).mappings().all()
    by_combo = {}
    for r in rows:
        by_combo.setdefault(r["combo_id"], set()).add(r["staged_by"])
    return by_combo


def upsert_combo_suggestion(engine, combo_id: int, tier, department: str, source_key: str, label: str, n_upcs_total: int, actor: str, is_admin: bool = False) -> dict:
    """Records `actor`'s own opinion of this combo's department — their
    own row only, replacing any EARLIER opinion they personally gave,
    never anyone else's.

    Resolution requires genuine UNANIMITY among whoever currently has an
    opinion on file — the instant every current vote lands on the same
    department, it's decided (the overwhelmingly common case: a lone
    suggester, or everyone already agreeing). A disagreement can ONLY be
    resolved by the people who actually hold the differing opinions
    changing their OWN vote to match — an uninvolved third person voting
    for one of the existing options does NOT tip it, because the
    dissenting vote is still sitting there unchanged; it only adds their
    name to that option's supporter list. A third person who wants to
    weigh in either backs an existing option (which does nothing to force
    a resolution) or proposes a genuinely different one (which does
    nothing either, beyond being visible) — the only actions that matter
    are the ones from people already in the disagreement. Admins have a
    SEPARATE, exclusive override (see admin_override_combo) that isn't
    part of this voting pool at all.

    Capped at MAX_DISTINCT_SUGGESTIONS distinct departments per combo — a
    genuinely new (6th) department from someone who isn't already one of
    the existing distinct suggesters is refused outright (returns
    {"blocked": True, "departments": [...]}) rather than piling on
    indefinitely; from that point on only the people already among those
    departments can move it (by changing their own vote to one of the
    existing options), or an admin overrides. Backing an EXISTING option
    is never capped, no matter how many people do it.

    Returns {"disputed": bool, "departments": [...]} normally, or
    {"blocked": True, "departments": [...]} when the cap refused a new
    department, or {"locked": True} when an admin override is sitting on
    this combo — it's off-limits to the normal voting pool entirely until
    an admin removes the override (see remove_combo_override)."""
    with engine.begin() as conn:
        locked = conn.execute(
            text("SELECT overridden_by FROM dbo.dept_mapping_pending_changes WHERE combo_id = :combo_id AND overridden_by IS NOT NULL"),
            {"combo_id": combo_id},
        ).mappings().first()
        if locked and not is_admin:
            return {"locked": True, "overridden_by": locked["overridden_by"]}
        existing = conn.execute(
            text("SELECT department, staged_by FROM dbo.dept_mapping_combo_suggestions WHERE combo_id = :combo_id"),
            {"combo_id": combo_id},
        ).mappings().all()
        others_departments = {r["department"] for r in existing if r["staged_by"] != actor}
        if department not in others_departments and len(others_departments) >= MAX_DISTINCT_SUGGESTIONS:
            return {"blocked": True, "departments": sorted(others_departments)}
        conn.execute(
            text("DELETE FROM dbo.dept_mapping_combo_suggestions WHERE combo_id = :combo_id AND staged_by = :staged_by"),
            {"combo_id": combo_id, "staged_by": actor},
        )
        conn.execute(
            text(
                "INSERT INTO dbo.dept_mapping_combo_suggestions "
                "(combo_id, staged_by, department, tier, source_key, label, n_upcs_total) "
                "VALUES (:combo_id, :staged_by, :department, :tier, :source_key, :label, :n_upcs_total)"
            ),
            {
                "combo_id": combo_id, "staged_by": actor, "department": department, "tier": tier,
                "source_key": source_key, "label": label, "n_upcs_total": n_upcs_total,
            },
        )
        rows = conn.execute(
            text(
                "SELECT department, staged_by, tier, source_key, label, n_upcs_total, suggested_at "
                "FROM dbo.dept_mapping_combo_suggestions WHERE combo_id = :combo_id ORDER BY suggested_at"
            ),
            {"combo_id": combo_id},
        ).mappings().all()
        distinct_departments = sorted({r["department"] for r in rows})
        if len(distinct_departments) > 1:
            conn.execute(text("DELETE FROM dbo.dept_mapping_pending_changes WHERE combo_id = :combo_id"), {"combo_id": combo_id})
            _clear_dept_push_approvals(conn)
            return {"disputed": True, "departments": distinct_departments}

        # Unanimous — whether that's a lone suggester or everyone who
        # ever disagreed has now converged. The suggestion pool is NEVER
        # cleared here (even after a genuine disagreement just settled) —
        # it stays the live record of who currently backs this decision,
        # so that a LATER divergent vote (a new third party, or one of
        # today's backers changing their mind again) is still recognized
        # as a real disagreement and pulls this back into "needs
        # agreement" instead of silently overwriting an already-resolved
        # decision. It's only ever cleared by admin_override_* (settled
        # by fiat) or by actually pushing/undoing the change.
        sole_dept = distinct_departments[0]
        backers = {r["staged_by"] for r in rows}
        primary = rows[0]["staged_by"]
        agreed_by = actor if len(backers) > 1 and actor != primary else None
        _resolve_combo(conn, combo_id, sole_dept, rows, agreed_by=agreed_by, clear_suggestions=False)
        _clear_dept_push_approvals(conn)
    return {"disputed": False, "departments": [sole_dept]}


def admin_override_combo(engine, combo_id: int, department: str, tier, source_key: str, label: str, n_upcs_total: int, actor: str) -> None:
    """Admin-exclusive power: forces this combo's decision to `department`
    regardless of the current vote state — NOT part of the normal voting
    pool, and never triggered by anything other than an explicit admin
    action. Clears every current suggestion (settled, one way or another)
    and discard-notifies anyone whose suggestion didn't win."""
    with engine.begin() as conn:
        rows = conn.execute(
            text(
                "SELECT department, staged_by, tier, source_key, label, n_upcs_total, suggested_at "
                "FROM dbo.dept_mapping_combo_suggestions WHERE combo_id = :combo_id ORDER BY suggested_at"
            ),
            {"combo_id": combo_id},
        ).mappings().all()
        conn.execute(text("DELETE FROM dbo.dept_mapping_pending_changes WHERE combo_id = :combo_id"), {"combo_id": combo_id})
        conn.execute(
            text(
                "INSERT INTO dbo.dept_mapping_pending_changes "
                "(combo_id, tier, department, source_key, label, n_upcs_total, staged_by, overridden_by, overridden_at, is_saved) "
                "VALUES (:combo_id, :tier, :department, :source_key, :label, :n_upcs_total, :staged_by, :overridden_by, SYSUTCDATETIME(), 1)"
            ),
            {
                "combo_id": combo_id, "tier": tier, "department": department, "source_key": source_key,
                "label": label, "n_upcs_total": n_upcs_total, "staged_by": actor, "overridden_by": actor,
            },
        )
        conn.execute(text("DELETE FROM dbo.dept_mapping_combo_suggestions WHERE combo_id = :combo_id"), {"combo_id": combo_id})
        _clear_dept_push_approvals(conn)
        dissenters = [r for r in rows if r["department"] != department]
    for d in dissenters:
        label = f"{(d['source_key'] or '').upper()} — {d['label']}"
        record_discard_notice(
            engine, "dept_review", label,
            originally_staged_by=d["staged_by"],
            reason=f"{actor} (admin) overrode this to {department} instead of {d['staged_by']}'s {d['department']} suggestion",
            triggered_by=actor,
            group_label=label,
        )


def remove_combo_override(engine, combo_id: int, actor: str) -> None:
    """Admin-exclusive: unlocks a combo an admin previously overrode —
    the department value stays exactly as the admin set it, but the
    normal voting pool (Suggest/I also agree/Update) opens back up for
    everyone again. To actually change the department too, either call
    admin_override_combo again with a different one, or use the normal
    Undo (admin force-undo) to discard it outright."""
    with engine.begin() as conn:
        row = conn.execute(
            text(
                "SELECT tier, department, source_key, label, n_upcs_total, staged_by "
                "FROM dbo.dept_mapping_pending_changes WHERE combo_id = :combo_id"
            ),
            {"combo_id": combo_id},
        ).mappings().first()
        conn.execute(
            text(
                "UPDATE dbo.dept_mapping_pending_changes SET overridden_by = NULL, overridden_at = NULL "
                "WHERE combo_id = :combo_id"
            ),
            {"combo_id": combo_id},
        )
        # admin_override_combo wipes the suggestion pool entirely (it's
        # settled by fiat, nothing left to compare against) — restore a
        # row for the current decided value so a LATER divergent
        # suggestion is still recognized as a real disagreement instead
        # of silently overwriting the admin's decision, same reasoning as
        # every other resolve path in this module.
        if row:
            conn.execute(
                text(
                    "INSERT INTO dbo.dept_mapping_combo_suggestions "
                    "(combo_id, staged_by, department, tier, source_key, label, n_upcs_total) "
                    "VALUES (:combo_id, :staged_by, :department, :tier, :source_key, :label, :n_upcs_total)"
                ),
                {
                    "combo_id": combo_id, "staged_by": row["staged_by"], "department": row["department"],
                    "tier": row["tier"], "source_key": row["source_key"], "label": row["label"],
                    "n_upcs_total": row["n_upcs_total"],
                },
            )


def _resolve_combo(conn, combo_id: int, department: str, suggestion_rows, agreed_by, clear_suggestions: bool = True) -> None:
    """Writes the actual decided row (dept_mapping_pending_changes) for
    `department`, attributed to whoever voted for it FIRST — called with
    the caller's own open connection/transaction. clear_suggestions=False
    for the no-real-dispute case (nothing was actually settled, so the
    vote record needs to survive in case someone else later votes
    differently); True once a genuine multi-department disagreement has
    actually been resolved."""
    winner = next(r for r in suggestion_rows if r["department"] == department)
    conn.execute(text("DELETE FROM dbo.dept_mapping_pending_changes WHERE combo_id = :combo_id"), {"combo_id": combo_id})
    conn.execute(
        text(
            "INSERT INTO dbo.dept_mapping_pending_changes "
            "(combo_id, tier, department, source_key, label, n_upcs_total, staged_by, agreed_by, agreed_at, is_saved) "
            "VALUES (:combo_id, :tier, :department, :source_key, :label, :n_upcs_total, :staged_by, :agreed_by, "
            + ("SYSUTCDATETIME()" if agreed_by else "NULL") + ", 1)"
        ),
        {
            "combo_id": combo_id, "tier": winner.get("tier"), "department": department,
            "source_key": winner.get("source_key"), "label": winner.get("label"), "n_upcs_total": winner.get("n_upcs_total"),
            "staged_by": winner["staged_by"], "agreed_by": agreed_by,
        },
    )
    if clear_suggestions:
        conn.execute(text("DELETE FROM dbo.dept_mapping_combo_suggestions WHERE combo_id = :combo_id"), {"combo_id": combo_id})


def delete_pending_change(engine, combo_id: int) -> None:
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM dbo.dept_mapping_pending_changes WHERE combo_id = :combo_id"), {"combo_id": combo_id})
        conn.execute(text("DELETE FROM dbo.dept_mapping_combo_suggestions WHERE combo_id = :combo_id"), {"combo_id": combo_id})
        conn.execute(
            text("DELETE FROM dbo.dept_mapping_undo_requests WHERE entity_type = 'combo' AND entity_id = :combo_id"),
            {"combo_id": str(combo_id)},
        )
        _clear_dept_push_approvals(conn)


def get_undo_requests(engine, entity_type: str, entity_ids=None) -> dict:
    """{entity_id: set(requested_by)} — who has so far asked to undo each
    combo/UPC decision. entity_type is 'combo' or 'upc'."""
    query = "SELECT entity_id, requested_by FROM dbo.dept_mapping_undo_requests WHERE entity_type = :entity_type"
    params = {"entity_type": entity_type}
    with engine.connect() as conn:
        if entity_ids is not None:
            if not entity_ids:
                return {}
            ids = [str(i) for i in entity_ids]
            rows = conn.execute(
                text(f"{query} AND entity_id IN :entity_ids").bindparams(bindparam("entity_ids", expanding=True)),
                {**params, "entity_ids": ids},
            ).mappings().all()
        else:
            rows = conn.execute(text(query), params).mappings().all()
    by_id = {}
    for r in rows:
        key = int(r["entity_id"]) if entity_type == "combo" else r["entity_id"]
        by_id.setdefault(key, set()).add(r["requested_by"])
    return by_id


def request_undo_combo(engine, combo_id: int, actor: str, is_admin: bool = False) -> dict:
    """The person who originally staged this decision can undo it alone,
    no matter how many people later agreed with it — it's still their
    call to walk it back. Anyone else clicking Undo — whether they never
    weighed in or they agreed with the exact department being undone —
    doesn't undo anything by itself; it just leaves a request for the
    original stager (or an admin) to actually act on. Returns {"executed":
    bool, "requested_by": set, "waiting_on": str | None}."""
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT staged_by FROM dbo.dept_mapping_pending_changes WHERE combo_id = :combo_id"),
            {"combo_id": combo_id},
        ).mappings().first()
    staged_by = row["staged_by"] if row else None
    if is_admin or actor == staged_by:
        delete_pending_change(engine, combo_id)
        return {"executed": True, "requested_by": set(), "waiting_on": None}
    with engine.begin() as conn:
        conn.execute(
            text(
                "DELETE FROM dbo.dept_mapping_undo_requests WHERE entity_type = 'combo' AND entity_id = :combo_id AND requested_by = :actor"
            ),
            {"combo_id": str(combo_id), "actor": actor},
        )
        conn.execute(
            text(
                "INSERT INTO dbo.dept_mapping_undo_requests (entity_type, entity_id, requested_by) "
                "VALUES ('combo', :combo_id, :actor)"
            ),
            {"combo_id": str(combo_id), "actor": actor},
        )
        requested = {
            r[0] for r in conn.execute(
                text("SELECT requested_by FROM dbo.dept_mapping_undo_requests WHERE entity_type = 'combo' AND entity_id = :combo_id"),
                {"combo_id": str(combo_id)},
            ).fetchall()
        }
    return {"executed": False, "requested_by": requested, "waiting_on": staged_by}


def get_pending_upc_changes(engine) -> dict:
    """Staged-but-not-pushed per-UPC Department decisions from Broken Out,
    keyed by UPC — exactly one row per UPC (the ownership model gives each
    UPC a single decider; see stage_broken_out_decisions and
    dept_mapping_upc_change_suggestions for how a disagreement is handled
    instead of a second competing row)."""
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT upc, combo_id, department, label, description, source_key, staged_by, staged_at, "
                "revised_by, revised_at, overridden_by, overridden_at FROM dbo.dept_mapping_pending_upc_changes"
            ),
        ).mappings().all()
    return {r["upc"]: dict(r) for r in rows}


def get_upc_backers(engine, upcs) -> dict:
    """{upc: owner} for RESOLVED UPCs — who currently owns (decided) each
    one. Kept as a dict-of-sets for call-site compatibility with the combo
    version, even though a UPC only ever has one owner now."""
    if not upcs:
        return {}
    with engine.connect() as conn:
        rows = conn.execute(
            text("SELECT upc, staged_by FROM dbo.dept_mapping_pending_upc_changes WHERE upc IN :upcs").bindparams(
                bindparam("upcs", expanding=True)
            ),
            {"upcs": list(upcs)},
        ).mappings().all()
    return {r["upc"]: {r["staged_by"]} for r in rows}


def stage_broken_out_decisions(engine, changes: dict, actor: str, is_admin: bool = False) -> dict:
    """changes: {upc: {"department", "combo_id", "label", "description",
    "source_key"}}. The ownership model for Broken Out: a UPC nobody has
    decided yet (or one `actor` already owns) gets decided outright —
    `actor` becomes/stays its owner. A UPC someone ELSE already owns is
    never silently overwritten, even if `actor` just claimed the group —
    claiming only grants authority over undecided items, not someone
    else's existing decision. Instead `actor`'s pick is recorded as a
    suggestion (dept_mapping_upc_change_suggestions) for the current owner
    to accept or deny. This is checked fresh against the database for
    every UPC at staging time — never against a stale in-browser snapshot
    — which is what makes it safe for someone to return to a long-idle,
    unrefreshed tab and stage: whatever's since been decided by someone
    else automatically routes to a suggestion instead of corrupting or
    silently losing either side's input. A UPC an admin has overridden is
    off-limits entirely (status "locked") until that admin removes the
    override — see remove_upc_override. Returns {upc: {"status": "decided"
    | "suggested" | "blocked" | "locked", "owner": str | None}}."""
    if not changes:
        return {}
    results = {}
    with engine.begin() as conn:
        for upc, c in changes.items():
            owner_row = conn.execute(
                text("SELECT staged_by, overridden_by FROM dbo.dept_mapping_pending_upc_changes WHERE upc = :upc"),
                {"upc": upc},
            ).mappings().first()
            owner = owner_row["staged_by"] if owner_row else None
            if owner_row and owner_row["overridden_by"] and not is_admin:
                results[upc] = {"status": "locked", "owner": owner}
                continue
            if owner and owner != actor:
                existing_sugg = conn.execute(
                    text("SELECT department FROM dbo.dept_mapping_upc_change_suggestions WHERE upc = :upc"),
                    {"upc": upc},
                ).mappings().all()
                others = {r["department"] for r in existing_sugg}
                if c["department"] not in others and len(others) >= MAX_DISTINCT_SUGGESTIONS:
                    results[upc] = {"status": "blocked", "owner": owner}
                    continue
                conn.execute(
                    text(
                        "DELETE FROM dbo.dept_mapping_upc_change_suggestions WHERE upc = :upc AND suggested_by = :actor"
                    ),
                    {"upc": upc, "actor": actor},
                )
                conn.execute(
                    text(
                        "INSERT INTO dbo.dept_mapping_upc_change_suggestions "
                        "(upc, suggested_by, department, combo_id, label, description, source_key) "
                        "VALUES (:upc, :actor, :department, :combo_id, :label, :description, :source_key)"
                    ),
                    {
                        "upc": upc, "actor": actor, "department": c["department"], "combo_id": c["combo_id"],
                        "label": c["label"], "description": c.get("description"), "source_key": c.get("source_key", ""),
                    },
                )
                results[upc] = {"status": "suggested", "owner": owner}
                continue
            conn.execute(
                text("DELETE FROM dbo.dept_mapping_pending_upc_changes WHERE upc = :upc"),
                {"upc": upc},
            )
            conn.execute(
                text(
                    "INSERT INTO dbo.dept_mapping_pending_upc_changes "
                    "(upc, combo_id, department, label, description, source_key, staged_by, is_saved) "
                    "VALUES (:upc, :combo_id, :department, :label, :description, :source_key, :staged_by, 1)"
                ),
                {
                    "upc": upc, "combo_id": c["combo_id"], "department": c["department"],
                    "label": c["label"], "description": c.get("description"),
                    "source_key": c.get("source_key", ""), "staged_by": actor,
                },
            )
            results[upc] = {"status": "decided", "owner": actor}
        _clear_dept_push_approvals(conn)
    return results


def get_upc_change_suggestions(engine, combo_ids=None) -> dict:
    """{upc: [{"suggested_by", "department", "combo_id", "label",
    "description", "source_key", "suggested_at"}, ...]} — pending
    suggestions awaiting the owner's accept/deny, optionally filtered to
    specific combos."""
    query = (
        "SELECT upc, suggested_by, department, combo_id, label, description, source_key, suggested_at "
        "FROM dbo.dept_mapping_upc_change_suggestions"
    )
    with engine.connect() as conn:
        if combo_ids is not None:
            if not combo_ids:
                return {}
            rows = conn.execute(
                text(f"{query} WHERE combo_id IN :combo_ids").bindparams(bindparam("combo_ids", expanding=True)),
                {"combo_ids": list(combo_ids)},
            ).mappings().all()
        else:
            rows = conn.execute(text(query)).mappings().all()
    by_upc = {}
    for r in rows:
        by_upc.setdefault(r["upc"], []).append(dict(r))
    return by_upc


def accept_upc_suggestion(engine, upc: str, suggested_by: str, department: str, actor: str) -> None:
    """The owner (or admin) accepts a suggestion — it becomes the decided
    value. `staged_by` (who originally decided this UPC) is left
    untouched; revised_by/revised_at record the ORIGINAL SUGGESTER whose
    pick got accepted (not whoever clicked Accept) — that's what makes
    this UPC a "more than one person reviewed it" decision for
    request_undo's purposes, same role agreed_by plays for combos."""
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE dbo.dept_mapping_pending_upc_changes SET department = :department, "
                "revised_by = :revised_by, revised_at = SYSUTCDATETIME() WHERE upc = :upc"
            ),
            {"department": department, "revised_by": suggested_by, "upc": upc},
        )
        conn.execute(
            text("DELETE FROM dbo.dept_mapping_upc_change_suggestions WHERE upc = :upc AND suggested_by = :suggested_by"),
            {"upc": upc, "suggested_by": suggested_by},
        )
        _clear_dept_push_approvals(conn)


def deny_upc_suggestion(engine, upc: str, suggested_by: str) -> None:
    """The owner (or admin) denies a suggestion — it's discarded, the
    decided value is untouched."""
    with engine.begin() as conn:
        conn.execute(
            text("DELETE FROM dbo.dept_mapping_upc_change_suggestions WHERE upc = :upc AND suggested_by = :suggested_by"),
            {"upc": upc, "suggested_by": suggested_by},
        )


def get_broken_out_claim(engine, combo_id: int, idle_minutes: int = 120) -> dict | None:
    """The current claim on a combo's still-undecided items, or None if
    unclaimed or the claim has gone idle past `idle_minutes` (checked on
    read — no background job needed; an expired claim is lazily deleted
    the next time anyone looks at or touches this combo). The claim is
    purely a permission gate on who may bulk-decide UNDECIDED items right
    now — it is never where a decision lives, so its expiry can never lose
    or corrupt one; see stage_broken_out_decisions for why a returning,
    long-idle tab is still safe to stage from even after its claim expired."""
    with engine.begin() as conn:
        row = conn.execute(
            text(
                "SELECT combo_id, claimed_by, claimed_at, last_activity_at "
                "FROM dbo.dept_mapping_broken_out_claims WHERE combo_id = :combo_id"
            ),
            {"combo_id": combo_id},
        ).mappings().first()
        if not row:
            return None
        idle = conn.execute(
            text("SELECT DATEDIFF(MINUTE, :last_activity_at, SYSUTCDATETIME())"),
            {"last_activity_at": row["last_activity_at"]},
        ).scalar()
        if idle >= idle_minutes:
            conn.execute(text("DELETE FROM dbo.dept_mapping_broken_out_claims WHERE combo_id = :combo_id"), {"combo_id": combo_id})
            return None
        return dict(row)


def get_broken_out_claims(engine, combo_ids=None, idle_minutes: int = 120) -> dict:
    """Batch version of get_broken_out_claim for a list page — {combo_id:
    {...}}, expired claims lazily cleaned up and excluded same as the
    single-combo version."""
    with engine.begin() as conn:
        query = "SELECT combo_id, claimed_by, claimed_at, last_activity_at FROM dbo.dept_mapping_broken_out_claims"
        if combo_ids is not None:
            if not combo_ids:
                return {}
            rows = conn.execute(
                text(f"{query} WHERE combo_id IN :combo_ids").bindparams(bindparam("combo_ids", expanding=True)),
                {"combo_ids": list(combo_ids)},
            ).mappings().all()
        else:
            rows = conn.execute(text(query)).mappings().all()
        expired = [
            r["combo_id"] for r in rows
            if conn.execute(
                text("SELECT DATEDIFF(MINUTE, :last_activity_at, SYSUTCDATETIME())"),
                {"last_activity_at": r["last_activity_at"]},
            ).scalar() >= idle_minutes
        ]
        if expired:
            conn.execute(
                text("DELETE FROM dbo.dept_mapping_broken_out_claims WHERE combo_id IN :combo_ids").bindparams(
                    bindparam("combo_ids", expanding=True)
                ),
                {"combo_ids": expired},
            )
    expired_set = set(expired)
    return {r["combo_id"]: dict(r) for r in rows if r["combo_id"] not in expired_set}


def claim_broken_out_group(engine, combo_id: int, actor: str) -> dict:
    """Claims `combo_id` for `actor` if unclaimed (or the existing claim
    has gone idle) — returns {"claimed": bool, "claimed_by": str}; when
    `claimed` is False, someone else already holds an active claim and
    `claimed_by` names them."""
    current = get_broken_out_claim(engine, combo_id)
    if current and current["claimed_by"] != actor:
        return {"claimed": False, "claimed_by": current["claimed_by"]}
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM dbo.dept_mapping_broken_out_claims WHERE combo_id = :combo_id"), {"combo_id": combo_id})
        conn.execute(
            text(
                "INSERT INTO dbo.dept_mapping_broken_out_claims (combo_id, claimed_by) VALUES (:combo_id, :actor)"
            ),
            {"combo_id": combo_id, "actor": actor},
        )
    return {"claimed": True, "claimed_by": actor}


def touch_broken_out_claim(engine, combo_id: int, actor: str) -> None:
    """Refreshes the idle clock — called on every rerun while `actor` is
    the active claimant, so an actively-worked session (even one spanning
    hours of real time) never expires out from under them; only genuine
    inactivity counts toward idle_minutes."""
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE dbo.dept_mapping_broken_out_claims SET last_activity_at = SYSUTCDATETIME() "
                "WHERE combo_id = :combo_id AND claimed_by = :actor"
            ),
            {"combo_id": combo_id, "actor": actor},
        )


def release_broken_out_claim(engine, combo_id: int, actor: str, is_admin: bool = False) -> None:
    """Releases a claim — normally only the claimant, but an admin may
    force-release someone else's (e.g. they're unreachable and blocking
    others from even starting on the remaining undecided items)."""
    with engine.begin() as conn:
        if is_admin:
            conn.execute(text("DELETE FROM dbo.dept_mapping_broken_out_claims WHERE combo_id = :combo_id"), {"combo_id": combo_id})
        else:
            conn.execute(
                text("DELETE FROM dbo.dept_mapping_broken_out_claims WHERE combo_id = :combo_id AND claimed_by = :actor"),
                {"combo_id": combo_id, "actor": actor},
            )


def admin_override_upc(engine, upc: str, department: str, combo_id: int, label: str, source_key: str, description, actor: str) -> None:
    """Admin-exclusive: forces `upc` to `department` regardless of its
    current owner or any pending suggestions on it — settles it outright
    and clears any suggestions, since they're moot once an admin has
    decided."""
    with engine.begin() as conn:
        prior = conn.execute(
            text("SELECT department, staged_by FROM dbo.dept_mapping_pending_upc_changes WHERE upc = :upc"),
            {"upc": upc},
        ).mappings().first()
        conn.execute(text("DELETE FROM dbo.dept_mapping_pending_upc_changes WHERE upc = :upc"), {"upc": upc})
        conn.execute(
            text(
                "INSERT INTO dbo.dept_mapping_pending_upc_changes "
                "(upc, combo_id, department, label, description, source_key, staged_by, overridden_by, overridden_at, is_saved) "
                "VALUES (:upc, :combo_id, :department, :label, :description, :source_key, :staged_by, :overridden_by, SYSUTCDATETIME(), 1)"
            ),
            {
                "upc": upc, "combo_id": combo_id, "department": department, "label": label,
                "description": description, "source_key": source_key, "staged_by": actor, "overridden_by": actor,
            },
        )
        conn.execute(text("DELETE FROM dbo.dept_mapping_upc_change_suggestions WHERE upc = :upc"), {"upc": upc})
        _clear_dept_push_approvals(conn)
    if prior and prior["department"] != department:
        record_discard_notice(
            engine, "dept_review",
            f"{upc} — {description or upc}",
            originally_staged_by=prior["staged_by"],
            reason=f"{actor} (admin) overrode this to {department} instead of {prior['staged_by']}'s {prior['department']} decision",
            triggered_by=actor,
            group_label=f"{(source_key or '').upper()} — {label}",
        )


def admin_override_upc_group(engine, combo_id: int, department: str, actor: str) -> int:
    """Admin-exclusive: force-decides EVERY currently-staged UPC in a
    Broken Out group to `department` in one shot — the group-level
    equivalent of admin_override_upc, for when a whole batch needs
    settling at once rather than one disputed item at a time. Returns how
    many UPCs it touched."""
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT upc, label, source_key, description FROM dbo.dept_mapping_pending_upc_changes "
                "WHERE combo_id = :combo_id"
            ),
            {"combo_id": combo_id},
        ).mappings().all()
    for r in rows:
        admin_override_upc(engine, r["upc"], department, combo_id, r["label"], r["source_key"], r["description"], actor)
    return len(rows)


def remove_upc_override(engine, upc: str, actor: str) -> None:
    """Admin-exclusive: unlocks a UPC an admin previously overrode — same
    idea as remove_combo_override, at the per-UPC level."""
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE dbo.dept_mapping_pending_upc_changes SET overridden_by = NULL, overridden_at = NULL "
                "WHERE upc = :upc"
            ),
            {"upc": upc},
        )


def remove_upc_override_group(engine, combo_id: int, actor: str) -> int:
    """Unlocks every currently admin-overridden UPC in a Broken Out
    group at once. Returns how many it touched."""
    with engine.begin() as conn:
        result = conn.execute(
            text(
                "UPDATE dbo.dept_mapping_pending_upc_changes SET overridden_by = NULL, overridden_at = NULL "
                "WHERE combo_id = :combo_id AND overridden_by IS NOT NULL"
            ),
            {"combo_id": combo_id},
        )
        return result.rowcount


def get_broken_out_group_primary(engine, combo_id: int) -> str | None:
    """The person considered the "first editor" of a Broken Out group's
    current staged work — whoever's row has the earliest staged_at among
    its still-pending UPC decisions. They (or an admin) can move the
    WHOLE group back in one step; see request_undo_upc_group_all."""
    with engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT TOP 1 staged_by FROM dbo.dept_mapping_pending_upc_changes "
                "WHERE combo_id = :combo_id ORDER BY staged_at"
            ),
            {"combo_id": combo_id},
        ).mappings().first()
    return row["staged_by"] if row else None


def get_broken_out_group_primaries(engine, combo_ids) -> dict:
    """Batch version of get_broken_out_group_primary for a whole page of
    groups at once — {combo_id: staged_by}."""
    if not combo_ids:
        return {}
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT combo_id, staged_by, staged_at FROM dbo.dept_mapping_pending_upc_changes "
                "WHERE combo_id IN :combo_ids ORDER BY staged_at"
            ).bindparams(bindparam("combo_ids", expanding=True)),
            {"combo_ids": list(combo_ids)},
        ).mappings().all()
    primaries = {}
    for r in rows:
        primaries.setdefault(r["combo_id"], r["staged_by"])
    return primaries


def request_undo_upc_group_all(engine, combo_id: int, actor: str, is_admin: bool = False) -> dict:
    """Undoing a whole Broken Out group at once: the group's "first
    editor" (get_broken_out_group_primary) can move the whole thing back
    in one step, same as a combo's original stager can. Anyone else's
    click just leaves a request for that person (or an admin) to act on.
    Returns {"executed": bool, "requested_by": set, "waiting_on": str |
    None}."""
    primary = get_broken_out_group_primary(engine, combo_id)
    if primary is None:
        return {"executed": True, "requested_by": set(), "waiting_on": None}
    if is_admin or actor == primary:
        delete_pending_upc_changes_for_combo(engine, combo_id)
        with engine.begin() as conn:
            conn.execute(
                text("DELETE FROM dbo.dept_mapping_undo_requests WHERE entity_type = 'upc_group' AND entity_id = :combo_id"),
                {"combo_id": str(combo_id)},
            )
        return {"executed": True, "requested_by": set(), "waiting_on": None}
    with engine.begin() as conn:
        conn.execute(
            text(
                "DELETE FROM dbo.dept_mapping_undo_requests WHERE entity_type = 'upc_group' AND entity_id = :combo_id AND requested_by = :actor"
            ),
            {"combo_id": str(combo_id), "actor": actor},
        )
        conn.execute(
            text(
                "INSERT INTO dbo.dept_mapping_undo_requests (entity_type, entity_id, requested_by) "
                "VALUES ('upc_group', :combo_id, :actor)"
            ),
            {"combo_id": str(combo_id), "actor": actor},
        )
        requested = {
            r[0] for r in conn.execute(
                text("SELECT requested_by FROM dbo.dept_mapping_undo_requests WHERE entity_type = 'upc_group' AND entity_id = :combo_id"),
                {"combo_id": str(combo_id)},
            ).fetchall()
        }
    return {"executed": False, "requested_by": requested, "waiting_on": primary}


def withdraw_combo_suggestion(engine, combo_id: int, actor: str) -> dict:
    """Lets someone in a combo dispute retract their OWN suggestion —
    the one thing they unilaterally control, same spirit as a lone
    stager undoing their own decision. If that was the last dissenting
    voice and everyone remaining now agrees, it resolves on the spot,
    same as if they'd changed their vote to match instead of withdrawing
    outright. Returns {"resolved": bool, "departments": [...],
    "withdrew": bool} — withdrew is False if actor had no suggestion to
    withdraw."""
    with engine.begin() as conn:
        deleted = conn.execute(
            text("DELETE FROM dbo.dept_mapping_combo_suggestions WHERE combo_id = :combo_id AND staged_by = :actor"),
            {"combo_id": combo_id, "actor": actor},
        )
        if deleted.rowcount == 0:
            return {"resolved": False, "departments": [], "withdrew": False}
        rows = conn.execute(
            text(
                "SELECT department, staged_by, tier, source_key, label, n_upcs_total, suggested_at "
                "FROM dbo.dept_mapping_combo_suggestions WHERE combo_id = :combo_id ORDER BY suggested_at"
            ),
            {"combo_id": combo_id},
        ).mappings().all()
        if not rows:
            _clear_dept_push_approvals(conn)
            return {"resolved": False, "departments": [], "withdrew": True}
        distinct_departments = sorted({r["department"] for r in rows})
        if len(distinct_departments) > 1:
            _clear_dept_push_approvals(conn)
            return {"resolved": False, "departments": distinct_departments, "withdrew": True}
        sole_dept = distinct_departments[0]
        backers = {r["staged_by"] for r in rows}
        primary = rows[0]["staged_by"]
        agreed_by = actor if len(backers) > 1 and actor != primary else None
        _resolve_combo(conn, combo_id, sole_dept, rows, agreed_by=agreed_by, clear_suggestions=False)
        _clear_dept_push_approvals(conn)
        return {"resolved": True, "departments": [sole_dept], "withdrew": True}


def delete_pending_upc_change(engine, upc: str) -> None:
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM dbo.dept_mapping_pending_upc_changes WHERE upc = :upc"), {"upc": upc})
        conn.execute(text("DELETE FROM dbo.dept_mapping_upc_change_suggestions WHERE upc = :upc"), {"upc": upc})
        conn.execute(text("DELETE FROM dbo.dept_mapping_undo_requests WHERE entity_type = 'upc' AND entity_id = :upc"), {"upc": upc})
        _clear_dept_push_approvals(conn)


def delete_pending_upc_changes_for_combo(engine, combo_id: int) -> None:
    with engine.begin() as conn:
        upcs = [
            r[0] for r in conn.execute(
                text("SELECT upc FROM dbo.dept_mapping_pending_upc_changes WHERE combo_id = :combo_id"),
                {"combo_id": combo_id},
            ).fetchall()
        ]
        conn.execute(text("DELETE FROM dbo.dept_mapping_pending_upc_changes WHERE combo_id = :combo_id"), {"combo_id": combo_id})
        conn.execute(text("DELETE FROM dbo.dept_mapping_upc_change_suggestions WHERE combo_id = :combo_id"), {"combo_id": combo_id})
        if upcs:
            conn.execute(
                text("DELETE FROM dbo.dept_mapping_undo_requests WHERE entity_type = 'upc' AND entity_id IN :upcs").bindparams(
                    bindparam("upcs", expanding=True)
                ),
                {"upcs": upcs},
            )
        _clear_dept_push_approvals(conn)


def has_pending_review_work(engine) -> bool:
    """True if anyone, anywhere in the app, currently has a staged-but-
    not-yet-pushed Item Master change or Department Review decision sitting
    around. Auto-merge checks this before running — merging while someone
    has real in-progress review work risks silently discarding it (their
    decision's evidence can change out from under them the moment the
    engine recomputes), which used to be rare (a deliberate, occasional
    manual Push) and is now common (an automatic trigger on almost any
    admin action). Skipping auto-merge in that window and falling back to
    the ordinary "raw data changed, run Merge" banner keeps that decision
    a conscious one instead of an invisible side effect."""
    with engine.connect() as conn:
        counts = conn.execute(
            text(
                "SELECT "
                "(SELECT COUNT(*) FROM dbo.item_master_pending_changes) + "
                "(SELECT COUNT(*) FROM dbo.dept_mapping_pending_changes) + "
                "(SELECT COUNT(*) FROM dbo.dept_mapping_pending_upc_changes)"
            )
        ).scalar()
    return bool(counts)


def record_discard_notice(
    engine, entity_type: str, entity_label: str, originally_staged_by, reason: str, triggered_by: str,
    group_label: str | None = None,
) -> None:
    """group_label is which combo/Broken Out group entity_label's item came
    from (e.g. "KEHE — HEALTH BODY CARE / HBC / AROMATHERAPY BODY OILS") —
    distinct from entity_label itself, which identifies the specific item
    (a UPC) or, for a combo-level notice, is already the group's own label.
    Lets the UI sub-group a pile of per-UPC notices by their originating
    combo instead of showing them as one undifferentiated list."""
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO dbo.change_discard_notices "
                "(entity_type, entity_label, originally_staged_by, reason, triggered_by, group_label) "
                "VALUES (:entity_type, :entity_label, :originally_staged_by, :reason, :triggered_by, :group_label)"
            ),
            {
                "entity_type": entity_type, "entity_label": entity_label,
                "originally_staged_by": originally_staged_by, "reason": reason, "triggered_by": triggered_by,
                "group_label": group_label,
            },
        )


def get_discard_notices(engine) -> pd.DataFrame:
    """Undismissed discard notices, newest first — durable and visible to
    every editor, not just whoever happened to trigger the merge that
    caused one."""
    with engine.connect() as conn:
        return pd.read_sql(
            text(
                "SELECT id, entity_type, entity_label, originally_staged_by, reason, triggered_by, triggered_at, group_label "
                "FROM dbo.change_discard_notices WHERE dismissed = 0 ORDER BY triggered_at DESC"
            ),
            conn,
        )


def dismiss_discard_notice(engine, notice_id: int) -> None:
    with engine.begin() as conn:
        conn.execute(text("UPDATE dbo.change_discard_notices SET dismissed = 1 WHERE id = :id"), {"id": notice_id})


def dismiss_discard_notices(engine, notice_ids: list) -> None:
    """Bulk version of dismiss_discard_notice — one round trip for a whole
    group (e.g. every notice from the same admin-override batch) instead
    of one write per card."""
    if not notice_ids:
        return
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE dbo.change_discard_notices SET dismissed = 1 WHERE id IN :ids").bindparams(
                bindparam("ids", expanding=True)
            ),
            {"ids": [int(i) for i in notice_ids]},
        )


def dismiss_all_discard_notices(engine) -> None:
    with engine.begin() as conn:
        conn.execute(text("UPDATE dbo.change_discard_notices SET dismissed = 1 WHERE dismissed = 0"))


def clear_pending_for_combo(engine, combo_id: int) -> None:
    """A department decision staged for a combo (Approve, or per-item
    Department choices from Broken Out) is only ever valid given the
    state that produced it — an immediate action that changes that state
    (Break Out, Send Back, or undoing one of them) must discard anything
    staged on top of it, or a later push could silently apply a decision
    to a combo that's no longer in the state it was staged for."""
    delete_pending_change(engine, combo_id)
    delete_pending_upc_changes_for_combo(engine, combo_id)
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM dbo.dept_mapping_broken_out_claims WHERE combo_id = :combo_id"), {"combo_id": combo_id})


# ---------------------------------------------------------------------------
# Item Master (Add Item / Delete Item / Edit Item Master) pending changes
#
# Same staged -> pushed model as the combo pending tables above, on
# dbo.item_master_pending_changes: every click writes immediately
# (durable, and visible to every editor right away — there's no private
# draft state), Push performs the real dbo.items/manual_overrides/
# deleted_upcs writes. One row per UPC — staging a new action for a UPC
# replaces whatever was already pending for it. change_type is 'add',
# 'delete', or 'edit'; source_key only ever matters for 'edit' (an
# added/deleted item has no distributor source of its own).
# ---------------------------------------------------------------------------

def get_item_master_pending(engine) -> dict:
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT upc, change_type, description, department, category, subcategory, brand, "
                "pack, size, uom, source_key, staged_by, staged_at FROM dbo.item_master_pending_changes"
            ),
        ).mappings().all()
    return {
        r["upc"]: {
            "change_type": r["change_type"], "description": r["description"], "department": r["department"],
            "category": r["category"], "subcategory": r["subcategory"], "brand": r["brand"],
            "pack": r["pack"], "size": r["size"], "uom": r["uom"],
            "source_key": r["source_key"], "staged_by": r["staged_by"], "staged_at": r["staged_at"],
        }
        for r in rows
    }


def save_item_master_pending(
    engine, upc: str, change_type: str, description, department, category, subcategory, brand, actor: str,
    source_key=None, pack=None, size=None, uom=None,
) -> dict | None:
    """First-write-wins: if `upc` already has a pending edit staged by a
    DIFFERENT person, this does nothing and returns that existing row
    instead of silently overwriting it — the caller shows the actor
    exactly what's already pending and who staged it, rather than the
    change just vanishing later with no explanation. Returns None on a
    normal write (nothing was blocked)."""
    return save_item_master_pending_bulk(
        engine,
        {
            upc: {
                "change_type": change_type, "description": description, "department": department,
                "category": category, "subcategory": subcategory, "brand": brand,
                "pack": pack, "size": size, "uom": uom, "source_key": source_key,
            }
        },
        actor,
    ).get(upc)


def save_item_master_pending_bulk(engine, changes: dict, actor: str) -> dict:
    """changes: {upc: {"change_type", "description", "department", "category",
    "subcategory", "brand", "pack", "size", "uom", "source_key"}} — every
    non-conflicting UPC written in ONE transaction (a DELETE per UPC plus
    one batched INSERT), for Edit Item Master's multi-row Save, which can
    stage many changed rows from a single click. Immediately visible to
    every editor.

    First-write-wins: any UPC that already has a pending edit staged by a
    DIFFERENT person is left completely untouched rather than overwritten
    — two people editing the same item at once is rare but real, and
    whoever staged first shouldn't lose their edit just because someone
    else's click landed a moment later. Returns {upc: existing_row_dict}
    for every UPC that was blocked this way, so the caller can tell the
    person exactly what's already pending (change type, department, who
    staged it) instead of their edit just quietly not showing up."""
    if not changes:
        return {}
    with engine.begin() as conn:
        existing = {
            r["upc"]: dict(r) for r in conn.execute(
                text(
                    "SELECT upc, change_type, description, department, staged_by, staged_at "
                    "FROM dbo.item_master_pending_changes WHERE upc IN :upcs"
                ).bindparams(bindparam("upcs", expanding=True)),
                {"upcs": list(changes)},
            ).mappings().all()
        }
        blocked = {upc: row for upc, row in existing.items() if row["staged_by"] and row["staged_by"] != actor}
        to_write = {upc: c for upc, c in changes.items() if upc not in blocked}
        if not to_write:
            return blocked
        for upc in to_write:
            conn.execute(text("DELETE FROM dbo.item_master_pending_changes WHERE upc = :upc"), {"upc": upc})
        conn.execute(
            text(
                "INSERT INTO dbo.item_master_pending_changes "
                "(upc, change_type, description, department, category, subcategory, brand, pack, size, uom, source_key, is_saved, staged_by) "
                "VALUES (:upc, :change_type, :description, :department, :category, :subcategory, :brand, :pack, :size, :uom, :source_key, 1, :staged_by)"
            ),
            [
                {
                    "upc": upc, "change_type": c["change_type"], "description": c.get("description"),
                    "department": c.get("department"), "category": c.get("category"),
                    "subcategory": c.get("subcategory"), "brand": c.get("brand"),
                    "pack": c.get("pack"), "size": c.get("size"), "uom": c.get("uom"),
                    "source_key": c.get("source_key"), "staged_by": actor,
                }
                for upc, c in to_write.items()
            ],
        )
    return blocked


def delete_item_master_pending(engine, upc: str) -> None:
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM dbo.item_master_pending_changes WHERE upc = :upc"), {"upc": upc})


# ---------------------------------------------------------------------------
# Sources tab pending changes
#
# Same staged -> pushed model as everything else: editing an existing
# source or adding a new one writes immediately here (durable, visible to
# every editor right away) instead of touching dbo.sources directly.
# Push actually applies it. One row per source_key. change_type is 'add'
# or 'edit'; apply_now only matters for 'edit' — see the migration's own
# docstring (add_source_pending_changes.py) for the full reasoning.
# ---------------------------------------------------------------------------

SOURCE_CONFIG_COLUMNS = [
    "source_label", "enabled", "priority_rank", "file_keyword",
    "sheet_name", "header_row", "upc_column", "upc_suffix_column", "strip_trailing_digits",
    "department_column", "category_column", "subcategory_column",
    "brand_column", "description_column",
    "pack_column", "size_column", "size_format", "uom_column", "uom_aliases",
    "exclude_column", "exclude_values",
    "blank_brand_when_equals", "blank_department_when_equals", "blank_department_default",
    "brand_suffix_match", "brand_suffix_result", "dedup_deprioritize_brand_value",
    "strip_leading_code_fields",
    "notes",
]


def get_source_pending_changes(engine) -> dict:
    cols = ", ".join(SOURCE_CONFIG_COLUMNS)
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                f"SELECT source_key, change_type, apply_now, {cols}, staged_by, staged_at "
                "FROM dbo.source_pending_changes"
            ),
        ).mappings().all()
    return {
        r["source_key"]: {
            "change_type": r["change_type"], "apply_now": bool(r["apply_now"]),
            **{col: r[col] for col in SOURCE_CONFIG_COLUMNS},
            "staged_by": r["staged_by"], "staged_at": r["staged_at"],
        }
        for r in rows
    }


def save_source_pending_change(
    engine, source_key: str, change_type: str, config: dict, apply_now: bool, actor: str,
) -> None:
    """config: {col: value} for every column in SOURCE_CONFIG_COLUMNS —
    the full desired end state for this source, not a delta, same
    convention as every other pending-change table here."""
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM dbo.source_pending_changes WHERE source_key = :sk"), {"sk": source_key})
        cols = ", ".join(SOURCE_CONFIG_COLUMNS)
        placeholders = ", ".join(f":{col}" for col in SOURCE_CONFIG_COLUMNS)
        conn.execute(
            text(
                f"INSERT INTO dbo.source_pending_changes "
                f"(source_key, change_type, apply_now, {cols}, staged_by) "
                f"VALUES (:source_key, :change_type, :apply_now, {placeholders}, :staged_by)"
            ),
            {
                "source_key": source_key, "change_type": change_type, "apply_now": apply_now,
                "staged_by": actor, **{col: config.get(col) for col in SOURCE_CONFIG_COLUMNS},
            },
        )


def delete_source_pending_change(engine, source_key: str) -> None:
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM dbo.source_pending_changes WHERE source_key = :sk"), {"sk": source_key})


def set_source_pending_apply_now(engine, source_key: str, apply_now: bool) -> None:
    """Lets apply_now be changed after the fact, on an already-staged
    edit — set at staging time via the Sources grid's own checkbox, but
    a person reviewing Pending Changes later (or who forgot to check it
    the first time) shouldn't have to Undo and re-stage the whole edit
    just to flip this one flag."""
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE dbo.source_pending_changes SET apply_now = :apply_now WHERE source_key = :sk"),
            {"apply_now": apply_now, "sk": source_key},
        )


def record_merge(
    engine, actor: str, upc_count: int, added_count=None, changed_count=None, removed_count=None,
    overrides_applied=None, deleted_excluded=None, changed_by_field: dict = None, changed_by_source: dict = None,
) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO dbo.merge_log "
                "(merged_by, upc_count, added_count, changed_count, removed_count, "
                "overrides_applied, deleted_excluded, changed_by_field, changed_by_source) "
                "VALUES (:actor, :upc_count, :added_count, :changed_count, :removed_count, "
                ":overrides_applied, :deleted_excluded, :changed_by_field, :changed_by_source)"
            ),
            {
                "actor": actor, "upc_count": upc_count, "added_count": added_count,
                "changed_count": changed_count, "removed_count": removed_count,
                "overrides_applied": overrides_applied, "deleted_excluded": deleted_excluded,
                "changed_by_field": json.dumps(changed_by_field) if changed_by_field else None,
                "changed_by_source": json.dumps(changed_by_source) if changed_by_source else None,
            },
        )


def get_last_merge_summary(engine) -> dict | None:
    """The most recently pushed Merge's full impact summary — same shape as
    the pre-push "Computed merge" panel — so it stays visible on the Merge
    tab after the fact instead of only flashing once in the push's own
    success toast."""
    with engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT TOP 1 merged_at, merged_by, upc_count, added_count, changed_count, "
                "removed_count, overrides_applied, deleted_excluded, changed_by_field, changed_by_source "
                "FROM dbo.merge_log ORDER BY merged_at DESC"
            )
        ).mappings().fetchone()
    if row is None:
        return None
    result = dict(row)
    result["changed_by_field"] = json.loads(result["changed_by_field"]) if result["changed_by_field"] else {}
    result["changed_by_source"] = json.loads(result["changed_by_source"]) if result["changed_by_source"] else {}
    return result


def get_stale_sources_since_last_merge(engine) -> list[str]:
    """Every real data refresh to raw_items — an Upload & Ingest submit or
    an Apply Now re-run on a Sources config edit — logs a row in
    dbo.ingestion_log. Merge itself never re-reads dbo.sources or re-maps
    anything; it only combines whatever's already sitting in raw_items.
    So a source can have brand new raw_items data that Item Master and
    Department Review still don't reflect at all, until Merge actually
    runs again — the exact confusion "I updated the config and pushed,
    why doesn't Item Master show it?" comes from missing this gap. This
    powers a banner naming exactly which source(s) are affected, instead
    of leaving that gap invisible."""
    with engine.connect() as conn:
        last_merge_at = conn.execute(text("SELECT MAX(merged_at) FROM dbo.merge_log")).scalar()
        if last_merge_at is None:
            rows = conn.execute(text("SELECT DISTINCT source_key FROM dbo.ingestion_log")).fetchall()
        else:
            rows = conn.execute(
                text("SELECT DISTINCT source_key FROM dbo.ingestion_log WHERE uploaded_at > :last_merge_at"),
                {"last_merge_at": last_merge_at},
            ).fetchall()
    return sorted(r[0] for r in rows)


# ---------------------------------------------------------------------------
# Snapshots — a deliberate, point-in-time copy of dbo.items /
# dbo.dept_mapping_combos / dbo.dept_mapping_upc_overrides that a person can
# always come back to later, taken manually (before a batch of changes)
# rather than on every Merge — Merge happens far more often than a person
# would want a durable checkpoint. Restoring one is a real live-database
# write, so it always takes its own safety snapshot of current state first,
# making even a restore itself undoable.
# ---------------------------------------------------------------------------

def take_snapshot(engine, actor: str, label: str = None) -> int:
    with engine.begin() as conn:
        item_count = conn.execute(text("SELECT COUNT(*) FROM dbo.items")).scalar()
        combo_count = conn.execute(text("SELECT COUNT(*) FROM dbo.dept_mapping_combos")).scalar()
        snapshot_id = conn.execute(
            text(
                "INSERT INTO dbo.dept_mapping_snapshots "
                "(snapshot_month, taken_at, item_count, combo_count, label, taken_by) "
                "OUTPUT inserted.snapshot_id "
                "VALUES (FORMAT(SYSUTCDATETIME(), 'yyyy-MM'), SYSUTCDATETIME(), :item_count, :combo_count, :label, :taken_by)"
            ),
            {"item_count": item_count, "combo_count": combo_count, "label": label, "taken_by": actor},
        ).scalar()

        conn.execute(
            text(
                "INSERT INTO dbo.items_snapshot "
                "(snapshot_id, upc, description, department, category, subcategory, brand, source_key, pack, size, uom) "
                "SELECT :snapshot_id, upc, description, department, category, subcategory, brand, source_key, pack, size, uom "
                "FROM dbo.items"
            ),
            {"snapshot_id": snapshot_id},
        )
        conn.execute(
            text(
                "INSERT INTO dbo.dept_mapping_combos_snapshot "
                "(snapshot_id, combo_id, source_key, raw_department, raw_category, raw_subcategory, "
                "tier, purity, n_evidence, resolved_via, decision_state, decided_department, decided_via, "
                "suggested_department, majority_department, chain_round, is_strict, manual_department, approved, rejected, "
                "is_new_this_run, is_stale, first_seen_at, last_computed_at, last_decided_at, last_decided_by, "
                "runner_up_department, runner_up_share, n_upcs_total) "
                "SELECT :snapshot_id, combo_id, source_key, raw_department, raw_category, raw_subcategory, "
                "tier, purity, n_evidence, resolved_via, decision_state, decided_department, decided_via, "
                "suggested_department, majority_department, chain_round, is_strict, manual_department, approved, rejected, "
                "is_new_this_run, is_stale, first_seen_at, last_computed_at, last_decided_at, last_decided_by, "
                "runner_up_department, runner_up_share, n_upcs_total "
                "FROM dbo.dept_mapping_combos"
            ),
            {"snapshot_id": snapshot_id},
        )
        conn.execute(
            text(
                "INSERT INTO dbo.dept_mapping_upc_overrides_snapshot "
                "(snapshot_id, upc, combo_id, department, decided_via, suggested_department, suggested_via, "
                "updated_by, updated_at) "
                "SELECT :snapshot_id, upc, combo_id, department, decided_via, suggested_department, suggested_via, "
                "updated_by, updated_at "
                "FROM dbo.dept_mapping_upc_overrides"
            ),
            {"snapshot_id": snapshot_id},
        )
        conn.execute(
            text(
                "INSERT INTO dbo.dept_mapping_combo_upcs_snapshot (snapshot_id, combo_id, upc, is_evidence, p1_department) "
                "SELECT :snapshot_id, combo_id, upc, is_evidence, p1_department FROM dbo.dept_mapping_combo_upcs"
            ),
            {"snapshot_id": snapshot_id},
        )

        # A snapshot is a restore point for the whole in-progress session,
        # not just committed data — capture every currently-staged (never
        # yet pushed) pending change too, across all four staging tables,
        # so restoring brings back exactly what was going on at the time.
        conn.execute(
            text(
                f"INSERT INTO dbo.source_pending_changes_snapshot (snapshot_id, {', '.join(SOURCE_CONFIG_COLUMNS)}, "
                "source_key, change_type, apply_now, staged_by, staged_at) "
                f"SELECT :snapshot_id, {', '.join(SOURCE_CONFIG_COLUMNS)}, "
                "source_key, change_type, apply_now, staged_by, staged_at "
                "FROM dbo.source_pending_changes"
            ),
            {"snapshot_id": snapshot_id},
        )
        conn.execute(
            text(
                "INSERT INTO dbo.item_master_pending_changes_snapshot "
                "(snapshot_id, upc, change_type, description, department, category, subcategory, brand, "
                "is_saved, staged_by, staged_at, source_key, pack, size, uom) "
                "SELECT :snapshot_id, upc, change_type, description, department, category, subcategory, brand, "
                "is_saved, staged_by, staged_at, source_key, pack, size, uom "
                "FROM dbo.item_master_pending_changes"
            ),
            {"snapshot_id": snapshot_id},
        )
        conn.execute(
            text(
                "INSERT INTO dbo.dept_mapping_pending_changes_snapshot "
                "(snapshot_id, combo_id, tier, department, source_key, label, n_upcs_total, staged_by, staged_at, is_saved) "
                "SELECT :snapshot_id, combo_id, tier, department, source_key, label, n_upcs_total, staged_by, staged_at, is_saved "
                "FROM dbo.dept_mapping_pending_changes"
            ),
            {"snapshot_id": snapshot_id},
        )
        conn.execute(
            text(
                "INSERT INTO dbo.dept_mapping_pending_upc_changes_snapshot "
                "(snapshot_id, upc, combo_id, department, label, description, source_key, staged_by, staged_at, is_saved) "
                "SELECT :snapshot_id, upc, combo_id, department, label, description, source_key, staged_by, staged_at, is_saved "
                "FROM dbo.dept_mapping_pending_upc_changes"
            ),
            {"snapshot_id": snapshot_id},
        )
    return snapshot_id


def list_snapshots(engine) -> pd.DataFrame:
    with engine.connect() as conn:
        return pd.read_sql(
            text(
                "SELECT snapshot_id, snapshot_month, taken_at, taken_by, label, item_count, combo_count "
                "FROM dbo.dept_mapping_snapshots ORDER BY taken_at DESC"
            ),
            conn,
        )


def has_snapshot_this_month(engine) -> bool:
    with engine.connect() as conn:
        count = conn.execute(
            text("SELECT COUNT(*) FROM dbo.dept_mapping_snapshots WHERE snapshot_month = FORMAT(SYSUTCDATETIME(), 'yyyy-MM')")
        ).scalar()
    return count > 0


def restore_snapshot(engine, snapshot_id: int, actor: str) -> int:
    """Always takes its own safety snapshot of whatever's live right now,
    labeled to say why, before overwriting anything — restoring a snapshot
    must never be the one action you can't come back from."""
    safety_id = take_snapshot(engine, actor, label=f"Auto-safety before restoring snapshot #{snapshot_id}")
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM dbo.items"))
        conn.execute(
            text(
                "INSERT INTO dbo.items (upc, description, department, category, subcategory, brand, source_key, pack, size, uom) "
                "SELECT upc, description, department, category, subcategory, brand, source_key, pack, size, uom "
                "FROM dbo.items_snapshot WHERE snapshot_id = :snapshot_id"
            ),
            {"snapshot_id": snapshot_id},
        )
        conn.execute(text("DELETE FROM dbo.dept_mapping_upc_overrides"))
        conn.execute(text("DELETE FROM dbo.dept_mapping_combo_upcs"))
        conn.execute(text("DELETE FROM dbo.dept_mapping_combos"))
        conn.execute(text("SET IDENTITY_INSERT dbo.dept_mapping_combos ON"))
        conn.execute(
            text(
                "INSERT INTO dbo.dept_mapping_combos "
                "(combo_id, source_key, raw_department, raw_category, raw_subcategory, "
                "tier, purity, n_evidence, resolved_via, decision_state, decided_department, decided_via, "
                "suggested_department, majority_department, chain_round, is_strict, manual_department, approved, rejected, "
                "is_new_this_run, is_stale, first_seen_at, last_computed_at, last_decided_at, last_decided_by, "
                "runner_up_department, runner_up_share, n_upcs_total) "
                "SELECT combo_id, source_key, raw_department, raw_category, raw_subcategory, "
                "tier, purity, n_evidence, resolved_via, decision_state, decided_department, decided_via, "
                "suggested_department, majority_department, chain_round, is_strict, manual_department, approved, rejected, "
                "is_new_this_run, is_stale, first_seen_at, last_computed_at, last_decided_at, last_decided_by, "
                "runner_up_department, runner_up_share, n_upcs_total "
                "FROM dbo.dept_mapping_combos_snapshot WHERE snapshot_id = :snapshot_id"
            ),
            {"snapshot_id": snapshot_id},
        )
        conn.execute(text("SET IDENTITY_INSERT dbo.dept_mapping_combos OFF"))
        conn.execute(
            text(
                "INSERT INTO dbo.dept_mapping_combo_upcs (combo_id, upc, is_evidence, p1_department) "
                "SELECT combo_id, upc, is_evidence, p1_department "
                "FROM dbo.dept_mapping_combo_upcs_snapshot WHERE snapshot_id = :snapshot_id"
            ),
            {"snapshot_id": snapshot_id},
        )
        conn.execute(
            text(
                "INSERT INTO dbo.dept_mapping_upc_overrides "
                "(upc, combo_id, department, decided_via, suggested_department, suggested_via, updated_by, updated_at) "
                "SELECT upc, combo_id, department, decided_via, suggested_department, suggested_via, updated_by, updated_at "
                "FROM dbo.dept_mapping_upc_overrides_snapshot WHERE snapshot_id = :snapshot_id"
            ),
            {"snapshot_id": snapshot_id},
        )

        # Restore the in-progress session too — every pending change that
        # existed (staged, not pushed) at snapshot time replaces whatever
        # is currently staged, matching "go back to exactly how it was."
        conn.execute(text("DELETE FROM dbo.source_pending_changes"))
        conn.execute(
            text(
                f"INSERT INTO dbo.source_pending_changes (source_key, change_type, apply_now, {', '.join(SOURCE_CONFIG_COLUMNS)}, staged_by, staged_at) "
                f"SELECT source_key, change_type, apply_now, {', '.join(SOURCE_CONFIG_COLUMNS)}, staged_by, staged_at "
                "FROM dbo.source_pending_changes_snapshot WHERE snapshot_id = :snapshot_id"
            ),
            {"snapshot_id": snapshot_id},
        )
        conn.execute(text("DELETE FROM dbo.item_master_pending_changes"))
        conn.execute(
            text(
                "INSERT INTO dbo.item_master_pending_changes "
                "(upc, change_type, description, department, category, subcategory, brand, is_saved, "
                "staged_by, staged_at, source_key, pack, size, uom) "
                "SELECT upc, change_type, description, department, category, subcategory, brand, is_saved, "
                "staged_by, staged_at, source_key, pack, size, uom "
                "FROM dbo.item_master_pending_changes_snapshot WHERE snapshot_id = :snapshot_id"
            ),
            {"snapshot_id": snapshot_id},
        )
        conn.execute(text("DELETE FROM dbo.dept_mapping_pending_changes"))
        conn.execute(
            text(
                "INSERT INTO dbo.dept_mapping_pending_changes "
                "(combo_id, tier, department, source_key, label, n_upcs_total, staged_by, staged_at, is_saved) "
                "SELECT combo_id, tier, department, source_key, label, n_upcs_total, staged_by, staged_at, is_saved "
                "FROM dbo.dept_mapping_pending_changes_snapshot WHERE snapshot_id = :snapshot_id"
            ),
            {"snapshot_id": snapshot_id},
        )
        conn.execute(text("DELETE FROM dbo.dept_mapping_pending_upc_changes"))
        conn.execute(
            text(
                "INSERT INTO dbo.dept_mapping_pending_upc_changes "
                "(upc, combo_id, department, label, description, source_key, staged_by, staged_at, is_saved) "
                "SELECT upc, combo_id, department, label, description, source_key, staged_by, staged_at, is_saved "
                "FROM dbo.dept_mapping_pending_upc_changes_snapshot WHERE snapshot_id = :snapshot_id"
            ),
            {"snapshot_id": snapshot_id},
        )
    return safety_id


def delete_snapshot(engine, snapshot_id: int) -> None:
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM dbo.items_snapshot WHERE snapshot_id = :sid"), {"sid": snapshot_id})
        conn.execute(text("DELETE FROM dbo.dept_mapping_combos_snapshot WHERE snapshot_id = :sid"), {"sid": snapshot_id})
        conn.execute(text("DELETE FROM dbo.dept_mapping_combo_upcs_snapshot WHERE snapshot_id = :sid"), {"sid": snapshot_id})
        conn.execute(text("DELETE FROM dbo.dept_mapping_upc_overrides_snapshot WHERE snapshot_id = :sid"), {"sid": snapshot_id})
        conn.execute(text("DELETE FROM dbo.source_pending_changes_snapshot WHERE snapshot_id = :sid"), {"sid": snapshot_id})
        conn.execute(text("DELETE FROM dbo.item_master_pending_changes_snapshot WHERE snapshot_id = :sid"), {"sid": snapshot_id})
        conn.execute(text("DELETE FROM dbo.dept_mapping_pending_changes_snapshot WHERE snapshot_id = :sid"), {"sid": snapshot_id})
        conn.execute(text("DELETE FROM dbo.dept_mapping_pending_upc_changes_snapshot WHERE snapshot_id = :sid"), {"sid": snapshot_id})
        conn.execute(text("DELETE FROM dbo.dept_mapping_snapshots WHERE snapshot_id = :sid"), {"sid": snapshot_id})


# ---------------------------------------------------------------------------
# Staged Merge — "Run Merge" computes the new item master into
# dbo.items_staged instead of writing dbo.items directly; a separate Push
# actually replaces dbo.items with it. Only the item-priority-merge part is
# staged this way: it's a pure Python computation with no live-table
# interdependencies, so redirecting its output is low-risk. The combo
# engine (run_engine) is NOT staged the same way — see push_merge_compute.
# ---------------------------------------------------------------------------

_MERGE_COMPARE_FIELDS = ["description", "department", "category", "subcategory", "brand", "pack", "size", "uom", "source_key"]


def compute_merge_final_df(engine, priority_order: list) -> tuple:
    """The actual merge computation — walks priority order (lowest
    priority_rank first), takes Category/Subcategory/Brand/Description
    together from the first source with every field filled in (falling
    back to the highest-priority source with any data at all), substitutes
    Department Review's decision in for any non-P1 winner, then layers
    manual_overrides on top and drops deleted_upcs. Pulled out of the old
    Compute Merge button handler so it can be called from more than one
    place — the manual button, and an automatic recompute triggered right
    after anything that changes raw_items or a Department Review decision.

    Returns (final_df, overrides_applied, deleted_count); final_df is None
    if there's nothing to merge at all (no enabled source has any raw data
    and no manual overrides exist yet)."""
    with engine.connect() as conn:
        raw = pd.read_sql(
            text(
                "SELECT r.upc AS UPC, r.source_key AS SourceKey, r.department AS Department, "
                "r.category AS Category, r.subcategory AS Subcategory, r.brand AS Brand, "
                "r.description AS Description, r.pack AS Pack, r.size AS Size, r.uom AS UOM "
                "FROM dbo.raw_items r JOIN dbo.sources s ON r.source_key = s.source_key "
                "WHERE s.enabled = 1"
            ),
            conn,
        )
        overrides = pd.read_sql(text("SELECT * FROM dbo.manual_overrides"), conn)
        deleted = set(pd.read_sql(text("SELECT upc FROM dbo.deleted_upcs"), conn)["upc"])

    if raw.empty and overrides.empty:
        return None, 0, 0

    upc_department_overrides = get_upc_department_overrides(engine)

    # Vectorized equivalent of "walk priority order, take the first source
    # where every field is filled in, falling back to the highest-priority
    # source with any data at all" — verified byte-for-byte identical
    # against the original per-UPC Python loop on the real 330K-item
    # dataset. Pack/Size are deliberately not part of the completeness check.
    merge_fields = ["Department", "Category", "Subcategory", "Brand", "Description"]
    if raw.empty:
        final_df = pd.DataFrame(columns=[
            "upc", "description", "department", "category", "subcategory",
            "brand", "pack", "size", "uom", "source_key",
        ])
    else:
        priority_rank_map = {sk: i for i, sk in enumerate(priority_order)}
        raw = raw.copy()
        raw["_priority_rank"] = raw["SourceKey"].map(priority_rank_map)
        # Matches Python's bool(x) truthiness exactly, the same test the
        # original per-UPC loop applied to each field — NaN is a nonzero
        # float (truthy), so only a literal empty string counts as
        # "missing" here, same as before.
        is_complete = pd.Series(True, index=raw.index)
        for f in merge_fields:
            is_complete &= (raw[f] != "")
        raw["_is_complete"] = is_complete
        raw = raw.sort_values(["UPC", "_is_complete", "_priority_rank"], ascending=[True, False, True])
        # NOT groupby().first() — that aggregates NaN-skipping PER COLUMN
        # independently, silently mixing fields from different sources'
        # rows for the same UPC. This takes one whole row per UPC.
        chosen = raw.drop_duplicates(subset="UPC", keep="first")

        non_nwg_dept = chosen["UPC"].map(upc_department_overrides)
        is_nwg_complete = (chosen["SourceKey"] == "nwg") & (chosen["Department"] != "")
        department_col = chosen["Department"].where(is_nwg_complete, non_nwg_dept)

        def _or_none(col):
            return col.where(col != "", None)

        final_df = pd.DataFrame({
            "upc": chosen["UPC"],
            "description": chosen["Description"].where(chosen["Description"] != "", chosen["UPC"]),
            "department": department_col,
            "category": _or_none(chosen["Category"]),
            "subcategory": _or_none(chosen["Subcategory"]),
            "brand": _or_none(chosen["Brand"]),
            "pack": _or_none(chosen["Pack"]),
            "size": _or_none(chosen["Size"]),
            "uom": _or_none(chosen["UOM"]),
            "source_key": chosen["SourceKey"],
        })

    final_rows = final_df.set_index("upc", drop=False).to_dict("index")

    # Manual edits/additions win over whatever the priority merge picked —
    # non-null override fields replace the merged value; a UPC that isn't
    # in any raw source at all (manually added) becomes its own row.
    overrides_applied = 0
    for _, o in overrides.iterrows():
        row = final_rows.get(o["upc"], {
            "upc": o["upc"], "description": o["upc"], "department": None,
            "category": None, "subcategory": None, "brand": None,
            "pack": None, "size": None, "uom": None, "source_key": None,
        })
        for field in ["description", "department", "category", "subcategory", "brand", "pack", "size", "uom"]:
            if pd.notna(o[field]) and o[field] != "":
                row[field] = o[field]
        row["source_key"] = row["source_key"] or "manual"
        final_rows[o["upc"]] = row
        overrides_applied += 1

    for upc in deleted:
        final_rows.pop(upc, None)

    return pd.DataFrame(final_rows.values()), overrides_applied, len(deleted)


def save_merge_compute(engine, final_df, actor: str, overrides_applied: int, deleted_excluded: int) -> dict:
    """Compares the computed final_df against whatever's currently live in
    dbo.items — added (brand new UPCs), changed (an existing UPC with a
    different value in any field), removed (a live UPC that this compute
    no longer produces at all) — so a person can see the real shape of a
    Merge's impact before pushing it, not just the raw item count.

    changed_by_field/changed_by_source break the Changed count down by WHAT
    changed (which fields actually differ) and WHOSE data changed (which
    source's rows account for the difference) — a static total-items-per-
    source count barely moves month to month and says nothing about a
    specific run's impact, e.g. "a source's Size/UOM cleaning rule changed"
    shows up here as a big size/uom count concentrated in that one source,
    not as an unrelated total row count."""
    with engine.connect() as conn:
        live = pd.read_sql(text(f"SELECT upc, {', '.join(_MERGE_COMPARE_FIELDS)} FROM dbo.items"), conn)

    computed_upcs = set(final_df["upc"]) if not final_df.empty else set()
    live_upcs = set(live["upc"])
    added_count = len(computed_upcs - live_upcs)
    removed_count = len(live_upcs - computed_upcs)

    changed_count = 0
    changed_by_field = {}
    changed_by_source = {}
    if not final_df.empty and not live.empty:
        merged = final_df.merge(live, on="upc", how="inner", suffixes=("_new", "_live"))
        if not merged.empty:
            diff_mask = pd.Series(False, index=merged.index)
            field_masks = {}
            for field in _MERGE_COMPARE_FIELDS:
                new_col, live_col = merged[f"{field}_new"], merged[f"{field}_live"]
                both_blank = new_col.isna() & live_col.isna()
                field_diff = ~both_blank & (new_col != live_col)
                field_masks[field] = field_diff
                diff_mask |= field_diff
            changed_count = int(diff_mask.sum())
            if changed_count:
                changed_by_field = {f: int(m.sum()) for f, m in field_masks.items() if m.sum()}
                changed_by_source = {
                    k: int(v) for k, v in merged.loc[diff_mask, "source_key_new"].value_counts().items()
                }

    with engine.begin() as conn:
        conn.execute(text("DELETE FROM dbo.items_staged"))
        if not final_df.empty:
            final_df.to_sql("items_staged", conn, schema="dbo", if_exists="append", index=False, chunksize=5000)
        conn.execute(text("DELETE FROM dbo.merge_compute_meta"))
        conn.execute(
            text(
                "INSERT INTO dbo.merge_compute_meta "
                "(computed_at, computed_by, item_count, overrides_applied, deleted_excluded, "
                "added_count, changed_count, removed_count, changed_by_field, changed_by_source) "
                "VALUES (SYSUTCDATETIME(), :actor, :item_count, :overrides_applied, :deleted_excluded, "
                ":added_count, :changed_count, :removed_count, :changed_by_field, :changed_by_source)"
            ),
            {
                "actor": actor, "item_count": len(final_df),
                "overrides_applied": overrides_applied, "deleted_excluded": deleted_excluded,
                "added_count": added_count, "changed_count": changed_count, "removed_count": removed_count,
                "changed_by_field": json.dumps(changed_by_field) if changed_by_field else None,
                "changed_by_source": json.dumps(changed_by_source) if changed_by_source else None,
            },
        )
    return {
        "added_count": added_count, "changed_count": changed_count, "removed_count": removed_count,
        "changed_by_field": changed_by_field, "changed_by_source": changed_by_source,
    }


MERGE_PUSH_REQUIRED_APPROVALS = 2

# Editors never see the Merge tab (admin-only), so Department Review's own
# "Push N Item(s) to the Database" button — which lands staged decisions in
# dept_mapping_combos/dept_mapping_upc_overrides — gets its own, separate
# two-approver-or-admin gate here, distinct from MERGE_PUSH_REQUIRED_APPROVALS.
DEPT_PUSH_REQUIRED_APPROVALS = 2


def get_dept_push_approvals(engine) -> list:
    with engine.connect() as conn:
        row = conn.execute(text("SELECT approvals FROM dbo.dept_push_approvals WHERE id = 1")).fetchone()
    if row is None or not row[0]:
        return []
    return json.loads(row[0])


def approve_dept_push(engine, actor: str) -> list:
    """Records `actor` as having reviewed and approved the CURRENT staged
    batch of Department Review decisions — push_dept_pending_changes
    refuses to push until DEPT_PUSH_REQUIRED_APPROVALS distinct people
    have done this (admins exempt). Clicking Approve twice as the same
    person doesn't count twice."""
    approvals = get_dept_push_approvals(engine)
    if not any(a["approver"] == actor for a in approvals):
        approvals = approvals + [{"approver": actor, "approved_at": datetime.now(timezone.utc).isoformat()}]
    with engine.begin() as conn:
        conn.execute(text("UPDATE dbo.dept_push_approvals SET approvals = :approvals WHERE id = 1"), {"approvals": json.dumps(approvals)})
    return approvals


def _clear_dept_push_approvals(conn) -> None:
    """Approvals are for the batch as it existed when approved — the
    instant that batch changes (a new decision staged, one undone, or a
    push clears it out entirely), any existing approvals are stale and
    must not silently carry over to a different set of changes. Callers
    pass their own open connection/transaction so this rides along with
    whatever staging mutation triggered it."""
    conn.execute(text("UPDATE dbo.dept_push_approvals SET approvals = '[]' WHERE id = 1"))


def get_merge_compute_meta(engine) -> dict | None:
    with engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT computed_at, computed_by, item_count, overrides_applied, deleted_excluded, "
                "added_count, changed_count, removed_count, changed_by_field, changed_by_source, approvals "
                "FROM dbo.merge_compute_meta"
            )
        ).mappings().fetchone()
    if row is None:
        return None
    result = dict(row)
    result["changed_by_field"] = json.loads(result["changed_by_field"]) if result["changed_by_field"] else {}
    result["changed_by_source"] = json.loads(result["changed_by_source"]) if result["changed_by_source"] else {}
    result["approvals"] = json.loads(result["approvals"]) if result["approvals"] else []
    return result


def approve_merge_compute(engine, actor: str) -> list:
    """Records `actor` as having reviewed and approved the currently
    computed (not-yet-pushed) merge draft — push_merge_compute refuses to
    actually push until MERGE_PUSH_REQUIRED_APPROVALS distinct people have
    done this, since a Merge push replaces the ENTIRE live item master and
    now happens automatically from routine actions (Sources Apply Now, an
    Upload, a Department Review push), not just a deliberate manual click.
    Clicking Approve twice as the same person doesn't count twice. Returns
    the updated approvals list."""
    meta = get_merge_compute_meta(engine)
    if meta is None:
        return []
    approvals = meta["approvals"]
    if not any(a["approver"] == actor for a in approvals):
        approvals = approvals + [{"approver": actor, "approved_at": datetime.now(timezone.utc).isoformat()}]
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE dbo.merge_compute_meta SET approvals = :approvals"),
            {"approvals": json.dumps(approvals)},
        )
    return approvals


def discard_merge_compute(engine) -> None:
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM dbo.items_staged"))
        conn.execute(text("DELETE FROM dbo.merge_compute_meta"))


def get_stale_sources_since_compute(engine) -> list[str]:
    """A computed-but-not-yet-pushed dbo.items_staged draft is a snapshot
    of raw_items as it looked at compute time. If a Sources config edit
    with Apply Now (or a fresh Upload & Ingest) refreshes any source's
    raw_items in the window between Compute and Push, that draft is now
    stale — pushing it anyway would silently apply outdated data and
    throw away the fresher upload with no warning. Same "don't act on
    evidence that's since changed" rule as everywhere else in this audit,
    just checked in the opposite direction: this time it's the computed
    Merge itself that can go stale, not a pending decision sitting under it."""
    meta = get_merge_compute_meta(engine)
    if meta is None:
        return []
    with engine.connect() as conn:
        rows = conn.execute(
            text("SELECT DISTINCT source_key FROM dbo.ingestion_log WHERE uploaded_at > :computed_at"),
            {"computed_at": meta["computed_at"]},
        ).fetchall()
    return sorted(r[0] for r in rows)


def push_merge_compute(engine, actor: str, on_progress=None, is_admin: bool = False) -> dict:
    """Takes its own safety snapshot first (same reasoning as
    restore_snapshot — this replaces the entire live item master, so it
    must never be the one action you can't come back from), then pushes
    the computed dbo.items_staged live, then runs the combo engine
    directly against the now-current data (unchanged, real-time — see the
    module note on why this part isn't staged). The combo engine runs in
    its own separate step after the items push has already committed: if
    it fails, the item master update still stands (not rolled back) and
    the failure is reported back rather than raised, since a stale combo
    engine result just means Department Review needs a manual re-run —
    not a reason to undo an otherwise-successful item merge.

    Checked again here, not just at display time on the Merge page, as a
    hard guard: if any source's raw data was refreshed after this draft
    was computed, it's discarded outright rather than pushed — the caller
    must Compute again against the current data.

    Also requires MERGE_PUSH_REQUIRED_APPROVALS distinct people to have
    called approve_merge_compute() on this exact draft first — a Merge
    push replaces the ENTIRE live item master, and now happens far more
    often than the old "someone deliberately clicks a button once in a
    while" model assumed. is_admin bypasses this: an admin's own push
    always goes through on their word alone, no separate approval needed,
    same as before this feature existed for anyone.

    on_progress(label, fraction), if given, is called at each real stage
    boundary — this is a multi-step, often slow (a minute or more on a
    full item master) operation, and a caller showing a single static
    spinner for the whole thing gives no sign of whether it's actually
    progressing or stuck. Fractions are rough, hand-picked weights (the
    Department Review recompute dominates the real wall-clock time), not
    a measured percent-complete."""
    def _progress(label, frac):
        if on_progress:
            on_progress(label, frac)

    _progress("Checking for newer source data...", 0.02)
    stale_sources = get_stale_sources_since_compute(engine)
    if stale_sources:
        discard_merge_compute(engine)
        return {
            "aborted_stale": True, "stale_sources": stale_sources,
            "safety_snapshot_id": None, "item_count": 0,
            "engine_summary": None, "engine_error": None,
            "discarded_pending_combo_ids": [], "discarded_item_master_upcs": [],
        }

    if not is_admin:
        meta = get_merge_compute_meta(engine) or {}
        distinct_approvers = {a["approver"] for a in meta.get("approvals", [])}
        if len(distinct_approvers) < MERGE_PUSH_REQUIRED_APPROVALS:
            return {
                "aborted_insufficient_approvals": True,
                "approvals": meta.get("approvals", []), "required": MERGE_PUSH_REQUIRED_APPROVALS,
                "aborted_stale": False, "safety_snapshot_id": None, "item_count": 0,
                "engine_summary": None, "engine_error": None,
                "discarded_pending_combo_ids": [], "discarded_item_master_upcs": [],
            }

    _progress("Taking a safety snapshot of today's data...", 0.08)
    safety_id = take_snapshot(engine, actor, label="Auto-safety before Merge push")

    item_master_fields = ["description", "department", "category", "subcategory", "brand", "source_key", "pack", "size", "uom"]
    item_cols_sql = ", ".join(item_master_fields)
    item_select_stmt = text(f"SELECT upc, {item_cols_sql} FROM dbo.items WHERE upc IN :upcs").bindparams(
        bindparam("upcs", expanding=True)
    )
    with engine.connect() as conn:
        watched_upcs = {r[0] for r in conn.execute(text("SELECT upc FROM dbo.item_master_pending_changes")).fetchall()}
        before_items = {}
        if watched_upcs:
            before_items = {
                r[0]: tuple(r[1:]) for r in conn.execute(item_select_stmt, {"upcs": list(watched_upcs)}).fetchall()
            }

    # Captured before discard_merge_compute wipes dbo.merge_compute_meta —
    # this is the only chance to carry the pre-push impact summary
    # (Added/Changed/Removed, and what/whose data actually changed) forward
    # onto the merge_log row, so it can still be shown after the fact.
    compute_meta = get_merge_compute_meta(engine) or {}

    _progress("Replacing the live item master...", 0.25)
    with engine.begin() as conn:
        item_count = conn.execute(text("SELECT COUNT(*) FROM dbo.items_staged")).scalar()
        conn.execute(text("DELETE FROM dbo.items"))
        conn.execute(
            text(
                "INSERT INTO dbo.items (upc, description, department, category, subcategory, brand, source_key, pack, size, uom) "
                "SELECT upc, description, department, category, subcategory, brand, source_key, pack, size, uom "
                "FROM dbo.items_staged"
            )
        )

    record_merge(
        engine, actor, item_count,
        added_count=compute_meta.get("added_count"), changed_count=compute_meta.get("changed_count"),
        removed_count=compute_meta.get("removed_count"), overrides_applied=compute_meta.get("overrides_applied"),
        deleted_excluded=compute_meta.get("deleted_excluded"),
        changed_by_field=compute_meta.get("changed_by_field"), changed_by_source=compute_meta.get("changed_by_source"),
    )
    discard_merge_compute(engine)

    # A pending Item Master Add/Delete/Edit stores the full desired end
    # state as it looked at staging time — it doesn't touch dbo.items
    # until it's pushed. If a Merge Push runs in between, this item's own
    # row can come out of the fresh merge with genuinely different data
    # (a new distributor file, a different winning source) — pushing the
    # stale pending change later would blindly overwrite that fresh data
    # with what was true when it was staged, and for an edit, permanently
    # lock the staleness in via manual_overrides, since that's what every
    # future Merge re-applies on top. An 'add' has the opposite risk: the
    # UPC it was staged for may now legitimately exist, which would crash
    # a plain INSERT at push time. Same rule as the combo engine check
    # above — discard rather than let a stale decision go live unseen.
    discarded_item_master_upcs = []
    if watched_upcs:
        with engine.connect() as conn:
            after_items = {
                r[0]: tuple(r[1:]) for r in conn.execute(item_select_stmt, {"upcs": list(watched_upcs)}).fetchall()
            }
        pending_items = get_item_master_pending(engine)
        for upc in watched_upcs:
            change = pending_items.get(upc)
            if change is None:
                continue
            if change["change_type"] == "add":
                stale = upc in after_items
            else:
                stale = before_items.get(upc) != after_items.get(upc)
            if stale:
                delete_item_master_pending(engine, upc)
                discarded_item_master_upcs.append(upc)
                record_discard_notice(
                    engine, "item_master",
                    f"{change['change_type'].title()} {upc} — {change.get('description') or '(no description)'}",
                    change.get("staged_by"),
                    "This Merge changed that item's underlying data, so pushing the staged edit "
                    "would have overwritten the new data with what was true when it was staged.",
                    actor,
                )

    engine_summary = None
    engine_error = None
    discarded_pending_combo_ids = []
    try:
        # A pending combo-level Approve or per-UPC Broken Out decision
        # doesn't touch dbo.dept_mapping_combos until it's pushed — so
        # while it's sitting staged, run_engine below can refresh that
        # same combo's tier/suggested department right out from under it.
        # Capture what those combos looked like before the recompute so a
        # meaningful change can be detected after, and discard the pending
        # decision rather than let it be pushed later against evidence the
        # reviewer never actually saw — same "an immediate state change
        # invalidates anything staged on top of it" rule clear_pending_for_combo
        # already applies to Break Out / Send Back.
        with engine.connect() as conn:
            pending_combo_rows = conn.execute(
                text("SELECT combo_id, source_key, label, staged_by FROM dbo.dept_mapping_pending_changes")
            ).mappings().all()
            pending_combo_ids = {r["combo_id"] for r in pending_combo_rows}
            pending_upc_rows = conn.execute(
                text("SELECT combo_id, source_key, label, staged_by FROM dbo.dept_mapping_pending_upc_changes")
            ).mappings().all()
            pending_upc_combo_ids = {r["combo_id"] for r in pending_upc_rows}
            combo_notice_info = {}
            for r in pending_combo_rows:
                combo_notice_info.setdefault(r["combo_id"], (r["source_key"], r["label"], r["staged_by"]))
            for r in pending_upc_rows:
                combo_notice_info.setdefault(r["combo_id"], (r["source_key"], r["label"], r["staged_by"]))
            watched_ids = pending_combo_ids | pending_upc_combo_ids
            before_state = {}
            if watched_ids:
                id_list = ", ".join(str(int(i)) for i in watched_ids)
                before_state = {
                    r[0]: (r[1], r[2]) for r in conn.execute(
                        text(f"SELECT combo_id, tier, suggested_department FROM dbo.dept_mapping_combos WHERE combo_id IN ({id_list})")
                    ).fetchall()
                }

        _progress("Recomputing Department Review groups — this is the slow part...", 0.4)
        engine_summary = run_engine(engine)
        _progress("Finishing up...", 0.9)

        if watched_ids:
            with engine.connect() as conn:
                id_list = ", ".join(str(int(i)) for i in watched_ids)
                after_state = {
                    r[0]: (r[1], r[2]) for r in conn.execute(
                        text(f"SELECT combo_id, tier, suggested_department FROM dbo.dept_mapping_combos WHERE combo_id IN ({id_list})")
                    ).fetchall()
                }
            changed_ids = {cid for cid in watched_ids if before_state.get(cid) != after_state.get(cid)}
            for combo_id in changed_ids:
                if combo_id in pending_combo_ids:
                    delete_pending_change(engine, combo_id)
                if combo_id in pending_upc_combo_ids:
                    delete_pending_upc_changes_for_combo(engine, combo_id)
                source_key, label, staged_by = combo_notice_info.get(combo_id, (None, f"combo #{combo_id}", None))
                notice_label = f"{(source_key or '').upper()} — {label}".strip(" —")
                record_discard_notice(
                    engine, "dept_review", notice_label, staged_by,
                    "This Merge's recomputed evidence for that group changed, so the staged decision "
                    "was discarded rather than pushed against evidence nobody actually reviewed.",
                    actor,
                    group_label=notice_label,
                )
            discarded_pending_combo_ids = sorted(changed_ids)
    except Exception as e:
        engine_error = str(e)

    return {
        "aborted_stale": False,
        "safety_snapshot_id": safety_id, "item_count": item_count,
        "engine_summary": engine_summary, "engine_error": engine_error,
        "discarded_pending_combo_ids": discarded_pending_combo_ids,
        "discarded_item_master_upcs": discarded_item_master_upcs,
    }
