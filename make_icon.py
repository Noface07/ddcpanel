import sys
from PySide6.QtGui import QGuiApplication

app = QGuiApplication(sys.argv)
from ddcpanel import make_icon  # noqa: E402

make_icon().pixmap(64, 64).save("icon.ico")
print("wrote icon.ico")