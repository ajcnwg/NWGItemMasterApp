"""Merge → "Check the item master against every rule and decision": on the
baseline every item is already in line, so it finds nothing to fix."""
from uiharness import *
F = []
aj = session("aj"); run(aj, "load"); goto(aj, "Merge")
aj.button(key="rules_check_btn").click(); run(aj, "Check now")
res = [x.value for x in list(aj.success) + list(aj.warning)]
print("  result:", res[:2])
ok = any("Everything matches" in r for r in res) and not aj.exception
print(("  PASS " if ok else "  FAIL ") + "the check runs and finds every item in line with the rules and decisions")
print("FAILURES:", 0 if ok else 1)
