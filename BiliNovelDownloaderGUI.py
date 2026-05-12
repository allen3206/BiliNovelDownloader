import io
import re
import sys
import html
import time
import json
import ctypes
import shutil
import opencc
import zipfile
import logging
import threading
import subprocess
import webbrowser
import tkinter as tk
from pathlib import Path
from enum import Enum, auto
from bs4 import BeautifulSoup
from datetime import datetime
from tkinter import font as tkfont
from tkinter import ttk, messagebox
from typing import Optional, List, Tuple
from dataclasses import dataclass, field

try:
    # 讓 Windows 知道這個程式支援高解析度縮放，避免字體模糊
    ctypes.windll.shcore.SetProcessDpiAwareness(1)
except Exception:
    pass

try:
    import requests
    from PIL import Image, ImageTk
    import psutil
    HAS_PILLOW_REQUESTS = True
except ImportError:
    HAS_PILLOW_REQUESTS = False

# ================= 全域常數 =================
USER_AGENT = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36'
DIR_DOWNLOADS = 'downloads'
DIR_SIMP = '簡體'
DIR_TRAD = '繁體'

VALID_URL_PATTERN = re.compile(
    r'^https?://tw\.linovelib\.com/novel/\d+(?:\.html)?(?:/.*)?$'
)

# 設定全域 OpenCC 實例，避免重複載入字典檔
S2TW_CONVERTER = opencc.OpenCC('s2tw')

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
        return version_file.read_text(encoding='utf-8').strip()
    return 'v0.0.0-dev'

APP_VERSION = _read_version()

def sanitize_filename(name: str) -> str:
    """清理檔案/資料夾名稱中的不合法字元"""
    return re.sub(r'[\\/*?:"<>|]', "", name)

def find_downloader_exe(base_path: Path) -> Optional[Path]:
    """動態尋找 tools 目錄下的下載器執行檔"""
    tools_dir = base_path / 'tools'
    exe_paths = list(tools_dir.glob('bili_novel_packer*.exe'))
    if exe_paths:
        return exe_paths[0]
    return None

def kill_existing_packer():
    """精準地結束所有相關的下載器進程"""
    try:
        for proc in psutil.process_iter(['pid', 'name']):
            try:
                if 'bili_novel_packer' in proc.info['name'].lower():
                    proc.kill()
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                pass
        time.sleep(0.5)
    except Exception as e:
        logging.warning(f"清理進程時發生錯誤: {e}")

def classify_error(e: Exception) -> tuple[str, str]:
    """回傳 (錯誤標題, 使用者友善訊息)"""
    import requests as req_module
    
    if isinstance(e, FileNotFoundError):
        return ("找不到下載器", 
                "在 tools 資料夾中找不到 bili_novel_packer 執行檔。\n\n"
                "請確認 tools 資料夾與主程式在同一層目錄，並且包含下載器 .exe 檔案。")
    
    if isinstance(e, req_module.ConnectionError):
        return ("網路連線失敗",
                "無法連接至嗶哩輕小說伺服器。\n\n請確認網路連線是否正常。")
    
    if isinstance(e, req_module.Timeout):
        return ("連線逾時",
                "伺服器回應超時，可能是網路不穩定或伺服器忙碌。\n\n請稍後再試。")
    
    msg = str(e)
    if "404" in msg or "找不到" in msg:
        return ("小說不存在",
                "找不到對應的小說頁面。\n\n請確認輸入的網址或 ID 是否正確。")
    
    if "未偵測到下載檔案" in msg or "下載失敗" in msg:
        return ("下載失敗",
                f"{msg}\n\n請查閱程式目錄下 log 資料夾內的日誌檔案以取得詳細資訊。")
    
    return ("發生未預期錯誤", f"錯誤訊息：{msg}\n\n請截圖此訊息並回報給開發者。")

def clear_temp_directory(temp_dir: Path):
    if temp_dir.exists():
        for item in temp_dir.iterdir():
            if item.name == 'covers':
                continue
            for i in range(3):
                try:
                    if item.is_dir():
                        shutil.rmtree(item)
                    else:
                        item.unlink()
                    break
                except Exception as e:
                    if i == 2:
                        logging.warning(f"無法刪除暫存檔 {item}: {e}")
                    time.sleep(0.5)

def merge_folders(src: Path, dst: Path):
    for item in src.iterdir():
        dst_item = dst / item.name
        if item.is_dir():
            dst_item.mkdir(parents=True, exist_ok=True)
            merge_folders(item, dst_item)
        else:
            shutil.copy2(item, dst_item)
            logging.info(f"檔案已覆蓋/複製: {dst_item}")

def convert_epub_with_opencc(input_epub: Path, output_epub: Path, converter: opencc.OpenCC):
    text_extensions = ('.html', '.xhtml', '.xml', '.opf', '.ncx', '.txt', '.css')
    with zipfile.ZipFile(input_epub, 'r') as zin:
        with zipfile.ZipFile(output_epub, 'w', zipfile.ZIP_DEFLATED) as zout:
            for item in zin.infolist():
                content = zin.read(item.filename)
                if item.filename.lower().endswith(text_extensions):
                    try:
                        text = content.decode('utf-8')
                        converted_text = converter.convert(text)
                        zout.writestr(item, converted_text.encode('utf-8'))
                    except UnicodeDecodeError:
                        zout.writestr(item, content)
                else:
                    zout.writestr(item, content)

