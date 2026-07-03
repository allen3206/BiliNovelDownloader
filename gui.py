import os
import re
import sys
import time
import json
import ctypes
import shutil
import logging
import threading
import subprocess
import webbrowser
import tkinter as tk
import socket as _socket
from pathlib import Path
from enum import Enum, auto
from datetime import datetime
from typing import List, Tuple
from tkinter import font as tkfont
from tkinter import ttk, messagebox

from PIL import ImageTk

from core import (DIR_DOWNLOADS, DIR_TRAD, VALID_URL_PATTERN,
                  DOWNLOAD_TIMEOUT_SECONDS, STALL_TIMEOUT_SECONDS,
                  get_resource_path, get_base_path, APP_VERSION, sanitize_filename)
from scraper import NovelScraper, NovelInfo, classify_error, packer_log_is_network_error
from epub_tools import S2TW_CONVERTER, process_downloaded_folder, clear_temp_directory
from packer import find_downloader_exe, write_packer_pid, clear_packer_pid, cleanup_orphaned_packer
from updater import APP_REPO, PACKER_REPO, get_local_packer_version, check_target


_SINGLE_INSTANCE_PORT = 19876
_lock_socket = None

def _listen_for_reactivation(app: 'Application'):
    """第一個實例在背景監聽，收到 show 訊號就把視窗帶到前景"""
    def _server():
        while True:
            try:
                conn, _ = _lock_socket.accept()
                msg = conn.recv(16).decode(errors='ignore').strip()
                conn.close()
                if msg == "show":
                    app.after(0, _bring_to_front, app)
            except Exception:
                break
    threading.Thread(target=_server, daemon=True).start()

def _bring_to_front(app: 'Application'):
    """把視窗帶到最前面"""
    app.deiconify()
    app.lift()
    app.focus_force()
    try:
        ctypes.windll.user32.FlashWindow(int(app.wm_frame()), True)
    except Exception:
        pass

