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
import threading
import time
from collections import Counter
from datetime import datetime, timedelta, timezone

import pandas as pd
from sqlalchemy import bindparam, text
from sqlalchemy.types import TypeDecorator, UnicodeText

BLANK = ""  # matches ingest.py's convention: blanks are "", never NULL/NaN, in raw_items.

MAX_DISTINCT_SUGGESTIONS = 5  # cap on distinct departments a single combo/UPC dispute can accumulate; see upsert_combo_suggestion.


class _JsonList(TypeDecorator):
    """A list of UPCs sent as ONE JSON text parameter and read back with
    OPENJSON — instead of an "IN (?, ?, … ×1000)" list, which SQL Server
    takes seconds to compile each time (measured: ~8 s per 1,000 UPCs vs
    0.2 s for 13,000 as JSON). Queries write `IN (SELECT v FROM OPENJSON(:u)
    WITH (v VARCHAR(400) '$'))` and pass a plain Python list."""
    impl = UnicodeText
    cache_ok = True

    def process_bind_param(self, value, dialect):
        return None if value is None else json.dumps([str(v) for v in value])


JSON_LIST = _JsonList()


def _chunks(items: list, size: int = 1000):
    # SQL Server rejects a statement with more than 2,100 bound parameters.
    for i in range(0, len(items), size):
        yield items[i:i + size]

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


_STRONG_TABLES_CACHE = {}


def _strong_tables(combos: list, cfg: dict) -> tuple:
    """(strong_brands, strong_roots, strong_words) — the donor tables for the
    per-UPC passes, built from every auto combo. They depend only on the
    combo set and the thresholds, so they're built once per engine pass /
    prepared data set and reused by every Break Out that follows, instead
    of re-reading every auto combo's brands, UPCs and descriptions each time."""
    keys = ("upc_brand_match_min_sample", "upc_brand_match_min_purity", "upc_root_match_min_sample",
            "upc_root_match_min_purity", "description_match_min_sample", "description_match_min_purity")
    ck = (id(combos), len(combos), tuple(cfg[k] for k in keys))
    hit = _STRONG_TABLES_CACHE.get(ck)
    if hit is not None and hit[0] is combos:
        return hit[1]
    auto_combos = [c for c in combos if c["tier"] == "auto"]

    def strong(evidence, min_sample, min_purity, weight_by_sample=False):
        out = {}
        for key, counter in evidence.items():
            n = sum(counter.values())
            if n < min_sample:
                continue
            dept, count = counter.most_common(1)[0]
            purity = count / n
            if purity >= min_purity:
                out[key] = (dept, n if weight_by_sample else purity)
        return out

    brand_evidence, root_evidence, word_evidence = {}, {}, {}
    for combo in auto_combos:
        dept = combo["suggested_department"]
        for brand in combo.get("brands", []):
            brand_evidence.setdefault(brand, Counter())[dept] += 1
        for upc in combo.get("evidence_upcs", []):
            root_evidence.setdefault(str(upc)[:7], Counter())[dept] += 1
        # One vote per ITEM containing a word (deduped via set()), not per
        # occurrence — a word repeated twice in one description still only
        # counts once for that item.
        for desc in combo.get("descriptions", []):
            for tok in _tokenize_description(desc):
                word_evidence.setdefault(tok, Counter())[dept] += 1
    tables = (
        strong(brand_evidence, cfg["upc_brand_match_min_sample"], cfg["upc_brand_match_min_purity"]),
        strong(root_evidence, cfg["upc_root_match_min_sample"], cfg["upc_root_match_min_purity"]),
        # weight = the word's own sample size
        strong(word_evidence, cfg["description_match_min_sample"], cfg["description_match_min_purity"], True),
    )
    _STRONG_TABLES_CACHE.clear()  # only the newest combo set is ever needed
    _STRONG_TABLES_CACHE[ck] = (combos, tables)
    return tables


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
    strong_brands, strong_roots, strong_words = _strong_tables(combos, cfg)

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
    for row in _candidates(covered):
        hit = strong_roots.get(str(row["upc"])[:7])
        if hit:
            dept, purity = hit
            if _has_keyword_conflict(row.get("description"), dept):
                continue
            decisions[row["upc"]] = {"department": dept, "decided_via": f"Auto-Applied (UPC Root Match, {purity:.1%})"}
            covered.add(row["upc"])

    # --- Pass 6: Description Word Match -------------------------------
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


# ---------------------------------------------------------------------------
# Prepared engine data cache. Loading + preparing every raw row is the slow
# part of both a Merge's engine run and a Break Out's auto-match preview
# (~15s at this data volume, vs. well under a second for the matching
# itself). It only changes when raw data, the merge winners, or the engine
# settings change, so it's cached in-process and reused until
# _engine_data_token says otherwise.
# ---------------------------------------------------------------------------
_PREPARED = {"token": None, "data": None}
_PREPARED_LOCK = threading.Lock()
# Held for the whole (slow) preparation, so a second caller arriving mid-way
# (e.g. a Break Out click while the background warm-up is still loading)
# waits for that result instead of starting a second full load.
_PREPARE_BUILD_LOCK = threading.Lock()


def _engine_data_token(engine) -> tuple:
    """Cheap fingerprint of everything the prepared engine data depends on:
    a new upload (ingestion_log), a merge push or snapshot restore (the
    merge winners in dbo.items), and the Settings tables."""
    with engine.connect() as conn:
        return tuple(conn.execute(text(
            """
            SELECT
                (SELECT MAX(id) FROM dbo.ingestion_log),
                (SELECT MAX(id) FROM dbo.merge_log),
                (SELECT MAX(snapshot_id) FROM dbo.dept_mapping_snapshots),
                (SELECT CHECKSUM_AGG(BINARY_CHECKSUM(*)) FROM dbo.dept_mapping_config),
                (SELECT CHECKSUM_AGG(BINARY_CHECKSUM(*)) FROM dbo.dept_mapping_strict_departments),
                (SELECT CHECKSUM_AGG(BINARY_CHECKSUM(*)) FROM dbo.dept_mapping_unmatched_defaults)
            """
        )).one())


def _pin_existing_members(engine, members: pd.DataFrame, winner_key: dict) -> pd.DataFrame:
    """Source files never change an existing item — that includes which
    Department Review group it's in. An item already in a group stays in
    that group even if next month's file recategorizes it or stops listing
    it; only new UPCs are placed into groups from the file's own text."""
    with engine.connect() as conn:
        pinned = pd.read_sql(text(
            "SELECT cu.upc, c.source_key AS p_source, c.raw_department AS p_dept, c.raw_category AS p_cat, "
            "c.raw_subcategory AS p_sub FROM dbo.dept_mapping_combo_upcs cu "
            "JOIN dbo.dept_mapping_combos c ON c.combo_id = cu.combo_id"), conn)
    if pinned.empty:
        return members
    pinned = pinned[pinned["upc"].isin(winner_key)]  # still in the item master
    m = members.merge(pinned, on="upc", how="left")
    has = m["p_source"].notna()
    m.loc[has, "source_key"] = m.loc[has, "p_source"]
    m.loc[has, "department"] = m.loc[has, "p_dept"].fillna("")
    m.loc[has, "category"] = m.loc[has, "p_cat"].fillna("")
    m.loc[has, "subcategory"] = m.loc[has, "p_sub"].fillna("")
    missing = pinned[~pinned["upc"].isin(members["upc"])]
    extra = pd.DataFrame({
        "upc": missing["upc"], "source_key": missing["p_source"], "department": missing["p_dept"].fillna(""),
        "category": missing["p_cat"].fillna(""), "subcategory": missing["p_sub"].fillna(""), "brand": "", "description": "",
    })
    return pd.concat([m[members.columns], extra], ignore_index=True)


def _prepare_engine_data(engine, config_overrides: dict = None) -> dict:
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
    member_non_p1 = _pin_existing_members(engine, member_non_p1, winner_key)
    member_rows = member_non_p1.to_dict("records")
    fresh_combos = compute_combos(member_rows, evidence_rows, p1_department, strict_map, config, unmatched_defaults)
    return {"config": config, "member_rows": member_rows, "fresh_combos": fresh_combos, "p1_department": p1_department}


def get_prepared_engine_data(engine) -> dict:
    """Prepared engine inputs for the current data — from cache when nothing
    they depend on has changed since the last preparation. Callers must
    treat the result as read-only (it's shared)."""
    token = _engine_data_token(engine)
    with _PREPARED_LOCK:
        if _PREPARED["token"] == token:
            return _PREPARED["data"]
    with _PREPARE_BUILD_LOCK:
        with _PREPARED_LOCK:
            if _PREPARED["token"] == token:
                return _PREPARED["data"]
        data = _prepare_engine_data(engine)
        with _PREPARED_LOCK:
            _PREPARED.update(token=token, data=data)
    return data


def run_engine(engine, config_overrides: dict = None) -> dict:
    """Runs the full engine against live data and writes the incremental
    delta back to dbo.dept_mapping_combos / dept_mapping_combo_upcs.
    Returns a summary dict for display: new_combos, auto_decided,
    needs_review, unmatched, demoted.
    """
    if config_overrides:
        prepared = _prepare_engine_data(engine, config_overrides)
    else:
        prepared = get_prepared_engine_data(engine)
    config, member_rows, fresh_combos = prepared["config"], prepared["member_rows"], prepared["fresh_combos"]
    p1_department = prepared["p1_department"]

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