def process_downloaded_folder(
    source_folder_path: Path,
    base_path: Path,
    logger: logging.Logger = None
):
    if logger is None:
        logger = logging.getLogger(__name__)
    source_path = Path(source_folder_path)
    if not source_path.exists():
        logger.error(f"找不到來源資料夾: {source_path}")
        return

    base_dir = Path(base_path)
    downloads_dir = base_dir / DIR_DOWNLOADS
    dest_folder_simp = downloads_dir / DIR_SIMP
    dest_folder_trad = downloads_dir / DIR_TRAD

    dest_folder_simp.mkdir(parents=True, exist_ok=True)
    dest_folder_trad.mkdir(parents=True, exist_ok=True)

    folder_name = source_path.name
    logger.info(f"開始處理資料夾: {folder_name}")

    for file_path in list(source_path.glob('*')):
        if file_path.is_file():
            prefix = f"{folder_name} {folder_name}"
            if file_path.name.startswith(prefix):
                new_name = file_path.name.replace(prefix, folder_name, 1)
                new_file_path = file_path.with_name(new_name)
                if not new_file_path.exists():
                    file_path.rename(new_file_path)
                    logger.info(f"清理重複檔名: {file_path.name} -> {new_name}")

    target_simp = dest_folder_simp / folder_name
    if target_simp.exists():
        logger.info(f"簡體備份目標已存在，進行合併: {target_simp}")
        merge_folders(source_path, target_simp)
    else:
        shutil.copytree(source_path, target_simp)
        logger.info(f"已備份至: {target_simp}")

    epub_files = list(source_path.glob('*.epub'))
    if not epub_files:
        logger.warning("未在下載資料夾中找到 .epub 檔案。")

    for epub_file in epub_files:
        logger.info(f"正在原生轉換內容: {epub_file.name}")
        temp_output = epub_file.with_suffix('.temp.epub')
        
        try:
            convert_epub_with_opencc(epub_file, temp_output, S2TW_CONVERTER)

            # 確認 temp 檔確實存在且有內容，才進行覆蓋
            if not temp_output.exists() or temp_output.stat().st_size == 0:
                raise Exception("轉換輸出檔不存在或大小為零，取消覆蓋原檔")

            epub_file.unlink()
            temp_output.rename(epub_file)
            logger.info(f"轉換成功: {epub_file.name}")
        except Exception as e:
            logger.error(f"轉換失敗: {epub_file.name}, 錯誤: {e}")
            # 清理 temp 檔，但原檔不動
            if temp_output.exists():
                temp_output.unlink()
                logger.info(f"已清除暫存檔: {temp_output.name}")

    for file_path in list(source_path.glob('*')):
        if file_path.is_file():
            new_name = sanitize_filename(S2TW_CONVERTER.convert(file_path.name))
            if new_name != file_path.name:
                new_path = file_path.with_name(new_name)
                if not new_path.exists():
                    file_path.rename(new_path)
                    logger.info(f"檔名已更新: {file_path.name} -> {new_name}")
                else:
                    logger.warning(f"無法更名，目標已存在: {new_name}")

    new_folder_name = sanitize_filename(S2TW_CONVERTER.convert(source_path.name))
    if new_folder_name != source_path.name:
        new_folder_path = source_path.parent / new_folder_name
        source_path.rename(new_folder_path)
        source_path = new_folder_path
        logger.info(f"資料夾名稱已更新: {folder_name} -> {new_folder_name}")

    target_trad = dest_folder_trad / source_path.name
    if target_trad.exists():
        logger.info(f"繁體工作區已存在，進行合併: {target_trad}")
        merge_folders(source_path, target_trad)
        shutil.rmtree(source_path)
    else:
        shutil.move(source_path, target_trad)
        logger.info(f"已移動至工作區: {target_trad}")

    logger.info("所有程序完成。")


# ================= 爬蟲邏輯 =================

@dataclass
class NovelInfo:
    url: str
    title: str = "未知書名"
    cover_url: Optional[str] = None
    author: str = "未知"
    status: str = "未知"
    tags: str = "未知"
    latest: str = "未知"
    update_time: str = "未知"
    desc: str = "無簡介"
    rating: str = "未知"
    volumes: List[Tuple[str, str]] = field(default_factory=list)

class NovelScraper:
    def __init__(self):
        self.headers = {'User-Agent': USER_AGENT}

    def fetch_info(self, url: str) -> NovelInfo:
        res = requests.get(url, headers=self.headers, timeout=60)
        if res.status_code == 403:
            raise Exception("被網站防火牆阻擋 (HTTP 403)，請確認網址或稍後再試。")
        elif res.status_code != 200:
            raise Exception(f"無法存取網頁 (HTTP {res.status_code})")
            
        res.encoding = 'utf-8'
        try:
            import lxml
            parser = 'lxml'
        except ImportError:
            parser = 'html.parser'
        soup = BeautifulSoup(res.text, parser)
        info = NovelInfo(url=url)
        
        # 標題
        meta_title = soup.find("meta", property="og:title")
        if meta_title:
            info.title = meta_title.get("content", "未知書名")
        else:
            title_div = soup.find("div", id="title")
            if title_div:
                info.title = title_div.get_text(strip=True)
                
        # 評價
        score_div = soup.find("div", class_="score-num")
        if score_div:
            rating_text = score_div.get_text(strip=True)
            info.rating = f"{rating_text} 分"
            count_p = soup.find("p", class_="done-count")
            if count_p and count_p.find("em"):
                info.rating += f" ({count_p.find('em').get_text(strip=True)})"
                
        # 作者
        meta_author = soup.find("meta", property="og:novel:author")
        if meta_author: info.author = meta_author.get("content", "未知")
        
        # 狀態與分類
        meta_status = soup.find("meta", property="og:novel:status")
        if meta_status: info.status = meta_status.get("content", "未知")
        meta_category = soup.find("meta", property="og:novel:category")
        if meta_category: info.tags = meta_category.get("content", "未知")
        
        # 最新章節與時間
        meta_latest = soup.find("meta", property="og:novel:latest_chapter_name")
        if meta_latest: info.latest = meta_latest.get("content", "未知")
        meta_update = soup.find("meta", property="og:novel:update_time")
        if meta_update: 
            info.update_time = meta_update.get("content", "未知")
        else:
            update_div = soup.find("div", class_="book-meta-l")
            if update_div:
                m_update2 = re.search(r'(\d{4}-\d{2}-\d{2})', update_div.get_text())
                if m_update2: info.update_time = m_update2.group(1)
                
        # 簡介
        info.desc = self._extract_description(soup, url)
        
        # 封面
        meta_image = soup.find("meta", property="og:image")
        if meta_image:
            info.cover_url = meta_image.get("content")
        else:
            img_tag = soup.find("img", border="0")
            if img_tag and img_tag.get("src"):
                info.cover_url = img_tag.get("src")
                
        if info.cover_url and not info.cover_url.startswith("http"):
            domain = "https://tw.linovelib.com/"
            info.cover_url = f"{domain}{info.cover_url.lstrip('/')}"
            
        self._convert_info_to_tw(info)
        return info

    def _extract_description(self, soup: BeautifulSoup, url: str) -> str:
        desc = "無簡介"
        summary_tag = soup.find(class_="book-summary")
        if summary_tag:
            content_tag = summary_tag.find("content") or summary_tag.find(class_="notice-body")
            desc = content_tag.get_text(separator=" ", strip=True) if content_tag else summary_tag.get_text(separator=" ", strip=True)
        
        if desc == "無簡介" or len(desc) < 10:
            intro_tag = soup.find(class_="book-intro")
            if intro_tag:
                desc = intro_tag.get_text(strip=True)
            else:
                meta_desc = soup.find("meta", property="og:description")
                if meta_desc:
                    desc = meta_desc.get("content", "無簡介")

        desc = re.sub(r'^(簡介|內容簡介|内容简介)[：:]\s*', '', desc)
        desc = re.sub(r'\s+', ' ', desc).strip()
        if len(desc) > 1000:
            desc = desc[:997] + "..."
        return desc

    def _convert_info_to_tw(self, info: NovelInfo):
        info.title = html.unescape(S2TW_CONVERTER.convert(info.title))
        info.author = html.unescape(S2TW_CONVERTER.convert(info.author))
        
        status_tw = S2TW_CONVERTER.convert(info.status)
        info.status = "連載中" if status_tw == "連載" else ("已完結" if status_tw == "完結" else status_tw)
        info.tags = S2TW_CONVERTER.convert(info.tags)
        info.latest = html.unescape(S2TW_CONVERTER.convert(info.latest))
        info.desc = html.unescape(S2TW_CONVERTER.convert(info.desc))

    def fetch_catalog(self, url: str) -> List[Tuple[str, str]]:
        cat_url = url.replace('.html', '/catalog')
        res = requests.get(cat_url, headers=self.headers, timeout=60)
        vols = []
        if res.status_code == 200:
            soup = BeautifulSoup(res.text, 'html.parser')
            vol_tags = soup.select('.chapter-bar h3')
            for i, v in enumerate(vol_tags, 1):
                v_name = v.get_text(strip=True)
                vols.append((str(i), S2TW_CONVERTER.convert(v_name)))
        return vols

    def download_cover(self, cover_url: str) -> Optional['Image.Image']:
        if not cover_url: return None
        import hashlib
        
        cache_dir = get_base_path() / 'temp' / 'covers'
        cache_dir.mkdir(parents=True, exist_ok=True)
        
        url_hash = hashlib.md5(cover_url.encode('utf-8')).hexdigest()
        cache_path = cache_dir / f"{url_hash}.jpg"
        
        # 檢查快取是否存在且未過期 (24小時)
        if cache_path.exists():
            if time.time() - cache_path.stat().st_mtime < 86400:
                try:
                    img = Image.open(cache_path).copy()
                    img.thumbnail((200, 280), Image.LANCZOS)
                    return img
                except Exception:
                    cache_path.unlink(missing_ok=True)
            else:
                cache_path.unlink(missing_ok=True)
                
        # 快取未命中或已過期：下載並存入快取
        try:
            res = requests.get(cover_url, headers=self.headers, timeout=15)
            if res.status_code == 200:
                img = Image.open(io.BytesIO(res.content))
                if img.mode != 'RGB':
                    img = img.convert('RGB')
                img.save(cache_path, 'JPEG', quality=85)
                img.thumbnail((200, 280), Image.LANCZOS)
                return img
        except Exception as e:
            logging.warning(f"封面下載失敗: {e}")
        return None