def ensure_single_instance() -> bool:
    """
    回傳 True  : 這是第一個實例，可以繼續啟動
    回傳 False : 已有實例在跑，已發送 show 訊號，應靜默退出
    """
    global _lock_socket
    sock = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
    sock.setsockopt(_socket.SOL_SOCKET, _socket.SO_REUSEADDR, 0)
    try:
        sock.bind(('127.0.0.1', _SINGLE_INSTANCE_PORT))
        sock.listen(5)
        _lock_socket = sock
        return True
    except OSError:
        sock.close()
        try:
            s = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
            s.connect(('127.0.0.1', _SINGLE_INSTANCE_PORT))
            s.sendall(b"show")
            s.close()
        except Exception:
            pass
        return False

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
        icon_path = get_resource_path('icon.ico')
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
        self._abort_reason = None
        self.last_activity_time = 0
        self.last_checked_url = ""
        self._pg_reset()

        self._update_results = None       # 更新檢查結果，主程式與核心下載器各一筆
        self._update_checking = False
        self._update_menu_added = False
        self._update_win = None

        self.create_widgets()
        self.setup_logging()
        self._start_update_check()
        
        # 綁定視窗關閉事件
        self.protocol("WM_DELETE_WINDOW", self.on_closing)

    def _load_history(self) -> list:
        history_path = get_base_path() / 'history.json'
        if history_path.exists():
            try:
                return json.loads(history_path.read_text(encoding='utf-8'))
            except Exception as e:
                logging.warning(f"歷史紀錄檔讀取失敗，已重置: {e}")
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
        try:
            menu.tk_popup(x, y)
        finally:
            menu.grab_release()

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
        self._build_menu()
        self._build_url_frame()
        self._build_settings_frame()
        self._build_run_frame()
        self._build_bottom_frame()

    def _build_menu(self):
        self.menubar = tk.Menu(self)
        file_menu = tk.Menu(self.menubar, tearoff=0)
        file_menu.add_command(label="開啟下載資料夾", command=self._open_downloads_folder)
        self.menubar.add_cascade(label="檔案", menu=file_menu)
        help_menu = tk.Menu(self.menubar, tearoff=0)
        help_menu.add_command(label="檢查更新", command=lambda: self._show_update_window(recheck=True))
        help_menu.add_command(label="關於", command=self._show_about)
        self.menubar.add_cascade(label="說明", menu=help_menu)
        self.config(menu=self.menubar)

    def _show_about(self):
        win = tk.Toplevel(self)
        win.title("關於")
        win.resizable(False, False)
        win.transient(self)
        icon_path = get_resource_path('icon.ico')
        if icon_path.exists():
            try:
                win.iconbitmap(str(icon_path))
            except Exception:
                pass
        frm = ttk.Frame(win, padding=20)
        frm.pack(fill="both", expand=True)
        ttk.Label(frm, text=f"BiliNovelDownloader {APP_VERSION}",
                  font=("Microsoft JhengHei", 12, "bold")).pack(anchor="w")
        ttk.Label(frm, text="嗶哩輕小說自動下載與繁化工具").pack(anchor="w", pady=(0, 10))

        def add_link(text, url):
            lbl = ttk.Label(frm, text=text, foreground="#0066CC", cursor="hand2")
            lbl.pack(anchor="w")
            lbl.bind("<Button-1>", lambda e: webbrowser.open(url))

        add_link("https://github.com/allen3206/BiliNovelDownloader",
                 "https://github.com/allen3206/BiliNovelDownloader")
        ttk.Label(frm, text="\n核心下載功能由 bili_novel_packer（Montaro2017）提供").pack(anchor="w")
        add_link("https://github.com/Montaro2017/bili_novel_packer",
                 "https://github.com/Montaro2017/bili_novel_packer")
        ttk.Label(frm, text="\nMIT License").pack(anchor="w")

        btnf = ttk.Frame(frm)
        btnf.pack(fill="x", pady=(15, 0))
        ttk.Button(btnf, text="第三方授權", command=self._open_third_party_licenses).pack(side="left")

        # 置中於主視窗
        win.grab_set()
        win.update_idletasks()
        w, h = win.winfo_width(), win.winfo_height()
        x = self.winfo_rootx() + (self.winfo_width() - w) // 2
        y = self.winfo_rooty() + (self.winfo_height() - h) // 2
        win.geometry(f"+{max(x, 0)}+{max(y, 0)}")

    def _open_third_party_licenses(self):
        path = get_base_path() / 'THIRD_PARTY_LICENSES.txt'
        if not path.exists():
            messagebox.showinfo("第三方授權", "找不到 THIRD_PARTY_LICENSES.txt。")
            return
        try:
            os.startfile(str(path))
        except Exception:
            webbrowser.open(path.as_uri())

    def _open_downloads_folder(self):
        folder = get_base_path() / DIR_DOWNLOADS
        try:
            folder.mkdir(parents=True, exist_ok=True)
            os.startfile(str(folder))
        except Exception as e:
            messagebox.showerror("錯誤", f"無法開啟下載資料夾:\n{e}")

    def _start_update_check(self):
        """背景查一次更新，已在查則不重複啟動"""
        if self._update_checking:
            return
        self._update_checking = True
        self._refresh_update_window()
        threading.Thread(target=self._thread_check_updates, daemon=True).start()

    def _thread_check_updates(self):
        packer_exe = find_downloader_exe(get_base_path())
        results = {
            'app': check_target(APP_REPO, APP_VERSION),
            'packer': check_target(PACKER_REPO, get_local_packer_version(packer_exe)),
        }
        self._update_results = results
        self._update_checking = False
        self.after(0, self._apply_update_results)

    def _apply_update_results(self):
        results = self._update_results or {}
        if (not self._update_menu_added
                and any(r.get('status') == 'update' for r in results.values())):
            self.menubar.add_command(label="有可用更新",
                                     command=lambda: self._show_update_window(recheck=False))
            self._update_menu_added = True
        self._refresh_update_window()

    def _show_update_window(self, recheck=False):
        if self._update_win is not None and self._update_win.winfo_exists():
            self._update_win.lift()
            self._update_win.focus_force()
        else:
            self._build_update_window()
        if recheck or self._update_results is None:
            self._start_update_check()
        self._refresh_update_window()

    def _build_update_window(self):
        win = tk.Toplevel(self)
        win.title("檢查更新")
        win.resizable(False, False)
        win.transient(self)
        icon_path = get_resource_path('icon.ico')
        if icon_path.exists():
            try:
                win.iconbitmap(str(icon_path))
            except Exception:
                pass
        frm = ttk.Frame(win, padding=20)
        frm.pack(fill="both", expand=True)

        local_packer = get_local_packer_version(find_downloader_exe(get_base_path()))
        self._upd_labels = {}
        rows = (('app', '主程式', APP_VERSION),
                ('packer', '核心下載器', f"v{local_packer}" if local_packer else "無法判定"))
        for i, (key, name, current) in enumerate(rows):
            base = i * 2
            pad_top = (0, 0) if i == 0 else (10, 0)
            ttk.Label(frm, text=name, font=("Microsoft JhengHei", 10, "bold")).grid(
                row=base, column=0, sticky="w", pady=pad_top)
            ttk.Label(frm, text=current).grid(row=base, column=1, sticky="w", padx=(15, 0), pady=pad_top)
            ttk.Label(frm, text="最新版本").grid(row=base + 1, column=0, sticky="w")
            latest = ttk.Label(frm, text="檢查中…", foreground="gray")
            latest.grid(row=base + 1, column=1, sticky="w", padx=(15, 0))
            link = ttk.Label(frm, text="前往下載", foreground="#0066CC", cursor="hand2")
            link.grid(row=base + 1, column=2, sticky="w", padx=(10, 0))
            link.grid_remove()
            self._upd_labels[key] = {'latest': latest, 'link': link}

        self._upd_warn = ttk.Label(frm, text="", foreground="#B25900", wraplength=320)
        self._upd_warn.grid(row=4, column=0, columnspan=3, sticky="w", pady=(12, 0))
        self._upd_warn.grid_remove()

        btnf = ttk.Frame(frm)
        btnf.grid(row=5, column=0, columnspan=3, sticky="e", pady=(15, 0))
        self._upd_recheck_btn = ttk.Button(btnf, text="重新檢查",
                                           command=lambda: self._show_update_window(recheck=True))
        self._upd_recheck_btn.pack(side="left", padx=(0, 5))
        ttk.Button(btnf, text="關閉", command=win.destroy).pack(side="left")

        self._update_win = win
        win.grab_set()
        win.update_idletasks()
        w, h = win.winfo_width(), win.winfo_height()
        x = self.winfo_rootx() + (self.winfo_width() - w) // 2
        y = self.winfo_rooty() + (self.winfo_height() - h) // 2
        win.geometry(f"+{max(x, 0)}+{max(y, 0)}")

    def _refresh_update_window(self):
        """把目前的檢查狀態反映到檢查更新視窗（未開啟時不做事）"""
        if self._update_win is None or not self._update_win.winfo_exists():
            return
        results = self._update_results or {}
        checking = self._update_checking
        for key in ('app', 'packer'):
            widgets = self._upd_labels[key]
            r = results.get(key)
            if checking or not r:
                widgets['latest'].config(text="檢查中…" if checking else "—", foreground="gray")
                widgets['link'].grid_remove()
                continue
            status = r['status']
            if status == 'update':
                widgets['latest'].config(text=r['latest'], foreground="#0066CC")
                widgets['link'].bind("<Button-1>", lambda e, u=r['url']: webbrowser.open(u))
                widgets['link'].grid()
            elif status == 'latest':
                widgets['latest'].config(text=f"{r['latest']}（已是最新）", foreground="green")
                widgets['link'].grid_remove()
            elif status == 'no_local':
                widgets['latest'].config(text=r['latest'] or "—", foreground="black")
                widgets['link'].grid_remove()
            else:
                widgets['latest'].config(text="—", foreground="gray")
                widgets['link'].grid_remove()

        statuses = [r.get('status') for r in results.values()]
        if checking:
            warn = ""
        elif 'ratelimit' in statuses:
            warn = "查詢過於頻繁，請稍後再試。"
        elif 'network' in statuses:
            warn = "無法取得更新資訊，請檢查網路連線。"
        else:
            warn = ""
        if warn:
            self._upd_warn.config(text=warn)
            self._upd_warn.grid()
        else:
            self._upd_warn.grid_remove()
        self._upd_recheck_btn.config(state="disabled" if checking else "normal")

    def _build_url_frame(self):
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

        preview_frame = tk.Frame(frame_url)
        preview_frame.pack(fill="x", padx=10, pady=5)
        
        self.cover_container = tk.Frame(preview_frame, width=200, height=280, bg="lightgray")
        self.cover_container.pack(side="left", padx=(0, 10))
        self.cover_container.pack_propagate(False)
        
        self.cover_label = tk.Label(self.cover_container, text="封面縮圖預覽", bg="lightgray", wraplength=150)
        self.cover_label.pack(expand=True, fill="both")
        
        info_text_frame = tk.Frame(preview_frame)
        info_text_frame.pack(side="left", fill="both", expand=True)
        
        self.title_text = self._make_info_field(info_text_frame, ("Microsoft JhengHei", 12, "bold"))
        self.title_text.pack(fill="x", pady=(0, 5))
        self.author_text = self._make_info_field(info_text_frame, ("Microsoft JhengHei", 10))
        self.author_text.pack(fill="x", pady=2)
        self.meta_text = self._make_info_field(info_text_frame, ("Microsoft JhengHei", 10))
        self.meta_text.pack(fill="x", pady=2)
        self.update_text = self._make_info_field(info_text_frame, ("Microsoft JhengHei", 10))
        self.update_text.pack(fill="x", pady=2)

        self._set_info_field(self.title_text, "書名：尚未載入")
        self._set_info_field(self.author_text, "-")
        self._set_info_field(self.meta_text, "-")
        self._set_info_field(self.update_text, "-")
        
        ttk.Label(info_text_frame, text="簡介：", font=("Microsoft JhengHei", 10)).pack(anchor="nw", pady=(2, 0))

        desc_container = tk.Frame(info_text_frame)
        desc_container.pack(anchor="nw", fill="x", expand=True, pady=(0, 5))
        desc_container.columnconfigure(0, weight=1)

        desc_v_scroll = ttk.Scrollbar(desc_container, orient="vertical")
        self.desc_text = tk.Text(desc_container, height=7, font=("Microsoft JhengHei", 10), foreground="black",
                                 wrap="word", borderwidth=0, highlightthickness=0, insertwidth=0)
        self.desc_text.bind("<Key>", self._readonly_text_key)  # 唯讀但可選取

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
        self._set_desc("-")

        self.status_label = ttk.Label(info_text_frame, text="", foreground="blue")
        self.status_label.pack(anchor="nw", pady=(5, 0))

    def _readonly_text_key(self, event):
        """讓 Text 唯讀但仍可選取, 複製"""
        if event.state & 0x0004 and event.keysym.lower() in ("c", "insert"):
            return  # Ctrl+C, Ctrl+Insert 複製
        if not (event.state & 0x0004) and event.keysym in (
            "Left", "Right", "Up", "Down", "Home", "End", "Prior", "Next",
            "Shift_L", "Shift_R", "Control_L", "Control_R",
        ):
            return  # 移動游標與選取用的鍵
        return "break"

    def _autosize_info_field(self, widget):
        """依實際換行後的顯示行數調整 Text 高度，讓長字串完整顯示不被裁切"""
        try:
            res = widget.count("1.0", "end-1c", "displaylines")
            n = res[0] if isinstance(res, tuple) else res
        except Exception:
            n = 1
        n = max(int(n or 1), 1)
        if int(widget.cget("height")) != n:
            widget.configure(height=n)

    def _make_info_field(self, parent, font):
        """建立唯讀、可選取、會自動換行並依內容調整高度的資訊欄位"""
        txt = tk.Text(parent, font=font, height=1, wrap="word", relief="flat",
                      borderwidth=0, highlightthickness=0, padx=0, pady=0,
                      insertwidth=0, bg=self.cget("bg"))
        txt.bind("<Key>", self._readonly_text_key)
        txt.bind("<Configure>", lambda e, w=txt: self._autosize_info_field(w))
        return txt

    def _set_info_field(self, widget, text):
        """更新資訊欄位內容並在版面就緒後重算高度"""
        widget.delete("1.0", "end")
        widget.insert("1.0", text)
        widget.after_idle(lambda: self._autosize_info_field(widget))

    def _set_desc(self, text):
        """更新簡介內容（唯讀 Text，免切換 state）"""
        self.desc_text.delete("1.0", "end")
        self.desc_text.insert("1.0", text)

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
            self._pg_reset()
            self.check_btn.config(state="disabled")
            self.start_btn.config(text="取消下載", command=self.cancel_download)
            self.progress_canvas.pack(pady=(2, 0))
            self.progress_canvas.coords(self.progress_rect, -100, 0, 0, 14)
            self._animate_progress_bar()

    def _animate_progress_bar(self):
        """進度條：有真實章節進度時顯示填充，否則退回原滑動動畫"""
        if self.app_state != AppState.DOWNLOADING:
            return
        coords = self.progress_canvas.coords(self.progress_rect)
        if not coords:
            return

        frac = self._real_progress_fraction()
        if frac is not None:
            # 真實進度：從左端填到對應比例
            self.progress_canvas.coords(self.progress_rect, 0, 0, frac * 500, 14)
            # 進度數字與進度條同一輪更新，避免數字比進度條慢
            if not self._pg_complete:
                text = self._build_progress_text(self._pg_elapsed_str)
                if text != self._pg_last_label:
                    self._pg_last_label = text
                    self.progress_var.set(text)
        else:
            # fallback：尚無可用進度標記時維持滑動動畫
            x1, y1, x2, y2 = coords
            step, width = 6, 100
            x1 += step
            x2 += step
            if x1 > 500:
                x1, x2 = -width, 0
            self.progress_canvas.coords(self.progress_rect, x1, y1, x2, y2)

        self.after(20, self._animate_progress_bar)

    def _pg_reset(self):
        """重置真實進度追蹤狀態（每次下載開始時呼叫）"""
        self._pg_vols = None              # 卷名清單，來自 packer 的 PackArgument
        self._pg_total = []               # 每卷總章數（該卷列目錄的 ==> 行數）
        self._pg_fetch = []               # 每卷已抓取的章節頁數（GET {url} OK）
        self._pg_res = []                 # 每卷已解析／打包完成的章數（OK 書名 行）
        self._pg_cur = -1                 # 目前顯示的卷索引（0 起算，尚未開始為 -1）
        self._pg_url_vol = {}             # 章節網址 → 卷索引，列目錄時建立
        self._pg_fetched_urls = set()     # 已計入抓取的章節網址，避免重試重複計數
        self._pg_active = False           # 是否已有抓取／解析活動，決定顯示真實進度或動畫
        self._pg_complete = False         # 下載器成功結束，進度補滿 100%
        self._pg_frac_hi = 0.0            # 已顯示過的最高比例，用來鉗制成只進不退
        self._pg_elapsed_str = "00:00"    # 由監看執行緒每秒更新，供進度標籤顯示耗時
        self._pg_last_label = None        # 已顯示的標籤文字，避免重複 set

    def _match_vol(self, body: str) -> int:
        """判斷這行章節屬於哪一卷，取最長相符的卷名以避免前綴撞名，找不到回 -1"""
        best, best_len = -1, -1
        for k, name in enumerate(self._pg_vols):
            if body.startswith(name) and len(name) > best_len:
                best, best_len = k, len(name)
        return best

    def _update_progress_from_line(self, raw_line: str):
        """從下載器 log 逐行解析進度訊號（在 tail 執行緒呼叫）

        下載一章要經兩階段：先「抓頁面」(`GET {url} OK`)、再「解析＋圖片＋打包」(`OK 書名`)
        列目錄的 ==> 行同時帶卷名與章節網址，用來建「網址→卷索引」表並累計總章數
        抓頁面與解析各算半步，進度條到滿代表兩階段都完成，避免頁抓完就滿進度條還在跑
        0.2.43 兩階段交錯、0.2.44 先抓完再解析，皆適用
        """
        content = raw_line.lstrip('│ \t')
        if self._pg_vols is None:
            if content.startswith('PackArgument'):
                m = re.search(r'packVolumes:\s*\[(.*)\]', content)
                if m and m.group(1).strip():
                    vols = [v.strip() for v in m.group(1).split(', ') if v.strip()]
                    if vols:
                        self._pg_vols = vols
                        self._pg_total = [0] * len(vols)
                        self._pg_fetch = [0] * len(vols)
                        self._pg_res = [0] * len(vols)
            return
        if content.startswith('==> '):
            body = content[4:]
            k = self._match_vol(body)
            if k >= 0:
                self._pg_total[k] += 1
                self._pg_url_vol[body.rsplit(' ', 1)[-1]] = k  # 最後一個 token 是章節網址
        elif content.startswith('GET ') and content.endswith(' OK'):
            parts = content.split(' ')
            if len(parts) >= 2:
                url = parts[1]
                k = self._pg_url_vol.get(url)  # 只認章節頁網址，圖片等其他 GET 不在表中
                if k is not None and url not in self._pg_fetched_urls and self._pg_fetch[k] < self._pg_total[k]:
                    self._pg_fetched_urls.add(url)
                    self._pg_fetch[k] += 1
                    self._pg_cur = k
                    self._pg_active = True
        elif content.startswith('OK '):
            k = self._match_vol(content[3:])
            if k >= 0 and self._pg_res[k] < self._pg_total[k]:
                self._pg_res[k] += 1
                self._pg_cur = k
                self._pg_active = True

    def _real_progress_fraction(self):
        """回傳整體進度比例 0.0~1.0；尚無可用資料時回 None（改用滑動動畫）"""
        if self._pg_complete:
            return 1.0
        if not self._pg_active or not self._pg_vols or self._pg_cur < 0:
            return None
        # 每卷各佔 1/N，卷內以（已抓頁 + 已解析）/（2×總章）填充，兩階段各半步
        n = len(self._pg_vols)
        acc = 0.0
        for k in range(n):
            t = self._pg_total[k]
            if t > 0:
                acc += min((self._pg_fetch[k] + self._pg_res[k]) / (2 * t), 1.0)
        overall = min(max(acc / n, 0.0), 1.0)
        # 單調鉗制：只前進不後退
        if overall < self._pg_frac_hi:
            overall = self._pg_frac_hi
        else:
            self._pg_frac_hi = overall
        return overall

    def _build_progress_text(self, time_str: str) -> str:
        """組合進度標籤文字；顯示章數由半步進度換算，與進度條同步"""
        if not self._pg_active or not self._pg_vols or self._pg_cur < 0:
            return f"狀態: 下載中...   (已耗時 {time_str})  "
        k = self._pg_cur
        total = self._pg_total[k]
        if total <= 0:
            return f"狀態: 下載中...   (已耗時 {time_str})  "
        done = min(int((self._pg_fetch[k] + self._pg_res[k]) / 2), total)
        if len(self._pg_vols) > 1:
            core = f"第 {k + 1}/{len(self._pg_vols)} 卷 · 本卷 {done}/{total} 章"
        else:
            core = f"{done}/{total} 章"
        return f"下載中… {core} · 已耗時 {time_str}"

    def reset_info_labels(self):
        self._set_info_field(self.title_text, "書名：尚未載入")
        self._set_info_field(self.author_text, "-")
        self._set_info_field(self.meta_text, "-")
        self._set_info_field(self.update_text, "-")
        self._set_desc("-")

        for item in self.vol_tree_id.get_children(): self.vol_tree_id.delete(item)
        for item in self.vol_tree_name.get_children(): self.vol_tree_name.delete(item)
        
        self.cover_label.config(image='', text="正在載入封面...", width=28, height=14)
        self.cover_label.image = None

    def check_novel_info(self, event=None, auto_start=False):
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
        self._set_info_field(self.title_text, "書名：載入中...")
        
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
            self.after(0, self._reset_state_after_check)

    def _reset_state_after_check(self):
        if self.app_state == AppState.CHECKING:
            self.set_app_state(AppState.IDLE)

    def _apply_info_to_ui(self, info: NovelInfo):
        self._set_info_field(self.title_text, f"書名：{info.title}")
        self._set_info_field(self.author_text, f"作者：{info.author}")
        self._set_info_field(self.meta_text, f"{info.status} | {info.tags} | {info.rating}")
        self._set_info_field(self.update_text, f"最新進度：{info.latest} (更新時間：{info.update_time})")

        # 修正微軟正黑體會將 em-dash (— 或 ―) 顯示為上橫線的字體渲染 Bug
        # 改用製表符 (Box Drawing Light Horizontal U+2500) 讓線條相連
        display_desc = info.desc.replace('—', '─').replace('―', '─')
        self._set_desc(display_desc)

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
        self._set_info_field(self.title_text, "書名：載入失敗")
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
        self._abort_reason = None
        try:
            summary = self._main_logic(url, o1, o2, o3)
            elapsed = int(time.time() - start_time)
            mins, secs = divmod(elapsed, 60)
            
            if self.cancel_event.is_set():
                self.after(0, lambda: self.progress_var.set("狀態: 程序已由使用者取消"))
                self.after(0, lambda: messagebox.showwarning("已取消", "程序已終止。"))
            elif self._abort_reason == "stall":
                self.after(0, lambda: self.progress_var.set("狀態: 下載停滯，已終止"))
                self.after(0, lambda: messagebox.showwarning("下載停滯",
                    f"下載已停滯（{STALL_TIMEOUT_SECONDS // 60} 分鐘無回應），可能是網路中斷或伺服器無回應。\n\n請檢查網路後再試一次。"))
            elif self._abort_reason == "timeout":
                self.after(0, lambda: self.progress_var.set("狀態: 下載超時，已終止"))
                self.after(0, lambda: messagebox.showwarning("下載超時",
                    f"下載超過 {DOWNLOAD_TIMEOUT_SECONDS // 3600} 小時上限，已強制終止。"))
            elif self._abort_reason == "network":
                self.after(0, lambda: self.progress_var.set("狀態: 無法連線到伺服器"))
                self.after(0, lambda: messagebox.showerror("網路連線失敗",
                    "無法連線到下載伺服器 www.bilinovel.com，下載已中止。\n\n"
                    "可能是你的網路或 DNS 連不到這個網域。可以嘗試改公用 DNS 或開 VPN 後再試。\n\n"
                    "詳細紀錄見 log 資料夾。"))
            elif self._abort_reason == "error":
                self.after(0, lambda: self.progress_var.set("狀態: 下載器異常結束"))
                self.after(0, lambda: messagebox.showerror("下載失敗",
                    "下載器異常結束，請稍後再試。\n\n若持續發生，可查看 log 資料夾的紀錄。"))
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
                            self.last_activity_time = time.time()
                            self._update_progress_from_line(stripped)
                            # 用 after(0, ...) 確保 UI 更新在主執行緒
                            self.after(0, lambda l=stripped: logging.info(f"[下載器] {l}"))
                    else:
                        if process_done:
                            break
                        time.sleep(0.3)
        except Exception as e:
            logging.warning(f"讀取下載器 log 時發生錯誤: {e}")

    def _classify_packer_failure(self, log_path: Path) -> str:
        """讀下載器 log 判斷失敗型態：網路／DNS 問題回 'network'，其餘回 'error'。"""
        try:
            log_text = log_path.read_text(encoding='utf-8', errors='ignore')
        except OSError:
            return "error"
        return "network" if packer_log_is_network_error(log_text) else "error"

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
            write_packer_pid(Path(cwd), self.current_process.pid)

            self.current_process.stdin.write(f"{url}\n{o1}\n{o2}\n{o3}\n")
            self.current_process.stdin.flush()
            self.current_process.stdin.close()
            
            log_path = Path(cwd) / 'bili_novel.log'
            tail_thread = threading.Thread(
                target=self._tail_log_file,
                args=(log_path,),
                daemon=True
            )
            self.last_activity_time = time.time()
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
                    
                self._pg_elapsed_str = time_str
                # 有進度後由動畫迴圈更新標籤（與進度條同步），還沒進度時這裡先顯示耗時
                if not self._pg_active:
                    self.after(0, lambda t=time_str: self.progress_var.set(f"狀態: 下載中...   (已耗時 {t})  "))
                time.sleep(1)
                
                if elapsed > DOWNLOAD_TIMEOUT_SECONDS:
                    self.current_process.kill()
                    self.current_process.wait()
                    self._abort_reason = "timeout"
                    logging.error(f"下載超時 ({DOWNLOAD_TIMEOUT_SECONDS // 3600}小時)，已強制終止。")
                    return False

                if time.time() - self.last_activity_time > STALL_TIMEOUT_SECONDS:
                    self.current_process.kill()
                    self.current_process.wait()
                    self._abort_reason = "stall"
                    logging.error(f"下載停滯（{STALL_TIMEOUT_SECONDS // 60} 分鐘無回應），已強制終止。")
                    return False
            
            self.current_process.wait()
            if self.current_process.returncode == 0:
                logging.info("下載程序執行完畢。")
                self._pg_complete = True
                self.after(0, lambda: self.progress_var.set("狀態: 下載完成，進行後續轉換..."))
                return True
            else:
                if not self.cancel_event.is_set():
                    logging.error(f"下載器異常退出，代碼: {self.current_process.returncode}")
                    self._abort_reason = self._classify_packer_failure(log_path)
                return False
                
        except Exception as e:
            if not self.cancel_event.is_set():
                logging.error(f"執行下載器錯誤: {e}")
                self._abort_reason = "error"
            return False
        finally:
            if self.current_process:
                try:
                    if self.current_process.poll() is None:
                        self.current_process.kill()
                        self.current_process.wait()
                except: pass
                self.current_process = None
            clear_packer_pid(Path(cwd))
            time.sleep(1)

    def _main_logic(self, url, o1, o2, o3):
        base_path = get_base_path()
        exe_path = find_downloader_exe(base_path)
        if not exe_path:
            logging.error("在 tools 目錄下找不到 bili_novel_packer 執行檔！")
            raise FileNotFoundError("找不到下載器執行檔")

        log_dir = base_path / 'log'
        log_dir.mkdir(exist_ok=True)
        temp_dir = base_path / 'temp'
        temp_dir.mkdir(parents=True, exist_ok=True)

        # 清理上次異常殘留的下載器，須在 clear_temp_directory 之前，先釋放 temp/ 的檔案鎖才能成功清空
        cleanup_orphaned_packer(temp_dir, exe_path)

        clear_temp_directory(temp_dir)
        
        if self.cancel_event.is_set(): return

        before_snapshot = {x for x in temp_dir.iterdir() if x.is_dir()}

        if self._run_downloader_process(exe_path, url, o1, o2, o3, cwd=str(temp_dir)):
            if self.cancel_event.is_set(): return
            
            log_file = temp_dir / 'bili_novel.log'
            packer_log_text = ""
            if log_file.exists():
                try:
                    packer_log_text = log_file.read_text(encoding='utf-8', errors='ignore')
                except OSError:
                    pass
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
                    
            after_snapshot = {x for x in temp_dir.iterdir() if x.is_dir()}
            new_folders = [x for x in (after_snapshot - before_snapshot) if x.name not in {'.ipynb_checkpoints'}]
            
            if new_folders:
                if len(new_folders) > 1:
                    logging.warning(f"偵測到 {len(new_folders)} 個新資料夾，將全部處理：{[f.name for f in new_folders]}")
                trad_dir = base_path / DIR_DOWNLOADS / DIR_TRAD
                total_epub = 0
                for new_folder in new_folders:
                    logging.info(f"偵測到新下載資料夾: {new_folder.name}")
                    process_downloaded_folder(new_folder, base_path)
                    if trad_dir.exists():
                        result_folder = trad_dir / sanitize_filename(S2TW_CONVERTER.convert(new_folder.name))
                        if result_folder.exists():
                            total_epub += len(list(result_folder.glob('*.epub')))
                folder_name = "、".join(S2TW_CONVERTER.convert(f.name) for f in new_folders)
                return {'epub_count': total_epub, 'folder_name': folder_name}
            else:
                logging.warning("下載器執行完畢，但未偵測到新資料夾產生。")
                if packer_log_is_network_error(packer_log_text):
                    self._abort_reason = "network"
                    return {}
                raise Exception("未偵測到下載檔案，下載可能已失敗 (請檢查日誌)。")
        else:
            if not self.cancel_event.is_set():
                logging.error("下載失敗，終止後續處理。")
        return {}