# decided_via for an auto-decided group held back for a person because its
# Department was marked Strict in Settings (see _write_back).
STRICT_HOLD = "Held for review (Strict)"


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

    with engine.connect() as conn:
        override_upcs = set(conn.execute(text("SELECT upc FROM dbo.dept_mapping_upc_overrides")).scalars().all())
    reopened_ids = set()
    new_keys = set()

    summary = {"new_combos": 0, "auto_decided": 0, "needs_review": 0, "unmatched": 0, "demoted": 0, "reopened": 0}
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
            new_keys.add(key)
            row["decision_state"] = "not_reviewed"
            row["decided_department"] = combo.get("suggested_department") if combo["tier"] == "auto" else None
            row["decided_via"] = "Auto" if combo["tier"] == "auto" else None
            row["manual_department"] = None
            row["approved"] = False
            row["rejected"] = False
            row["combo_id"] = None
        else:
            # Carry-forward rule for a combo that already exists: the engine
            # never changes where it sits or what it's decided as — only a
            # person does (and every such move is tracked). Evidence fields
            # above still refresh, so reviewers see current numbers, but:
            #   - Broken Out / Decided item-by-item: state kept (a decided one
            #     reopens below if genuinely new items showed up in it).
            #   - Decided as a whole (manual or auto): department PINNED — new
            #     items that fall into the group inherit it automatically,
            #     since the decision is combo-level.
            #   - Waiting in Crosswalk/Unmatched (including one a person sent
            #     back out of Decided): stays undecided, never auto-decided or
            #     auto-broken-out behind their back.
            row["combo_id"] = prior["combo_id"]
            row["manual_department"] = prior["manual_department"]
            row["approved"] = prior["approved"]
            row["rejected"] = prior["rejected"]
            state = prior["decision_state"]
            if state in ("broken_out", "decided_broken_out"):
                row["decision_state"] = state
                row["decided_department"] = prior["decided_department"]
                row["decided_via"] = prior["decided_via"]
            elif prior["manual_department"]:
                row["decision_state"] = "decided"
                row["decided_department"] = prior["manual_department"]
                row["decided_via"] = prior["decided_via"] or "Manually Reviewed"
            elif (prior["decided_department"] and prior["decided_via"] == "Auto" and state == "not_reviewed"
                  and combo["is_strict"] and combo["tier"] != "auto"):
                # Only ever auto-decided, and its Department is now marked
                # Strict in Settings: hand it to a person, as Strict means.
                # Remembered, so un-marking it puts the auto decision back.
                row["decision_state"] = "not_reviewed"
                row["decided_department"] = None
                row["decided_via"] = STRICT_HOLD
                summary["demoted"] += 1
            elif prior["decided_department"] and not prior["rejected"]:
                row["decision_state"] = state
                row["decided_department"] = prior["decided_department"]
                row["decided_via"] = prior["decided_via"]
            elif prior["decided_via"] == STRICT_HOLD and combo["tier"] == "auto" and not prior["rejected"]:
                row["decision_state"] = "not_reviewed"
                row["decided_department"] = combo.get("suggested_department")
                row["decided_via"] = "Auto"
            else:
                row["decision_state"] = "not_reviewed"
                row["decided_department"] = None
                row["decided_via"] = STRICT_HOLD if prior["decided_via"] == STRICT_HOLD else None
                row["approved"] = False

        # Auto-break-out: confirmed against script.py's real source
        # (build_final_dataset's `auto_break_out_keys`) — a combo that
        # never reached tier=="auto" itself, but has >=1 member UPC
        # individually decided by a per-UPC pass (Brand/UPC Root/
        # Description Match), moves to UPC-level review automatically
        # this same run — no human has to click "Break Out to UPC-Level"
        # first. Only applies when there's no standing human decision on
        # the combo already (a genuine manual_department/approved/rejected
        # state, handled above, always wins).
        # Brand-new combos only — an existing one stays where a person left it.
        combo_has_upc_hits = any(u in upc_decisions for u in combo["upcs"])
        if prior is None and row["decision_state"] == "not_reviewed" and combo_has_upc_hits:
            row["decision_state"] = "broken_out"
        # A fully decided Broken Out group that picked up genuinely new items
        # (no decision on file for them anywhere) goes back to Broken Out so
        # an editor decides those by hand; its existing decisions stay.
        if prior is not None and row["decision_state"] == "decided_broken_out":
            if any(u not in override_upcs for u in combo["upcs"]):
                row["decision_state"] = "broken_out"
                reopened_ids.add(prior["combo_id"])

        if row["tier"] == "auto":
            summary["auto_decided"] += 1
        elif row["tier"] == "review":
            summary["needs_review"] += 1
        else:
            summary["unmatched"] += 1

        to_upsert.append((key, row, combo["upcs"]))

    summary["reopened"] = len(reopened_ids)

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
                    if hit and key not in new_keys:
                        # An existing group's new items wait for an editor —
                        # the per-UPC match is offered as a suggestion only.
                        candidate = {
                            "upc": upc, "combo_id": combo_id,
                            "suggested_department": hit["department"], "suggested_via": hit["decided_via"],
                            "department": None, "decided_via": "not_reviewed",
                        }
                    elif hit:
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
                delete_stmt = text("DELETE FROM dbo.dept_mapping_upc_overrides WHERE upc IN (SELECT v FROM OPENJSON(:upcs) WITH (v VARCHAR(400) '$'))").bindparams(
                    bindparam("upcs", type_=JSON_LIST)
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
    owns caching via its existing @st.cache_data + .clear() convention.
    A combo's whole-group department only counts while it's decided as a
    whole (same rule as get_decided_combos) — a Broken Out group's items
    only ever take their own per-item decision, so undecided ones stay
    blank rather than inheriting a stale group value."""
    with engine.connect() as conn:
        combo_rows = conn.execute(
            text(
                """
                SELECT cu.upc, c.decided_department
                FROM dbo.dept_mapping_combo_upcs cu
                JOIN dbo.dept_mapping_combos c ON c.combo_id = cu.combo_id
                WHERE c.decided_department IS NOT NULL AND c.decision_state IN ('not_reviewed', 'decided')
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
    given tier ("review" for Crosswalk, "unmatched" for Unmatched) —
    including an undecided auto-tier combo (one a person sent back out of
    Decided, or an existing one whose evidence has since strengthened —
    the engine never decides an existing combo on its own), which lands in
    the queue origin_tier says it came from."""
    with engine.connect() as conn:
        return pd.read_sql(
            text(
                "SELECT combo_id, source_key, raw_department, raw_category, raw_subcategory, "
                "n_upcs_total, n_evidence, purity, majority_department, "
                "runner_up_department, runner_up_share, suggested_department, "
                "resolved_via FROM dbo.dept_mapping_combos "
                "WHERE decision_state = 'not_reviewed' AND decided_department IS NULL AND ("
                "  tier = :tier OR (tier = 'auto' AND "
                "    CASE WHEN n_evidence > 0 THEN 'review' ELSE 'unmatched' END = :tier)"
                ") "
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
                     WHERE o.combo_id = c.combo_id) AS override_count,
                    (SELECT COUNT(*) FROM dbo.dept_mapping_upc_overrides o
                     WHERE o.combo_id = c.combo_id AND o.decided_via LIKE 'Auto-Applied%') AS auto_count
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
            text("UPDATE dbo.dept_mapping_upc_overrides SET decided_via = 'Manually Reviewed' WHERE upc IN (SELECT v FROM OPENJSON(:upcs) WITH (v VARCHAR(400) '$'))").bindparams(
                bindparam("upcs", type_=JSON_LIST)
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


def get_combo_upc_decisions(engine, combo_id: int, limit: int = 20000) -> pd.DataFrame:
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
                "pushed_by = :pushed_by, pushed_at = SYSUTCDATETIME(), decided_note = :note "
                "WHERE upc = :upc"
            ),
            [
                {"upc": u, "department": d["department"], "staged_by": d.get("staged_by") or pushed_by, "pushed_by": pushed_by,
                 "note": d.get("origin_note")}
                for u, d in decisions.items()
            ],
        )
        combo_ids = [
            r[0] for r in conn.execute(
                text("SELECT DISTINCT combo_id FROM dbo.dept_mapping_upc_overrides WHERE upc IN (SELECT v FROM OPENJSON(:upcs) WITH (v VARCHAR(400) '$'))").bindparams(
                    bindparam("upcs", type_=JSON_LIST)
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
                "n_upcs_total, decided_department, decided_via, approved, decision_state, tier, n_evidence, "
                "last_decided_by, last_decided_at, pushed_by, pushed_at, decided_note "
                "FROM dbo.dept_mapping_combos "
                "WHERE decided_department IS NOT NULL AND decision_state IN ('not_reviewed', 'decided')"
            ),
            conn,
        )
        broken = pd.read_sql(
            text(
                "SELECT combo_id, source_key, raw_department, raw_category, raw_subcategory, "
                "n_upcs_total, decided_department, decided_via, approved, decision_state, tier, n_evidence, "
                "last_decided_by, last_decided_at, pushed_by, pushed_at, decided_note "
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


def _json_safe_rows(rows) -> list:
    out = []
    for r in rows:
        d = dict(r)
        for k, v in d.items():
            if isinstance(v, datetime):
                d[k] = v.isoformat(sep=" ")
        out.append(d)
    return out


STAGED_TABLES = (
    "dept_mapping_pending_changes", "dept_mapping_combo_suggestions",
    "dept_mapping_pending_upc_changes", "dept_mapping_upc_change_suggestions",
)


def get_combo_snapshot(engine, combo_id: int) -> dict:
    """Full current-state snapshot of one combo, captured right before an
    immediate, no-push action (Break Out / Send Back) changes it, so Undo
    can restore the EXACT prior state rather than approximating it: the
    combo row's decision fields (plus who last decided it), every per-UPC
    decision with its own audit trail (who decided / pushed it, when), and
    everything staged on it at that moment (a pending decision, dispute
    votes, staged item decisions, item suggestions) — a move discards
    staged work, and undoing the move should bring it back."""
    with engine.connect() as conn:
        return _combo_snapshot(conn, combo_id)


def _combo_snapshot(conn, combo_id: int) -> dict:
    """get_combo_snapshot on an existing connection/transaction."""
    combo_row = conn.execute(
        text(
            "SELECT decision_state, decided_department, decided_via, approved, rejected, manual_department, "
            "last_decided_by, last_decided_at, decided_note FROM dbo.dept_mapping_combos WHERE combo_id = :combo_id"
        ),
        {"combo_id": combo_id},
    ).mappings().first()
    override_rows = conn.execute(
        text(
            "SELECT upc, department, decided_via, suggested_department, suggested_via, "
            "updated_by, updated_at, pushed_by, pushed_at, decided_note "
            "FROM dbo.dept_mapping_upc_overrides WHERE combo_id = :combo_id"
        ),
        {"combo_id": combo_id},
    ).mappings().all()
    staged = {
        t: _json_safe_rows(conn.execute(text(f"SELECT * FROM dbo.{t} WHERE combo_id = :c"), {"c": combo_id}).mappings().all())
        for t in STAGED_TABLES
    }
    combo = _json_safe_rows([combo_row])[0] if combo_row else None
    return {"combo": combo, "overrides": _json_safe_rows(override_rows), "staged": staged}


def _lock_combo(conn, combo_id: int) -> None:
    """Holds the combo's row for the rest of the transaction, so two undo /
    redo clicks on the same group can't interleave — the second waits for
    the first to finish, then checks against what it left."""
    conn.execute(
        text("SELECT combo_id FROM dbo.dept_mapping_combos WITH (UPDLOCK, HOLDLOCK) WHERE combo_id = :c"),
        {"c": combo_id},
    )


def _restore_staged(conn, combo_id: int, snapshot: dict) -> None:
    """Puts back exactly what was staged on the combo when `snapshot` was
    taken (no-op for older snapshots that didn't record it)."""
    staged = snapshot.get("staged")
    if staged is None:
        return
    for t in STAGED_TABLES:
        conn.execute(text(f"DELETE FROM dbo.{t} WHERE combo_id = :c"), {"c": combo_id})
    upcs = [r["upc"] for r in staged.get("dept_mapping_pending_upc_changes", [])]
    for chunk in _chunks(upcs):
        # one staged row per UPC — clear any row it picked up elsewhere since
        conn.execute(
            text("DELETE FROM dbo.dept_mapping_pending_upc_changes WHERE upc IN (SELECT v FROM OPENJSON(:u) WITH (v VARCHAR(400) '$'))").bindparams(bindparam("u", type_=JSON_LIST)),
            {"u": chunk},
        )
    for t in STAGED_TABLES:
        rows = staged.get(t) or []
        if rows:
            cols = list(rows[0].keys())
            conn.execute(
                text(f"INSERT INTO dbo.{t} ({', '.join(cols)}) VALUES ({', '.join(':' + c for c in cols)})"),
                [{**r, "combo_id": combo_id} for r in rows],
            )


def _restore_combo_snapshot(conn, combo_id: int, snapshot: dict, actor: str) -> None:
    """Restores the exact combo-row + per-UPC-override state
    get_combo_snapshot captured, inside the caller's transaction — a real
    undo of a Break Out / Send Back, not a generic "send it back to review"
    that would lose whatever item-level progress had been made."""
    combo = snapshot.get("combo")
    if combo is None:
        return
    conn.execute(
        text(
            """
            UPDATE dbo.dept_mapping_combos
            SET decision_state = :decision_state, decided_department = :decided_department,
                decided_via = :decided_via, approved = :approved, rejected = :rejected,
                manual_department = :manual_department, decided_note = :decided_note,
                last_decided_at = :last_decided_at, last_decided_by = :last_decided_by
            WHERE combo_id = :combo_id
            """
        ),
        # Put back who last decided it and when, exactly as they were —
        # an undo restores history, it doesn't author a new decision.
        # (Snapshots recorded before these fields were captured fall back
        # to the person undoing.)
        {
            "last_decided_by": actor, "last_decided_at": datetime.now(timezone.utc).replace(tzinfo=None).isoformat(sep=" "),
            "decided_note": None, **combo, "combo_id": combo_id,
        },
    )
    conn.execute(text("DELETE FROM dbo.dept_mapping_upc_overrides WHERE combo_id = :combo_id"), {"combo_id": combo_id})
    overrides = snapshot.get("overrides") or []
    if overrides:
        cols = ["upc", "department", "decided_via", "suggested_department", "suggested_via",
                "updated_by", "updated_at", "pushed_by", "pushed_at", "decided_note"]
        conn.execute(
            text(
                f"INSERT INTO dbo.dept_mapping_upc_overrides (combo_id, {', '.join(cols)}) "
                f"VALUES (:combo_id, {', '.join(':' + c for c in cols)})"
            ),
            # updated_at is NOT NULL — older snapshots didn't record it
            [{**{c: None for c in cols}, **o, "combo_id": combo_id,
              "updated_at": o.get("updated_at") or datetime.now(timezone.utc).replace(tzinfo=None).isoformat(sep=" ")}
             for o in overrides],
        )
    _restore_staged(conn, combo_id, snapshot)


def _reset_to_review(conn, combo_id: int, actor: str) -> None:
    """Back to "no decision": the group returns to the queue its tier says
    (an auto-tier one is flagged rejected so the engine leaves it for a person)."""
    conn.execute(
        text(
            """
            UPDATE dbo.dept_mapping_combos
            SET manual_department = NULL, decision_state = 'not_reviewed',
                decided_department = NULL, decided_via = NULL, approved = 0, decided_note = NULL,
                rejected = CASE WHEN tier = 'auto' THEN 1 ELSE 0 END,
                last_decided_at = SYSUTCDATETIME(), last_decided_by = :actor
            WHERE combo_id = :combo_id
            """
        ),
        {"actor": actor, "combo_id": combo_id},
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
        _reset_to_review(conn, combo_id, actor)


def origin_tier(tier, n_evidence) -> str:
    """Which review queue a combo belongs in once a person needs to look at
    it. An auto-tier combo was promoted by the engine out of one of them —
    the engine's own core rule is "any direct evidence -> Crosswalk
    (review), none -> Unmatched" — so that's where it goes back to."""
    if tier != "auto":
        return tier
    return "review" if (n_evidence or 0) > 0 else "unmatched"


def reopen_broken_out(engine, combo_id: int, actor: str, upc_decisions: dict = None) -> None:
    """Sends a finished (decided_broken_out) group back to Broken Out for
    its items to be decided again: its current item decisions are cleared
    and it's re-seeded exactly like a fresh Break Out — with whatever
    auto-matching could decide right now (upc_decisions, from
    compute_upc_decisions_for_combo(..., ignore_own=True)) or blank. The
    caller snapshots first, so Undo restores every prior item decision."""
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM dbo.dept_mapping_upc_overrides WHERE combo_id = :c"), {"c": combo_id})
    break_out_combo(engine, combo_id, actor, upc_decisions=upc_decisions)


def revert_combo(engine, combo_id: int, actor: str) -> None:
    """Sends a Decided combo back for fresh review — clears the manual
    override and decision entirely, so it reverts to whatever its own
    already-computed tier says (auto/review/unmatched), immediately
    reachable again from Crosswalk/Unmatched without needing an engine
    re-run. The reversal path a genuine manual override always has — an
    auto-decision that turns out to be wrong is never permanent. An
    auto-tier combo is flagged rejected so the next engine run leaves it
    for a person instead of silently re-deciding it, and get_review_queue
    shows it in the queue it was promoted out of (see origin_tier)."""
    with engine.begin() as conn:
        _reset_to_review(conn, combo_id, actor)


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
                "updated_by, updated_at FROM dbo.manual_overrides WHERE upc IN (SELECT v FROM OPENJSON(:upcs) WITH (v VARCHAR(400) '$'))"
            ).bindparams(bindparam("upcs", type_=JSON_LIST)),
            {"upcs": upcs},
        ).mappings().all()
    return {r["upc"]: dict(r) for r in rows}


def add_department(engine, department: str, actor: str = None) -> None:
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
            log_activity(conn, actor, "Settings", "Added a Department", department)


def remove_department(engine, department: str, actor: str = None) -> None:
    """Removes a Department from the dropdown list. Only ever removes a
    MANUAL row — an 'auto' row (one of Scan Advantage's own real current
    Departments) can't be removed here since it would just reappear on
    the next engine run anyway; the underlying data would need to change
    first."""
    with engine.begin() as conn:
        n = conn.execute(
            text("DELETE FROM dbo.dept_mapping_departments WHERE department = :department AND source_type = 'manual'"),
            {"department": department.strip().upper()},
        ).rowcount
        if n:
            log_activity(conn, actor, "Settings", "Removed a Department", department.strip().upper())


def get_unmatched_defaults(engine) -> pd.DataFrame:
    with engine.connect() as conn:
        return pd.read_sql(
            text(
                "SELECT source_key, old_department, new_department FROM dbo.dept_mapping_unmatched_defaults "
                "ORDER BY source_key, old_department"
            ),
            conn,
        )


def unmatched_old_departments(engine) -> pd.DataFrame:
    """One row per distributor Department text the Unmatched groups use —
    the texts nothing in the data can place — with which sources use it,
    how many groups / items it covers, its default (for every source) and
    any per-source exceptions. Saved defaults whose text no Unmatched group
    uses any more are listed too. Settings → Unmatched Department Defaults
    is edited from this."""
    with engine.connect() as conn:
        used = pd.read_sql(text(
            "SELECT source_key, UPPER(LTRIM(RTRIM(ISNULL(raw_department, '')))) AS old_department, "
            "COUNT(*) AS n_groups, "
            "SUM(CASE WHEN tier = 'unmatched' AND decision_state = 'not_reviewed' THEN 1 ELSE 0 END) AS n_waiting, "
            "SUM(n_upcs_total) AS n_items "
            "FROM dbo.dept_mapping_combos "
            "WHERE tier = 'unmatched' OR (tier = 'auto' AND ISNULL(n_evidence, 0) = 0) "
            "GROUP BY source_key, UPPER(LTRIM(RTRIM(ISNULL(raw_department, ''))))"), conn)
        saved = pd.read_sql(text(
            "SELECT source_key, UPPER(LTRIM(RTRIM(old_department))) AS old_department, new_department "
            "FROM dbo.dept_mapping_unmatched_defaults"), conn)
    anyrow = dict(zip(saved.loc[saved["source_key"] == "any", "old_department"], saved.loc[saved["source_key"] == "any", "new_department"]))
    exact = saved[saved["source_key"] != "any"]
    rows = []
    for old in sorted(set(used["old_department"]) | set(saved["old_department"])):
        u = used[used["old_department"] == old]
        ex = exact[exact["old_department"] == old]
        rows.append({
            "old_department": old,
            "sources": ", ".join(sorted(u["source_key"].str.upper())),
            "n_groups": int(u["n_groups"].sum()), "n_waiting": int(u["n_waiting"].sum()), "n_items": int(u["n_items"].sum()),
            "default": anyrow.get(old),
            "exceptions": "; ".join(f"{r.source_key.upper()} → {r.new_department}" for r in ex.itertuples()) or None,
        })
    df = pd.DataFrame(rows, columns=["old_department", "sources", "n_groups", "n_waiting", "n_items", "default", "exceptions"])
    df = df.astype(object).where(df.notna(), None)
    return df.sort_values(["n_waiting", "n_items"], ascending=False, key=lambda c: c.astype(int)).reset_index(drop=True)


def set_unmatched_default(conn, source_key: str, old_department: str, new_department: str | None, actor: str) -> None:
    """Sets (or with None, clears) one default."""
    old = old_department.strip().upper()
    conn.execute(text("DELETE FROM dbo.dept_mapping_unmatched_defaults WHERE source_key = :s "
                      "AND UPPER(LTRIM(RTRIM(old_department))) = :o"), {"s": source_key, "o": old})
    if new_department:
        conn.execute(text("INSERT INTO dbo.dept_mapping_unmatched_defaults (source_key, old_department, new_department, updated_by) "
                          "VALUES (:s, :o, :n, :a)"), {"s": source_key, "o": old, "n": new_department, "a": actor})


def department_usage(engine, department: str) -> dict:
    """Everywhere a Department is in use — so removing one from the list
    never leaves a decision, default or staged change pointing at a
    Department nobody can pick any more."""
    d = department.strip().upper()
    q = {
        "groups decided as it": "SELECT COUNT(*) FROM dbo.dept_mapping_combos WHERE UPPER(decided_department) = :d "
                                "AND decision_state IN ('decided', 'not_reviewed')",
        "item decisions": "SELECT COUNT(*) FROM dbo.dept_mapping_upc_overrides WHERE UPPER(department) = :d",
        "staged group decisions": "SELECT COUNT(*) FROM dbo.dept_mapping_pending_changes WHERE UPPER(department) = :d",
        "staged item decisions": "SELECT COUNT(*) FROM dbo.dept_mapping_pending_upc_changes WHERE UPPER(department) = :d",
        "staged UPC overrides / adds": "SELECT COUNT(*) FROM dbo.item_master_pending_changes WHERE UPPER(department) = :d",
        "UPC overrides": "SELECT COUNT(*) FROM dbo.manual_overrides WHERE UPPER(department) = :d",
        "Unmatched Defaults": "SELECT COUNT(*) FROM dbo.dept_mapping_unmatched_defaults WHERE UPPER(new_department) = :d",
        "items in the item master": "SELECT COUNT(*) FROM dbo.items WHERE UPPER(department) = :d",
    }
    with engine.connect() as conn:
        return {k: int(conn.execute(text(sql), {"d": d}).scalar() or 0) for k, sql in q.items()}


def department_usage_all(engine) -> pd.DataFrame:
    """department_usage for every Department at once: one row per
    Department, one column per place it can be used."""
    q = {
        "Groups": "SELECT UPPER(decided_department) d, COUNT(*) n FROM dbo.dept_mapping_combos "
                  "WHERE decision_state IN ('decided', 'not_reviewed') GROUP BY UPPER(decided_department)",
        "Item decisions": "SELECT UPPER(department) d, COUNT(*) n FROM dbo.dept_mapping_upc_overrides GROUP BY UPPER(department)",
        "Staged": "SELECT d, SUM(n) n FROM (SELECT UPPER(department) d, COUNT(*) n FROM dbo.dept_mapping_pending_changes GROUP BY UPPER(department) "
                  "UNION ALL SELECT UPPER(department), COUNT(*) FROM dbo.dept_mapping_pending_upc_changes GROUP BY UPPER(department) "
                  "UNION ALL SELECT UPPER(department), COUNT(*) FROM dbo.item_master_pending_changes GROUP BY UPPER(department)) x GROUP BY d",
        "UPC overrides": "SELECT UPPER(department) d, COUNT(*) n FROM dbo.manual_overrides GROUP BY UPPER(department)",
        "Defaults": "SELECT UPPER(new_department) d, COUNT(*) n FROM dbo.dept_mapping_unmatched_defaults GROUP BY UPPER(new_department)",
        "Items": "SELECT UPPER(department) d, COUNT(*) n FROM dbo.items GROUP BY UPPER(department)",
    }
    out = get_departments(engine)
    out["key"] = out["department"].str.upper()
    with engine.connect() as conn:
        for col, sql in q.items():
            m = dict(conn.execute(text(sql)).all())
            out[col] = out["key"].map(m).fillna(0).astype(int)
    return out.drop(columns="key")


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


def approve_combo(engine, combo_id: int, department: str, actor: str, pushed_by: str | None = None,
                  note: str | None = None) -> None:
    """A human confirms `department` for this combo's whole membership —
    manual_department is the durable record (never silently overwritten
    by a future engine run's fresh evidence; see _write_back). `actor` is
    who actually decided the department — normally whoever staged the
    Approve, preserved through to push rather than overwritten by
    whoever happens to click Push; `pushed_by` records that separately,
    when known, so both survive independently."""
    with engine.begin() as conn:
        # A whole-group decision on a group that was decided item by item
        # replaces those item decisions — otherwise they'd keep winning over it.
        conn.execute(text(
            "DELETE FROM dbo.dept_mapping_upc_overrides WHERE combo_id = :c AND EXISTS (SELECT 1 FROM dbo.dept_mapping_combos "
            "WHERE combo_id = :c AND decision_state IN ('broken_out', 'decided_broken_out'))"), {"c": combo_id})
        conn.execute(
            text(
                """
                UPDATE dbo.dept_mapping_combos
                SET manual_department = :department, decision_state = 'decided',
                    decided_department = :department, decided_via = 'Manually Reviewed',
                    approved = 1, rejected = 0,
                    last_decided_at = SYSUTCDATETIME(), last_decided_by = :actor,
                    pushed_by = :pushed_by, pushed_at = SYSUTCDATETIME(), decided_note = :note
                WHERE combo_id = :combo_id
                """
            ),
            {"department": department, "actor": actor, "combo_id": combo_id, "pushed_by": pushed_by or actor, "note": note},
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


def compute_upc_decisions_for_combo(engine, combo_id: int, config: dict = None, ignore_own: bool = False) -> dict:
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
    prepared = get_prepared_engine_data(engine)
    cfg = {**prepared["config"], **(config or {})}
    fresh_combos = prepared["fresh_combos"]

    with engine.connect() as conn:
        # Only this group's items are candidates, so only their standing
        # decisions matter.
        already_covered_rows = conn.execute(
            text("SELECT o.upc FROM dbo.dept_mapping_upc_overrides o JOIN dbo.dept_mapping_combo_upcs cu "
                 "ON cu.upc = o.upc AND cu.combo_id = :combo_id WHERE o.decided_via <> 'not_reviewed'"),
            {"combo_id": combo_id},
        ).fetchall()
        combo_upcs = {
            r[0] for r in conn.execute(
                text("SELECT upc FROM dbo.dept_mapping_combo_upcs WHERE combo_id = :combo_id"),
                {"combo_id": combo_id},
            ).fetchall()
        }
    already_covered_upcs = {r[0] for r in already_covered_rows}
    if ignore_own:
        # Re-deciding a finished group from scratch: its own current item
        # decisions are about to be replaced, so they don't block matching.
        already_covered_upcs -= combo_upcs
    # Only this combo's own rows are candidates — each UPC is matched
    # independently against the donor tables (built from every auto combo),
    # so the result for these UPCs is identical to a full pass.
    candidates = [r for r in prepared["member_rows"] if r["upc"] in combo_upcs]
    upc_decisions = apply_upc_level_overrides(fresh_combos, candidates, already_covered_upcs, cfg)
    return {upc: v for upc, v in upc_decisions.items() if upc in combo_upcs}


def set_broken_out_auto(engine, combo_id: int, mode: str) -> int:
    """After an undo lands a group back in Broken Out, keep only what the
    person chose:
      'auto_only' — the auto-matched item decisions; staged item decisions dropped
      'rerun'     — staged and auto dropped, auto-matching run fresh
      'blank'     — every item blank (staged, auto and earlier decisions)
    ('as_was' needs nothing — the undo already restored everything.)
    Returns how many items end up auto-decided."""
    delete_pending_upc_changes_for_combo(engine, combo_id)
    with engine.begin() as conn:
        if mode == "blank":
            conn.execute(text(
                "UPDATE dbo.dept_mapping_upc_overrides SET department = NULL, decided_via = 'not_reviewed' WHERE combo_id = :c"),
                {"c": combo_id})
        elif mode == "rerun":
            conn.execute(text(
                "UPDATE dbo.dept_mapping_upc_overrides SET department = NULL, decided_via = 'not_reviewed' "
                "WHERE combo_id = :c AND decided_via LIKE 'Auto-Applied%'"), {"c": combo_id})
    n = 0
    if mode == "rerun":
        hits = compute_upc_decisions_for_combo(engine, combo_id)
        with engine.begin() as conn:
            for u, h in hits.items():
                n += conn.execute(text(
                    "UPDATE dbo.dept_mapping_upc_overrides SET department = :d, decided_via = :v, suggested_department = :d, "
                    "suggested_via = :v WHERE upc = :u AND combo_id = :c AND decided_via = 'not_reviewed'"),
                    {"d": h["department"], "v": h["decided_via"], "u": u, "c": combo_id}).rowcount
    with engine.begin() as conn:
        left = conn.execute(text("SELECT COUNT(*) FROM dbo.dept_mapping_upc_overrides WHERE combo_id = :c "
                                 "AND decided_via = 'not_reviewed'"), {"c": combo_id}).scalar()
        if left:
            conn.execute(text("UPDATE dbo.dept_mapping_combos SET decision_state = 'broken_out' "
                              "WHERE combo_id = :c AND decision_state = 'decided_broken_out'"), {"c": combo_id})
    return n


def auto_counts(overrides: list) -> tuple:
    """(auto-decided, decided by people) among a snapshot's item rows."""
    auto = sum(1 for o in overrides or [] if str(o.get("decided_via") or "").startswith("Auto-Applied"))
    people = sum(1 for o in overrides or [] if o.get("decided_via") not in (None, "not_reviewed")
                 and not str(o.get("decided_via")).startswith("Auto-Applied"))
    return auto, people


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
                text("SELECT upc FROM dbo.dept_mapping_upc_overrides WHERE upc IN (SELECT v FROM OPENJSON(:upcs) WITH (v VARCHAR(400) '$'))").bindparams(
                    bindparam("upcs", type_=JSON_LIST)
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
                "snapshot_json, created_by, created_at, origin_note "
                "FROM dbo.dept_mapping_recent_moves ORDER BY move_id DESC"
            )
        ).mappings().all()
    return [
        {
            "move_id": r["move_id"], "combo_id": r["combo_id"], "source_key": r["source_key"],
            "label": r["label"], "n_upcs_total": r["n_upcs_total"], "description": r["description"],
            "snapshot": json.loads(r["snapshot_json"]), "created_by": r["created_by"], "created_at": r["created_at"],
            "origin_note": r["origin_note"],
        }
        for r in rows
    ]


MAX_RECENT_MOVES = 15


def _decision_signature(snapshot: dict) -> tuple:
    """What makes two snapshots the same place in a combo's history — its
    decision fields and every per-UPC decision. Suggestions are left out:
    they're regenerated by the engine on each Break Out, not decisions."""
    combo = snapshot.get("combo") or {}
    fields = tuple(
        combo.get(k) for k in ("decision_state", "decided_department", "decided_via", "manual_department")
    ) + (bool(combo.get("approved")), bool(combo.get("rejected")))
    overrides = tuple(sorted(
        (o["upc"], o.get("department"), o.get("decided_via")) for o in (snapshot.get("overrides") or [])
    ))
    return fields, overrides


def record_recent_move(
    engine, combo_id: int, source_key: str, label: str, n_upcs_total: int,
    description: str, snapshot: dict, actor: str, origin_note: str | None = None,
) -> None:
    """Pushes one Break Out/Send Back onto the combo's undo stack —
    `snapshot` is the state it was taken FROM. If the move lands the combo
    back in a state it already had earlier in the stack (Crosswalk ->
    Broken Out -> Crosswalk), those moves were a round trip: they're popped
    instead, so moving back and forth never grows an ever-longer history
    and the stack never holds the same state twice — unless the state being
    left had staged work on it, which only an undo could bring back. A move
    that changed nothing isn't recorded at all."""
    after_sig = _decision_signature(get_combo_snapshot(engine, combo_id))
    left_had_staged = any((snapshot.get("staged") or {}).values())
    if after_sig == _decision_signature(snapshot) and not left_had_staged:
        return
    # Collapsing would erase the only record of work staged on the state
    # being left — record the move instead, so Undo can bring it back.
    if left_had_staged:
        stack = []
    else:
        with engine.connect() as conn:
            stack = conn.execute(
                text(
                    "SELECT move_id, snapshot_json FROM dbo.dept_mapping_recent_moves "
                    "WHERE combo_id = :c ORDER BY move_id"
                ),
                {"c": combo_id},
            ).mappings().all()
    for m in stack:
        earlier = json.loads(m["snapshot_json"])
        if _decision_signature(earlier) == after_sig:
            with engine.begin() as conn:
                conn.execute(
                    text("DELETE FROM dbo.dept_mapping_recent_moves WHERE combo_id = :c AND move_id >= :m"),
                    {"c": combo_id, "m": m["move_id"]},
                )
                # A round trip is an undo: put back that earlier state
                # exactly — item records, who-last-decided, staged work.
                _restore_combo_snapshot(conn, combo_id, earlier, actor)
            return
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO dbo.dept_mapping_recent_moves "
                "(combo_id, source_key, label, n_upcs_total, description, snapshot_json, created_by, origin_note) "
                "VALUES (:combo_id, :source_key, :label, :n_upcs_total, :description, :snapshot_json, :created_by, :origin_note)"
            ),
            {
                "combo_id": combo_id, "source_key": source_key, "label": label,
                "n_upcs_total": n_upcs_total, "description": description,
                "snapshot_json": json.dumps(snapshot), "created_by": actor, "origin_note": origin_note,
            },
        )
        # Capped PER COMBO, not globally — a global cap let unrelated combos'
        # moves silently prune this combo's older steps, which the undo-stage
        # picker needs to walk all the way back. Pushing a real decision
        # already clears a combo's stack (clear_recent_moves_for_combo), so
        # this only bounds unpushed back-and-forth.
        conn.execute(
            text(
                f"""
                DELETE FROM dbo.dept_mapping_recent_moves WHERE move_id IN (
                    SELECT move_id FROM dbo.dept_mapping_recent_moves
                    WHERE combo_id = :combo_id
                    ORDER BY move_id DESC
                    OFFSET {MAX_RECENT_MOVES} ROWS FETCH NEXT 1000 ROWS ONLY
                )
                """
            ),
            {"combo_id": combo_id},
        )


def clear_recent_moves_for_combo(engine, combo_id: int) -> None:
    """Called whenever a REAL decision for this combo gets pushed (its
    whole-group Approve, or its per-item Department choices) — that
    decision IS the new database state, so any earlier Break Out/Send
    Back snapshots for this same combo are no longer meaningful undo
    targets (there is nothing earlier left worth walking back to)."""
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM dbo.dept_mapping_recent_moves WHERE combo_id = :combo_id"), {"combo_id": combo_id})


