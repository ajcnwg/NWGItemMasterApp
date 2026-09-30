from uiharness import *
for u in ["aj", "jason", "viewer"]:
    print("==", u)
    at = session(u)
    run(at, "first load")
    tabs = at.radio(key="active_tab").options
    print("  tabs:", tabs)
    print("  topbar buttons:", [b.label for b in at.button if b.key and b.key.startswith("topbar")])
    print("  sidebar:", [e.value for e in at.sidebar.markdown][:3])
    if u != "viewer":
        for sub in ["Crosswalk", "Unmatched", "Broken Out", "Pending Changes", "Decided", "Settings"]:
            goto(at, "Department Review", sub)
    for t in tabs:
        if t != "Department Review":
            goto(at, t)
