"""Settings roundtrip: close() -> closeEvent -> _save_settings, then a
fresh MainWindow must read them back. Redirects QSettings to a scratch
dir so the real %APPDATA% ini is never touched."""

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import daub_gui as dg  # noqa: E402
from PySide6.QtCore import QSettings  # noqa: E402

app = dg.QApplication(sys.argv)
scratch = os.path.join(os.environ.get("TEMP", "/tmp"), "daub_gui_ini_test")
QSettings.setPath(QSettings.IniFormat, QSettings.UserScope, scratch)
QSettings.setDefaultFormat(QSettings.IniFormat)
app.setApplicationName("daub_gui")
app.setOrganizationName("daub")

w1 = dg.MainWindow()
w1.ed_out.setText(r"C:\出图\中文目录")
w1.ck_kra.setChecked(True)
w1.sp_conc.setValue(2)
w1.close()   # -> closeEvent -> _save_settings

ini = os.path.join(scratch, "daub", "daub_gui.ini")
print("ini written:", os.path.isfile(ini), flush=True)

w2 = dg.MainWindow()
ok = (w2.ed_out.text() == r"C:\出图\中文目录"
      and w2.ck_kra.isChecked() and w2.sp_conc.value() == 2)
print(("PASS settings roundtrip" if ok else "FAIL settings roundtrip"),
      "| out=%r kra=%s conc=%d"
      % (w2.ed_out.text(), w2.ck_kra.isChecked(), w2.sp_conc.value()),
      flush=True)