def describe_combo_state(state: dict | None, tier: str | None) -> str:
    """Plain-English name for where a combo sits, from a combo row or a
    snapshot's "combo" part — e.g. "Crosswalk", "Broken Out", "Decided as
    GROCERY"."""
    if not state:
        return "unknown"
    ds = state.get("decision_state")
    if ds == "decided_broken_out":
        return "Decided (item by item)"
    if ds == "broken_out":
        return "Broken Out"
    # Same rule get_decided_combos uses: a whole-group decision is a
    # decided_department on a not_reviewed/decided row.
    if state.get("decided_department"):
        return f"Decided as {state['decided_department']}"
    return {"review": "Crosswalk", "unmatched": "Unmatched", "auto": "Auto"}.get(tier, "Review")


def get_combo_undo_path(engine, combo_id: int) -> dict:
    """Everything the undo-stage picker needs for one combo: where it is
    now, what's staged on top of it, and its unpushed Break Out/Send Back
    history (newest first) — each move carries the state it was taken FROM,
    so undoing moves[0..k] means restoring moves[k]'s snapshot.

    `authorized` is who may execute an undo that discards staged work
    without anyone else's say-so (admins always may): the combo's stager
    for a resolved combo decision, else its first suggester for a dispute,
    else the Broken Out group's first editor. None = nothing is staged, so
    anyone may walk back the moves themselves (same as Recent Moves always
    allowed)."""
    with engine.connect() as conn:
        combo = conn.execute(
            text(
                "SELECT source_key, raw_department, raw_category, raw_subcategory, tier, n_evidence, "
                "decision_state, decided_department FROM dbo.dept_mapping_combos WHERE combo_id = :c"
            ),
            {"c": combo_id},
        ).mappings().first()
        if combo is None:
            return {}
        pending = conn.execute(
            text("SELECT department, staged_by FROM dbo.dept_mapping_pending_changes WHERE combo_id = :c"),
            {"c": combo_id},
        ).mappings().first()
        votes = conn.execute(
            text("SELECT staged_by FROM dbo.dept_mapping_combo_suggestions WHERE combo_id = :c ORDER BY suggested_at"),
            {"c": combo_id},
        ).scalars().all()
        upc_staged = conn.execute(
            text(
                "SELECT COUNT(*) AS n, MIN(staged_at) AS first_at FROM dbo.dept_mapping_pending_upc_changes WHERE combo_id = :c"
            ),
            {"c": combo_id},
        ).mappings().first()
        upc_sugg_n = conn.execute(
            text("SELECT COUNT(*) FROM dbo.dept_mapping_upc_change_suggestions WHERE combo_id = :c"),
            {"c": combo_id},
        ).scalar()
        move_rows = conn.execute(
            text(
                "SELECT move_id, description, snapshot_json, created_by, created_at "
                "FROM dbo.dept_mapping_recent_moves WHERE combo_id = :c ORDER BY move_id DESC"
            ),
            {"c": combo_id},
        ).mappings().all()
    tier = origin_tier(combo["tier"], combo["n_evidence"])
    moves = []
    for m in move_rows:
        snap = json.loads(m["snapshot_json"])
        moves.append({
            "move_id": m["move_id"], "description": m["description"],
            "created_by": m["created_by"], "created_at": m["created_at"],
            "snapshot": snap, "before": describe_combo_state(snap.get("combo"), tier),
        })
    if pending:
        authorized = pending["staged_by"]
    elif votes:
        authorized = votes[0]
    elif upc_staged["n"]:
        authorized = get_broken_out_group_primary(engine, combo_id)
    else:
        authorized = None
    return {
        "combo_id": combo_id,
        "tier": tier,
        "current": describe_combo_state(dict(combo), tier),
        "staged": {
            "combo_decision": pending["department"] if pending else None,
            "combo_votes": len(votes),
            "upc_items": int(upc_staged["n"] or 0),
            "upc_suggestions": int(upc_sugg_n or 0),
        },
        "has_staged": bool(pending or votes or upc_staged["n"] or upc_sugg_n),
        "moves": moves,
        "authorized": authorized,
    }


