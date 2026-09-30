"""#0 + the V2 old-workbook import, kept as a temporary snapshot for the tests."""
import io, os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from itemmaster.db import get_engine
from itemmaster import dept_mapping as dm, old_workbook_import as owi
from testbase import BASE, SP, V2

E = get_engine()
dm.restore_snapshot(E, BASE, "AJ")
ex = owi.extract(owi.read_workbook(io.BytesIO(open(V2, "rb").read())))
p = owi.plan(E, ex, dm.get_departments(E).iloc[:, 0].tolist(), "AJ")
r = owi.apply(E, p, "AJ", True, "department_workbook V2.xlsx")
print("applied:", r)
print("summary:", dm.old_workbook_import_summary(E))
sid = dm.take_snapshot(E, "AJ", label="TEST — old-workbook import state (deleted after the tests)", kind="manual")
open(os.path.join(SP, "import_snap.txt"), "w").write(str(sid))
print("import snapshot:", sid)
