import logging
from pathlib import Path
from typing import Optional

import psutil


def find_downloader_exe(base_path: Path) -> Optional[Path]:
    """動態尋找 tools 目錄下的下載器執行檔"""
    tools_dir = base_path / 'tools'
    exe_paths = list(tools_dir.glob('bili_novel_packer*.exe'))
    if exe_paths:
        return exe_paths[0]
    return None

def _packer_pid_file(temp_dir: Path) -> Path:
    return temp_dir / 'packer.pid'

def write_packer_pid(temp_dir: Path, pid: int):
    """記錄本程式啟動的下載器 PID，供下次啟動時清理異常殘留。"""
    try:
        temp_dir.mkdir(parents=True, exist_ok=True)
        _packer_pid_file(temp_dir).write_text(str(pid), encoding='utf-8')
    except Exception as e:
        logging.warning(f"寫入下載器 PID 檔失敗: {e}")

def clear_packer_pid(temp_dir: Path):
    """清除 PID 檔（下載正常結束或清理完畢後呼叫）。"""
    try:
        _packer_pid_file(temp_dir).unlink(missing_ok=True)
    except Exception as e:
        logging.warning(f"刪除下載器 PID 檔失敗: {e}")

def cleanup_orphaned_packer(temp_dir: Path, exe_path: Path):
    """清理上次因異常殘留的下載器進程"""
    pid_file = _packer_pid_file(temp_dir)
    try:
        if not pid_file.exists():
            return
        pid = int(pid_file.read_text(encoding='utf-8').strip())
    except (ValueError, OSError):
        clear_packer_pid(temp_dir)
        return

    try:
        proc = psutil.Process(pid)
        if Path(proc.exe()).resolve() == exe_path.resolve():
            proc.kill()
            proc.wait(timeout=5)
            logging.info(f"已清理上次殘留的下載器進程 (PID {pid})。")
    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess, psutil.TimeoutExpired):
        pass
    except Exception as e:
        logging.warning(f"清理殘留下載器時發生錯誤: {e}")
    finally:
        clear_packer_pid(temp_dir)