def undo_combo_to_stage(
    engine, combo_id: int, n_moves: int, actor: str,
    expected_move_ids: list = None, expected_state: str = None,
) -> bool:
    """Discards everything staged on this combo, then (n_moves > 0) walks
    back its n_moves most recent Break Out/Send Back moves by restoring the
    snapshot of the oldest one being undone — one transaction, so a
    failure partway can never leave the combo half-restored. n_moves == 0
    undoes only the staged decisions and leaves the combo where it is.

    expected_move_ids / expected_state are what the person SAW when they
    picked this undo (the popup's history and the group's state). If the
    group changed since — someone else undid, moved, or staged on it —
    nothing happens and this returns False, so an undo can never reach
    further back than chosen or throw away work nobody reviewed. The row
    lock makes a second simultaneous click wait, then fail this check."""
    with engine.begin() as conn:
        _lock_combo(conn, combo_id)
        rows = conn.execute(
            text(
                "SELECT move_id, snapshot_json FROM dbo.dept_mapping_recent_moves "
                "WHERE combo_id = :c ORDER BY move_id DESC"
            ),
            {"c": combo_id},
        ).mappings().all()
        if expected_move_ids is not None and [r["move_id"] for r in rows] != list(expected_move_ids):
            return False
        if expected_state is not None and _redo_state_key(_combo_snapshot(conn, combo_id)) != expected_state:
            return False
        moves = [{"move_id": r["move_id"], "snapshot": json.loads(r["snapshot_json"])} for r in rows[:max(n_moves, 0)]]
        upcs = conn.execute(
            text("SELECT upc FROM dbo.dept_mapping_pending_upc_changes WHERE combo_id = :c"), {"c": combo_id}
        ).scalars().all()
        for stmt in (
            "DELETE FROM dbo.dept_mapping_pending_changes WHERE combo_id = :c",
            "DELETE FROM dbo.dept_mapping_combo_suggestions WHERE combo_id = :c",
            "DELETE FROM dbo.dept_mapping_pending_upc_changes WHERE combo_id = :c",
            "DELETE FROM dbo.dept_mapping_upc_change_suggestions WHERE combo_id = :c",
            "DELETE FROM dbo.dept_mapping_broken_out_claims WHERE combo_id = :c",
        ):
            conn.execute(text(stmt), {"c": combo_id})
        conn.execute(
            text(
                "DELETE FROM dbo.dept_mapping_undo_requests "
                "WHERE entity_type IN ('combo', 'upc_group') AND entity_id = :c"
            ),
            {"c": str(combo_id)},
        )
        for chunk in _chunks(list(upcs)):
            conn.execute(
                text("DELETE FROM dbo.dept_mapping_undo_requests WHERE entity_type = 'upc' AND entity_id IN (SELECT v FROM OPENJSON(:u) WITH (v VARCHAR(400) '$'))")
                .bindparams(bindparam("u", type_=JSON_LIST)),
                {"u": chunk},
            )
        if moves:
            _restore_combo_snapshot(conn, combo_id, moves[-1]["snapshot"], actor)
            for chunk in _chunks([m["move_id"] for m in moves]):
                conn.execute(
                    text("DELETE FROM dbo.dept_mapping_recent_moves WHERE move_id IN :ids")
                    .bindparams(bindparam("ids", expanding=True)),
                    {"ids": chunk},
                )
        _clear_dept_push_approvals(conn)
    return True


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
                "agreed_by, agreed_at, overridden_by, overridden_at, origin_note FROM dbo.dept_mapping_pending_changes"
            ),
        ).mappings().all()
    return {
        r["combo_id"]: {
            "tier": r["tier"], "action": "approve", "department": r["department"],
            "source_key": r["source_key"], "label": r["label"], "n_upcs_total": r["n_upcs_total"],
            "staged_by": r["staged_by"], "staged_at": r["staged_at"],
            "agreed_by": r["agreed_by"], "agreed_at": r["agreed_at"],
            "overridden_by": r["overridden_by"], "overridden_at": r["overridden_at"],
            "origin_note": r["origin_note"],
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
        dissenters = [r for r in rows if r["department"] != department and r["staged_by"] != actor]
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
    # Where the decision came from (e.g. the one-time old-workbook import)
    # stays with it while it's the same Department; a changed one drops it.
    prior = conn.execute(text("SELECT department, origin_note FROM dbo.dept_mapping_pending_changes WHERE combo_id = :c"),
                         {"c": combo_id}).mappings().first()
    note = prior["origin_note"] if prior and prior["department"] == department else None
    conn.execute(text("DELETE FROM dbo.dept_mapping_pending_changes WHERE combo_id = :combo_id"), {"combo_id": combo_id})
    conn.execute(
        text(
            "INSERT INTO dbo.dept_mapping_pending_changes "
            "(combo_id, tier, department, source_key, label, n_upcs_total, staged_by, agreed_by, agreed_at, is_saved, origin_note) "
            "VALUES (:combo_id, :tier, :department, :source_key, :label, :n_upcs_total, :staged_by, :agreed_by, "
            + ("SYSUTCDATETIME()" if agreed_by else "NULL") + ", 1, :note)"
        ),
        {
            "combo_id": combo_id, "tier": winner.get("tier"), "department": department,
            "source_key": winner.get("source_key"), "label": winner.get("label"), "n_upcs_total": winner.get("n_upcs_total"),
            "staged_by": winner["staged_by"], "agreed_by": agreed_by, "note": note,
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
                "revised_by, revised_at, overridden_by, overridden_at, origin_note FROM dbo.dept_mapping_pending_upc_changes"
            ),
        ).mappings().all()
    return {r["upc"]: dict(r) for r in rows}


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
    upcs = list(changes)
    with engine.begin() as conn:
        owners = {}
        sugg_depts = {}
        for chunk in _chunks(upcs):
            for r in conn.execute(
                text("SELECT upc, staged_by, overridden_by FROM dbo.dept_mapping_pending_upc_changes WHERE upc IN (SELECT v FROM OPENJSON(:upcs) WITH (v VARCHAR(400) '$'))")
                .bindparams(bindparam("upcs", type_=JSON_LIST)),
                {"upcs": chunk},
            ).mappings():
                owners[r["upc"]] = r
            for r in conn.execute(
                text("SELECT upc, department FROM dbo.dept_mapping_upc_change_suggestions WHERE upc IN (SELECT v FROM OPENJSON(:upcs) WITH (v VARCHAR(400) '$'))")
                .bindparams(bindparam("upcs", type_=JSON_LIST)),
                {"upcs": chunk},
            ).mappings():
                sugg_depts.setdefault(r["upc"], set()).add(r["department"])

        decided_rows, sugg_rows = [], []
        for upc, c in changes.items():
            owner_row = owners.get(upc)
            owner = owner_row["staged_by"] if owner_row else None
            if owner_row and owner_row["overridden_by"] and not is_admin:
                results[upc] = {"status": "locked", "owner": owner}
            elif owner and owner != actor:
                others = sugg_depts.get(upc, set())
                if c["department"] not in others and len(others) >= MAX_DISTINCT_SUGGESTIONS:
                    results[upc] = {"status": "blocked", "owner": owner}
                    continue
                sugg_rows.append({
                    "upc": upc, "actor": actor, "department": c["department"], "combo_id": c["combo_id"],
                    "label": c["label"], "description": c.get("description"), "source_key": c.get("source_key", ""),
                })
                results[upc] = {"status": "suggested", "owner": owner}
            else:
                decided_rows.append({
                    "upc": upc, "combo_id": c["combo_id"], "department": c["department"],
                    "label": c["label"], "description": c.get("description"),
                    "source_key": c.get("source_key", ""), "staged_by": actor,
                })
                results[upc] = {"status": "decided", "owner": actor}

        if sugg_rows:
            for chunk in _chunks([r["upc"] for r in sugg_rows]):
                conn.execute(
                    text("DELETE FROM dbo.dept_mapping_upc_change_suggestions WHERE suggested_by = :actor AND upc IN (SELECT v FROM OPENJSON(:upcs) WITH (v VARCHAR(400) '$'))")
                    .bindparams(bindparam("upcs", type_=JSON_LIST)),
                    {"actor": actor, "upcs": chunk},
                )
            conn.execute(
                text(
                    "INSERT INTO dbo.dept_mapping_upc_change_suggestions "
                    "(upc, suggested_by, department, combo_id, label, description, source_key) "
                    "VALUES (:upc, :actor, :department, :combo_id, :label, :description, :source_key)"
                ),
                sugg_rows,
            )
        if decided_rows:
            prior = {}
            for chunk in _chunks([r["upc"] for r in decided_rows]):
                prior.update({r[0]: (r[1], r[2]) for r in conn.execute(
                    text("SELECT upc, department, origin_note FROM dbo.dept_mapping_pending_upc_changes "
                         "WHERE upc IN (SELECT v FROM OPENJSON(:upcs) WITH (v VARCHAR(400) '$'))")
                    .bindparams(bindparam("upcs", type_=JSON_LIST)), {"upcs": chunk}).all()})
                conn.execute(
                    text("DELETE FROM dbo.dept_mapping_pending_upc_changes WHERE upc IN (SELECT v FROM OPENJSON(:upcs) WITH (v VARCHAR(400) '$'))")
                    .bindparams(bindparam("upcs", type_=JSON_LIST)),
                    {"upcs": chunk},
                )
            for r in decided_rows:  # a note stays while the Department does
                old = prior.get(r["upc"])
                r["origin_note"] = changes[r["upc"]].get("origin_note") or (old[1] if old and old[0] == r["department"] else None)
            conn.execute(
                text(
                    "INSERT INTO dbo.dept_mapping_pending_upc_changes "
                    "(upc, combo_id, department, label, description, source_key, staged_by, is_saved, origin_note) "
                    "VALUES (:upc, :combo_id, :department, :label, :description, :source_key, :staged_by, 1, :origin_note)"
                ),
                decided_rows,
            )
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


CLAIM_IDLE_MINUTES = 120  # a Broken Out claim auto-releases after this long idle


def get_broken_out_claim(engine, combo_id: int, idle_minutes: int = CLAIM_IDLE_MINUTES) -> dict | None:
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


def get_broken_out_claims(engine, combo_ids=None, idle_minutes: int = CLAIM_IDLE_MINUTES) -> dict:
    """Batch version of get_broken_out_claim for a list page — {combo_id:
    {...}}, expired claims lazily cleaned up and excluded same as the
    single-combo version."""
    with engine.begin() as conn:
        query = ("SELECT combo_id, claimed_by, claimed_at, last_activity_at, "
                 "DATEDIFF(MINUTE, last_activity_at, SYSUTCDATETIME()) AS idle_minutes "
                 "FROM dbo.dept_mapping_broken_out_claims")
        if combo_ids is not None:
            if not combo_ids:
                return {}
            rows = conn.execute(
                text(f"{query} WHERE combo_id IN :combo_ids").bindparams(bindparam("combo_ids", expanding=True)),
                {"combo_ids": list(combo_ids)},
            ).mappings().all()
        else:
            rows = conn.execute(text(query)).mappings().all()
        expired = [r["combo_id"] for r in rows if r["idle_minutes"] >= idle_minutes]
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
    if prior and prior["department"] != department and prior["staged_by"] != actor:
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


def delete_pending_upc_changes(engine, upcs: list) -> None:
    if not upcs:
        return
    with engine.begin() as conn:
        for chunk in _chunks(list(upcs)):
            params = {"upcs": chunk}
            for stmt in (
                "DELETE FROM dbo.dept_mapping_pending_upc_changes WHERE upc IN (SELECT v FROM OPENJSON(:upcs) WITH (v VARCHAR(400) '$'))",
                "DELETE FROM dbo.dept_mapping_upc_change_suggestions WHERE upc IN (SELECT v FROM OPENJSON(:upcs) WITH (v VARCHAR(400) '$'))",
                "DELETE FROM dbo.dept_mapping_undo_requests WHERE entity_type = 'upc' AND entity_id IN (SELECT v FROM OPENJSON(:upcs) WITH (v VARCHAR(400) '$'))",
            ):
                conn.execute(text(stmt).bindparams(bindparam("upcs", type_=JSON_LIST)), params)
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
                text("DELETE FROM dbo.dept_mapping_undo_requests WHERE entity_type = 'upc' AND entity_id IN (SELECT v FROM OPENJSON(:upcs) WITH (v VARCHAR(400) '$'))").bindparams(
                    bindparam("upcs", type_=JSON_LIST)
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
                "pack, size, uom, source_key, staged_by, staged_at, origin_note FROM dbo.item_master_pending_changes"
            ),
        ).mappings().all()
    return {
        r["upc"]: {
            "change_type": r["change_type"], "description": r["description"], "department": r["department"],
            "category": r["category"], "subcategory": r["subcategory"], "brand": r["brand"],
            "pack": r["pack"], "size": r["size"], "uom": r["uom"],
            "source_key": r["source_key"], "staged_by": r["staged_by"], "staged_at": r["staged_at"],
            "origin_note": r["origin_note"],
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
        existing = {}
        for chunk in _chunks(list(changes)):
            existing.update({
                r["upc"]: dict(r) for r in conn.execute(
                    text(
                        "SELECT upc, change_type, description, department, staged_by, staged_at, origin_note "
                        "FROM dbo.item_master_pending_changes WHERE upc IN (SELECT v FROM OPENJSON(:upcs) WITH (v VARCHAR(400) '$'))"
                    ).bindparams(bindparam("upcs", type_=JSON_LIST)),
                    {"upcs": chunk},
                ).mappings().all()
            })
        blocked = {upc: row for upc, row in existing.items() if row["staged_by"] and row["staged_by"] != actor}
        to_write = {upc: c for upc, c in changes.items() if upc not in blocked}
        if not to_write:
            return blocked
        replacing = [upc for upc in to_write if upc in existing]
        for chunk in _chunks(replacing):
            conn.execute(text("DELETE FROM dbo.item_master_pending_changes WHERE upc IN (SELECT v FROM OPENJSON(:upcs) WITH (v VARCHAR(400) '$'))")
                         .bindparams(bindparam("upcs", type_=JSON_LIST)), {"upcs": chunk})
        conn.execute(
            text(
                "INSERT INTO dbo.item_master_pending_changes "
                "(upc, change_type, description, department, category, subcategory, brand, pack, size, uom, source_key, is_saved, staged_by, origin_note) "
                "VALUES (:upc, :change_type, :description, :department, :category, :subcategory, :brand, :pack, :size, :uom, :source_key, 1, :staged_by, :origin_note)"
            ),
            [
                {
                    "upc": upc, "change_type": c["change_type"], "description": c.get("description"),
                    "department": c.get("department"), "category": c.get("category"),
                    "subcategory": c.get("subcategory"), "brand": c.get("brand"),
                    "pack": c.get("pack"), "size": c.get("size"), "uom": c.get("uom"),
                    "source_key": c.get("source_key"), "staged_by": actor,
                    # a note stays while the Department does
                    "origin_note": c.get("origin_note") or (
                        existing[upc].get("origin_note") if upc in existing and existing[upc].get("department") == c.get("department") else None),
                }
                for upc, c in to_write.items()
            ],
        )
        kinds = Counter(c["change_type"] for c in to_write.values())
        names = {"add": "item(s) to add", "delete": "item(s) to delete", "edit": "UPC override(s) / item edit(s)"}
        depts = Counter(c.get("department") for c in to_write.values() if c["change_type"] == "edit" and c.get("department"))
        log_activity(conn, actor, "Items", "Staged " + ", ".join(f"{n:,} {names.get(k, k)}" for k, n in kinds.items()),
                     ", ".join(list(to_write)[:5]) + (f" … (+{len(to_write) - 5:,} more)" if len(to_write) > 5 else ""),
                     len(to_write), details={"upcs": list(to_write)[:2000], "departments": dict(depts)})
    return blocked


def delete_item_master_pending(engine, upc: str) -> None:
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM dbo.item_master_pending_changes WHERE upc = :upc"), {"upc": upc})


def delete_item_master_pending_many(engine, upcs: list) -> None:
    with engine.begin() as conn:
        for chunk in _chunks(list(upcs)):
            conn.execute(text("DELETE FROM dbo.item_master_pending_changes WHERE upc IN (SELECT v FROM OPENJSON(:u) WITH (v VARCHAR(400) '$'))")
                         .bindparams(bindparam("u", type_=JSON_LIST)), {"u": chunk})


ITEM_EDIT_FIELDS = ["description", "department", "category", "subcategory", "brand", "pack", "size", "uom"]


def push_item_master_edits(engine, changes: dict, actor: str) -> None:
    """Many staged Edits (UPC overrides) in ONE transaction and a handful of
    batched statements — what push_item_master_edit does per item: set the
    live row, and pin only the fields that actually change in
    manual_overrides so they survive the next Merge."""
    if not changes:
        return
    blank = lambda v: v is None or (isinstance(v, float) and pd.isna(v)) or str(v).strip() == ""
    norm = lambda v: None if blank(v) else str(v).strip()
    cols = ", ".join(ITEM_EDIT_FIELDS)
    with engine.begin() as conn:
        live = {}
        for chunk in _chunks(list(changes)):
            for r in conn.execute(text(f"SELECT upc, {cols} FROM dbo.items WHERE upc IN (SELECT v FROM OPENJSON(:u) WITH (v VARCHAR(400) '$'))")
                                  .bindparams(bindparam("u", type_=JSON_LIST)), {"u": chunk}).mappings():
                live[r["upc"]] = dict(r)
        conn.execute(text(
            "UPDATE dbo.items SET description = :description, department = :department, category = :category, "
            "subcategory = :subcategory, brand = :brand, pack = :pack, size = :size, uom = :uom, "
            "source_key = :source_key, updated_at = SYSUTCDATETIME() WHERE upc = :upc"),
            [{"upc": u, **{f: c.get(f) for f in ITEM_EDIT_FIELDS}, "source_key": c.get("source_key")}
             for u, c in changes.items()])
        by_fields = {}
        for u, c in changes.items():
            changed = tuple(f for f in ITEM_EDIT_FIELDS if norm(c.get(f)) != norm(live.get(u, {}).get(f)))
            if changed:
                by_fields.setdefault(changed, []).append({"upc": u, "updated_by": actor, "note": c.get("origin_note"),
                                                          **{f: c.get(f) for f in changed}})
        for changed, rows in by_fields.items():
            # the note describes the Department — a new Department replaces it
            note_set = ", decided_note = :note" if "department" in changed else ""
            conn.execute(text(f"""
                MERGE dbo.manual_overrides AS target
                USING (SELECT :upc AS upc) AS src ON target.upc = src.upc
                WHEN MATCHED THEN UPDATE SET {", ".join(f"{f} = :{f}" for f in changed)},
                    updated_by = :updated_by, updated_at = SYSUTCDATETIME(){note_set}
                WHEN NOT MATCHED THEN INSERT (upc, {", ".join(changed)}, updated_by, decided_note)
                    VALUES (:upc, {", ".join(":" + f for f in changed)}, :updated_by, :note);"""), rows)
        for chunk in _chunks(list(changes)):
            conn.execute(text("DELETE FROM dbo.item_master_pending_changes WHERE upc IN (SELECT v FROM OPENJSON(:u) WITH (v VARCHAR(400) '$'))")
                         .bindparams(bindparam("u", type_=JSON_LIST)), {"u": chunk})
        stagers = Counter(c.get("staged_by") or "?" for c in changes.values())
        log_activity(conn, actor, "Pushed live", f"Pushed {len(changes):,} UPC override(s) / item edit(s)",
                     "staged by " + ", ".join(f"{k} ({n:,})" for k, n in stagers.items()),
                     len(changes), details={"upcs": list(changes)[:2000]})


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
        log_activity(conn, actor, "Sources", "Staged a new source" if change_type == "add" else "Staged a source edit",
                     source_key, details={c: config.get(c) for c in SOURCE_CONFIG_COLUMNS})


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

# Smaller tables a snapshot stores whole (every row, every column) in
# dept_mapping_snapshot_tables, so a restore brings back everything that was
# going on: staged work, votes, suggestions, undo history and requests,
# claims, notices, approvals, manual Item Master edits, deletions, and the
# merge log. The big tables (items, combos, combo membership, per-UPC
# decisions) keep their own typed _snapshot tables.
SNAPSHOT_FULL_TABLES = (
    "dept_mapping_pending_changes", "dept_mapping_combo_suggestions",
    "dept_mapping_pending_upc_changes", "dept_mapping_upc_change_suggestions",
    "dept_mapping_recent_moves", "dept_mapping_undo_requests", "dept_mapping_broken_out_claims",
    "change_discard_notices", "dept_push_approvals", "manual_overrides", "deleted_upcs",
    "item_master_pending_changes", "source_pending_changes", "merge_log",
    "dept_mapping_combo_agreements", "dept_mapping_upc_agreements",
    # Department Review Settings — small, and a restore should put them back too.
    "dept_mapping_departments", "dept_mapping_strict_departments", "dept_mapping_unmatched_defaults",
    "dept_mapping_config", "dept_mapping_keyword_rules",
    "merge_added_items", "dept_settings_requests",
    # a workbook upload's "which is right?" questions, alongside the work it staged
    "import_choices",
)
# Captured like the tables above, but restored by _restore_sources: raw data
# hangs off each source (too big to snapshot), so a source is updated in
# place rather than deleted and re-inserted.
SNAPSHOT_SOURCE_TABLE = "sources"


def _snapshot_full_tables(conn, snapshot_id: int) -> None:
    # FOR JSON on the server keeps full DATETIME2 precision — round-tripping
    # through Python datetimes silently drops the last digit.
    for t in SNAPSHOT_FULL_TABLES + (SNAPSHOT_SOURCE_TABLE,):
        conn.execute(
            text(
                "INSERT INTO dbo.dept_mapping_snapshot_tables (snapshot_id, table_name, rows_json) "
                f"SELECT :s, :t, ISNULL((SELECT * FROM dbo.{t} FOR JSON PATH, INCLUDE_NULL_VALUES), '[]')"
            ),
            {"s": snapshot_id, "t": t},
        )


def _openjson_columns(conn, table: str) -> list:
    """(name, sql type) for every column, for an OPENJSON ... WITH clause."""
    cols = []
    for r in conn.execute(
        text(
            "SELECT COLUMN_NAME, DATA_TYPE, CHARACTER_MAXIMUM_LENGTH, NUMERIC_PRECISION, NUMERIC_SCALE, DATETIME_PRECISION "
            "FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_SCHEMA = 'dbo' AND TABLE_NAME = :t ORDER BY ORDINAL_POSITION"
        ),
        {"t": table},
    ).mappings():
        dt = r["DATA_TYPE"]
        if dt in ("varchar", "nvarchar", "char", "nchar"):
            ln = "MAX" if r["CHARACTER_MAXIMUM_LENGTH"] == -1 else str(r["CHARACTER_MAXIMUM_LENGTH"])
            typ = f"{dt}({ln})"
        elif dt in ("decimal", "numeric"):
            typ = f"{dt}({r['NUMERIC_PRECISION']},{r['NUMERIC_SCALE']})"
        elif dt == "datetime2":
            typ = f"datetime2({r['DATETIME_PRECISION']})"
        else:
            typ = dt
        cols.append((r["COLUMN_NAME"], typ))
    return cols


def _restore_full_tables(conn, snapshot_id: int) -> bool:
    """Returns False for an older snapshot that predates full-table capture."""
    stored = conn.execute(
        text("SELECT table_name FROM dbo.dept_mapping_snapshot_tables WHERE snapshot_id = :s"), {"s": snapshot_id}
    ).scalars().all()
    if not stored:
        return False
    for t in SNAPSHOT_FULL_TABLES:
        conn.execute(text(f"DELETE FROM dbo.{t}"))
        if t not in stored:
            continue  # the snapshot is older than this table: it was empty then
        cols = _openjson_columns(conn, t)
        names = ", ".join(f"[{c}]" for c, _ in cols)
        with_clause = ", ".join(f"[{c}] {typ} '$.\"{c}\"'" for c, typ in cols)
        has_identity = conn.execute(
            text("SELECT COUNT(*) FROM sys.identity_columns WHERE object_id = OBJECT_ID(:t)"), {"t": f"dbo.{t}"}
        ).scalar()
        if has_identity:
            conn.execute(text(f"SET IDENTITY_INSERT dbo.{t} ON"))
        conn.execute(
            text(
                f"INSERT INTO dbo.{t} ({names}) SELECT {names} FROM OPENJSON("
                "(SELECT rows_json FROM dbo.dept_mapping_snapshot_tables WHERE snapshot_id = :s AND table_name = :t)"
                f") WITH ({with_clause})"
            ),
            {"s": snapshot_id, "t": t},
        )
        if has_identity:
            conn.execute(text(f"SET IDENTITY_INSERT dbo.{t} OFF"))
    if SNAPSHOT_SOURCE_TABLE in stored:
        _restore_sources(conn, snapshot_id)
    return True


def _restore_sources(conn, snapshot_id: int) -> None:
    """Puts every source's settings back as they were. A source added after
    the snapshot is removed along with its raw data and upload history; a
    source removed since can't come back here (its raw data is gone) —
    re-add it on the Sources tab. Raw data itself isn't part of a snapshot."""
    cols = _openjson_columns(conn, SNAPSHOT_SOURCE_TABLE)
    names = [c for c, _ in cols]
    with_clause = ", ".join(f"[{c}] {typ} '$.\"{c}\"'" for c, typ in cols)
    snap = (f"(SELECT * FROM OPENJSON((SELECT rows_json FROM dbo.dept_mapping_snapshot_tables "
            f"WHERE snapshot_id = {int(snapshot_id)} AND table_name = 'sources')) WITH ({with_clause}))")
    gone = f"SELECT source_key FROM dbo.sources WHERE source_key NOT IN (SELECT source_key FROM {snap} AS s)"
    conn.execute(text(f"DELETE FROM dbo.ingestion_rejected_rows WHERE log_id IN (SELECT id FROM dbo.ingestion_log WHERE source_key IN ({gone}))"))
    for t in ("ingestion_log", "source_raw_uploads", "raw_items"):
        conn.execute(text(f"DELETE FROM dbo.{t} WHERE source_key IN ({gone})"))
    conn.execute(text(f"DELETE FROM dbo.sources WHERE source_key IN ({gone})"))
    sets = ", ".join(f"t.[{c}] = s.[{c}]" for c in names if c != "source_key")
    conn.execute(text(f"UPDATE t SET {sets} FROM dbo.sources t JOIN {snap} AS s ON s.source_key = t.source_key"))


def take_snapshot(engine, actor: str, label: str = None, kind: str = "manual", restored_from: int = None) -> int:
    """kind: 'manual' (kept until someone deletes it), 'monthly', or an
    automatic safety copy ('safety_merge' / 'safety_restore') — see
    prune_snapshots for how long each is kept."""
    with engine.begin() as conn:
        item_count = conn.execute(text("SELECT COUNT(*) FROM dbo.items")).scalar()
        combo_count = conn.execute(text("SELECT COUNT(*) FROM dbo.dept_mapping_combos")).scalar()
        snapshot_id = conn.execute(
            text(
                "INSERT INTO dbo.dept_mapping_snapshots "
                "(snapshot_month, taken_at, item_count, combo_count, label, taken_by, kind, restored_from) "
                "OUTPUT inserted.snapshot_id "
                "VALUES (FORMAT(SYSUTCDATETIME(), 'yyyy-MM'), SYSUTCDATETIME(), :item_count, :combo_count, :label, :taken_by, "
                ":kind, :restored_from)"
            ),
            {"item_count": item_count, "combo_count": combo_count, "label": label, "taken_by": actor,
             "kind": kind, "restored_from": restored_from},
        ).scalar()

        conn.execute(
            text(
                "INSERT INTO dbo.items_snapshot "
                "(snapshot_id, upc, description, department, category, subcategory, brand, source_key, pack, size, uom, "
                "created_at, updated_at) "
                "SELECT :snapshot_id, upc, description, department, category, subcategory, brand, source_key, pack, size, uom, "
                "created_at, updated_at "
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
                "runner_up_department, runner_up_share, n_upcs_total, pushed_by, pushed_at, decided_note) "
                "SELECT :snapshot_id, combo_id, source_key, raw_department, raw_category, raw_subcategory, "
                "tier, purity, n_evidence, resolved_via, decision_state, decided_department, decided_via, "
                "suggested_department, majority_department, chain_round, is_strict, manual_department, approved, rejected, "
                "is_new_this_run, is_stale, first_seen_at, last_computed_at, last_decided_at, last_decided_by, "
                "runner_up_department, runner_up_share, n_upcs_total, pushed_by, pushed_at, decided_note "
                "FROM dbo.dept_mapping_combos"
            ),
            {"snapshot_id": snapshot_id},
        )
        conn.execute(
            text(
                "INSERT INTO dbo.dept_mapping_upc_overrides_snapshot "
                "(snapshot_id, upc, combo_id, department, decided_via, suggested_department, suggested_via, "
                "updated_by, updated_at, pushed_by, pushed_at, decided_note) "
                "SELECT :snapshot_id, upc, combo_id, department, decided_via, suggested_department, suggested_via, "
                "updated_by, updated_at, pushed_by, pushed_at, decided_note "
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
        _snapshot_full_tables(conn, snapshot_id)
        details = _snapshot_details(conn, snapshot_id)
        details["live"] = _live_extra(conn)
        conn.execute(text("UPDATE dbo.dept_mapping_snapshots SET details_json = :d WHERE snapshot_id = :s"),
                     {"d": json.dumps(details, default=str), "s": snapshot_id})
    if kind == "manual":
        log_activity(engine, actor, "Snapshots", f"Took snapshot #{snapshot_id}", label)
    else:
        prune_snapshots(engine)
    return snapshot_id


def list_snapshots(engine) -> pd.DataFrame:
    with engine.connect() as conn:
        return pd.read_sql(
            text(
                "SELECT snapshot_id, snapshot_month, taken_at, taken_by, label, item_count, combo_count, "
                "kind, restored_from, details_json "
                "FROM dbo.dept_mapping_snapshots ORDER BY taken_at DESC"
            ),
            conn,
        )


# How long each kind of snapshot is kept (see prune_snapshots).
KEEP_SAFETY_SNAPSHOTS = 15
KEEP_MONTHLY_SNAPSHOTS = 12
SNAPSHOT_KINDS = {
    "manual": "Manual",
    "monthly": "Monthly",
    "safety_merge": "Automatic — before a Merge push",
    "safety_restore": "Automatic — before a restore",
    "safety_import": "Automatic — before a workbook upload",
}


def prune_snapshots(engine) -> list:
    """Manual snapshots are kept until someone deletes them. Monthly ones:
    the newest KEEP_MONTHLY_SNAPSHOTS months. Automatic safety copies: the
    newest KEEP_SAFETY_SNAPSHOTS — except the copy taken before the latest
    restore, which is what "Undo this restore" needs. Returns deleted ids."""
    with engine.connect() as conn:
        rows = conn.execute(text(
            "SELECT snapshot_id, kind FROM dbo.dept_mapping_snapshots ORDER BY snapshot_id DESC")).all()
    safety = [r[0] for r in rows if r[1] in ("safety_merge", "safety_restore", "safety_import")]
    last_restore = next((r[0] for r in rows if r[1] == "safety_restore"), None)
    monthly = [r[0] for r in rows if r[1] == "monthly"]
    gone = [i for i in safety[KEEP_SAFETY_SNAPSHOTS:] if i != last_restore] + monthly[KEEP_MONTHLY_SNAPSHOTS:]
    for sid in gone:
        delete_snapshot(engine, sid)
    return gone


def take_monthly_snapshot(engine, actor: str) -> int:
    """This month's snapshot, refreshed after every Merge push so it always
    holds the month's latest state; the last 12 months are kept."""
    with engine.connect() as conn:
        old = conn.execute(text(
            "SELECT snapshot_id FROM dbo.dept_mapping_snapshots WHERE kind = 'monthly' "
            "AND snapshot_month = FORMAT(SYSUTCDATETIME(), 'yyyy-MM')")).scalars().all()
    label = f"Monthly — {datetime.now(timezone.utc):%B %Y} (latest, after {actor}'s Merge push)"
    sid = take_snapshot(engine, actor, label=label, kind="monthly")
    for o in old:
        delete_snapshot(engine, o)
    return sid


def get_last_restore(engine) -> dict | None:
    """The most recent restore: which snapshot was restored, when, by whom,
    and the safety copy taken just before it (restoring that copy undoes it)."""
    with engine.connect() as conn:
        r = conn.execute(text(
            "SELECT TOP 1 s.snapshot_id AS safety_id, s.restored_from, s.taken_at, s.taken_by, "
            "r.label AS restored_label, r.kind AS restored_kind, "
            "(SELECT COUNT(*) FROM dbo.merge_log m WHERE m.merged_at > s.taken_at) AS merges_since "
            "FROM dbo.dept_mapping_snapshots s LEFT JOIN dbo.dept_mapping_snapshots r ON r.snapshot_id = s.restored_from "
            "WHERE s.kind = 'safety_restore' ORDER BY s.snapshot_id DESC")).mappings().first()
    return dict(r) if r else None


def _json_count(conn, snapshot_id: int, table: str) -> int | None:
    return conn.execute(text(
        "SELECT (SELECT COUNT(*) FROM OPENJSON(rows_json)) FROM dbo.dept_mapping_snapshot_tables "
        "WHERE snapshot_id = :s AND table_name = :t"), {"s": snapshot_id, "t": table}).scalar()


def _json_rows(conn, snapshot_id: int, table: str) -> list | None:
    raw = conn.execute(text(
        "SELECT rows_json FROM dbo.dept_mapping_snapshot_tables WHERE snapshot_id = :s AND table_name = :t"),
        {"s": snapshot_id, "t": table}).scalar()
    return None if raw is None else json.loads(raw)


def _snapshot_details(conn, snapshot_id: int) -> dict:
    """What a snapshot holds, read from the snapshot itself (works for old
    snapshots too): items per source, Department Review groups per state,
    staged work, manual edits, Settings, sources."""
    p = {"s": snapshot_id}
    by_source = dict(conn.execute(text(
        "SELECT ISNULL(source_key, '(none)'), COUNT(*) FROM dbo.items_snapshot WHERE snapshot_id = :s GROUP BY source_key"), p).all())
    no_dept = conn.execute(text(
        "SELECT COUNT(*) FROM dbo.items_snapshot WHERE snapshot_id = :s AND ISNULL(department, '') = ''"), p).scalar()
    groups = dict(conn.execute(text(
        """
        SELECT CASE
            WHEN decision_state = 'broken_out' THEN 'broken_out'
            WHEN decision_state = 'decided_broken_out' THEN 'decided_by_item'
            WHEN decision_state = 'decided' OR manual_department IS NOT NULL THEN 'decided_by_person'
            WHEN decided_department IS NOT NULL THEN 'auto'
            WHEN tier = 'unmatched' THEN 'unmatched'
            ELSE 'crosswalk' END, COUNT(*)
        FROM dbo.dept_mapping_combos_snapshot WHERE snapshot_id = :s GROUP BY CASE
            WHEN decision_state = 'broken_out' THEN 'broken_out'
            WHEN decision_state = 'decided_broken_out' THEN 'decided_by_item'
            WHEN decision_state = 'decided' OR manual_department IS NOT NULL THEN 'decided_by_person'
            WHEN decided_department IS NOT NULL THEN 'auto'
            WHEN tier = 'unmatched' THEN 'unmatched'
            ELSE 'crosswalk' END
        """), p).all())
    d = {
        "items": {"total": sum(by_source.values()), "by_source": by_source, "no_department": no_dept},
        "groups": groups,
        "staged": {
            "group_decisions": _json_count(conn, snapshot_id, "dept_mapping_pending_changes"),
            "item_decisions": _json_count(conn, snapshot_id, "dept_mapping_pending_upc_changes"),
            "votes": _json_count(conn, snapshot_id, "dept_mapping_combo_suggestions"),
            "item_suggestions": _json_count(conn, snapshot_id, "dept_mapping_upc_change_suggestions"),
            "item_master_changes": _json_count(conn, snapshot_id, "item_master_pending_changes"),
            "source_changes": _json_count(conn, snapshot_id, "source_pending_changes"),
        },
        "manual": {
            "overrides": _json_count(conn, snapshot_id, "manual_overrides"),
            "deleted": _json_count(conn, snapshot_id, "deleted_upcs"),
        },
        "settings": {
            "departments": _json_count(conn, snapshot_id, "dept_mapping_departments"),
            "strict": _json_count(conn, snapshot_id, "dept_mapping_strict_departments"),
            "unmatched_defaults": _json_count(conn, snapshot_id, "dept_mapping_unmatched_defaults"),
        },
    }
    srcs = _json_rows(conn, snapshot_id, "sources")
    if srcs is not None:
        d["sources"] = [
            {"key": r["source_key"], "label": r.get("source_label"), "enabled": bool(r.get("enabled")),
             "priority": r.get("priority_rank")}
            for r in sorted(srcs, key=lambda r: r.get("priority_rank") or 0)
        ]
    staged_by = set()
    for t in ("dept_mapping_pending_changes", "dept_mapping_pending_upc_changes", "item_master_pending_changes"):
        for r in _json_rows(conn, snapshot_id, t) or []:
            if r.get("staged_by"):
                staged_by.add(r["staged_by"])
    d["staged"]["by"] = sorted(staged_by)
    return d


def _live_extra(conn) -> dict:
    """Things only known at the moment a snapshot is taken: the latest file
    ingested for each source, and the last Merge."""
    files = conn.execute(text(
        """
        SELECT l.source_key, l.original_filename, l.uploaded_at, l.rows_staged FROM dbo.ingestion_log l
        WHERE l.id = (SELECT MAX(id) FROM dbo.ingestion_log x WHERE x.source_key = l.source_key)
        """)).mappings().all()
    merge = conn.execute(text("SELECT TOP 1 merged_at, merged_by FROM dbo.merge_log ORDER BY id DESC")).mappings().first()
    return {
        "files": {r["source_key"]: {"file": r["original_filename"], "uploaded_at": str(r["uploaded_at"])[:16],
                                    "rows": r["rows_staged"]} for r in files},
        "last_merge": {"at": str(merge["merged_at"])[:16], "by": merge["merged_by"]} if merge else None,
    }


def get_snapshot_details(engine, snapshot_id: int) -> dict:
    """Stored details, worked out (and saved) the first time for a snapshot
    taken before details were recorded."""
    with engine.begin() as conn:
        raw = conn.execute(text("SELECT details_json FROM dbo.dept_mapping_snapshots WHERE snapshot_id = :s"),
                           {"s": snapshot_id}).scalar()
        if raw:
            return json.loads(raw)
        d = _snapshot_details(conn, snapshot_id)
        conn.execute(text("UPDATE dbo.dept_mapping_snapshots SET details_json = :d WHERE snapshot_id = :s"),
                     {"d": json.dumps(d, default=str), "s": snapshot_id})
        return d


def compare_snapshot_to_live(engine, snapshot_id: int) -> dict:
    """How the live data differs from a snapshot right now — what restoring
    it would change. Items added / removed / changed (and which fields),
    and Department Review groups whose state or Department differs."""
    fields = ["description", "department", "category", "subcategory", "brand", "pack", "size", "uom", "source_key"]
    p = {"s": snapshot_id}
    with engine.connect() as conn:
        added = conn.execute(text(
            "SELECT COUNT(*) FROM dbo.items i WHERE NOT EXISTS (SELECT 1 FROM dbo.items_snapshot s "
            "WHERE s.snapshot_id = :s AND s.upc = i.upc)"), p).scalar()
        removed = conn.execute(text(
            "SELECT COUNT(*) FROM dbo.items_snapshot s WHERE s.snapshot_id = :s AND NOT EXISTS "
            "(SELECT 1 FROM dbo.items i WHERE i.upc = s.upc)"), p).scalar()
        diff_cols = ", ".join(
            f"SUM(CASE WHEN ISNULL(i.{f}, '') <> ISNULL(s.{f}, '') THEN 1 ELSE 0 END) AS {f}" for f in fields)
        any_diff = " OR ".join(f"ISNULL(i.{f}, '') <> ISNULL(s.{f}, '')" for f in fields)
        row = conn.execute(text(
            f"SELECT {diff_cols}, SUM(CASE WHEN {any_diff} THEN 1 ELSE 0 END) AS any_change "
            "FROM dbo.items i JOIN dbo.items_snapshot s ON s.upc = i.upc AND s.snapshot_id = :s"), p).mappings().one()
        groups = conn.execute(text(
            "SELECT COUNT(*) FROM dbo.dept_mapping_combos c FULL JOIN (SELECT * FROM dbo.dept_mapping_combos_snapshot "
            "WHERE snapshot_id = :s) s ON s.combo_id = c.combo_id WHERE c.combo_id IS NULL OR s.combo_id IS NULL "
            "OR c.decision_state <> s.decision_state OR ISNULL(c.decided_department, '') <> ISNULL(s.decided_department, '')"),
            p).scalar()
    return {"added": added, "removed": removed, "changed": row["any_change"] or 0,
            "changed_by_field": {f: row[f] for f in fields if row[f]}, "groups_different": groups}


def has_snapshot_this_month(engine) -> bool:
    with engine.connect() as conn:
        count = conn.execute(
            text("SELECT COUNT(*) FROM dbo.dept_mapping_snapshots WHERE snapshot_month = FORMAT(SYSUTCDATETIME(), 'yyyy-MM')")
        ).scalar()
    return count > 0


def restore_snapshot(engine, snapshot_id: int, actor: str) -> int:
    """Always takes its own safety snapshot of whatever's live right now,
    labeled to say why, before overwriting anything — restoring a snapshot
    must never be the one action you can't come back from. Refuses a
    snapshot that doesn't exist or holds no items (it would empty the item
    master)."""
    with engine.connect() as conn:
        exists = conn.execute(text("SELECT item_count FROM dbo.dept_mapping_snapshots WHERE snapshot_id = :s"),
                              {"s": snapshot_id}).first()
        held = conn.execute(text("SELECT COUNT(*) FROM dbo.items_snapshot WHERE snapshot_id = :s"), {"s": snapshot_id}).scalar()
    if exists is None:
        raise ValueError(f"Snapshot #{snapshot_id} doesn't exist.")
    if not held:
        raise ValueError(f"Snapshot #{snapshot_id} holds no items — restoring it would empty the item master.")
    safety_id = take_snapshot(engine, actor, label=f"Auto-safety before restoring snapshot #{snapshot_id}",
                              kind="safety_restore", restored_from=snapshot_id)
    log_activity(engine, actor, "Snapshots", f"Restored snapshot #{snapshot_id} (today's data saved first as #{safety_id})")
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM dbo.items"))
        conn.execute(
            text(
                "INSERT INTO dbo.items (upc, description, department, category, subcategory, brand, source_key, pack, size, uom, "
                "created_at, updated_at) "
                "SELECT upc, description, department, category, subcategory, brand, source_key, pack, size, uom, "
                "ISNULL(created_at, SYSUTCDATETIME()), ISNULL(updated_at, SYSUTCDATETIME()) "
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
                "runner_up_department, runner_up_share, n_upcs_total, pushed_by, pushed_at, decided_note) "
                "SELECT combo_id, source_key, raw_department, raw_category, raw_subcategory, "
                "tier, purity, n_evidence, resolved_via, decision_state, decided_department, decided_via, "
                "suggested_department, majority_department, chain_round, is_strict, manual_department, approved, rejected, "
                "is_new_this_run, is_stale, first_seen_at, last_computed_at, last_decided_at, last_decided_by, "
                "runner_up_department, runner_up_share, n_upcs_total, pushed_by, pushed_at, decided_note "
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
                "(upc, combo_id, department, decided_via, suggested_department, suggested_via, updated_by, updated_at, "
                "pushed_by, pushed_at, decided_note) "
                "SELECT upc, combo_id, department, decided_via, suggested_department, suggested_via, updated_by, updated_at, "
                "pushed_by, pushed_at, decided_note "
                "FROM dbo.dept_mapping_upc_overrides_snapshot WHERE snapshot_id = :snapshot_id"
            ),
            {"snapshot_id": snapshot_id},
        )

        # A Merge draft computed after the snapshot doesn't match the
        # restored data — drop it so it can't be pushed by mistake.
        conn.execute(text("DELETE FROM dbo.items_staged"))
        conn.execute(text("DELETE FROM dbo.merge_compute_meta"))
        conn.execute(text("DELETE FROM dbo.dept_mapping_action_log"))
        if _restore_full_tables(conn, snapshot_id):
            return safety_id
        # Older snapshot (before full-table capture): restore the staged
        # session from its per-table copies, as it always did.
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
        conn.execute(text("DELETE FROM dbo.dept_mapping_snapshot_tables WHERE snapshot_id = :sid"), {"sid": snapshot_id})
        conn.execute(text("DELETE FROM dbo.dept_mapping_snapshots WHERE snapshot_id = :sid"), {"sid": snapshot_id})
    _renumber_next_snapshot(engine)


def _renumber_next_snapshot(engine) -> None:
    """Keeps snapshot numbers small: the next snapshot gets the number right
    after the highest one still kept (#0 when none are left), instead of
    counting on from every snapshot ever taken."""
    try:
        with engine.begin() as conn:
            top = conn.execute(text("SELECT MAX(snapshot_id) FROM dbo.dept_mapping_snapshots")).scalar()
            conn.execute(text(f"DBCC CHECKIDENT ('dbo.dept_mapping_snapshots', RESEED, {-1 if top is None else int(top)})"))
    except Exception:
        pass  # numbering is cosmetic; never fail a delete over it


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


def _department_targets(engine, table: str) -> pd.DataFrame:
    """Every row of `table` (dbo.items or the dbo.items_staged draft) with
    the Department a Merge would give it right now: a manual override's
    Department first; then, for an item in a Department Review group, that
    group's decision (blank if undecided). A live item in no group keeps
    its Department; a new (draft) item takes Scan Advantage's own when
    that's its winning source."""
    with engine.connect() as conn:
        rows = pd.read_sql(text(f"SELECT upc, source_key, department FROM dbo.{table}"), conn)
        nwg = pd.read_sql(text(
            "SELECT upc, department FROM dbo.raw_items WHERE source_key = 'nwg' AND ISNULL(department, '') <> ''"), conn)
        mo = pd.read_sql(text(
            "SELECT upc, department FROM dbo.manual_overrides WHERE ISNULL(department, '') <> ''"), conn)
    decided = get_upc_department_overrides(engine)
    with engine.connect() as conn:
        reviewed = set(conn.execute(text("SELECT upc FROM dbo.dept_mapping_combo_upcs")).scalars().all())
        reviewed |= set(conn.execute(text("SELECT upc FROM dbo.dept_mapping_upc_overrides")).scalars().all())
    target = rows["upc"].map(decided)
    is_nwg = rows["source_key"] == "nwg"
    nwg_dept = rows["upc"].map(dict(zip(nwg["upc"], nwg["department"])))
    if table == "items":
        # Live items: a Department Review group decides the Department of
        # the items in it; anything else (Scan Advantage's own items, items
        # no file lists any more) keeps the Department it already has —
        # source files never change an existing item.
        in_review = rows["upc"].isin(reviewed)
        target = target.where(in_review, rows["department"])
    else:
        target = nwg_dept.where(is_nwg & nwg_dept.notna(), target)
    mo_dept = rows["upc"].map(dict(zip(mo["upc"], mo["department"])))
    target = mo_dept.where(mo_dept.notna(), target)
    rows["target"] = target.where(target.notna(), None)
    return rows


def combo_decision_map(engine) -> dict:
    """combo_id -> (tier, decision_state, decided_department), for a quick
    "what did that change" count around an engine run."""
    with engine.connect() as conn:
        return {r[0]: tuple(r[1:]) for r in conn.execute(text(
            "SELECT combo_id, tier, decision_state, decided_department FROM dbo.dept_mapping_combos"))}


def sync_item_departments(engine) -> dict:
    """Brings Department in the live item master (and in any computed Merge
    draft) in line with Department Review's current decisions and manual
    overrides — so a pushed decision shows in Item Master right away, not
    only after the next full Merge. Touches only rows whose Department
    actually changes. Returns {"items": n_changed, "draft": n_changed}."""
    changed = {}
    for table, key in (("items", "items"), ("items_staged", "draft")):
        df = _department_targets(engine, table)
        blank = lambda v: v is None or (isinstance(v, float) and pd.isna(v)) or v == ""
        diff = df[[(None if blank(a) else a) != (None if blank(b) else b) for a, b in zip(df["department"], df["target"])]]
        changed[key] = len(diff)
        if diff.empty:
            continue
        with engine.begin() as conn:
            conn.execute(text("CREATE TABLE #dept_sync (upc VARCHAR(12) PRIMARY KEY, department NVARCHAR(200) NULL)"))
            conn.execute(
                text("INSERT INTO #dept_sync (upc, department) VALUES (:upc, :department)"),
                [{"upc": u, "department": None if blank(d) else d} for u, d in zip(diff["upc"], diff["target"])],
            )
            touch = ", updated_at = SYSUTCDATETIME()" if table == "items" else ""
            conn.execute(text(
                f"UPDATE t SET department = s.department{touch} FROM dbo.{table} t JOIN #dept_sync s ON s.upc = t.upc"))
            conn.execute(text("DROP TABLE #dept_sync"))
    return changed


def save_merge_compute(engine, final_df, actor: str, overrides_applied: int, deleted_excluded: int) -> dict:
    """New items only. A Merge adds UPCs the item master doesn't have yet
    (cleaned by every rule and decision) and never changes or removes an
    existing item from source files — those are only used to confirm an
    item isn't new. The draft (dbo.items_staged) therefore holds just the
    new rows. For information, the meta also counts existing items whose
    source data now differs (ignored — changed_count, broken down by field
    and source) and items no longer in any file (kept — removed_count).

    Original notes: compares the computed final_df against whatever's currently live in
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
                if field == "department":
                    continue  # set by the rules/decisions for every item, not taken from files
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

    new_df = final_df[~final_df["upc"].isin(live_upcs)] if not final_df.empty else final_df
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM dbo.items_staged"))
        if not new_df.empty:
            new_df.to_sql("items_staged", conn, schema="dbo", if_exists="append", index=False, chunksize=5000)
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
                "actor": actor, "item_count": len(live_upcs) + added_count,
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


def run_engine_guarded(engine, actor: str) -> tuple:
    """Re-runs the Department engine (after a Merge push, or a Settings
    change) and discards any staged decision whose group's evidence changed
    underneath it — nobody reviewed that new evidence. Returns
    (engine_summary, discarded_combo_ids)."""
    discarded = []
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

    engine_summary = run_engine(engine)

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
                "Re-running the Department engine changed that group's evidence, so the staged decision "
                "was discarded rather than pushed against evidence nobody actually reviewed.",
                actor,
                group_label=notice_label,
            )
        discarded = sorted(changed_ids)
    return engine_summary, discarded


def _reapply_manual_work_to_draft(conn) -> None:
    """A draft can be computed, then sit while someone pushes Item Master
    edits, adds or deletes. Re-apply manual_overrides / deleted_upcs to it
    right before it goes live, exactly as compute_merge_final_df would, so
    that work isn't lost until the next Merge."""
    f = ["description", "department", "category", "subcategory", "brand", "pack", "size", "uom"]
    conn.execute(text(
        "UPDATE s SET " + ", ".join(f"{c} = COALESCE(NULLIF(o.{c}, ''), s.{c})" for c in f)
        + " FROM dbo.items_staged s JOIN dbo.manual_overrides o ON o.upc = s.upc"))
    conn.execute(text(
        f"INSERT INTO dbo.items_staged (upc, {', '.join(f)}, source_key) "
        "SELECT o.upc, COALESCE(NULLIF(o.description, ''), o.upc), "
        + ", ".join(f"NULLIF(o.{c}, '')" for c in f[1:])
        + ", 'manual' FROM dbo.manual_overrides o WHERE NOT EXISTS (SELECT 1 FROM dbo.items_staged s WHERE s.upc = o.upc)"))
    conn.execute(text("DELETE s FROM dbo.items_staged s JOIN dbo.deleted_upcs d ON d.upc = s.upc"))


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
    safety_id = take_snapshot(engine, actor, label="Auto-safety before Merge push", kind="safety_merge")

    item_master_fields = ["description", "department", "category", "subcategory", "brand", "source_key", "pack", "size", "uom"]
    item_cols_sql = ", ".join(item_master_fields)
    item_select_stmt = text(f"SELECT upc, {item_cols_sql} FROM dbo.items WHERE upc IN (SELECT v FROM OPENJSON(:upcs) WITH (v VARCHAR(400) '$'))").bindparams(
        bindparam("upcs", type_=JSON_LIST)
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

    _progress("Adding the new items...", 0.25)
    with engine.begin() as conn:
        _reapply_manual_work_to_draft(conn)
        # New items only: an existing item is never rewritten or removed by
        # source files, even if a file stops listing it.
        added_upcs = conn.execute(text(
            "SELECT s.upc FROM dbo.items_staged s WHERE NOT EXISTS (SELECT 1 FROM dbo.items i WHERE i.upc = s.upc)")).scalars().all()
        conn.execute(
            text(
                "INSERT INTO dbo.items (upc, description, department, category, subcategory, brand, source_key, pack, size, uom) "
                "SELECT s.upc, s.description, s.department, s.category, s.subcategory, s.brand, s.source_key, s.pack, s.size, s.uom "
                "FROM dbo.items_staged s WHERE NOT EXISTS (SELECT 1 FROM dbo.items i WHERE i.upc = s.upc)"
            )
        )
        item_count = conn.execute(text("SELECT COUNT(*) FROM dbo.items")).scalar()

    record_merge(
        engine, actor, item_count,
        added_count=compute_meta.get("added_count"), changed_count=compute_meta.get("changed_count"),
        removed_count=compute_meta.get("removed_count"), overrides_applied=compute_meta.get("overrides_applied"),
        deleted_excluded=compute_meta.get("deleted_excluded"),
        changed_by_field=compute_meta.get("changed_by_field"), changed_by_source=compute_meta.get("changed_by_source"),
    )
    # Which items this push added — the Merge tab's "Items added" report.
    with engine.begin() as conn:
        merge_id = conn.execute(text("SELECT MAX(id) FROM dbo.merge_log")).scalar()
        if added_upcs:
            conn.execute(text("INSERT INTO dbo.merge_added_items (merge_id, upc) VALUES (:m, :u)"),
                         [{"m": merge_id, "u": u} for u in added_upcs])
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
        _progress("Recomputing Department Review groups — this is the slow part...", 0.4)
        engine_summary, discarded_pending_combo_ids = run_engine_guarded(engine, actor)
        _progress("Finishing up...", 0.9)
        # The engine can re-decide groups from the fresh data; carry that
        # into the item master now rather than a Merge later.
        sync_item_departments(engine)
    except Exception as e:
        engine_error = str(e)

    monthly_id = None
    try:
        _progress("Saving this month's snapshot...", 0.95)
        monthly_id = take_monthly_snapshot(engine, actor)
    except Exception as e:
        engine_error = (engine_error + "; " if engine_error else "") + f"monthly snapshot failed: {e}"

    return {
        "monthly_snapshot_id": monthly_id,
        "aborted_stale": False,
        "safety_snapshot_id": safety_id, "item_count": item_count,
        "engine_summary": engine_summary, "engine_error": engine_error,
        "discarded_pending_combo_ids": discarded_pending_combo_ids,
        "discarded_item_master_upcs": discarded_item_master_upcs,
    }


# ---------------------------------------------------------------------------
# Notifications ("since your last visit")
# ---------------------------------------------------------------------------
def cleanup_old_undo(engine, username: str | None = None) -> dict:
    """Undo history is for the work in front of you, not an archive: a
    person's top-bar Undo steps and their saved-but-unstaged grid work are
    dropped after UNDO_KEEP_HOURS without activity (and on logout — see
    clear_user_undo). Nothing staged or pushed is touched."""
    p = {"h": -UNDO_KEEP_HOURS}
    who = ""
    if username:
        p["u"] = username
        who = " AND actor = :u"
    with engine.begin() as conn:
        return {
            "action_log": conn.execute(text(
                "DELETE FROM dbo.dept_mapping_action_log WHERE COALESCE(changed_at, created_at) "
                f"< DATEADD(hour, :h, SYSUTCDATETIME()){who}"), p).rowcount,
            "workspace": conn.execute(text(
                "DELETE FROM dbo.user_workspace WHERE updated_at < DATEADD(hour, :h, SYSUTCDATETIME())"
                + (" AND username = :u" if username else "")), p).rowcount,
        }


def clear_user_undo(engine, username: str) -> None:
    """Logging out ends the session's undo history and unstaged grid work."""
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM dbo.dept_mapping_action_log WHERE actor = :u"), {"u": username})
        conn.execute(text("DELETE FROM dbo.user_workspace WHERE username = :u"), {"u": username})


def start_visit(engine, username: str) -> datetime:
    """Records this visit and returns when the user was last here (their
    "since" point). A first-ever visit looks back 7 days."""
    try:
        cleanup_old_undo(engine)
    except Exception:
        pass  # housekeeping only — never block a sign-in over it
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    with engine.begin() as conn:
        prev = conn.execute(
            text("SELECT last_seen_at FROM dbo.user_last_seen WHERE username = :u"), {"u": username}
        ).scalar()
        conn.execute(
            text(
                "MERGE dbo.user_last_seen AS t USING (SELECT :u AS username) AS s ON t.username = s.username "
                "WHEN MATCHED THEN UPDATE SET last_seen_at = :now "
                "WHEN NOT MATCHED THEN INSERT (username, last_seen_at) VALUES (:u, :now);"
            ),
            {"u": username, "now": now},
        )
    return prev or (now - timedelta(days=7))


NOTIFICATION_KINDS = {
    "suggestion": "Suggestions on your items", "undo": "Undo requests", "claim": "Groups you're working on",
    "discarded": "Discarded work", "pushed": "Pushed live", "moved": "Groups moved", "dispute": "Needs agreement",
    "request": "Settings requests",
}


def get_last_seen(engine, username: str) -> datetime:
    """When `username` last opened the app — read only (doesn't count as a visit)."""
    with engine.connect() as conn:
        prev = conn.execute(
            text("SELECT last_seen_at FROM dbo.user_last_seen WHERE username = :u"), {"u": username}
        ).scalar()
    return prev or (datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=7))


def mark_seen(engine, username: str) -> datetime:
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    with engine.begin() as conn:
        conn.execute(text("UPDATE dbo.user_last_seen SET last_seen_at = :now WHERE username = :u"), {"u": username, "now": now})
    return now


def _combo_labels(conn, combo_ids) -> dict:
    ids = [int(i) for i in set(combo_ids) if i is not None]
    out = {}
    for chunk in _chunks(ids):
        for r in conn.execute(
            text(
                "SELECT combo_id, source_key, raw_department, raw_category, raw_subcategory "
                "FROM dbo.dept_mapping_combos WHERE combo_id IN :ids"
            ).bindparams(bindparam("ids", expanding=True)),
            {"ids": chunk},
        ).mappings():
            bits = [b for b in (r["raw_department"], r["raw_category"], r["raw_subcategory"]) if b]
            out[r["combo_id"]] = (f"{(r['source_key'] or '').upper()} — {' / '.join(bits)}", " / ".join(bits))
    return out


def get_notifications(engine, username: str, since: datetime, is_admin: bool = False) -> dict:
    """Everything worth telling `username` about, in two groups:

    "action" — waiting on YOU right now (shown until handled, flagged new if
    it arrived since your last visit): suggestions on items you own, and
    undo requests only you can act on.

    "updates" — what happened since your last visit: your staged work that
    was discarded, your decisions someone else pushed, your groups someone
    else moved, and new disputes (Needs agreement) — the ones you're part of
    first.

    Each entry: {"combo_id", "title", "search", "detail", "when", "new"}.
    """
    action, updates = [], []
    with engine.connect() as conn:
        # --- suggestions on items you own -------------------------------
        sugg = conn.execute(text(
            """
            SELECT s.combo_id, s.suggested_by, COUNT(*) AS n, MAX(s.suggested_at) AS latest
            FROM dbo.dept_mapping_upc_change_suggestions s
            JOIN dbo.dept_mapping_pending_upc_changes p ON p.upc = s.upc
            WHERE p.staged_by = :u AND s.suggested_by <> :u
            GROUP BY s.combo_id, s.suggested_by
            """
        ), {"u": username}).mappings().all()

        # --- undo requests waiting on you --------------------------------
        combo_undo = conn.execute(text(
            """
            SELECT pc.combo_id, r.requested_by, r.requested_at FROM dbo.dept_mapping_undo_requests r
            JOIN dbo.dept_mapping_pending_changes pc ON CAST(pc.combo_id AS NVARCHAR(50)) = r.entity_id
            WHERE r.entity_type = 'combo' AND pc.staged_by = :u AND r.requested_by <> :u
            """
        ), {"u": username}).mappings().all()
        group_undo = conn.execute(text(
            "SELECT entity_id, requested_by, requested_at FROM dbo.dept_mapping_undo_requests "
            "WHERE entity_type = 'upc_group' AND requested_by <> :u"
        ), {"u": username}).mappings().all()

        # --- Broken Out groups you currently hold a claim on -------------
        claims = conn.execute(text(
            "SELECT combo_id, claimed_at, DATEDIFF(MINUTE, last_activity_at, SYSUTCDATETIME()) AS idle "
            "FROM dbo.dept_mapping_broken_out_claims WHERE claimed_by = :u "
            "AND DATEDIFF(MINUTE, last_activity_at, SYSUTCDATETIME()) < :idle_limit"
        ), {"u": username, "idle_limit": CLAIM_IDLE_MINUTES}).mappings().all()

        # --- your staged work that was discarded -------------------------
        discarded = conn.execute(text(
            """
            SELECT COALESCE(group_label, entity_label) AS grp, triggered_by, reason, COUNT(*) AS n, MAX(triggered_at) AS latest
            FROM dbo.change_discard_notices
            WHERE originally_staged_by = :u AND dismissed = 0 AND triggered_at > :since
            GROUP BY COALESCE(group_label, entity_label), triggered_by, reason
            """
        ), {"u": username, "since": since}).mappings().all()

        # --- your decisions pushed by someone else ------------------------
        pushed_whole = conn.execute(text(
            """
            SELECT combo_id, decided_department, pushed_by, pushed_at FROM dbo.dept_mapping_combos
            WHERE last_decided_by = :u AND pushed_by IS NOT NULL AND pushed_by <> :u AND pushed_at > :since
              AND decided_department IS NOT NULL AND decision_state IN ('not_reviewed', 'decided')
            """
        ), {"u": username, "since": since}).mappings().all()
        pushed_items = conn.execute(text(
            """
            SELECT combo_id, pushed_by, COUNT(*) AS n, MAX(pushed_at) AS latest FROM dbo.dept_mapping_upc_overrides
            WHERE updated_by = :u AND pushed_by IS NOT NULL AND pushed_by <> :u AND pushed_at > :since
            GROUP BY combo_id, pushed_by
            """
        ), {"u": username, "since": since}).mappings().all()

        # --- your groups moved by someone else ----------------------------
        moves = conn.execute(text(
            "SELECT combo_id, description, created_by, created_at, snapshot_json FROM dbo.dept_mapping_recent_moves "
            "WHERE created_by <> :u AND created_at > :since"
        ), {"u": username, "since": since}).mappings().all()

        # --- new disputes (Needs agreement) -------------------------------
        disputes = conn.execute(text(
            """
            SELECT combo_id, COUNT(DISTINCT department) AS depts, MAX(suggested_at) AS latest,
                   MAX(CASE WHEN staged_by = :u THEN 1 ELSE 0 END) AS mine
            FROM dbo.dept_mapping_combo_suggestions GROUP BY combo_id
            HAVING COUNT(DISTINCT department) > 1 AND MAX(suggested_at) > :since
            """
        ), {"u": username, "since": since}).mappings().all()
        item_disputes = conn.execute(text(
            """
            SELECT s.combo_id, COUNT(*) AS n, MAX(s.suggested_at) AS latest,
                   MAX(CASE WHEN s.suggested_by = :u OR p.staged_by = :u THEN 1 ELSE 0 END) AS mine
            FROM dbo.dept_mapping_upc_change_suggestions s
            LEFT JOIN dbo.dept_mapping_pending_upc_changes p ON p.upc = s.upc
            GROUP BY s.combo_id HAVING MAX(s.suggested_at) > :since
            """
        ), {"u": username, "since": since}).mappings().all()

        ids = ([r["combo_id"] for r in sugg] + [r["combo_id"] for r in combo_undo]
               + [int(r["entity_id"]) for r in group_undo] + [r["combo_id"] for r in pushed_whole]
               + [r["combo_id"] for r in pushed_items] + [r["combo_id"] for r in moves]
               + [r["combo_id"] for r in disputes] + [r["combo_id"] for r in item_disputes]
               + [r["combo_id"] for r in claims])
        labels = _combo_labels(conn, ids)

    def entry(bucket, cid, detail, when, kind, title=None, tab="Pending Changes"):
        full, search = labels.get(cid, (title or f"group #{cid}", title or ""))
        bucket.append({"combo_id": cid, "title": full, "search": search, "detail": detail, "tab": tab,
                       "when": when, "new": when is not None and when > since, "kind": kind})

    for r in sugg:
        entry(action, r["combo_id"], f"{r['suggested_by']} suggested a different Department on {r['n']} of your item(s) — accept or deny", r["latest"], "suggestion")
    for r in combo_undo:
        entry(action, r["combo_id"], f"{r['requested_by']} asked you to undo your decision", r["requested_at"], "undo")
    if group_undo:
        primaries = get_broken_out_group_primaries(engine, [int(r["entity_id"]) for r in group_undo])
        for r in group_undo:
            if primaries.get(int(r["entity_id"])) == username:
                entry(action, int(r["entity_id"]), f"{r['requested_by']} asked you to undo this Broken Out group", r["requested_at"], "undo")

    for r in claims:
        left = max(0, CLAIM_IDLE_MINUTES - r["idle"])
        entry(action, r["combo_id"],
              f"You're working on this Broken Out group (frees up after {left} min idle)", r["claimed_at"], "claim", tab="Broken Out")
    for r in discarded:
        who = r["triggered_by"] or "a Merge"
        kind = "replaced by an admin override" if "overrode" in (r["reason"] or "").lower() else "discarded"
        updates.append({"combo_id": None, "title": r["grp"], "search": r["grp"].split(" — ", 1)[-1], "tab": "Pending Changes",
                        "detail": f"{r['n']} of your staged item(s) {kind} by {who}", "when": r["latest"], "new": True,
                        "kind": "discarded"})
    for r in pushed_whole:
        entry(updates, r["combo_id"], f"{r['pushed_by']} pushed your decision ({r['decided_department']}) live", r["pushed_at"], "pushed", tab="Decided")
    for r in pushed_items:
        entry(updates, r["combo_id"], f"{r['pushed_by']} pushed {r['n']} of your item decision(s) live", r["latest"], "pushed", tab="Decided")
    for r in moves:
        snap = json.loads(r["snapshot_json"] or "{}")
        combo = snap.get("combo") or {}
        staged = snap.get("staged") or {}
        involved = combo.get("last_decided_by") == username or any(
            row.get("staged_by") == username or row.get("suggested_by") == username
            for rows in staged.values() for row in rows
        )
        if involved:
            entry(updates, r["combo_id"], f"{r['created_by']}: {r['description']}", r["created_at"], "moved")
    for r in disputes:
        entry(updates, r["combo_id"], ("A dispute you're in changed" if r["mine"] else "New group needs agreement")
              + f" — {r['depts']} different departments suggested", r["latest"], "dispute")
    for r in item_disputes:
        if not any(a["combo_id"] == r["combo_id"] for a in action):
            entry(updates, r["combo_id"], ("Item suggestions on a group you're in" if r["mine"] else "New item suggestions need agreement")
                  + f" — {r['n']} item(s)", r["latest"], "dispute")

    # --- Settings requests: admins are asked to review; editors hear back --
    for r in list_settings_requests(engine, status="pending") if is_admin else []:
        entry(action, None, f"{r['requested_by']} asks: {r['summary'].replace('**', '')} — approve or deny", r["requested_at"],
              "request", title=f"Settings request #{r['request_id']}", tab="Settings")
    for r in list_settings_requests(engine, requested_by=username):
        if r["status"] in ("approved", "denied") and r["decided_at"] and r["decided_at"] > since:
            note = f" — “{r['admin_note']}”" if r.get("admin_note") else ""
            entry(updates, None, f"{r['decided_by']} {r['status']} your request: {r['summary'].replace('**', '')}{note}",
                  r["decided_at"], "request", title=f"Settings request #{r['request_id']}", tab="Settings")

    newest_first = lambda e: e["when"] or datetime.min
    action.sort(key=newest_first, reverse=True)
    updates.sort(key=newest_first, reverse=True)
    return {"action": action, "updates": updates}


# ---------------------------------------------------------------------------
# Activity log — a permanent record of who changed what, for the admin
# Activity report. Separate from the undo history (which is short-lived and
# trimmed) and never touched by a snapshot restore.
# ---------------------------------------------------------------------------
ACTIVITY_AREAS = ["Department Review", "Pushed live", "Undo / Redo", "Items", "Sources", "Settings",
                  "Uploads & Merge", "Snapshots"]


ACTIVITY_VIAS = ["In the app", "Department workbook upload", "Group Excel file", "Spreadsheet upload",
                 "Monthly refresh", "File upload", "Undo / Redo"]
_via = threading.local()


class activity_via:
    """Everything logged inside `with activity_via("Monthly refresh"):` says
    that's how it was made."""

    def __init__(self, label: str):
        self.label = label

    def __enter__(self):
        self.prev = getattr(_via, "v", None)
        _via.v = self.label

    def __exit__(self, *exc):
        _via.v = self.prev


def _infer_via(area: str, action: str) -> str:
    if area == "Undo / Redo":
        return "Undo / Redo"
    if re.search(r"workbook", action or "", re.I):
        return "Department workbook upload"
    if re.search(r"from .+\.(xlsx|xls|csv)\b", action or "", re.I):
        return "Group Excel file"
    if area == "Uploads & Merge" and (action or "").startswith("Uploaded"):
        return "File upload"
    return "In the app"


def log_activity(conn_or_engine, actor: str, area: str, action: str, target: str = None, n_items: int = None,
                 combo_id: int = None, details=None, via: str = None) -> None:
    """One line in the activity log. Never fails the change it describes."""
    if not actor:
        return
    via = via or getattr(_via, "v", None) or _infer_via(area, action)
    params = {"via": via, "a": actor, "ar": area, "ac": action[:400], "t": (target or None) and str(target)[:600],
              "n": None if n_items is None else int(n_items), "c": None if combo_id is None else int(combo_id),
              "d": None if details is None else (details if isinstance(details, str) else json.dumps(details, default=str))}
    sql = text("INSERT INTO dbo.activity_log (actor, area, action, target, n_items, combo_id, details, via) "
               "VALUES (:a, :ar, :ac, :t, :n, :c, :d, :via)")
    try:
        if hasattr(conn_or_engine, "begin") and not hasattr(conn_or_engine, "in_transaction"):
            with conn_or_engine.begin() as conn:
                conn.execute(sql, params)
        else:
            with conn_or_engine.begin_nested():
                conn_or_engine.execute(sql, params)
    except Exception:
        pass


def list_activity(engine, since=None, until=None, actors=None, areas=None, limit: int = 20000) -> pd.DataFrame:
    where, p = ["1 = 1"], {}
    if since is not None:
        where.append("at >= :since"); p["since"] = since
    if until is not None:
        where.append("at < :until"); p["until"] = until
    sql = (f"SELECT TOP {int(limit)} activity_id, at, actor, area, action, target, n_items, combo_id, details, "
           "ISNULL(via, 'In the app') AS via "
           f"FROM dbo.activity_log WHERE {' AND '.join(where)} ORDER BY activity_id DESC")
    with engine.connect() as conn:
        df = pd.read_sql(text(sql), conn, params=p)
    if actors:
        df = df[df["actor"].isin(actors)]
    if areas:
        df = df[df["area"].isin(areas)]
    return df.reset_index(drop=True)


def staged_work_by_person(engine) -> pd.DataFrame:
    """What each person has staged right now and not pushed yet."""
    sql = """
        SELECT staged_by AS person, 'Group decisions' AS what, COUNT(*) AS n_changes, SUM(n_upcs_total) AS n_items
          FROM dbo.dept_mapping_pending_changes GROUP BY staged_by
        UNION ALL
        SELECT staged_by, 'Votes on disputed groups', COUNT(*), SUM(n_upcs_total)
          FROM dbo.dept_mapping_combo_suggestions GROUP BY staged_by
        UNION ALL
        SELECT staged_by, 'Broken Out item decisions', COUNT(*), COUNT(*)
          FROM dbo.dept_mapping_pending_upc_changes GROUP BY staged_by
        UNION ALL
        SELECT suggested_by, 'Suggestions on others'' items', COUNT(*), COUNT(*)
          FROM dbo.dept_mapping_upc_change_suggestions GROUP BY suggested_by
        UNION ALL
        SELECT staged_by, CASE change_type WHEN 'add' THEN 'Items to add' WHEN 'delete' THEN 'Items to delete'
               ELSE 'UPC overrides / item edits' END, COUNT(*), COUNT(*)
          FROM dbo.item_master_pending_changes GROUP BY staged_by, change_type
        UNION ALL
        SELECT staged_by, CASE change_type WHEN 'add' THEN 'New sources' ELSE 'Source edits' END, COUNT(*), NULL
          FROM dbo.source_pending_changes GROUP BY staged_by, change_type
    """
    with engine.connect() as conn:
        df = pd.read_sql(text(sql), conn)
    df["person"] = df["person"].fillna("(unknown)")
    return df.sort_values(["person", "what"]).reset_index(drop=True)


def retire_undo_for_combos(engine, combo_ids) -> int:
    """A push makes a group's decision the live data: nobody's top-bar Undo
    / Redo steps on it apply any more (going back is a Send Back from now
    on, which is itself undoable). Returns how many steps were retired."""
    ids = [int(c) for c in combo_ids]
    if not ids:
        return 0
    n = 0
    with engine.begin() as conn:
        for chunk in _chunks(ids):
            n += conn.execute(text(
                "UPDATE dbo.dept_mapping_action_log SET status = 'pushed', changed_at = SYSUTCDATETIME() "
                "WHERE status IN ('done', 'undone') AND combo_id IN (SELECT v FROM OPENJSON(:c) WITH (v INT '$'))")
                .bindparams(bindparam("c", type_=JSON_LIST)), {"c": chunk}).rowcount
    return n


# ---------------------------------------------------------------------------
# Top-bar Undo / Redo — a per-person log of every Department Review action
# ---------------------------------------------------------------------------
ACTION_LOG_KEEP = 50  # most recent actions kept per person
UNDO_KEEP_HOURS = 2   # undo history and unsaved grid work: kept for a refresh, dropped after this long idle (or on logout)


def _redo_state_key(snapshot: dict) -> str:
    """Comparable form of a snapshot: decisions + item records + staged work."""
    s = json.loads(json.dumps(snapshot, default=str))
    s["overrides"] = sorted(s.get("overrides") or [], key=lambda r: r["upc"])
    for t, rows in (s.get("staged") or {}).items():
        s["staged"][t] = sorted(rows, key=lambda r: json.dumps(r, sort_keys=True))
    return json.dumps(s, sort_keys=True)


def _capture(conn, combo_id: int) -> dict:
    moves = _json_safe_rows(conn.execute(
        text("SELECT * FROM dbo.dept_mapping_recent_moves WHERE combo_id = :c ORDER BY move_id"), {"c": combo_id}
    ).mappings().all())
    return {"snap": _combo_snapshot(conn, combo_id), "moves": moves}


def capture_combo_state(engine, combo_id: int) -> dict:
    """Everything about a group an action can change: decisions, item
    records, staged work, and its undo history."""
    with engine.connect() as conn:
        return _capture(conn, combo_id)


def _state_key(state: dict) -> str:
    return _redo_state_key(state["snap"]) + "|" + ",".join(str(m["move_id"]) for m in state["moves"])


def log_action(engine, actor: str, combo_id: int, label: str, description: str, before: dict,
               click_id: str = None) -> bool:
    """Records one action (the group's state just before it, and now just
    after). An action that changed nothing isn't recorded. Several writes
    from the same click on the same group (click_id) merge into ONE action
    — "Accept all" is one thing to undo, not one per item. A new action
    clears this person's redo list — redo only follows an undo."""
    after = capture_combo_state(engine, combo_id)
    with engine.begin() as conn:
        if click_id:
            last = conn.execute(text(
                "SELECT TOP 1 action_id, click_id, combo_id, status FROM dbo.dept_mapping_action_log "
                "WHERE actor = :a ORDER BY action_id DESC"), {"a": actor}).mappings().first()
            if last and last["click_id"] == click_id and last["combo_id"] == combo_id and last["status"] == "done":
                conn.execute(text("UPDATE dbo.dept_mapping_action_log SET after_json = :f WHERE action_id = :i"),
                             {"f": json.dumps(after, default=str), "i": last["action_id"]})
                return True
        if _state_key(after) == _state_key(before):
            return False
        conn.execute(text("DELETE FROM dbo.dept_mapping_action_log WHERE actor = :a AND status <> 'done'"), {"a": actor})
        conn.execute(
            text(
                "INSERT INTO dbo.dept_mapping_action_log (actor, combo_id, label, description, before_json, after_json, click_id) "
                "VALUES (:a, :c, :l, :d, :b, :f, :k)"
            ),
            {"a": actor, "c": combo_id, "l": label, "d": description,
             "b": json.dumps(before, default=str), "f": json.dumps(after, default=str), "k": click_id},
        )
        log_activity(conn, actor, "Department Review", description, label, conn.execute(text(
            "SELECT n_upcs_total FROM dbo.dept_mapping_combos WHERE combo_id = :c"), {"c": combo_id}).scalar(), combo_id)
        conn.execute(
            text(
                f"DELETE FROM dbo.dept_mapping_action_log WHERE actor = :a AND action_id NOT IN ("
                f"SELECT TOP {ACTION_LOG_KEEP} action_id FROM dbo.dept_mapping_action_log WHERE actor = :a ORDER BY action_id DESC)"
            ),
            {"a": actor},
        )
    return True


def _apply_state(conn, combo_id: int, target: dict, current: dict, actor: str) -> None:
    """Moves a group from `current` to `target` exactly: decisions, item
    records, staged work, and its undo-history rows."""
    _restore_combo_snapshot(conn, combo_id, target["snap"], actor)
    cur_ids = {m["move_id"] for m in current["moves"]}
    tgt = {m["move_id"]: m for m in target["moves"]}
    gone = [i for i in cur_ids if i not in tgt]
    for chunk in _chunks(gone):
        conn.execute(
            text("DELETE FROM dbo.dept_mapping_recent_moves WHERE move_id IN :ids").bindparams(bindparam("ids", expanding=True)),
            {"ids": chunk},
        )
    back = [tgt[i] for i in sorted(tgt) if i not in cur_ids]
    if back:
        conn.execute(text("SET IDENTITY_INSERT dbo.dept_mapping_recent_moves ON"))
        cols = list(back[0].keys())
        stmt = text(f"INSERT INTO dbo.dept_mapping_recent_moves ({', '.join(cols)}) VALUES ({', '.join(':' + c for c in cols)})")
        for r in back:
            conn.execute(stmt, r)
        conn.execute(text("SET IDENTITY_INSERT dbo.dept_mapping_recent_moves OFF"))


# Steps older than UNDO_KEEP_HOURS never count, whether or not the
# housekeeping delete has got to them yet.
_UNDO_FRESH = "COALESCE(changed_at, created_at) >= DATEADD(hour, -%d, SYSUTCDATETIME())"
_last_undo_cleanup = {}


def _cleanup_undo_now_and_then(engine, actor: str, every_minutes: int = 10) -> None:
    """The housekeeping delete, at most every few minutes per person rather
    than on every click (the fresh-only filter above keeps results exact)."""
    now = time.monotonic()
    if now - _last_undo_cleanup.get(actor, -1e9) >= every_minutes * 60:
        _last_undo_cleanup[actor] = now
        cleanup_old_undo(engine, actor)


def peek_undo_redo(engine, actor: str) -> dict:
    """What the top-bar Undo / Redo would act on right now (for its labels)."""
    _cleanup_undo_now_and_then(engine, actor)
    fresh = _UNDO_FRESH % UNDO_KEEP_HOURS
    with engine.connect() as conn:
        rows = conn.execute(text(
            "SELECT * FROM ("
            " SELECT TOP 1 'undo' AS which, action_id, combo_id, label, description, created_at, changed_at "
            f" FROM dbo.dept_mapping_action_log WHERE actor = :a AND status = 'done' AND {fresh} ORDER BY action_id DESC"
            ") u UNION ALL SELECT * FROM ("
            " SELECT TOP 1 'redo' AS which, action_id, combo_id, label, description, created_at, changed_at "
            f" FROM dbo.dept_mapping_action_log WHERE actor = :a AND status = 'undone' AND {fresh} "
            " ORDER BY changed_at DESC, action_id DESC) r"), {"a": actor}).mappings().all()
    got = {r["which"]: {k: v for k, v in r.items() if k != "which"} for r in rows}
    return {"undo": got.get("undo"), "redo": got.get("redo")}


def _why_changed(conn, combo_id: int, actor: str, since) -> str:
    """Plain words for what changed a group after `since`, for a refused
    Undo / Redo: another person's action, a push, a move, or else."""
    other = conn.execute(text(
        "SELECT TOP 1 actor, description FROM dbo.dept_mapping_action_log WHERE combo_id = :c AND actor <> :a "
        "AND COALESCE(changed_at, created_at) > :t ORDER BY COALESCE(changed_at, created_at) DESC"),
        {"c": combo_id, "a": actor, "t": since}).mappings().first()
    if other:
        return f"{other['actor']} changed it since ({other['description']})"
    pushed = conn.execute(text(
        "SELECT TOP 1 pushed_by FROM (SELECT pushed_by, pushed_at FROM dbo.dept_mapping_combos WHERE combo_id = :c "
        "UNION ALL SELECT pushed_by, pushed_at FROM dbo.dept_mapping_upc_overrides WHERE combo_id = :c) x "
        "WHERE pushed_at > :t ORDER BY pushed_at DESC"), {"c": combo_id, "t": since}).mappings().first()
    if pushed:
        return f"it's been pushed live since (by {pushed['pushed_by'] or 'someone'})"
    moved = conn.execute(text(
        "SELECT TOP 1 created_by, description FROM dbo.dept_mapping_recent_moves WHERE combo_id = :c AND created_at > :t "
        "ORDER BY created_at DESC"), {"c": combo_id, "t": since}).mappings().first()
    if moved:
        return f"{moved['created_by']} changed it since ({moved['description']})"
    return "it's been changed since (for example by a Merge, or someone else's staged work)"


def _step(engine, actor: str, undo: bool) -> dict:
    with engine.begin() as conn:
        # UPDLOCK: a second Undo clicked at the same moment (double click,
        # two tabs) waits here for the first to finish, then takes the next
        # step — instead of re-checking the step the first just undid.
        row = conn.execute(text(
            "SELECT TOP 1 * FROM dbo.dept_mapping_action_log WITH (UPDLOCK, ROWLOCK) WHERE actor = :a AND status = :s "
            f"AND {_UNDO_FRESH % UNDO_KEEP_HOURS} ORDER BY "
            + ("action_id DESC" if undo else "changed_at DESC, action_id DESC")
        ), {"a": actor, "s": "done" if undo else "undone"}).mappings().first()
        if not row:
            return {"ok": False, "reason": "nothing"}
        entry = {k: row[k] for k in ("action_id", "combo_id", "label", "description")}
        before, after = json.loads(row["before_json"]), json.loads(row["after_json"])
        expect, target = (after, before) if undo else (before, after)
        _lock_combo(conn, row["combo_id"])
        current = _capture(conn, row["combo_id"])
        if _state_key(current) != _state_key(expect):
            # Someone else has changed this group since — undoing/redoing
            # now would overwrite their work. Retire this entry so the next
            # Undo moves on to the action before it.
            why = _why_changed(conn, row["combo_id"], actor, row["changed_at"] or row["created_at"])
            conn.execute(text("UPDATE dbo.dept_mapping_action_log SET status = 'stale', changed_at = SYSUTCDATETIME() "
                              "WHERE action_id = :i"), {"i": row["action_id"]})
            return {"ok": False, "reason": "changed", "entry": entry, "why": why}
        _apply_state(conn, row["combo_id"], target, current, actor)
        conn.execute(text("UPDATE dbo.dept_mapping_action_log SET status = :s, changed_at = SYSUTCDATETIME() "
                          "WHERE action_id = :i"), {"s": "undone" if undo else "done", "i": row["action_id"]})
        log_activity(conn, actor, "Undo / Redo", f"{'Undid' if undo else 'Redid'}: {row['description']}", row["label"],
                     None, row["combo_id"])
        _clear_dept_push_approvals(conn)
    return {"ok": True, "entry": entry}


def undo_last_action(engine, actor: str) -> dict:
    """Takes back `actor`'s most recent Department Review action — exactly
    that one — if nobody has changed that group since."""
    return _step(engine, actor, undo=True)


def redo_last_action(engine, actor: str) -> dict:
    """Re-applies `actor`'s most recently undone action, if the group is
    still exactly as the undo left it."""
    return _step(engine, actor, undo=False)


# ---------------------------------------------------------------------------
# "What decided this item?" — for the Merge tab's new-item reports and the
# staged re-check.
# ---------------------------------------------------------------------------
ITEM_ROW_COLUMNS = ["upc", "description", "brand", "category", "subcategory", "source_key", "pack", "size", "uom", "department"]


def _in_chunks(conn, sql: str, upcs: list, name: str = "u") -> pd.DataFrame:
    frames = []
    for chunk in _chunks(list(upcs)):
        frames.append(pd.read_sql(text(sql).bindparams(bindparam(name, type_=JSON_LIST)), conn, params={name: chunk}))
    df = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    return df.astype(object).where(df.notna(), None)


def _group_label(r) -> str:
    bits = [b for b in (r.get("raw_department"), r.get("raw_category"), r.get("raw_subcategory")) if b]
    return f"{(r.get('source_key') or '').upper()} — {' / '.join(bits)}"


def _combo_status(c: dict, item_override: dict | None) -> tuple:
    """(status, how, tab) for an item in group c."""
    state = c["decision_state"]
    if state in ("broken_out", "decided_broken_out"):
        o = item_override or {}
        via = o.get("decided_via")
        if via and via != "not_reviewed":
            how = via if via.startswith("Auto") else f"{via} by {o.get('updated_by') or 'someone'}"
            return "Decided item by item", how, "Decided" if state == "decided_broken_out" else "Broken Out"
        return "Waiting in Broken Out", "not decided yet", "Broken Out"
    if c["manual_department"] or state == "decided":
        return "Decided by a person", f"group decided by {c['last_decided_by'] or 'someone'}", "Decided"
    if c["decided_department"]:
        via = c["decided_via"] or "Auto"
        return ("Auto-decided", f"group auto-decided ({c['resolved_via'] or via})", "Decided") if via == "Auto" else \
               ("Decided by a person", f"group {via.lower()}", "Decided")
    if c["tier"] == "unmatched":
        return "Waiting in Unmatched", "group not decided yet", "Unmatched"
    return "Waiting in Crosswalk", "group not decided yet", "Crosswalk"


def explain_items(engine, upcs: list, table: str = "items") -> pd.DataFrame:
    """One row per UPC: its row data (from dbo.items, or the Merge draft
    when table='items_staged'), its Department, and what gave it that
    Department — a group decision (auto or by whom), an item-by-item
    Broken Out decision, a manual override — or where it's waiting."""
    upcs = list(dict.fromkeys(upcs))
    cols = ", ".join(ITEM_ROW_COLUMNS)
    with engine.connect() as conn:
        rows = _in_chunks(conn, f"SELECT {cols} FROM dbo.{table} WHERE upc IN (SELECT v FROM OPENJSON(:u) WITH (v VARCHAR(400) '$'))", upcs)
        combos = _in_chunks(conn,
            "SELECT cu.upc, c.combo_id, c.source_key, c.raw_department, c.raw_category, c.raw_subcategory, c.tier, "
            "c.decision_state, c.decided_department, c.decided_via, c.manual_department, c.last_decided_by, c.resolved_via "
            "FROM dbo.dept_mapping_combo_upcs cu JOIN dbo.dept_mapping_combos c ON c.combo_id = cu.combo_id WHERE cu.upc IN (SELECT v FROM OPENJSON(:u) WITH (v VARCHAR(400) '$'))", upcs)
        overrides = _in_chunks(conn, "SELECT upc, department, decided_via, updated_by FROM dbo.dept_mapping_upc_overrides WHERE upc IN (SELECT v FROM OPENJSON(:u) WITH (v VARCHAR(400) '$'))", upcs)
        manual = _in_chunks(conn, "SELECT upc, department, updated_by FROM dbo.manual_overrides WHERE upc IN (SELECT v FROM OPENJSON(:u) WITH (v VARCHAR(400) '$'))", upcs)
        # a draft item isn't in a group yet: find the group its raw text will put it in
        raw = _in_chunks(conn,
            "SELECT r.upc, r.source_key, r.department, r.category, r.subcategory FROM dbo.raw_items r WHERE r.upc IN (SELECT v FROM OPENJSON(:u) WITH (v VARCHAR(400) '$'))", upcs) \
            if table == "items_staged" else pd.DataFrame()
        all_combos = pd.read_sql(text(
            "SELECT combo_id, source_key, raw_department, raw_category, raw_subcategory, tier, decision_state, decided_department, "
            "decided_via, manual_department, last_decided_by, resolved_via FROM dbo.dept_mapping_combos"), conn) \
            if table == "items_staged" else pd.DataFrame()
    all_combos = all_combos.astype(object).where(all_combos.notna(), None)
    cmap = {r["upc"]: r for r in combos.to_dict("records")} if not combos.empty else {}
    omap = {r["upc"]: r for r in overrides.to_dict("records")} if not overrides.empty else {}
    mmap = {r["upc"]: r for r in manual.to_dict("records")} if not manual.empty else {}
    keyed = {}
    if not all_combos.empty:
        keyed = {(r["source_key"], r["raw_department"], r["raw_category"], r["raw_subcategory"]): r
                 for r in all_combos.to_dict("records")}
    rawmap = {}
    for r in (raw.to_dict("records") if not raw.empty else []):
        rawmap[(r["upc"], r["source_key"])] = r
    out = []
    for r in rows.to_dict("records"):
        u = r["upc"]
        m = mmap.get(u)
        c = cmap.get(u)
        status, how, tab, group = None, "", None, ""
        if m and m.get("department"):
            status, how = "Manual override", f"Department set on UPC Overrides by {m.get('updated_by') or 'someone'}"
        if c is None and table == "items_staged" and r["source_key"] not in (None, "nwg", "manual"):
            rr = rawmap.get((u, r["source_key"]))
            if rr is not None:
                key = (r["source_key"], _norm(rr["department"]), _norm(rr["category"]), _norm(rr["subcategory"]))
                c = keyed.get(key)
                if c is None:
                    group = f"{r['source_key'].upper()} — {' / '.join(b for b in key[1:] if b)}"
                    status = status or "New group"
                    how = how or "forms a new group — the engine auto-decides it if the evidence is strong, otherwise it waits in Crosswalk/Unmatched"
                    tab = "Crosswalk"
        if c is not None:
            group = _group_label(c)
            s_, h_, t_ = _combo_status(c, omap.get(u))
            tab = t_
            if status is None:
                status, how = s_, h_
            elif status == "Manual override":
                how += f" (its group: {s_.lower()})"
        if status is None:
            if r["source_key"] == "nwg":
                status, how = "Scan Advantage's own", "Department comes from Scan Advantage's own data"
            elif r["source_key"] == "manual":
                status, how = "Manually added", "added on Add Item"
            else:
                status, how = "Not in any group", "no Department Review group"
        out.append({"UPC": u, "Description": r["description"], "Brand": r["brand"], "Category": r["category"],
                    "Subcategory": r["subcategory"], "Source": r["source_key"], "Pack": r["pack"], "Size": r["size"],
                    "UOM": r["uom"], "Department": r["department"], "Decision": status, "How": how,
                    "Group": group, "Tab": tab})
    return pd.DataFrame(out, columns=["UPC", "Description", "Brand", "Category", "Subcategory", "Source", "Pack", "Size",
                                      "UOM", "Department", "Decision", "How", "Group", "Tab"])


def draft_new_items(engine) -> pd.DataFrame:
    """The new items in the current Merge draft, with the decision each will
    get once pushed (the group it falls into and how that group stands)."""
    with engine.connect() as conn:
        upcs = conn.execute(text(
            "SELECT s.upc FROM dbo.items_staged s WHERE NOT EXISTS (SELECT 1 FROM dbo.items i WHERE i.upc = s.upc)")).scalars().all()
    df = explain_items(engine, upcs, table="items_staged")
    if not df.empty:
        # after the push, a new item in a decided group takes that group's Department
        pending = df["Decision"].isin(["Auto-decided", "Decided by a person"]) & df["Department"].isna()
        df.loc[pending, "How"] = df.loc[pending, "How"] + " — Department filled in on push"
    return df


def list_merges_with_additions(engine) -> pd.DataFrame:
    with engine.connect() as conn:
        return pd.read_sql(text(
            "SELECT m.id, m.merged_at, m.merged_by, m.added_count, "
            "(SELECT COUNT(*) FROM dbo.merge_added_items a WHERE a.merge_id = m.id) AS recorded "
            "FROM dbo.merge_log m ORDER BY m.id DESC"), conn)


def merge_added_items(engine, merge_id: int) -> pd.DataFrame:
    """Every item a Merge push added, as it stands now, with its decision."""
    with engine.connect() as conn:
        upcs = conn.execute(text("SELECT upc FROM dbo.merge_added_items WHERE merge_id = :m"), {"m": merge_id}).scalars().all()
    df = explain_items(engine, upcs)
    gone = sorted(set(upcs) - set(df["UPC"]))
    if gone:
        df = pd.concat([df, pd.DataFrame({"UPC": gone, "Decision": "No longer in the item master",
                                          "How": "deleted or rolled back since"})], ignore_index=True)
    return df


# ---------------------------------------------------------------------------
# The staged re-check ("recalculate"): every item out of line with the rules,
# shown with why, applied only on request, and undoable.
# ---------------------------------------------------------------------------
def plan_rules(engine) -> pd.DataFrame:
    """Every change a full re-check would make, one row per item + field:
    UPC, Field, Now, Will be, Why. Nothing is written."""
    fields = ["description", "department", "category", "subcategory", "brand", "pack", "size", "uom"]
    rows = []
    with engine.connect() as conn:
        for f in fields:
            for r in conn.execute(text(
                    f"SELECT i.upc, i.{f} AS now, o.{f} AS target, o.updated_by, o.updated_at FROM dbo.items i "
                    f"JOIN dbo.manual_overrides o ON o.upc = i.upc WHERE ISNULL(o.{f}, '') <> '' AND ISNULL(i.{f}, '') <> o.{f}")).mappings():
                rows.append({"UPC": r["upc"], "Field": f, "Now": r["now"], "Will be": r["target"],
                             "Why": f"UPC Override by {r['updated_by'] or 'someone'} ({str(r['updated_at'])[:10]}) isn't applied"})
        for r in conn.execute(text(
                "SELECT i.upc, i.description, d.deleted_by, d.deleted_at FROM dbo.items i JOIN dbo.deleted_upcs d ON d.upc = i.upc")).mappings():
            rows.append({"UPC": r["upc"], "Field": "(whole item)", "Now": r["description"], "Will be": "(removed)",
                         "Why": f"deleted by {r['deleted_by'] or 'someone'} ({str(r['deleted_at'])[:10]}) but still in the item master"})
        for r in conn.execute(text(
                "SELECT o.upc, o.description, o.updated_by FROM dbo.manual_overrides o WHERE NOT EXISTS (SELECT 1 FROM dbo.items i WHERE i.upc = o.upc) "
                "AND NOT EXISTS (SELECT 1 FROM dbo.deleted_upcs d WHERE d.upc = o.upc) "
                "AND NOT EXISTS (SELECT 1 FROM dbo.raw_items r WHERE r.upc = o.upc)")).mappings():
            rows.append({"UPC": r["upc"], "Field": "(whole item)", "Now": "(missing)", "Will be": r["description"] or r["upc"],
                         "Why": f"manually added by {r['updated_by'] or 'someone'} but missing from the item master"})
    df = _department_targets(engine, "items")
    blank = lambda v: v is None or (isinstance(v, float) and pd.isna(v)) or v == ""
    off = df[[(None if blank(a) else a) != (None if blank(b) else b) for a, b in zip(df["department"], df["target"])]]
    already = {(r["UPC"], r["Field"]) for r in rows}
    if not off.empty:
        why = explain_items(engine, off["upc"].tolist()).set_index("UPC")
        for r in off.to_dict("records"):
            if (r["upc"], "department") in already:
                continue
            w = why.loc[r["upc"]] if r["upc"] in why.index else None
            reason = (f"{w['Decision']}: {w['How']}" + (f" — {w['Group']}" if w["Group"] else "")) if w is not None else ""
            rows.append({"UPC": r["upc"], "Field": "department", "Now": r["department"] if not blank(r["department"]) else None,
                         "Will be": r["target"], "Why": reason})
    plan = pd.DataFrame(rows, columns=["UPC", "Field", "Now", "Will be", "Why"])
    if not plan.empty:
        with engine.connect() as conn:
            desc = _in_chunks(conn, "SELECT upc, description FROM dbo.items WHERE upc IN (SELECT v FROM OPENJSON(:u) WITH (v VARCHAR(400) '$'))", plan["UPC"].tolist())
        plan.insert(1, "Description", plan["UPC"].map(dict(zip(desc["upc"], desc["description"]))) if not desc.empty else None)
    return plan


def apply_rules_plan(engine, plan: pd.DataFrame, actor: str) -> dict:
    """Applies a plan from plan_rules — only if a fresh check still gives the
    same plan (so nothing changed in between). Returns {"ok", "undo"}, where
    undo holds the exact prior rows for undo_rules_plan."""
    fresh = plan_rules(engine)
    key = lambda p: sorted(map(tuple, p[["UPC", "Field", "Now", "Will be"]].astype(str).values.tolist()))
    if key(fresh) != key(plan):
        return {"ok": False, "reason": "changed"}
    upcs = plan["UPC"].unique().tolist()
    full = ["upc", "description", "department", "category", "subcategory", "brand", "source_key", "pack", "size", "uom",
            "created_at", "updated_at"]
    with engine.begin() as conn:
        before = _in_chunks(conn, f"SELECT {', '.join(full)} FROM dbo.items WHERE upc IN (SELECT v FROM OPENJSON(:u) WITH (v VARCHAR(400) '$'))", upcs)
        for r in plan.to_dict("records"):
            if r["Field"] == "(whole item)" and r["Will be"] == "(removed)":
                conn.execute(text("DELETE FROM dbo.items WHERE upc = :u"), {"u": r["UPC"]})
            elif r["Field"] == "(whole item)":
                conn.execute(text(
                    "INSERT INTO dbo.items (upc, description, department, category, subcategory, brand, pack, size, uom, source_key) "
                    "SELECT o.upc, COALESCE(NULLIF(o.description, ''), o.upc), NULLIF(o.department, ''), NULLIF(o.category, ''), "
                    "NULLIF(o.subcategory, ''), NULLIF(o.brand, ''), NULLIF(o.pack, ''), NULLIF(o.size, ''), NULLIF(o.uom, ''), 'manual' "
                    "FROM dbo.manual_overrides o WHERE o.upc = :u"), {"u": r["UPC"]})
            else:
                conn.execute(text(f"UPDATE dbo.items SET {r['Field']} = :v, updated_at = SYSUTCDATETIME() WHERE upc = :u"),
                             {"v": r["Will be"], "u": r["UPC"]})
    return {"ok": True, "undo": {"before": before.to_dict("records"), "upcs": upcs, "by": actor}}


def undo_rules_plan(engine, undo: dict) -> None:
    """Puts every item a re-check touched back exactly as it was."""
    before = pd.DataFrame(undo["before"])
    with engine.begin() as conn:
        for chunk in _chunks(undo["upcs"]):
            conn.execute(text("DELETE FROM dbo.items WHERE upc IN (SELECT v FROM OPENJSON(:u) WITH (v VARCHAR(400) '$'))").bindparams(bindparam("u", type_=JSON_LIST)), {"u": chunk})
        if not before.empty:
            for c in ("created_at", "updated_at"):
                before[c] = pd.to_datetime(before[c])
            before.to_sql("items", conn, schema="dbo", if_exists="append", index=False)


# ---------------------------------------------------------------------------
# Department upload (UPC Overrides tab): UPC + Department, sent to the right
# place — a Broken Out group's item decision where the UPC is broken out,
# otherwise a UPC override of just its Department.
# ---------------------------------------------------------------------------
def broken_out_items(engine, upcs: list) -> dict:
    """{upc: {"combo_id", "group", "label", "source_key", "current"}} for the
    UPCs that sit in a Broken Out group (in progress or finished) — current
    is its item Department right now (a staged pick wins over the decided one)."""
    upcs = [u for u in dict.fromkeys(upcs) if u]
    if not upcs:
        return {}
    with engine.connect() as conn:
        bo = _in_chunks(conn,
            "SELECT o.upc, o.combo_id, o.department AS item_department, c.source_key, c.raw_department, "
            "c.raw_category, c.raw_subcategory FROM dbo.dept_mapping_upc_overrides o "
            "JOIN dbo.dept_mapping_combos c ON c.combo_id = o.combo_id "
            "WHERE o.upc IN (SELECT v FROM OPENJSON(:u) WITH (v VARCHAR(400) '$')) AND c.decision_state IN ('broken_out', 'decided_broken_out')", upcs)
        staged = _in_chunks(conn, "SELECT upc, department FROM dbo.dept_mapping_pending_upc_changes WHERE upc IN (SELECT v FROM OPENJSON(:u) WITH (v VARCHAR(400) '$'))", upcs)
    smap = dict(zip(staged["upc"], staged["department"])) if not staged.empty else {}
    out = {}
    for r in ([] if bo.empty else bo.to_dict("records")):
        group = _group_label(r)
        out[r["upc"]] = {"combo_id": int(r["combo_id"]), "group": group, "label": group.split(" — ", 1)[-1],
                         "source_key": r["source_key"], "current": smap.get(r["upc"]) or r["item_department"]}
    return out


def upc_override_counts(engine) -> dict:
    """combo_id -> how many of its items have a UPC override setting their Department."""
    with engine.connect() as conn:
        return dict(conn.execute(text(
            "SELECT cu.combo_id, COUNT(*) FROM dbo.dept_mapping_combo_upcs cu JOIN dbo.manual_overrides o ON o.upc = cu.upc "
            "WHERE ISNULL(o.department, '') <> '' GROUP BY cu.combo_id")).all())


# ---------------------------------------------------------------------------
# Unexpected errors people hit in the app — recorded for the admins (see
# the 🔔 bell), never shown to the person as a wall of text.
# ---------------------------------------------------------------------------
_APP_ERRORS_DDL = """
IF NOT EXISTS (SELECT * FROM sys.tables WHERE name = 'app_errors')
BEGIN
    CREATE TABLE dbo.app_errors (
        error_id     INT IDENTITY(1,1) PRIMARY KEY,
        occurred_at  DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME(),
        username     NVARCHAR(200) NULL,
        where_in_app NVARCHAR(300) NULL,
        error_type   NVARCHAR(200) NULL,
        message      NVARCHAR(4000) NULL,
        details      NVARCHAR(MAX) NULL,
        resolved_by  NVARCHAR(200) NULL,
        resolved_at  DATETIME2 NULL
    );
END
"""


def ensure_app_errors_table(engine) -> None:
    with engine.begin() as conn:
        conn.execute(text(_APP_ERRORS_DDL))


def log_app_error(engine, username, where_in_app: str, ex: BaseException) -> int | None:
    """Records one unexpected error; returns its reference number (None if
    even that couldn't be saved — the server log still has it)."""
    import traceback
    details = "".join(traceback.format_exception(type(ex), ex, ex.__traceback__))
    params = {"u": username, "w": (where_in_app or "")[:300], "t": type(ex).__name__[:200],
              "m": str(ex)[:4000], "d": details}
    sql = ("INSERT INTO dbo.app_errors (username, where_in_app, error_type, message, details) "
           "OUTPUT INSERTED.error_id VALUES (:u, :w, :t, :m, :d)")
    try:
        with engine.begin() as conn:
            return conn.execute(text(sql), params).scalar()
    except Exception:
        try:
            ensure_app_errors_table(engine)
            with engine.begin() as conn:
                return conn.execute(text(sql), params).scalar()
        except Exception:
            return None


def list_app_errors(engine, include_resolved: bool = False, limit: int = 200) -> list:
    try:
        with engine.connect() as conn:
            return [dict(r) for r in conn.execute(text(
                f"SELECT TOP {int(limit)} error_id, occurred_at, username, where_in_app, error_type, message, "
                "details, resolved_by, resolved_at FROM dbo.app_errors "
                + ("" if include_resolved else "WHERE resolved_at IS NULL ")
                + "ORDER BY error_id DESC")).mappings().all()]
    except Exception:
        return []


def resolve_app_errors(engine, error_ids: list, actor: str) -> None:
    if not error_ids:
        return
    with engine.begin() as conn:
        conn.execute(text("UPDATE dbo.app_errors SET resolved_by = :a, resolved_at = SYSUTCDATETIME() "
                          "WHERE error_id IN :ids AND resolved_at IS NULL")
                     .bindparams(bindparam("ids", expanding=True)), {"a": actor, "ids": [int(i) for i in error_ids]})


# ---------------------------------------------------------------------------
# The full item master, kept in memory on the server between reloads. Only a
# one-row fingerprint of the table is read from the database each time; the
# ~330K rows are downloaded again only when that fingerprint changes (any
# add, edit, delete or merge changes it). Same rows, same order, every time.
# ---------------------------------------------------------------------------
ITEMS_SQL = (
    "SELECT i.upc AS UPC, i.description AS Description, i.department AS Department, "
    "i.category AS Category, i.subcategory AS Subcategory, i.brand AS Brand, "
    "i.pack AS Pack, i.size AS Size, i.uom AS UOM, "
    "i.source_key AS SourceKey, i.created_at AS CreatedAt, i.updated_at AS UpdatedAt, "
    # A manual_overrides row exists for BOTH a manually-added item and a
    # manually-corrected existing one — the one reliable signal for "a
    # human's correction is behind this row".
    "mo.updated_by AS ManuallyEditedBy, mo.updated_at AS ManuallyEditedAt "
    "FROM dbo.items i LEFT JOIN dbo.manual_overrides mo ON mo.upc = i.upc "
    "ORDER BY Department, Category, Description"
)
ITEMS_FINGERPRINT_SQL = (
    "SELECT COUNT_BIG(*), CHECKSUM_AGG(BINARY_CHECKSUM(i.upc, i.description, i.department, i.category, "
    "i.subcategory, i.brand, i.pack, i.size, i.uom, i.source_key, i.created_at, i.updated_at, "
    "mo.updated_by, mo.updated_at)) FROM dbo.items i LEFT JOIN dbo.manual_overrides mo ON mo.upc = i.upc"
)
_items_memo = {"fingerprint": None, "df": None}
_items_lock = threading.Lock()


def load_items_df(engine) -> pd.DataFrame:
    with _items_lock:  # a second caller waits for the first download instead of starting another
        with engine.connect() as conn:
            fp = tuple(conn.execute(text(ITEMS_FINGERPRINT_SQL)).one())
            if _items_memo["df"] is not None and _items_memo["fingerprint"] == fp:
                return _items_memo["df"]
            df = pd.read_sql(text(ITEMS_SQL), conn)
        _items_memo.update(fingerprint=fp, df=df)
        return df


def prefetch_items_in_background(engine) -> None:
    """Start downloading the item master while someone is still signing in."""
    if _items_memo["df"] is None and not _items_lock.locked():
        threading.Thread(target=lambda: _safe(load_items_df, engine), daemon=True).start()


def _safe(fn, *args):
    try:
        fn(*args)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Where a staged or decided change came from — for Pending Changes / Decided.
# ---------------------------------------------------------------------------
def group_facts(engine) -> dict:
    """{combo_id: {...}} — where each group sits now (Crosswalk / Unmatched /
    Broken Out / Decided), what it was decided as before, and the
    distributor's own text for it."""
    with engine.connect() as conn:
        rows = conn.execute(text(
            "SELECT combo_id, source_key, raw_department, raw_category, raw_subcategory, tier, n_evidence, decision_state, "
            "decided_department, manual_department, decided_via, suggested_department, n_upcs_total, last_decided_by, "
            "decided_note FROM dbo.dept_mapping_combos")).mappings().all()
    out = {}
    for r in rows:
        r = dict(r)
        state = r["decision_state"]
        if state == "broken_out":
            where = "Broken Out"
        elif state in ("decided", "decided_broken_out") or r["decided_department"]:
            where = "Decided"
        else:
            where = "Unmatched" if origin_tier(r["tier"], r["n_evidence"]) == "unmatched" else "Crosswalk"
        r["where"] = where
        r["queue"] = "Unmatched" if origin_tier(r["tier"], r["n_evidence"]) == "unmatched" else "Crosswalk"
        out[r["combo_id"]] = r
    return out


def pending_overrides_by_group(engine) -> pd.DataFrame:
    """Staged UPC overrides (Pending Changes tab), one row per item with the
    group it sits in, for listing them by group."""
    with engine.connect() as conn:
        return pd.read_sql(text(
            "SELECT p.upc, cu.combo_id, p.description, i.department AS now, p.department, p.origin_note, "
            "p.staged_by, p.staged_at FROM dbo.item_master_pending_changes p "
            "JOIN dbo.dept_mapping_combo_upcs cu ON cu.upc = p.upc LEFT JOIN dbo.items i ON i.upc = p.upc "
            "WHERE p.change_type = 'edit'"), conn)


def get_combo_member_items_bulk(engine, combo_ids: list) -> pd.DataFrame:
    """get_combo_member_items for many groups in ONE query (combo_id column added)."""
    ids = [int(c) for c in combo_ids]
    if not ids:
        return pd.DataFrame(columns=["combo_id", "upc", "description", "brand", "pack", "size", "uom", "manually_edited_by"])
    with engine.connect() as conn:
        return pd.read_sql(
            text(
                "SELECT cu.combo_id, i.upc, i.description, i.brand, i.pack, i.size, i.uom, mo.updated_by AS manually_edited_by "
                "FROM dbo.dept_mapping_combo_upcs cu JOIN dbo.items i ON i.upc = cu.upc "
                "LEFT JOIN dbo.manual_overrides mo ON mo.upc = i.upc "
                "WHERE cu.combo_id IN (SELECT v FROM OPENJSON(:ids) WITH (v BIGINT '$')) ORDER BY i.description"
            ),
            conn, params={"ids": json.dumps(ids)},
        )


def groups_for_upcs(engine, upcs: list) -> dict:
    """{upc: combo_id} for the given UPCs."""
    with engine.connect() as conn:
        df = _in_chunks(conn, "SELECT upc, combo_id FROM dbo.dept_mapping_combo_upcs WHERE upc IN (SELECT v FROM OPENJSON(:u) WITH (v VARCHAR(400) '$'))", upcs)
    return dict(zip(df["upc"], df["combo_id"])) if not df.empty else {}


def noted_item_counts(engine) -> dict:
    """{combo_id: {note: n}} — item decisions carrying a note (e.g. from the old-workbook import)."""
    with engine.connect() as conn:
        rows = conn.execute(text(
            "SELECT combo_id, decided_note, COUNT(*) FROM dbo.dept_mapping_upc_overrides "
            "WHERE decided_note IS NOT NULL GROUP BY combo_id, decided_note")).all()
    out = {}
    for cid, note, n in rows:
        out.setdefault(cid, {})[note] = n
    return out


# ---------------------------------------------------------------------------
# Undo a whole one-time old-workbook import (see old_workbook_import).
# ---------------------------------------------------------------------------
IMPORT_NOTE_LIKE = "📥 One-time old-workbook import%"


def old_workbook_import_summary(engine) -> dict:
    """What the import currently has staged or moved (still undoable)."""
    with engine.connect() as conn:
        q = lambda sql: conn.execute(text(sql), {"n": IMPORT_NOTE_LIKE}).scalar() or 0
        return {
            "groups": q("SELECT COUNT(*) FROM dbo.dept_mapping_pending_changes WHERE origin_note LIKE :n"),
            "items": q("SELECT COUNT(*) FROM dbo.dept_mapping_pending_upc_changes WHERE origin_note LIKE :n"),
            "overrides": q("SELECT COUNT(*) FROM dbo.item_master_pending_changes WHERE origin_note LIKE :n"),
            "moves": q("SELECT COUNT(DISTINCT combo_id) FROM dbo.dept_mapping_recent_moves WHERE origin_note LIKE :n"),
        }


def set_pending_note(engine, combo_id: int, note: str | None) -> None:
    """Where a staged group decision came from (shown on its card)."""
    with engine.begin() as conn:
        conn.execute(text("UPDATE dbo.dept_mapping_pending_changes SET origin_note = :n WHERE combo_id = :c"),
                     {"n": note, "c": combo_id})


def list_import_choices(engine, status: str | None = "open") -> list:
    """A workbook upload's "which is right?" questions (see old_workbook_import._choices)."""
    sql = "SELECT * FROM dbo.import_choices" + (" WHERE status = :s" if status else "") + " ORDER BY choice_id"
    with engine.connect() as conn:
        return [dict(r) for r in conn.execute(text(sql), {"s": status} if status else {}).mappings().all()]


def import_notes(engine) -> dict:
    """{combo_id: note text} — groups the workbook listed more than once where only one row decided anything."""
    with engine.connect() as conn:
        return {r[0]: r[1] for r in conn.execute(text(
            "SELECT combo_id, workbook_says FROM dbo.import_choices WHERE status = 'note'")).all()}


def close_import_choice(engine, choice_id: int, status: str, actor: str, picked: str = None) -> None:
    """status: 'kept' (what was in effect stays) or 'switched' (another option was taken); picked: that option."""
    with engine.begin() as conn:
        if picked:
            conn.execute(text("UPDATE dbo.import_choices SET alternative = :p WHERE choice_id = :i"), {"p": picked, "i": choice_id})
        r = conn.execute(text("SELECT label, applied, alternative FROM dbo.import_choices WHERE choice_id = :i"),
                         {"i": choice_id}).mappings().first()
        conn.execute(text("UPDATE dbo.import_choices SET status = :s, decided_by = :a, decided_at = SYSUTCDATETIME() "
                          "WHERE choice_id = :i"), {"s": status, "a": actor, "i": choice_id})
        if r:
            log_activity(conn, actor, "Department Review",
                         "Workbook vs app — chose: " + (r["alternative"] or r["applied"] or ""), r["label"],
                         via="Department workbook upload")


def reopen_import_choice(engine, choice_id: int) -> None:
    with engine.begin() as conn:
        conn.execute(text("UPDATE dbo.import_choices SET status = 'open', decided_by = NULL, decided_at = NULL "
                          "WHERE choice_id = :i"), {"i": choice_id})


def undo_old_workbook_import(engine, actor: str) -> dict:
    """Takes back everything the import staged and undoes the moves it made —
    and nothing anyone else did: a group someone else has since voted on or
    staged on is left as it is (and reported), and a move is only undone
    while it's still the group's latest step. Its top-bar Undo steps are
    cleared too. Returns counts, plus what was left alone and why."""
    done = {"groups": 0, "items": 0, "overrides": 0, "moves": 0, "kept": []}
    with engine.begin() as conn:
        n = {"n": IMPORT_NOTE_LIKE}
        conn.execute(text("DELETE FROM dbo.import_choices"))  # the import's questions go with it
        labels = _combo_labels(conn, [r[0] for r in conn.execute(text(
            "SELECT combo_id FROM dbo.dept_mapping_pending_changes WHERE origin_note LIKE :n "
            "UNION SELECT combo_id FROM dbo.dept_mapping_recent_moves WHERE origin_note LIKE :n"), n).all()])
        name = lambda cid: labels[cid][0] if cid in labels else f"group {cid}"
        # 1. staged group decisions (and the importer's own vote on them)
        for cid, stager in conn.execute(text(
                "SELECT combo_id, staged_by FROM dbo.dept_mapping_pending_changes WHERE origin_note LIKE :n"), n).all():
            others = conn.execute(text("SELECT COUNT(*) FROM dbo.dept_mapping_combo_suggestions "
                                       "WHERE combo_id = :c AND staged_by <> :s"), {"c": cid, "s": stager}).scalar()
            if others:
                done["kept"].append(f"{name(cid)}: others have voted on it since")
                continue
            conn.execute(text("DELETE FROM dbo.dept_mapping_pending_changes WHERE combo_id = :c"), {"c": cid})
            conn.execute(text("DELETE FROM dbo.dept_mapping_combo_suggestions WHERE combo_id = :c"), {"c": cid})
            conn.execute(text("DELETE FROM dbo.dept_mapping_undo_requests WHERE entity_type = 'combo' AND entity_id = :c"),
                         {"c": str(cid)})
            done["groups"] += 1
        # 2. staged Broken Out item decisions and 3. staged UPC overrides
        done["items"] = conn.execute(text(
            "DELETE FROM dbo.dept_mapping_pending_upc_changes WHERE origin_note LIKE :n"), n).rowcount
        done["overrides"] = conn.execute(text(
            "DELETE FROM dbo.item_master_pending_changes WHERE origin_note LIKE :n"), n).rowcount
        # 4. its moves — only while they're still the group's latest steps
        moved = {}
        for mid, cid, note, snap in conn.execute(text(
                "SELECT move_id, combo_id, origin_note, snapshot_json FROM dbo.dept_mapping_recent_moves "
                "ORDER BY move_id DESC")).all():
            moved.setdefault(cid, []).append((mid, note, snap))
        for cid, stack in moved.items():
            ours = [m for m in stack if m[1] and m[1].startswith(IMPORT_NOTE_LIKE[:-1])]
            if not ours:
                continue
            if stack[0][1] is None or not stack[0][1].startswith(IMPORT_NOTE_LIKE[:-1]):
                done["kept"].append(f"{name(cid)}: moved again since the import")
                continue
            busy = conn.execute(text(
                "SELECT (SELECT COUNT(*) FROM dbo.dept_mapping_pending_changes WHERE combo_id = :c) + "
                "(SELECT COUNT(*) FROM dbo.dept_mapping_pending_upc_changes WHERE combo_id = :c) + "
                "(SELECT COUNT(*) FROM dbo.dept_mapping_combo_suggestions WHERE combo_id = :c)"), {"c": cid}).scalar()
            if busy:
                done["kept"].append(f"{name(cid)}: someone has staged work on it since")
                continue
            _lock_combo(conn, cid)
            _restore_combo_snapshot(conn, cid, json.loads(ours[-1][2]), actor)
            conn.execute(text("DELETE FROM dbo.dept_mapping_recent_moves WHERE move_id IN :ids")
                         .bindparams(bindparam("ids", expanding=True)), {"ids": [m[0] for m in ours]})
            done["moves"] += 1
        # its top-bar Undo steps no longer apply
        conn.execute(text("DELETE FROM dbo.dept_mapping_action_log WHERE click_id LIKE 'oldwb-%'"))
        _clear_dept_push_approvals(conn)
    return done


# ---------------------------------------------------------------------------
# Settings requests: editors can't change Department Review settings, but can
# ask an admin to — add a Department, add a Strict Department, or set (or
# remove) an Unmatched Department Default. An admin approves (the change is
# made) or denies, with a note; either can be undone back to "waiting".
# ---------------------------------------------------------------------------
SETTINGS_REQUEST_KINDS = {
    "add_department": "Add a Department",
    "add_strict": "Add a Strict Department",
    "unmatched_default": "Set an Unmatched Department Default",
}
_SETTINGS_REQUESTS_DDL = """
IF NOT EXISTS (SELECT * FROM sys.tables WHERE name = 'dept_settings_requests')
BEGIN
    CREATE TABLE dbo.dept_settings_requests (
        request_id    INT IDENTITY(1,1) NOT NULL,
        kind          VARCHAR(30) NOT NULL,
        payload       NVARCHAR(MAX) NOT NULL,
        reason        NVARCHAR(1000) NULL,
        requested_by  NVARCHAR(200) NOT NULL,
        requested_at  DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME(),
        status        VARCHAR(20) NOT NULL DEFAULT 'pending',
        decided_by    NVARCHAR(200) NULL,
        decided_at    DATETIME2 NULL,
        admin_note    NVARCHAR(1000) NULL,
        undo_json     NVARCHAR(MAX) NULL,
        CONSTRAINT PK_dept_settings_requests PRIMARY KEY (request_id)
    );
END
"""


def ensure_settings_requests_table(engine) -> None:
    with engine.begin() as conn:
        conn.execute(text(_SETTINGS_REQUESTS_DDL))


def describe_settings_request(kind: str, p: dict) -> str:
    if kind == "add_department":
        return f"Add Department **{p.get('department')}**"
    src = "any source" if p.get("source_key") in (None, "", "any") else str(p["source_key"]).upper()
    if kind == "add_strict":
        return (f"Add Strict Department **{p.get('old_department')}** ({src})"
                + (" — trust direct evidence" if p.get("trust_direct_evidence") else ""))
    if not p.get("new_department"):
        return f"Remove the Unmatched Default for **{p.get('old_department')}** ({src})"
    return f"Unmatched Default: **{p.get('old_department')}** ({src}) → **{p.get('new_department')}**"


def _settings_request_problem(conn, kind: str, p: dict) -> str | None:
    """Why this can't be asked for / applied right now (None = fine)."""
    if kind == "add_department":
        d = (p.get("department") or "").strip().upper()
        if not d:
            return "Type the Department."
        if conn.execute(text("SELECT 1 FROM dbo.dept_mapping_departments WHERE department = :d"), {"d": d}).first():
            return f"{d} is already a Department."
    elif kind == "add_strict":
        if not (p.get("old_department") or "").strip():
            return "Type the Department text exactly as the distributor's file has it."
        if conn.execute(text("SELECT 1 FROM dbo.dept_mapping_strict_departments WHERE source_key = :s AND old_department = :o"),
                        {"s": p.get("source_key") or "any", "o": p["old_department"].strip()}).first():
            return "That's already a Strict Department."
    elif kind == "unmatched_default":
        if not (p.get("old_department") or "").strip():
            return "Type the Department text exactly as the distributor's file has it."
        cur = conn.execute(text("SELECT new_department FROM dbo.dept_mapping_unmatched_defaults WHERE source_key = :s "
                                "AND old_department = :o"), {"s": p.get("source_key"), "o": p["old_department"].strip()}).scalar()
        if (cur or None) == (p.get("new_department") or None):
            return "That's already how it's set." if cur else "There's no default for that to remove."
        if p.get("new_department") and not conn.execute(text("SELECT 1 FROM dbo.dept_mapping_departments WHERE department = :d"),
                                                        {"d": p["new_department"]}).first():
            return f"{p['new_department']} isn't a Department (request it first)."
    else:
        return "Unknown kind of request."
    return None


def create_settings_request(engine, kind: str, payload: dict, reason: str, actor: str) -> dict:
    ensure_settings_requests_table(engine)
    with engine.begin() as conn:
        problem = _settings_request_problem(conn, kind, payload)
        if problem:
            return {"ok": False, "problem": problem}
        dup = conn.execute(text("SELECT request_id FROM dbo.dept_settings_requests WHERE status = 'pending' AND kind = :k AND payload = :p"),
                           {"k": kind, "p": json.dumps(payload, sort_keys=True)}).scalar()
        if dup:
            return {"ok": False, "problem": f"That's already been asked for (request #{dup}) and is waiting on an admin."}
        rid = conn.execute(text(
            "INSERT INTO dbo.dept_settings_requests (kind, payload, reason, requested_by) OUTPUT INSERTED.request_id "
            "VALUES (:k, :p, :r, :a)"), {"k": kind, "p": json.dumps(payload, sort_keys=True), "r": reason or None, "a": actor}).scalar()
        log_activity(conn, actor, "Settings", f"Requested: {describe_settings_request(kind, payload).replace('**', '')}",
                     f"Request #{rid}", details={"reason": reason})
    return {"ok": True, "request_id": rid}


def list_settings_requests(engine, status: str | None = None, requested_by: str | None = None) -> list:
    try:
        with engine.connect() as conn:
            sql = "SELECT * FROM dbo.dept_settings_requests WHERE 1 = 1"
            params = {}
            if status:
                sql += " AND status = :st"; params["st"] = status
            if requested_by:
                sql += " AND requested_by = :a"; params["a"] = requested_by
            rows = [dict(r) for r in conn.execute(text(sql + " ORDER BY request_id DESC"), params).mappings().all()]
    except Exception:
        return []
    for r in rows:
        r["payload"] = json.loads(r["payload"])
        r["summary"] = describe_settings_request(r["kind"], r["payload"])
    return rows


def withdraw_settings_request(engine, request_id: int, actor: str) -> bool:
    with engine.begin() as conn:
        ok = conn.execute(text("UPDATE dbo.dept_settings_requests SET status = 'withdrawn', decided_by = :a, decided_at = SYSUTCDATETIME() "
                               "WHERE request_id = :r AND requested_by = :a AND status = 'pending'"),
                          {"r": request_id, "a": actor}).rowcount == 1
        if ok:
            log_activity(conn, actor, "Settings", "Withdrew a request", f"Request #{request_id}")
        return ok


def approve_settings_request(engine, request_id: int, admin: str, note: str | None = None) -> dict:
    """Makes the change and records how to take it back. Returns
    {"ok", "kind", "problem"}; a Strict Department or Unmatched Default
    only matters once the Department engine re-runs (the caller does that)."""
    with engine.begin() as conn:
        r = conn.execute(text("SELECT * FROM dbo.dept_settings_requests WITH (UPDLOCK) WHERE request_id = :r"),
                         {"r": request_id}).mappings().first()
        if not r or r["status"] != "pending":
            return {"ok": False, "problem": "This request isn't waiting any more (someone already handled it)."}
        kind, p = r["kind"], json.loads(r["payload"])
        problem = _settings_request_problem(conn, kind, p)
        if problem:
            return {"ok": False, "problem": problem}
        undo = {}
        if kind == "add_department":
            d = p["department"].strip().upper()
            conn.execute(text("INSERT INTO dbo.dept_mapping_departments (department, source_type) VALUES (:d, 'manual')"), {"d": d})
            undo = {"remove_department": d}
        elif kind == "add_strict":
            conn.execute(text("INSERT INTO dbo.dept_mapping_strict_departments (source_key, old_department, trust_direct_evidence, updated_by) "
                              "VALUES (:s, :o, :t, :a)"), {"s": p.get("source_key") or "any", "o": p["old_department"].strip(),
                                                           "t": bool(p.get("trust_direct_evidence")), "a": admin})
            undo = {"remove_strict": [p.get("source_key") or "any", p["old_department"].strip()]}
        else:
            s_, o = p.get("source_key"), p["old_department"].strip()
            prev = conn.execute(text("SELECT new_department FROM dbo.dept_mapping_unmatched_defaults WHERE source_key = :s "
                                     "AND old_department = :o"), {"s": s_, "o": o}).scalar()
            conn.execute(text("DELETE FROM dbo.dept_mapping_unmatched_defaults WHERE source_key = :s AND old_department = :o"),
                         {"s": s_, "o": o})
            if p.get("new_department"):
                conn.execute(text("INSERT INTO dbo.dept_mapping_unmatched_defaults (source_key, old_department, new_department, updated_by) "
                                  "VALUES (:s, :o, :n, :a)"), {"s": s_, "o": o, "n": p["new_department"], "a": admin})
            undo = {"restore_default": [s_, o, prev]}
        conn.execute(text("UPDATE dbo.dept_settings_requests SET status = 'approved', decided_by = :a, decided_at = SYSUTCDATETIME(), "
                          "admin_note = :n, undo_json = :u WHERE request_id = :r"),
                     {"a": admin, "n": note or None, "u": json.dumps(undo), "r": request_id})
        log_activity(conn, admin, "Settings", f"Approved: {describe_settings_request(kind, p).replace('**', '')}",
                     f"Request #{request_id} from {r['requested_by']}")
    return {"ok": True, "kind": kind}


def deny_settings_request(engine, request_id: int, admin: str, note: str | None = None) -> bool:
    with engine.begin() as conn:
        ok = conn.execute(text("UPDATE dbo.dept_settings_requests SET status = 'denied', decided_by = :a, decided_at = SYSUTCDATETIME(), "
                               "admin_note = :n WHERE request_id = :r AND status = 'pending'"),
                          {"a": admin, "n": note or None, "r": request_id}).rowcount == 1
        if ok:
            log_activity(conn, admin, "Settings", "Denied a request" + (f" — “{note}”" if note else ""), f"Request #{request_id}")
        return ok


def undo_settings_request_decision(engine, request_id: int, admin: str) -> dict:
    """Back to waiting: a denial is simply reopened; an approval's change is
    taken back first (the Department / Strict Department removed, the
    Unmatched Default put back as it was)."""
    with engine.begin() as conn:
        r = conn.execute(text("SELECT * FROM dbo.dept_settings_requests WITH (UPDLOCK) WHERE request_id = :r"),
                         {"r": request_id}).mappings().first()
        if not r or r["status"] not in ("approved", "denied"):
            return {"ok": False, "problem": "Only an approved or denied request can be undone."}
        undo = json.loads(r["undo_json"] or "{}") if r["status"] == "approved" else {}
        if "remove_department" in undo:
            conn.execute(text("DELETE FROM dbo.dept_mapping_departments WHERE department = :d AND source_type = 'manual'"),
                         {"d": undo["remove_department"]})
        if "remove_strict" in undo:
            conn.execute(text("DELETE FROM dbo.dept_mapping_strict_departments WHERE source_key = :s AND old_department = :o"),
                         {"s": undo["remove_strict"][0], "o": undo["remove_strict"][1]})
        if "restore_default" in undo:
            s_, o, prev = undo["restore_default"]
            conn.execute(text("DELETE FROM dbo.dept_mapping_unmatched_defaults WHERE source_key = :s AND old_department = :o"),
                         {"s": s_, "o": o})
            if prev:
                conn.execute(text("INSERT INTO dbo.dept_mapping_unmatched_defaults (source_key, old_department, new_department, updated_by) "
                                  "VALUES (:s, :o, :n, :a)"), {"s": s_, "o": o, "n": prev, "a": admin})
        conn.execute(text("UPDATE dbo.dept_settings_requests SET status = 'pending', decided_by = NULL, decided_at = NULL, "
                          "admin_note = NULL, undo_json = NULL WHERE request_id = :r"), {"r": request_id})
        log_activity(conn, admin, "Settings", "Undid the approval of a request" if r["status"] == "approved"
                     else "Undid the denial of a request", f"Request #{request_id}")
    return {"ok": True, "kind": r["kind"], "was": r["status"]}
