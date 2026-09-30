import json
from datetime import datetime, timedelta
from uiharness import *
from itemmaster import dept_mapping as dm
from itemmaster.db import get_engine
from sqlalchemy import text
E = get_engine(); F = []
def check(ok, msg):
    print(("  PASS " if ok else "  FAIL ") + msg)
    if not ok: F.append(msg)
now = datetime.utcnow()
dt = lambda m: {"__dt": (now - timedelta(minutes=m)).isoformat()}
step = lambda lbl, m, f=None: {"changes": {"bo_1": {"before": {"1": ""}, "after": {"1": "X"}}}, "where": "t", "label": lbl, "at": dt(m), "file": f}
steps = [step(f"3h old {i}", 180) for i in range(5)] + [step(f"recent {i}", 10, {"stem": "s", "name": f"f{i}.xlsx", "bytes": {"__b64": "AAAA"}} if i >= 50 else None) for i in range(56)]
with E.begin() as c:
    c.execute(text("DELETE FROM dbo.user_workspace WHERE username IN ('Jason','Kristi')"))
    c.execute(text("DELETE FROM dbo.dept_mapping_action_log WHERE actor IN ('Jason','Kristi')"))
    c.execute(text("INSERT INTO dbo.user_workspace (username, item_key, payload) VALUES ('Jason', '_draft_undo', :p)"), {"p": json.dumps(steps)})
    c.execute(text("INSERT INTO dbo.user_workspace (username, item_key, payload, updated_at) VALUES ('Jason', 'wb_bo_999999', '{\"draft\": {\"1\": \"X\"}, \"ver\": 0, \"filter\": \"\", \"show\": \"All\"}', DATEADD(hour, -3, SYSUTCDATETIME()))"))
    c.execute(text("INSERT INTO dbo.dept_mapping_action_log (actor, combo_id, label, description, before_json, after_json, created_at) VALUES "
                   "('Jason', 1, 'old', 'old', '{}', '{}', DATEADD(hour, -3, SYSUTCDATETIME())), ('Jason', 1, 'new', 'new', '{}', '{}', DATEADD(minute, -10, SYSUTCDATETIME())), "
                   "('Kristi', 1, 'k', 'k', '{}', '{}', SYSUTCDATETIME())"))
    c.execute(text("INSERT INTO dbo.user_workspace (username, item_key, payload) VALUES ('Kristi', 'wb_bo_888888', '{\"draft\": {}, \"ver\": 0, \"filter\": \"\", \"show\": \"All\"}')"))
j = session("jason"); run(j, "open the app (refresh)")
u = j.session_state["_draft_undo"] if "_draft_undo" in j.session_state else []
check(len(u) == 50 and all(e["label"].startswith("recent") for e in u), f"grid Undo: steps idle 2h+ dropped, newest 50 recent kept ({len(u)})")
check(sum(1 for e in u if e.get("file")) == 3, "only the 3 newest steps keep an Excel file")
with E.connect() as c:
    check(c.execute(text("SELECT COUNT(*) FROM dbo.user_workspace WHERE username='Jason' AND item_key='wb_bo_999999'")).scalar() == 0,
          "grid work idle 2h+ removed")
    check(c.execute(text("SELECT label FROM dbo.dept_mapping_action_log WHERE actor='Jason'")).scalars().all() == ["new"],
          "top-bar Undo: 3-hour-old step gone, 10-minute-old step kept")
lo = next(b for b in j.button if b.key in ("logout_btn", "Logout"))
lo.click(); run(j, "Logout")
with E.connect() as c:
    check(c.execute(text("SELECT COUNT(*) FROM dbo.dept_mapping_action_log WHERE actor='Jason'")).scalar() == 0
          and c.execute(text("SELECT COUNT(*) FROM dbo.user_workspace WHERE username='Jason'")).scalar() == 0,
          "logout clears Jason's undo history and grid work")
    check(c.execute(text("SELECT COUNT(*) FROM dbo.dept_mapping_action_log WHERE actor='Kristi'")).scalar() == 1
          and c.execute(text("SELECT COUNT(*) FROM dbo.user_workspace WHERE username='Kristi'")).scalar() == 1,
          "Kristi's is untouched")
with E.begin() as c:
    c.execute(text("DELETE FROM dbo.user_workspace WHERE username IN ('Jason','Kristi')"))
    c.execute(text("DELETE FROM dbo.dept_mapping_action_log WHERE actor IN ('Jason','Kristi')"))
print("FAILURES:", len(F))
