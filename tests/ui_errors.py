from uiharness import *
from itemmaster import dept_mapping as dm
from itemmaster.db import get_engine
from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError, OperationalError
E = get_engine(); F = []
def check(ok, msg):
    print(("  PASS " if ok else "  FAIL ") + msg)
    if not ok: F.append(msg)
real = dm.get_review_queue
def boom_bug(*a, **k): raise ValueError("test bug: something unexpected")
def boom_fw(*a, **k): raise ProgrammingError("SELECT", {}, Exception("(40615) Client with IP address '1.2.3.4' is not allowed to access the server."))
def boom_wake(*a, **k): raise OperationalError("SELECT", {}, Exception("[08001] TCP Provider: Timeout error [258]. Login timeout expired"))
before = max([e["error_id"] for e in dm.list_app_errors(E, True)] or [0])
try:
    for name, fn, title in [("bug", boom_bug, "Something went wrong"), ("firewall", boom_fw, "Can't reach the database from this network"),
                            ("waking", boom_wake, "Still connecting to the database")]:
        dm.get_review_queue = fn
        j = session("jason"); run(j, "load")
        try:
            goto(j, "Department Review", "Crosswalk")
        except Exception as ex:
            print("   goto raised", ex)
        heads = [m.value for m in j.markdown if m.value.startswith("### ")]
        check(any(title in h for h in heads), f"{name}: friendly card '{title}' ({heads})")
        check(len(j.exception) == 0, f"{name}: no red error box / traceback")
        check(not any(e.label.startswith("Details") for e in j.expander), f"{name}: editor doesn't see the raw details")
        if name == "firewall":
            check(any("1.2.3.4" in m.value for m in j.markdown), "firewall card names the blocked address")
    dm.get_review_queue = real
    new = [e for e in dm.list_app_errors(E) if e["error_id"] > before]
    check(len(new) == 1 and new[0]["username"] == "Jason" and "Department Review / Crosswalk" in new[0]["where_in_app"]
          and "test bug" in new[0]["message"] and "Traceback" in new[0]["details"],
          f"only the real bug is recorded, with who/where/traceback ({[(e['username'], e['where_in_app'], e['message']) for e in new]})")
    a = session("aj"); run(a, "admin load")
    bell = a.button(key="topbar_bell").label
    check(bell != "🔔", f"admin bell counts the error ({bell})")
    check(any("App errors (1)" in m.value for m in a.sidebar.markdown), "admin sees 'App errors (1)' under the bell")
    a.button(key=f"app_error_fix_{new[0]['error_id']}").click(); run(a, "mark fixed")
    check(not any(e["error_id"] == new[0]["error_id"] for e in dm.list_app_errors(E)), "Mark fixed clears it")
finally:
    dm.get_review_queue = real
    with E.begin() as c:
        c.execute(text("DELETE FROM dbo.app_errors WHERE error_id > :b"), {"b": before})
print("FAILURES:", len(F))
