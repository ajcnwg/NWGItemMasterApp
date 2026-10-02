"""Drives the real app.py headlessly as a given account (session pre-marked
as signed in — no password is ever typed), for UI tests."""
import os
import sys
import time

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(APP_DIR)
sys.path.insert(0, APP_DIR)

from streamlit.testing.v1 import AppTest  # noqa: E402

USERS = {"aj": "AJ", "jason": "Jason", "kristi": "Kristi", "eric": "Eric", "viewer": "Viewer Demo"}


def session(username: str, timeout: int = 300) -> AppTest:
    at = AppTest.from_file(os.path.join(APP_DIR, "app.py"), default_timeout=timeout)
    at.session_state["authentication_status"] = True
    at.session_state["username"] = username
    at.session_state["name"] = USERS[username]
    at.session_state["logout"] = False
    return at


def run(at: AppTest, label: str = "") -> AppTest:
    t = time.time()
    at.run()
    dt = time.time() - t
    if at.exception:
        print(f"  !! EXCEPTION after {label}:")
        for e in at.exception:
            print("    ", e.message)
            print("    ", "\n".join(e.stack_trace[-8:]) if e.stack_trace else "")
    if label:
        print(f"  [{dt:5.1f}s] {label}")
    return at


def goto(at: AppTest, tab: str, sub: str = None) -> AppTest:
    at.session_state["active_tab"] = tab
    if sub:
        at.session_state["dept_review_subtab"] = sub
    return run(at, f"open {tab}" + (f" / {sub}" if sub else ""))


def buttons(at: AppTest, contains: str = ""):
    return [b for b in at.button if contains.lower() in (b.label or "").lower()]


def btn(at: AppTest, key: str):
    return at.button(key=key)


def texts(at: AppTest) -> str:
    out = []
    for kind in ("markdown", "caption", "info", "warning", "error", "success", "toast", "subheader", "title"):
        try:
            out += [e.value for e in getattr(at, kind)]
        except Exception:
            pass
    return "\n".join(str(x) for x in out)


def walk(node):
    """Every element under node, in page order."""
    kids = getattr(node, "children", None)
    if not kids:
        yield node
        return
    for k in sorted(kids):
        yield from walk(kids[k])


def view_button_for(at, title_contains):
    """The 'View on …' button of the sidebar card whose title contains text."""
    last_title = None
    for el in walk(at.sidebar):
        t = getattr(el, "type", "")
        if t == "markdown" and el.value.startswith(("**", ":blue[New]")):
            last_title = el.value
        if t == "button" and ((el.label or "").startswith("View on") or el.label == "Open") and last_title and title_contains in last_title:
            return el
    return None


# ---- grid (st.data_editor) cell edits, sent the way the browser sends them
import json as _json
from streamlit.testing.v1 import element_tree as _et
from streamlit.proto.WidgetStates_pb2 import WidgetState as _WS

GRID_EDITS = {}  # element id -> {"edited_rows": {...}}
_orig_gws = _et.get_widget_state


def _gws(node):
    if getattr(node, "type", "") == "dataframe":
        pid = getattr(node.proto, "id", "")
        if pid in GRID_EDITS:
            w = _WS(id=pid)
            w.string_value = _json.dumps(GRID_EDITS[pid])
            return w
    return _orig_gws(node)


_et.get_widget_state = _gws


def grid(at, key):
    for el in walk(at.main):
        if getattr(el, "type", "") == "dataframe" and getattr(el, "key", None) == key:
            return el
    raise KeyError(key)


def edit_grid(at, key, rows: dict):
    """rows: {row position in the shown grid: {column: value}} — like typing/picking/ticking in the grid."""
    el = grid(at, key)
    cur = GRID_EDITS.setdefault(el.proto.id, {"edited_rows": {}, "added_rows": [], "deleted_rows": []})
    for r, vals in rows.items():
        cur["edited_rows"].setdefault(str(r), {}).update(vals)


def grid_key(at, prefix):
    """The key of the grid whose key starts with prefix (tick tables add a
    suffix naming the rows they show), or None."""
    for el in walk(at.main):
        if getattr(el, "type", "") == "dataframe" and (getattr(el, "key", None) or "").startswith(prefix):
            return el.key
    return None


def tick_upcs(at, prefix, column, upcs) -> list:
    """Ticks `column` on the rows of these UPCs in the tick table `prefix`;
    returns the UPCs it found to tick."""
    key = grid_key(at, prefix)
    if key is None:
        return []
    shown = list(grid(at, key).value["UPC"])
    found = [u for u in upcs if u in shown]
    edit_grid(at, key, {shown.index(u): {column: True} for u in found})
    return found