# ================= GUI =================

class AppState(Enum):
    IDLE = auto()
    CHECKING = auto()
    DOWNLOADING = auto()

class LogHandler(logging.Handler):
    def __init__(self, text_widget):
        super().__init__()
        self.text_widget = text_widget
        self.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s', datefmt='%Y-%m-%d %H:%M:%S'))

    def emit(self, record):
        msg = self.format(record)
        def append():
            self.text_widget.configure(state="normal")
            self.text_widget.insert("end", msg + "\n", record.levelname)
            self.text_widget.see("end")
            self.text_widget.configure(state="disabled")
        self.text_widget.after(0, append)

class Application(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(f"嗶哩輕小說自動下載與繁化工具 {APP_VERSION}")
        
        # 嘗試載入視窗圖示
        icon_path = get_resource_path('BiliNovelDownloaderIcon.ico')
        if icon_path.exists():
            try:
                self.iconbitmap(str(icon_path))
            except Exception as e:
                logging.warning(f"無法載入視窗圖示: {e}")
                
        width, height = 950, 700
        screen_width = self.winfo_screenwidth()
        screen_height = self.winfo_screenheight()
        x = (screen_width // 2) - (width // 2)
        y = (screen_height // 2) - (height // 2)
        self.geometry(f"{width}x{height}+{x}+{y}")
        self.minsize(width, height)
        self.resizable(True, True)
        self.configure(padx=10, pady=5)

        self.scraper = NovelScraper()
        self.app_state = AppState.IDLE
        self.current_process = None
        self.cancel_event = threading.Event()
        self.last_checked_url = ""

        self.create_widgets()
        self.setup_logging()
        
        # 綁定視窗關閉事件
        self.protocol("WM_DELETE_WINDOW", self.on_closing)

    def _load_history(self) -> list:
        history_path = get_base_path() / 'history.json'
        if history_path.exists():
            try:
                return json.loads(history_path.read_text(encoding='utf-8'))
            except Exception:
                return []
        return []

    def _save_to_history(self, url: str, title: str):
        history = self._load_history()
        history = [h for h in history if h.get('url') != url]
        history.insert(0, {'url': url, 'title': title, 'time': datetime.now().strftime('%Y-%m-%d %H:%M')})
        history = history[:15]
        history_path = get_base_path() / 'history.json'
        try:
            history_path.write_text(json.dumps(history, ensure_ascii=False, indent=2), encoding='utf-8')
        except Exception as e:
            logging.warning(f"無法儲存歷史紀錄: {e}")

    def _show_history_menu(self):
        history = self._load_history()
        if not history:
            messagebox.showinfo("下載歷史", "目前尚無下載記錄。")
            return
        
        menu = tk.Menu(self, tearoff=0)
        for item in history:
            label = f"{item['title']}  ({item.get('time', '')})"
            if len(label) > 40: label = label[:37] + "..."
            menu.add_command(
                label=label,
                command=lambda u=item['url']: self._fill_url_from_history(u)
            )
        
        x = self.history_btn.winfo_rootx()
        y = self.history_btn.winfo_rooty() + self.history_btn.winfo_height()
        menu.post(x, y)

    def _fill_url_from_history(self, url: str):
        self.url_var.set(url)
        self.entry_url.configure(foreground="black")
        self.last_checked_url = ""
        self.check_novel_info()

    def on_closing(self):
        if self.app_state == AppState.DOWNLOADING:
            if messagebox.askokcancel("退出確認", "目前正在下載中，確定要強制退出嗎？\n(這將終止所有下載與轉換進程)"):
                self.cancel_download()
                self.destroy()
        else:
            self.destroy()

    def create_widgets(self):
        self._build_url_frame()
        self._build_settings_frame()
        self._build_run_frame()
        self._build_bottom_frame()

    def _build_url_frame(self):
        # 1. 網址與 ID 輸入及預覽區塊
        frame_url = ttk.LabelFrame(self, text="小說來源與資訊預覽")
        frame_url.pack(fill="x", pady=5)
        
        url_input_frame = tk.Frame(frame_url)
        url_input_frame.pack(fill="x", padx=10, pady=5)
        
        ttk.Label(url_input_frame, text="輸入網址或數字 ID:").pack(side="left")
        self.url_var = tk.StringVar()
        self.placeholder = "例如：https://tw.linovelib.com/novel/2.html 或 2"
        self.entry_url = ttk.Entry(url_input_frame, textvariable=self.url_var, font=("Consolas", 11), width=50)
        self.entry_url.pack(side="left", fill="x", expand=True, padx=5)
        self.entry_url.bind("<Return>", self.check_novel_info)
        
        def on_focus_in(event):
            if self.url_var.get() == self.placeholder:
                self.url_var.set("")
                self.entry_url.configure(foreground="black")

        def on_focus_out(event):
            if not self.url_var.get().strip():
                self.url_var.set(self.placeholder)
                self.entry_url.configure(foreground="gray")

        self.entry_url.bind("<FocusIn>", on_focus_in)
        self.entry_url.bind("<FocusOut>", on_focus_out)
        self.url_var.set(self.placeholder)
        self.entry_url.configure(foreground="gray")
        
        self.check_btn = ttk.Button(url_input_frame, text="檢查並載入資訊", width=16, command=self.check_novel_info)
        self.check_btn.pack(side="left", padx=5)

        self.history_btn = ttk.Button(url_input_frame, text="歷史紀錄", command=self._show_history_menu)
        self.history_btn.pack(side="left", padx=5)

        self.go_url_btn = ttk.Button(url_input_frame, text="前往網址", command=self.open_url)
        self.go_url_btn.pack(side="left", padx=5)

        # 預覽資訊區塊
        preview_frame = tk.Frame(frame_url)
        preview_frame.pack(fill="x", padx=10, pady=5)
        
        self.cover_container = tk.Frame(preview_frame, width=200, height=280, bg="lightgray")
        self.cover_container.pack(side="left", padx=(0, 10))
        self.cover_container.pack_propagate(False)
        
        self.cover_label = tk.Label(self.cover_container, text="封面縮圖預覽", bg="lightgray", wraplength=150)
        self.cover_label.pack(expand=True, fill="both")
        
        info_text_frame = tk.Frame(preview_frame)
        info_text_frame.pack(side="left", fill="both", expand=True)
        
        self.title_var_display = tk.StringVar(value="書名：尚未載入")
        self.title_label = tk.Entry(info_text_frame, textvariable=self.title_var_display, font=("Microsoft JhengHei", 12, "bold"), 
                                    readonlybackground=self.cget("bg"), relief="flat", state="readonly")
        self.title_label.pack(fill="x", pady=(0, 5))
        
        self.author_var_display = tk.StringVar(value="-")
        self.author_label = tk.Entry(info_text_frame, textvariable=self.author_var_display, font=("Microsoft JhengHei", 10),
                                     readonlybackground=self.cget("bg"), relief="flat", state="readonly")
        self.author_label.pack(fill="x", pady=2)
        
        self.meta_var_display = tk.StringVar(value="-")
        self.meta_label = tk.Entry(info_text_frame, textvariable=self.meta_var_display, font=("Microsoft JhengHei", 10),
                                   readonlybackground=self.cget("bg"), relief="flat", state="readonly")
        self.meta_label.pack(fill="x", pady=2)
        
        self.update_var_display = tk.StringVar(value="-")
        self.update_label = tk.Entry(info_text_frame, textvariable=self.update_var_display, font=("Microsoft JhengHei", 10),
                                     readonlybackground=self.cget("bg"), relief="flat", state="readonly")
        self.update_label.pack(fill="x", pady=2)
        
        ttk.Label(info_text_frame, text="簡介：", font=("Microsoft JhengHei", 10)).pack(anchor="nw", pady=(2, 0))

        desc_container = tk.Frame(info_text_frame)
        desc_container.pack(anchor="nw", fill="x", expand=True, pady=(0, 5))
        desc_container.columnconfigure(0, weight=1)

        desc_v_scroll = ttk.Scrollbar(desc_container, orient="vertical")
        self.desc_text = tk.Text(desc_container, height=7, font=("Microsoft JhengHei", 10), foreground="black", 
                                 state="disabled", wrap="word", borderwidth=0, highlightthickness=0)

        def set_desc_sb(first, last):
            if float(first) <= 0.0 and float(last) >= 1.0:
                desc_v_scroll.grid_remove()
            else:
                desc_v_scroll.grid(row=0, column=1, sticky="ns")
            desc_v_scroll.set(first, last)

        self.desc_text.config(yscrollcommand=set_desc_sb)
        desc_v_scroll.config(command=self.desc_text.yview)

        self.desc_text.config(bg=self.cget("bg"))
        self.desc_text.grid(row=0, column=0, sticky="nsew")

        self.status_label = ttk.Label(info_text_frame, text="", foreground="blue")
        self.status_label.pack(anchor="nw", pady=(5, 0))

    def _build_settings_frame(self):
        # 設定區塊_分卷下載與進階選項
        settings_container = tk.Frame(self)
        settings_container.pack(fill="x", pady=2)

        # 2. 分卷下載設定
        frame_vol = ttk.LabelFrame(settings_container, text="分卷下載設定")
        frame_vol.pack(side="left", fill="both", expand=True, padx=(0, 5))
        
        self.vol_mode_var = tk.StringVar(value="all")
        ttk.Radiobutton(frame_vol, text="下載全部分卷", variable=self.vol_mode_var, value="all", command=self.on_vol_mode_change).grid(row=0, column=0, sticky="w", padx=10, pady=2)
        ttk.Radiobutton(frame_vol, text="下載指定範圍：", variable=self.vol_mode_var, value="specific", command=self.on_vol_mode_change).grid(row=1, column=0, sticky="w", padx=10, pady=2)
        
        self.vol_specific_var = tk.StringVar()
        self.vol_entry = ttk.Entry(frame_vol, textvariable=self.vol_specific_var, state="disabled", width=15)
        self.vol_entry.grid(row=1, column=1, sticky="w", padx=5)
        ttk.Label(frame_vol, text="(例: 1,2-9,11，可參考分卷對照表)", foreground="gray", font=("Arial", 9)).grid(row=1, column=2, sticky="w")
        
        # 3. 進階選項
        frame_opt = ttk.LabelFrame(settings_container, text="進階選項")
        frame_opt.pack(side="left", fill="both", expand=True, padx=(5, 0))
        
        self.merge_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(frame_opt, text="合併選取的分卷為單一檔案", variable=self.merge_var).pack(anchor="w", padx=10, pady=2)
        self.title_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(frame_opt, text="在每章開頭添加章節標題", variable=self.title_var).pack(anchor="w", padx=10, pady=2)
        
    def _build_run_frame(self):
        # 4. 狀態與執行
        frame_run = tk.Frame(self)
        frame_run.pack(fill="x", pady=5)
        
        self.start_btn = ttk.Button(frame_run, text="開始下載", command=self.start_process, width=20)
        self.start_btn.pack(pady=(0, 2))
        
        self.progress_var = tk.StringVar(value="")
        ttk.Label(frame_run, textvariable=self.progress_var, font=("Microsoft JhengHei", 10), foreground="black").pack()

        self.progress_canvas = tk.Canvas(frame_run, width=500, height=14, bg='#E0E0E0', highlightthickness=0)
        self.progress_rect = self.progress_canvas.create_rectangle(-100, 0, 0, 14, fill='#06B025', width=0)
        
    def _build_bottom_frame(self):
        # 下方區塊_分卷對照表與日誌視窗
        bottom_container = ttk.PanedWindow(self, orient=tk.HORIZONTAL)
        bottom_container.pack(fill="both", expand=True, pady=5)

        # 2.5 分卷對照表
        frame_vol_ref = ttk.LabelFrame(bottom_container, text="分卷對照表")
        bottom_container.add(frame_vol_ref, weight=4)
        
        frame_vol_ref.columnconfigure(0, weight=0)
        frame_vol_ref.columnconfigure(1, weight=1)
        frame_vol_ref.rowconfigure(0, weight=0)
        frame_vol_ref.rowconfigure(1, weight=1)

        def sync_yview(*args):
            self.vol_tree_id.yview(*args)
            self.vol_tree_name.yview(*args)

        self.header_id = ttk.Label(frame_vol_ref, text="編號", relief="flat", anchor="center", font=("Microsoft JhengHei", 9, "bold"), background="#E1E1E1")
        self.header_name = ttk.Label(frame_vol_ref, text="分卷名稱", relief="flat", anchor="center", font=("Microsoft JhengHei", 9, "bold"), background="#E1E1E1")
        self.header_id.grid(row=0, column=0, sticky="nsew")
        self.header_name.grid(row=0, column=1, sticky="nsew")

        self.vol_tree_id = ttk.Treeview(frame_vol_ref, columns=("id"), show="", selectmode="browse", height=1)
        self.vol_tree_id.column("#0", width=0, stretch=False)
        self.vol_tree_id.column("id", width=50, minwidth=50, stretch=False, anchor="center")
        
        def handle_id_click(event):
            if self.vol_tree_id.identify_region(event.x, event.y) == "separator": return "break"
        self.vol_tree_id.bind("<Button-1>", handle_id_click)
        self.vol_tree_id.bind("<B1-Motion>", handle_id_click)
        
        self.vol_tree_name = ttk.Treeview(frame_vol_ref, columns=("name"), show="", selectmode="browse", height=1)
        self.vol_tree_name.column("name", width=160, minwidth=100, stretch=True, anchor="w")
        
        vol_v_scroll = ttk.Scrollbar(frame_vol_ref, orient="vertical", command=sync_yview)
        vol_h_scroll = ttk.Scrollbar(frame_vol_ref, orient="horizontal", command=self.vol_tree_name.xview)
        
        def set_vol_v_sb(first, last):
            f, l = float(first), float(last)
            if (f <= 0.0 and l >= 0.999) or f == l: vol_v_scroll.grid_remove()
            else: vol_v_scroll.grid(row=1, column=2, sticky="ns")
            vol_v_scroll.set(first, last)

        def set_vol_h_sb(first, last):
            f, l = float(first), float(last)
            if (f <= 0.0 and l >= 0.999) or f == l: vol_h_scroll.grid_remove()
            else: vol_h_scroll.grid(row=2, column=1, sticky="ew")
            vol_h_scroll.set(first, last)

        self.vol_tree_id.config(yscrollcommand=set_vol_v_sb)
        self.vol_tree_name.config(yscrollcommand=set_vol_v_sb, xscrollcommand=set_vol_h_sb)
        
        def on_tree_mousewheel(event):
            delta = int(-1*(event.delta/120))
            self.vol_tree_id.yview_scroll(delta, "units")
            self.vol_tree_name.yview_scroll(delta, "units")
            return "break"

        self.vol_tree_id.bind("<MouseWheel>", on_tree_mousewheel)
        self.vol_tree_name.bind("<MouseWheel>", on_tree_mousewheel)
        self.vol_tree_id.grid(row=1, column=0, sticky="ns")
        self.vol_tree_name.grid(row=1, column=1, sticky="nsew")
        
        # 5. 日誌視窗
        frame_log = ttk.LabelFrame(bottom_container, text="執行日誌")
        bottom_container.add(frame_log, weight=6)
        frame_log.columnconfigure(0, weight=1)
        frame_log.rowconfigure(0, weight=1)
        
        log_v_scroll = ttk.Scrollbar(frame_log, orient="vertical")
        log_h_scroll = ttk.Scrollbar(frame_log, orient="horizontal")
        
        self.log_text = tk.Text(frame_log, state="disabled", font=("Consolas", 9), wrap="none", width=40)
        
        def set_log_sb(first, last, sb, orient):
            f, l = float(first), float(last)
            if (f <= 0.0 and l >= 0.999) or f == l: sb.grid_remove()
            else: sb.grid()
            sb.set(first, last)

        self.log_text.config(yscrollcommand=lambda f, l: set_log_sb(f, l, log_v_scroll, "v"), xscrollcommand=lambda f, l: set_log_sb(f, l, log_h_scroll, "h"))
        log_v_scroll.config(command=self.log_text.yview)
        log_h_scroll.config(command=self.log_text.xview)
        
        self.log_text.grid(row=0, column=0, sticky="nsew")
        log_v_scroll.grid(row=0, column=1, sticky="ns")
        log_h_scroll.grid(row=1, column=0, sticky="ew")
        
        self.log_text.tag_config("INFO", foreground="black")
        self.log_text.tag_config("WARNING", foreground="#D2691E")
        self.log_text.tag_config("ERROR", foreground="red")

    def setup_logging(self):
        logger = logging.getLogger()
        logger.setLevel(logging.INFO)
        if logger.hasHandlers():
            logger.handlers.clear()
            
        formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s', datefmt='%Y-%m-%d %H:%M:%S')
        
        # GUI 顯示的 Log
        gui_handler = LogHandler(self.log_text)
        gui_handler.setFormatter(formatter)
        logger.addHandler(gui_handler)
        
        # 寫入檔案的 Log
        log_dir = get_base_path() / 'log'
        log_dir.mkdir(exist_ok=True)
        file_handler = logging.FileHandler(log_dir / 'app_runtime.log', encoding='utf-8')
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)

    def open_url(self):
        url_input = self.url_var.get().strip()
        if not url_input or url_input == self.placeholder:
            messagebox.showwarning("警告", "請先輸入網址 or ID！")
            return
        url = f"https://tw.linovelib.com/novel/{url_input}.html" if url_input.isdigit() else url_input
        try:
            webbrowser.open(url)
        except Exception as e:
            messagebox.showerror("錯誤", f"無法開啟網址:\n{e}")

    def on_vol_mode_change(self):
        if self.vol_mode_var.get() == "all":
            self.vol_entry.config(state="disabled")
            self.vol_specific_var.set("")
        else:
            self.vol_entry.config(state="normal")
            self.vol_entry.focus()

    def set_app_state(self, state: AppState):
        self.app_state = state
        if state == AppState.IDLE:
            self.check_btn.config(state="normal", text="檢查並載入資訊")
            self.start_btn.config(state="normal", text="開始下載", command=self.start_process)
            self.progress_canvas.pack_forget()
        elif state == AppState.CHECKING:
            self.check_btn.config(state="disabled", text="檢查中...")
            self.status_label.config(text="正在獲取小說資訊...", foreground="orange")
        elif state == AppState.DOWNLOADING:
            self.check_btn.config(state="disabled")
            self.start_btn.config(text="取消下載", command=self.cancel_download)
            self.progress_canvas.pack(pady=(2, 0))
            self.progress_canvas.coords(self.progress_rect, -100, 0, 0, 14)
            self._animate_progress_bar()

    def _animate_progress_bar(self):
        """進度條"""
        if self.app_state == AppState.DOWNLOADING:
            coords = self.progress_canvas.coords(self.progress_rect)
            if not coords: return
            x1, y1, x2, y2 = coords
            
            # 寬度 20% (100px)，每次往右移動 6px
            step = 6
            width = 100
            
            x1 += step
            x2 += step
            
            # 如果整條綠色都超出右邊界，就從左邊重新出來
            if x1 > 500:
                x1 = -width
                x2 = 0
                
            self.progress_canvas.coords(self.progress_rect, x1, y1, x2, y2)
            self.after(20, self._animate_progress_bar)

    def reset_info_labels(self):
        self.title_var_display.set("書名：尚未載入")
        self.author_var_display.set("-")
        self.meta_var_display.set("-")
        self.update_var_display.set("-")
        
        self.desc_text.config(state="normal")
        self.desc_text.delete("1.0", "end")
        self.desc_text.config(state="disabled")
        
        for item in self.vol_tree_id.get_children(): self.vol_tree_id.delete(item)
        for item in self.vol_tree_name.get_children(): self.vol_tree_name.delete(item)
        
        self.cover_label.config(image='', text="正在載入封面...", width=28, height=14)
        self.cover_label.image = None

    def check_novel_info(self, event=None, auto_start=False):
        if not HAS_PILLOW_REQUESTS:
            messagebox.showwarning("缺少依賴項", "請先在終端機安裝 requests 與 Pillow 套件才能預覽縮圖：\npip install requests Pillow")
            return
            
        if self.app_state == AppState.CHECKING:
            messagebox.showinfo("提示", "正在獲取小說資訊中，請稍候...")
            return
        if self.app_state == AppState.DOWNLOADING:
            return
            
        url_input = self.url_var.get().strip()
        if not url_input or url_input == self.placeholder:
            messagebox.showwarning("警告", "請輸入小說網址或數字 ID！")
            return
            
        url = f"https://tw.linovelib.com/novel/{url_input}.html" if url_input.isdigit() else url_input

        if not url_input.isdigit() and not VALID_URL_PATTERN.match(url_input):
            messagebox.showwarning(
                "網址格式錯誤",
                "請輸入有效的嗶哩輕小說網址或純數字 ID。\n\n"
                "有效格式範例：\n"
                "  · https://tw.linovelib.com/novel/2.html\n"
                "  · 2"
            )
            return

        self.url_var.set(url)
        self.entry_url.icursor("end")
        self.entry_url.xview_moveto(1.0)
        self.entry_url.configure(foreground="black")
            
        if self.app_state != AppState.IDLE: return

        self.set_app_state(AppState.CHECKING)
        self.reset_info_labels()
        self.title_var_display.set("書名：載入中...")
        
        thread = threading.Thread(target=self._thread_fetch_info, args=(url, auto_start), daemon=True)
        thread.start()

    def _thread_fetch_info(self, url: str, auto_start: bool):
        try:
            info = self.scraper.fetch_info(url)
            self.after(0, lambda: self._apply_info_to_ui(info))
            self.last_checked_url = url
            
            # 抓取封面
            if info.cover_url:
                try:
                    cover_img = self.scraper.download_cover(info.cover_url)
                    if cover_img:
                        photo = ImageTk.PhotoImage(cover_img)
                        self.after(0, lambda p=photo: self._apply_cover_to_ui(p))
                    else:
                        self.after(0, lambda: self.status_label.config(text="找不到封面圖片", foreground="orange"))
                except Exception as e:
                    self.after(0, lambda err=e: self.status_label.config(text=f"封面下載失敗: {err}", foreground="orange"))

            # 抓取目錄
            try:
                vols = self.scraper.fetch_catalog(url)
                if vols:
                    self.after(0, lambda v=vols: self._apply_catalog_to_ui(v))
                    self.after(0, lambda: self.status_label.config(text="小說資訊與目錄載入成功", foreground="green"))
                else:
                    self.after(0, lambda: self.status_label.config(text="資訊載入成功 (但找不到目錄)", foreground="orange"))
            except Exception as e:
                self.after(0, lambda err=e: self.status_label.config(text=f"載入完成 (目錄抓取失敗: {err})", foreground="orange"))

            if auto_start:
                self.after(500, self.start_process)
                
        except Exception as e:
            self.after(0, lambda err=e: self._handle_fetch_error(err))
        finally:
            self.after(0, lambda: self.set_app_state(AppState.IDLE))

    def _apply_info_to_ui(self, info: NovelInfo):
        self.title_var_display.set(f"書名：{info.title}")
        self.author_var_display.set(f"作者：{info.author}")
        self.meta_var_display.set(f"{info.status} | {info.tags} | {info.rating}")
        self.update_var_display.set(f"最新進度：{info.latest} (更新時間：{info.update_time})")
        
        self.desc_text.config(state="normal")
        self.desc_text.delete("1.0", "end")
        
        # 修正微軟正黑體會將 em-dash (— 或 ―) 顯示為上橫線的字體渲染 Bug
        # 改用製表符 (Box Drawing Light Horizontal U+2500) 讓線條相連
        display_desc = info.desc.replace('—', '─').replace('―', '─')
        self.desc_text.insert("end", display_desc)
        
        self.desc_text.config(state="disabled")
        
        self._save_to_history(info.url, info.title)

    def _apply_cover_to_ui(self, photo):
        self.cover_label.config(image=photo, text="", width=200, height=280)
        self.cover_label.image = photo

    def _apply_catalog_to_ui(self, vols: List[Tuple[str, str]]):
        for item in self.vol_tree_id.get_children(): self.vol_tree_id.delete(item)
        for item in self.vol_tree_name.get_children(): self.vol_tree_name.delete(item)
        
        f = tkfont.Font(family="Microsoft JhengHei", size=9)
        max_w = 330
        for v_id, v_name in vols:
            w = f.measure(v_name)
            if w > max_w: max_w = w
        
        self.vol_tree_name.column("name", width=(max_w + 20 if max_w > 330 else 330), stretch=(max_w <= 330))
        for v_id, v_name in vols:
            self.vol_tree_id.insert("", "end", values=(v_id,))
            self.vol_tree_name.insert("", "end", values=(v_name,))

    def _handle_fetch_error(self, error: Exception):
        self.reset_info_labels()
        self.title_var_display.set("書名：載入失敗")
        self.status_label.config(text=str(error), foreground="red")
        self.cover_label.config(image='', text="[ 圖片載入失敗 ]", width=28, height=14)

    def cancel_download(self):
        if messagebox.askyesno("取消確認", "確定要終止目前的下載與轉換程序嗎？"):
            self.cancel_event.set()
            logging.warning("使用者觸發取消程序...")
            if self.current_process:
                try:
                    self.current_process.kill()
                    logging.info("已終止下載器進程。")
                except Exception as e:
                    logging.error(f"終止進程時發生錯誤: {e}")
            self.start_btn.config(state="disabled", text="正在取消...")

    def start_process(self):
        if self.app_state == AppState.CHECKING:
            messagebox.showinfo("提示", "正在獲取小說資訊中，請稍候再點擊下載。")
            return
            
        url_input = self.url_var.get().strip()
        if not url_input or url_input == self.placeholder:
            messagebox.showwarning("警告", "請輸入小說網址或數字 ID！")
            return
            
        url = f"https://tw.linovelib.com/novel/{url_input}.html" if url_input.isdigit() else url_input

        if not url_input.isdigit() and not VALID_URL_PATTERN.match(url_input):
            messagebox.showwarning(
                "網址格式錯誤",
                "請輸入有效的嗶哩輕小說網址或純數字 ID。\n\n"
                "有效格式範例：\n"
                "  · https://tw.linovelib.com/novel/2.html\n"
                "  · 2"
            )
            return

        self.url_var.set(url)
        self.entry_url.icursor("end")
        self.entry_url.xview_moveto(1.0)
        self.entry_url.configure(foreground="black")
            
        if not self.last_checked_url:
            logging.info("尚未載入資訊，自動執行資訊檢查並下載...")
            self.check_novel_info(auto_start=True)
            return
            
        if self.last_checked_url != url:
            if not messagebox.askyesno("提示", "偵測到輸入網址與目前預覽資訊不符。\n\n建議先點擊『檢查並載入資訊』以確認小說內容。\n是否仍要直接開始下載？"):
                return
            logging.info("使用者選擇直接下載，同步更新預覽資訊...")
            self.check_novel_info(auto_start=False)
            
        o1 = '0' if self.vol_mode_var.get() == "all" else self.vol_specific_var.get().strip()
        if self.vol_mode_var.get() != "all" and not o1:
            messagebox.showwarning("警告", "您勾選了指定範圍，請輸入範圍數字！")
            return
                
        o2 = '1' if self.merge_var.get() else '2'
        o3 = '1' if self.title_var.get() else '2'
        
        self.set_app_state(AppState.DOWNLOADING)
        self.cancel_event.clear()
        
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")
        
        threading.Thread(target=self._thread_run_main_logic, args=(url, o1, o2, o3), daemon=True).start()

    def _thread_run_main_logic(self, url, o1, o2, o3):
        start_time = time.time()
        try:
            summary = self._main_logic(url, o1, o2, o3)
            elapsed = int(time.time() - start_time)
            mins, secs = divmod(elapsed, 60)
            
            if self.cancel_event.is_set():
                self.after(0, lambda: self.progress_var.set("狀態: 程序已由使用者取消"))
                self.after(0, lambda: messagebox.showwarning("已取消", "程序已終止。"))
            else:
                self.after(0, lambda: self.progress_var.set("狀態: 下載完成"))
                epub_info = f"共 {summary['epub_count']} 個 EPUB 檔案" if summary and summary.get('epub_count') else "請查閱下載資料夾"
                time_info = f"{mins} 分 {secs} 秒" if mins > 0 else f"{secs} 秒"
                folder_name = summary.get('folder_name', '未知') if summary else '未知'
                self.after(0, lambda: messagebox.showinfo(
                    "完成",
                    f"小說下載與轉換程序已全部完成！\n\n"
                    f"書名：{folder_name}\n"
                    f"結果：{epub_info}\n"
                    f"耗時：{time_info}"
                ))
        except Exception as e:
            if not self.cancel_event.is_set():
                logging.error(f"發生錯誤: {e}", exc_info=True)
                self.after(0, lambda: self.progress_var.set("狀態: 發生錯誤"))
                title, msg = classify_error(e)
                self.after(0, lambda t=title, m=msg: messagebox.showerror(t, m))
        finally:
            self.current_process = None
            self.after(0, lambda: self.set_app_state(AppState.IDLE))

    def _tail_log_file(self, log_path: Path):
        """持續讀取下載器 log 檔並顯示到 UI，直到下載結束或取消"""
        # 等待 log 檔出現（最多 10 秒）
        for _ in range(20):
            if log_path.exists():
                break
            time.sleep(0.5)
        else:
            return  # log 檔沒出現就放棄

        try:
            with open(log_path, 'r', encoding='utf-8', errors='ignore') as f:
                f.seek(0, 2)  # 跳到末尾，只看後續新增的內容
                while not self.cancel_event.is_set():
                    # 如果 process 已結束，再讀一次把剩餘內容清空
                    process_done = (self.current_process is None or
                                    self.current_process.poll() is not None)
                    line = f.readline()
                    if line:
                        stripped = line.strip()
                        if stripped:
                            # 用 after(0, ...) 確保 UI 更新在主執行緒
                            self.after(0, lambda l=stripped: logging.info(f"[下載器] {l}"))
                    else:
                        if process_done:
                            break
                        time.sleep(0.3)
        except Exception as e:
            logging.warning(f"讀取下載器 log 時發生錯誤: {e}")

    def _run_downloader_process(self, exe_path: Path, url: str, o1: str, o2: str, o3: str, cwd: str) -> bool:
        logging.info("正在啟動下載器...")
        try:
            kwargs = {}
            if sys.platform == 'win32':
                kwargs['creationflags'] = subprocess.CREATE_NO_WINDOW
                
            self.current_process = subprocess.Popen(
                str(exe_path),
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                text=True,
                cwd=cwd,
                **kwargs
            )
            
            self.current_process.stdin.write(f"{url}\n{o1}\n{o2}\n{o3}\n")
            self.current_process.stdin.flush()
            self.current_process.stdin.close()
            
            log_path = Path(cwd) / 'bili_novel.log'
            tail_thread = threading.Thread(
                target=self._tail_log_file,
                args=(log_path,),
                daemon=True
            )
            tail_thread.start()
            
            start_time = time.time()
            while self.current_process.poll() is None:
                if self.cancel_event.is_set():
                    self.current_process.kill()
                    self.current_process.wait()
                    return False
                    
                elapsed = int(time.time() - start_time)
                hrs, rem = divmod(elapsed, 3600)
                mins, secs = divmod(rem, 60)
                time_str = f"{hrs:02d}:{mins:02d}:{secs:02d}" if hrs > 0 else f"{mins:02d}:{secs:02d}"
                    
                self.after(0, lambda ts=time_str: self.progress_var.set(f"狀態: 下載中...   (已耗時 {ts})  "))
                time.sleep(1)
                
                if elapsed > 18000:
                    self.current_process.kill()
                    self.current_process.wait()
                    self.after(0, lambda: self.progress_var.set("狀態: 下載超時"))
                    logging.error("下載超時 (5小時)，已強制終止。")
                    return False
            
            self.current_process.wait()
            if self.current_process.returncode == 0:
                logging.info("下載程序執行完畢。")
                self.after(0, lambda: self.progress_var.set("狀態: 下載完成，進行後續轉換..."))
                return True
            else:
                if not self.cancel_event.is_set():
                    logging.error(f"下載器異常退出，代碼: {self.current_process.returncode}")
                return False
                
        except Exception as e:
            if not self.cancel_event.is_set():
                logging.error(f"執行下載器錯誤: {e}")
            return False
        finally:
            if self.current_process:
                try:
                    if self.current_process.poll() is None:
                        self.current_process.kill()
                        self.current_process.wait()
                except: pass
                self.current_process = None
            time.sleep(1)

    def _main_logic(self, url, o1, o2, o3):
        kill_existing_packer()
        base_path = get_base_path()
        exe_path = find_downloader_exe(base_path)
        if not exe_path:
            logging.error("在 tools 目錄下找不到 bili_novel_packer 執行檔！")
            raise FileNotFoundError("找不到下載器執行檔")
        
        log_dir = base_path / 'log'
        log_dir.mkdir(exist_ok=True)
        temp_dir = base_path / 'temp'
        temp_dir.mkdir(parents=True, exist_ok=True)
        
        clear_temp_directory(temp_dir)
        
        if self.cancel_event.is_set(): return

        if self._run_downloader_process(exe_path, url, o1, o2, o3, cwd=str(temp_dir)):
            if self.cancel_event.is_set(): return
            
            log_file = temp_dir / 'bili_novel.log'
            if log_file.exists():
                for i in range(5):
                    try:
                        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
                        target_log = log_dir / f'bili_novel_{timestamp}.log'
                        shutil.move(str(log_file), str(target_log))
                        logging.info(f"下載器 Log 已儲存為: {target_log.name}")
                        break
                    except Exception as e:
                        if i == 4: logging.warning(f"移動 log 檔失敗: {e}")
                        time.sleep(1)
                    
            new_folders = [x for x in temp_dir.iterdir() if x.is_dir() and x.name not in {'.ipynb_checkpoints'}]
            
            if new_folders:
                new_folder = max(new_folders, key=lambda p: p.stat().st_mtime)
                logging.info(f"偵測到新下載資料夾: {new_folder.name}")
                process_downloaded_folder(new_folder, base_path)
                
                # 改進7: 收集摘要資訊
                summary = {
                    'epub_count': 0,
                    'folder_name': S2TW_CONVERTER.convert(new_folder.name),
                }
                trad_dir = base_path / DIR_DOWNLOADS / DIR_TRAD
                if trad_dir.exists():
                    tw_folder_name = sanitize_filename(S2TW_CONVERTER.convert(new_folder.name))
                    result_folder = trad_dir / tw_folder_name
                    if result_folder.exists():
                        summary['epub_count'] = len(list(result_folder.glob('*.epub')))
                return summary
            else:
                logging.warning("下載器執行完畢，但未偵測到新資料夾產生。")
                raise Exception("未偵測到下載檔案，下載可能已失敗 (請檢查日誌)。")
        else:
            if not self.cancel_event.is_set():
                logging.error("下載失敗，終止後續處理。")
                raise Exception("核心下載程序執行失敗。")
        return {}

if __name__ == "__main__":
    app = Application()
    app.mainloop()