import re
import sys
from pathlib import Path

# ================= 全域常數 =================
USER_AGENT = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36'
DIR_DOWNLOADS = 'downloads'
DIR_SIMP = '簡體'
DIR_TRAD = '繁體'

VALID_URL_PATTERN = re.compile(
    r'^https?://tw\.linovelib\.com/novel/\d+(?:\.html)?(?:/.*)?$'
)

DOWNLOAD_TIMEOUT_SECONDS = 21600  # 下載超時上限（秒），預設 6 小時
STALL_TIMEOUT_SECONDS = 900       # 停滯偵測：連續無 log 活動上限（秒），預設 15 分鐘

# ================= 核心工具函式 =================

def get_resource_path(relative_path: str) -> Path:
    """獲取資源檔案的絕對路徑"""
    if hasattr(sys, '_MEIPASS'):
        return Path(sys._MEIPASS) / relative_path
    return Path(__file__).resolve().parent / relative_path

def get_base_path() -> Path:
    """獲取程式執行的根目錄"""
    if getattr(sys, 'frozen', False):
        return Path(sys.executable).parent
    else:
        return Path(__file__).resolve().parent

def _read_version() -> str:
    version_file = get_resource_path('version.txt')
    if version_file.exists():
        return version_file.read_text(encoding='utf-8-sig').strip()
    return 'v0.0.0-dev'

APP_VERSION = _read_version()

def sanitize_filename(name: str) -> str:
    """清理檔案/資料夾名稱中的不合法字元"""
    return re.sub(r'[\\/*?:"<>|]', "", name)