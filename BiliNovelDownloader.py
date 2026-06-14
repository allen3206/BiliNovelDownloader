import sys
import ctypes
import importlib
import tkinter as tk
from tkinter import messagebox

try:
    # 讓 Windows 知道這個程式支援高解析度縮放，避免字體模糊
    ctypes.windll.shcore.SetProcessDpiAwareness(1)
except Exception:
    pass

# 啟動前檢查第三方套件，缺少時一次列出並友善退出
# lxml 為選用（fetch_info 已有 html.parser fallback）
def _check_dependencies():
    required = {
        'requests': 'requests', 'PIL': 'Pillow', 'psutil': 'psutil',
        'opencc': 'opencc', 'bs4': 'beautifulsoup4',
    }
    missing = []
    for mod, pkg in required.items():
        try:
            importlib.import_module(mod)
        except ImportError:
            missing.append(pkg)
    return missing

_missing_deps = _check_dependencies()
if _missing_deps:
    _root = tk.Tk()
    _root.withdraw()
    messagebox.showerror(
        "缺少必要套件",
        "無法啟動，缺少以下套件：\n\n"
        + "\n".join(f"　• {p}" for p in _missing_deps)
        + "\n\n請在終端機安裝後再開啟：\npip install " + " ".join(_missing_deps)
    )
    sys.exit(1)

# 依賴齊備後才載入會匯入第三方套件的模組，確保上面的友善提示能先生效
from gui import Application, ensure_single_instance, _listen_for_reactivation

if __name__ == "__main__":
    if not ensure_single_instance():
        sys.exit(0)
    app = Application()
    _listen_for_reactivation(app)
    app.mainloop()