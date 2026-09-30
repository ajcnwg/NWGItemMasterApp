"""Part 6: groups per page + page buttons (top and bottom, every tab),
shared vs locked filters, notification search/type filter, workbench
typing matcher, viewer role."""
from uiharness import *
from itemmaster import dept_mapping as dm
from itemmaster.db import get_engine

E = get_engine()
FAIL = []


def check(ok, msg):
    print(("  PASS " if ok else "  FAIL ") + msg)
    if not ok:
        FAIL.append(msg)


def cards(at, key_prefix):
    return [b for b in at.button if (b.key or "").startswith(key_prefix)]


jason = session("jason"); run(jason, "load")
tabs = {"Crosswalk": ("dept_review_review", "dept_review_page_num_review", "approve_review_"),
        "Unmatched": ("dept_review_unmatched", "dept_review_page_num_unmatched", "approve_unmatched_"),
        "Broken Out": ("broken_out", "broken_out_page_num", "revert_broken_out_"),
        "Decided": ("decided", "decided_page_num", "revert_decided_")}
for sub, (tk, pnk, card) in tabs.items():
    goto(jason, "Department Review", sub)
    size = jason.selectbox(key=f"{tk}_page_size")
    check(size.options == ["10", "25", "50", "100"] and size.value == 25, f"{sub}: page sizes 10/25/50/100, default 25 ({size.options}, {size.value})")
    n = len([b for b in cards(jason, card) if not b.key.startswith("revert_decided_queue_")])
    check(n == 25 or n < 25, f"{sub}: 25 groups on page 1 ({n})")
    size.select(10); run(jason, f"{sub}: 10 per page")
    n10 = len([b for b in cards(jason, card) if not b.key.startswith("revert_decided_queue_")])
    check(n10 == min(10, n) if n >= 10 else n10 == n, f"{sub}: 10 groups shown ({n10})")
    pg = jason.number_input(key=pnk)
    first = [b.key for b in cards(jason, card)][:3]
    if pg.proto.max > 1:
        pg.increment(); run(jason, f"{sub}: page 2 (top)")
        second = [b.key for b in cards(jason, card)][:3]
        check(first != second, f"{sub}: page 2 shows different groups")
        bottom = jason.number_input(key=f"{tk}_page_size_bottom".replace("_page_size_bottom", "") + "_bottom") if False else None
        bnum = [x for x in jason.number_input if x.key == f"{pnk}_bottom"]
        check(bnum and bnum[0].value == 2, f"{sub}: bottom Page box follows the top one ({bnum and bnum[0].value})")
        bnum[0].set_value(1); run(jason, f"{sub}: bottom -> page 1")
        check(jason.number_input(key=pnk).value == 1 and [b.key for b in cards(jason, card)][:3] == first, f"{sub}: bottom page box works")
        bsize = [x for x in jason.selectbox if x.key == f"{tk}_page_size_bottom"]
        bsize[0].select(50); run(jason, f"{sub}: bottom -> 50 per page")
        check(jason.selectbox(key=f"{tk}_page_size").value == 50, f"{sub}: bottom page-size box drives the top one")
    jason.selectbox(key=f"{tk}_page_size").select(25); run(jason, "back to 25")

# shared filter: search on Crosswalk carries to Decided unless locked
goto(jason, "Department Review", "Crosswalk")
jason.text_input(key="dept_review_review_search").input("kehe"); run(jason, "search kehe on Crosswalk")
goto(jason, "Department Review", "Decided")
check(jason.text_input(key="decided_search").value == "kehe", "search carries across tabs")
jason.checkbox(key="decided_lock").check(); run(jason, "lock Decided")
jason.text_input(key="decided_search").input("urm"); run(jason, "search urm on Decided")
goto(jason, "Department Review", "Crosswalk")
check(jason.text_input(key="dept_review_review_search").value == "kehe", "locked tab's search doesn't leak back")

# notifications: search + type filter only once the list is long (or on Team)
jason = session("jason"); run(jason, "reload")
n_notes = sum(len(x) for x in dm.get_notifications(E, "Jason", dm.get_last_seen(E, "Jason")).values())
has_search = any(t.key == "notif_search" for t in jason.sidebar.text_input)
check(has_search == (n_notes > 8), f"search/type filters only with more than 8 notifications ({n_notes}, shown={has_search})")
aj = session("aj"); run(aj, "AJ")
aj.sidebar.segmented_control(key="notif_view").set_value("Team"); run(aj, "Team view")
kinds = aj.sidebar.selectbox(key="notif_kind").options
print("  type filter:", kinds)
check(len(kinds) == 9, "type filter lists every kind")
jason.button(key="topbar_bell").click(); run(jason, "bell hides sidebar")
check(not any(m.value.startswith("### Notifications") for m in jason.sidebar.markdown), "sidebar hidden")
jason.button(key="topbar_bell").click(); run(jason, "bell shows sidebar")
check(any(m.value.startswith("### Notifications") for m in jason.sidebar.markdown), "sidebar back")

# workbench typing matcher (runs the app's own function)
import ast
src = open(__import__("os").path.join(__import__("os").path.dirname(__import__("os").path.dirname(__import__("os").path.abspath(__file__))), "app.py"), encoding="utf-8").read()
node = next(n for n in ast.parse(src).body if isinstance(n, ast.FunctionDef) and n.name == "match_department")
ns = {}
exec(compile(ast.Module([node], []), "match", "exec"), ns)
md = ns["match_department"]
opts = dm.get_departments(E)["department"].tolist()
for typed, want in [("grocery", "GROCERY"), ("gro", "GROCERY"), ("  Frozen ", "FROZEN"), ("froz", "FROZEN"),
                    ("xyz", None), ("", None), ("CARE", None)]:
    got = md(typed, opts)
    check(got == want, f"typing {typed!r} -> {got!r}")

# viewer: read-only Item Master only
v = session("viewer"); run(v, "viewer")
check(v.radio(key="active_tab").options == ["Item Master"], "viewer sees only Item Master")
check(not [b for b in v.button if (b.key or "").startswith("topbar_")], "viewer has no undo/redo/bell")

print("FAILURES:", len(FAIL))
for f in FAIL:
    print(" -", f)
