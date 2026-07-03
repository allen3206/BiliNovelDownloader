import re
from typing import Optional

import requests

from core import USER_AGENT

APP_REPO = 'allen3206/BiliNovelDownloader'
PACKER_REPO = 'Montaro2017/bili_novel_packer'

_PACKER_VERSION_PATTERN = re.compile(r'bili_novel_packer-(\d+(?:\.\d+)+)', re.IGNORECASE)


def parse_version(text) -> Optional[tuple]:
    """把 'v1.4.0' 這類版號字串轉成數字 tuple，解析失敗回 None"""
    if not text:
        return None
    m = re.fullmatch(r'v?(\d+(?:\.\d+)*)', text.strip(), re.IGNORECASE)
    if not m:
        return None
    return tuple(int(p) for p in m.group(1).split('.'))


def is_newer(remote: Optional[str], local: Optional[str]) -> bool:
    """遠端版號是否比本地新，任一邊解析不出一律視為否"""
    r, l = parse_version(remote), parse_version(local)
    if r is None or l is None:
        return False
    n = max(len(r), len(l))
    return r + (0,) * (n - len(r)) > l + (0,) * (n - len(l))


def get_local_packer_version(exe_path) -> Optional[str]:
    """從下載器 exe 檔名解析版號"""
    if not exe_path:
        return None
    m = _PACKER_VERSION_PATTERN.search(exe_path.name)
    return m.group(1) if m else None


def check_target(repo: str, local: Optional[str]) -> dict:
    """查一個 repo 的最新 release 並與本地版本比對

    回傳 dict：
      local   本地版號（可能為 None）
      latest  最新 release 的 tag（查詢失敗為 None）
      url     該 release 的頁面網址
      status  'update' 有新版 / 'latest' 已是最新 / 'no_local' 本地版本無法判定
              / 'ratelimit' API 限額 / 'network' 其他網路或解析錯誤
    """
    result = {'local': local, 'latest': None, 'url': None}
    headers = {'User-Agent': USER_AGENT, 'Accept': 'application/vnd.github+json'}
    try:
        resp = requests.get(f'https://api.github.com/repos/{repo}/releases/latest',
                            headers=headers, timeout=10)
        if resp.status_code == 404:
            # /releases/latest 只認正式版，bili_novel_packer 把所有 release 都標成 pre-release 會回 404，改列清單取最新的非草稿版
            resp = requests.get(f'https://api.github.com/repos/{repo}/releases?per_page=10',
                                headers=headers, timeout=10)
            if resp.status_code in (403, 429):
                result['status'] = 'ratelimit'
                return result
            resp.raise_for_status()
            data = next((item for item in resp.json() if not item.get('draft')), {})
        else:
            if resp.status_code in (403, 429):
                result['status'] = 'ratelimit'
                return result
            resp.raise_for_status()
            data = resp.json()
        result['latest'] = data.get('tag_name') or None
        result['url'] = data.get('html_url') or f'https://github.com/{repo}/releases'
    except Exception:
        result['status'] = 'network'
        return result

    if result['latest'] is None:
        result['status'] = 'network'
    elif parse_version(local) is None:
        result['status'] = 'no_local'
    elif is_newer(result['latest'], local):
        result['status'] = 'update'
    else:
        result['status'] = 'latest'
    return result