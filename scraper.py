import io
import re
import html
import time
import logging
from typing import Optional, List, Tuple
from dataclasses import dataclass, field

import requests
from bs4 import BeautifulSoup
from PIL import Image

from core import USER_AGENT, get_base_path
from epub_tools import S2TW_CONVERTER


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