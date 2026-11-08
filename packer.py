import logging
import codecs
import os
import queue
import re
import subprocess
import sys
import threading
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
from xml.etree import ElementTree

import psutil


@dataclass
class PackerResult:
    success: bool
    reason: str
    returncode: Optional[int] = None
    output_files: list[Path] = field(default_factory=list)
    message: str = ""


class PackerProtocolError(ValueError):
    pass


# 核心提示語保留上游原文，避免轉換文字後影響協定比對
_URL_PROMPTS = {
    "0.2.44": "请输入URL(支持哔哩轻小说&轻小说文库):",
    "0.2.50": "请输入链接(多个链接使用空格隔开):",
}
_VOLUME_PROMPTS = {
    "0.2.44": "请选择需要下载的分卷(可输入如1-9进行范围选择以及如2,5单独选择):",
    "0.2.50": "请选择要下载的分卷:",
}
_MERGE_PROMPT = "是否合并选择的分卷为一个文件?"
_TITLE_PROMPT = "是否在每章开头添加章节标题?"
_EXIT_PROMPT = "全部任务已完成，按回车键退出."
# 錯誤訊息尾端可能帶新版版本號，不以固定版本字串判斷失敗
_FATAL_PROMPT = re.compile(r"运行出错(?:，按回车键退出)?[.。](?:\([^\r\n()]+\))?")
_PROMPTS = tuple(_URL_PROMPTS.values()) + tuple(_VOLUME_PROMPTS.values()) + (
    _MERGE_PROMPT, _TITLE_PROMPT, _EXIT_PROMPT,
)
_ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_MAX_BUFFER = 65536
_CONSOLE_LOG_LIMIT = 2 * 1024 * 1024


def _selected_indices(value: str, menu: dict[int, str]) -> list[int]:
    """只正規化分卷編號輸入，保留卷名原文及其中的逗號。"""
    if not menu or set(menu) != set(range(1, max(menu) + 1)):
        raise PackerProtocolError("下載器分卷選單不完整。")
    value = str(value).strip().replace("，", ",")
    if value in ("", "0"):
        return list(menu)
    value = re.sub(r"\s*-\s*", "-", value)
    selected = []
    for part in re.split(r"[,\s]+", value):
        if not re.fullmatch(r"\d+(?:-\d+)?", part):
            raise PackerProtocolError(f"分卷範圍格式錯誤: {value}")
        ends = [int(n) for n in part.split("-")]
        start, end = min(ends), max(ends)
        if start < 1 or end > max(menu):
            raise PackerProtocolError(f"分卷範圍超出選單: {part}")
        for index in range(start, end + 1):
            if index not in selected:
                selected.append(index)
    return selected


class _PackerProtocol:
    """逐段讀取核心輸出，依可辨識的互動格式與順序送出答案。"""

    def __init__(self, url: str, volumes: str, merge: str, title: str, cwd: Path):
        if not url or re.search(r"\s", url):
            raise PackerProtocolError("一次下載僅接受一個完整網址。")
        if merge not in ("1", "2") or title not in ("1", "2"):
            raise PackerProtocolError("合併與章節標題選項應為 1 或 2。")
        self.url, self.volumes, self.merge, self.title = url, volumes, merge, title
        self.cwd = cwd.resolve()
        self.version = None
        self.profile = None
        self.phase = "url"
        self.menu: dict[int, str] = {}
        self.menu_complete = False
        self.selected: list[int] = []
        self.expected_outputs = 0
        self.output_files: list[Path] = []
        self.boundary = None
        self.buffer = ""

    def feed(self, text: str, final: bool = False) -> list[str]:
        self.buffer += text
        replies = []
        while "\n" in self.buffer:
            line, self.buffer = self.buffer.split("\n", 1)
            replies.extend(self._line(line))
        # 選卷與是非提問沒有換行，需另外處理尚未成行的輸出
        fragment = _ANSI.sub("", self.buffer).strip()
        if fragment in _PROMPTS:
            self.buffer = ""
            replies.extend(self._prompt(fragment))
        if _FATAL_PROMPT.fullmatch(_ANSI.sub("", self.buffer).strip()):
            raise PackerProtocolError("下載器回報執行錯誤。")
        if final and self.buffer.strip():
            line, self.buffer = self.buffer, ""
            replies.extend(self._line(line))
        elif self.buffer.strip():
            self._check_unknown_prompt(_ANSI.sub("", self.buffer).strip())
        # 限制未換行輸出的長度，避免核心異常輸出耗盡記憶體
        if len(self.buffer) > _MAX_BUFFER:
            self.buffer = self.buffer[-_MAX_BUFFER:]
        return replies

    @staticmethod
    def _check_unknown_prompt(line: str):
        if re.fullmatch(r"(?:请输入|请选择|是否|Please enter|Please select|Enter )[\s\S]*[?:：？]\s*", line):
            raise PackerProtocolError(f"下載器出現未知互動提示: {line[:200]}")

    def _line(self, raw: str) -> list[str]:
        line = _ANSI.sub("", raw).strip()
        if _FATAL_PROMPT.fullmatch(line):
            raise PackerProtocolError("下載器回報執行錯誤: " + line[:300])
        version = re.fullmatch(r"当前版本:\s*(\S+)", line)
        if version:
            if self.version is not None:
                if self.version != version.group(1):
                    raise PackerProtocolError("下載器在工作中變更版本資訊。")
                return []
            self.version = version.group(1)
            if self.version not in _URL_PROMPTS:
                logging.warning("核心版本 %s 尚未實測，將依互動格式嘗試執行；"
                                "遇到不相容的提問或完成訊號會停止。", self.version)
        if self.phase == "volumes":
            entry = re.fullmatch(r"\[\s*(\d+)\]\s*(.*)", line)
            if entry:
                index, name = int(entry.group(1)), entry.group(2)
                if index == 0:
                    self.menu_complete = True
                elif index in self.menu:
                    raise PackerProtocolError("下載器分卷選單有重複編號。")
                else:
                    self.menu[index] = name
        if self.phase == "packing" and line.startswith("打包完成:"):
            raw_path = line[len("打包完成:"):].strip()
            path = Path(raw_path)
            path = (path if path.is_absolute() else self.cwd / path).resolve()
            try:
                path.relative_to(self.cwd)
            except ValueError:
                raise PackerProtocolError("下載器輸出路徑超出本次暫存目錄。")
            if path.suffix.lower() != ".epub" or path in self.output_files:
                raise PackerProtocolError("下載器回報重複或非 EPUB 輸出。")
            if len(self.output_files) >= self.expected_outputs:
                raise PackerProtocolError("下載器回報的輸出數量超出所選分卷。")
            self.output_files.append(path)
        if line in _PROMPTS:
            return self._prompt(line)
        self._check_unknown_prompt(line)
        return []

    def _prompt(self, prompt: str) -> list[str]:
        if self.version is None:
            raise PackerProtocolError("下載器未提供可辨識的版本資訊。")
        if prompt in _URL_PROMPTS.values():
            if self.phase == "url":
                # 版本號只供提示；由實際網址提問選擇互動格式
                self.profile = next(key for key, value in _URL_PROMPTS.items() if value == prompt)
                self.phase = "volumes"
                return [self.url]
            if prompt != _URL_PROMPTS[self.profile]:
                raise PackerProtocolError("下載器在工作中變更網址提示格式。")
            if self.phase == "packing":
                self._check_complete()
                self.phase, self.boundary = "finished", "idle"
                return []
            raise PackerProtocolError("下載器提早返回網址提示，任務尚未完整完成。")
        if prompt in _VOLUME_PROMPTS.values():
            if (self.phase != "volumes" or prompt != _VOLUME_PROMPTS[self.profile]
                    or not self.menu_complete):
                raise PackerProtocolError("下載器分卷提示順序或選單格式異常。")
            self.selected = _selected_indices(self.volumes, self.menu)
            self.expected_outputs = (1 if len(self.selected) > 1 and self.merge == "1"
                                     else len(self.selected))
            self.phase = "merge" if len(self.selected) > 1 else "title"
            return [",".join(str(i) for i in self.selected)]
        if prompt == _MERGE_PROMPT and self.phase == "merge":
            self.phase = "title"
            return [self.merge]
        if prompt == _TITLE_PROMPT and self.phase == "title":
            self.phase = "packing"
            return [self.title]
        if prompt == _EXIT_PROMPT and self.phase == "packing":
            self._check_complete()
            self.phase, self.boundary = "finished", "exit"
            return [""]
        raise PackerProtocolError(f"下載器互動提示順序異常: {prompt}")

    def _check_complete(self):
        if not self.expected_outputs or len(self.output_files) != self.expected_outputs:
            raise PackerProtocolError(
                f"任務已返回等待輸入，但預期 {self.expected_outputs} 個 EPUB，"
                f"僅收到 {len(self.output_files)} 個完成記錄。")


def _validate_epub(path: Path):
    """檢查已關閉的 EPUB，包含 CRC、container 與 OPF 基本結構。"""
    if not path.is_file() or path.stat().st_size == 0:
        raise ValueError(f"EPUB 不存在或為空: {path.name}")
    with zipfile.ZipFile(path) as archive:
        if archive.testzip() is not None:
            raise ValueError(f"EPUB CRC 驗證失敗: {path.name}")
        if archive.read("mimetype").strip() != b"application/epub+zip":
            raise ValueError(f"EPUB mimetype 錯誤: {path.name}")
        container = ElementTree.fromstring(archive.read("META-INF/container.xml"))
        rootfiles = container.findall("{*}rootfiles/{*}rootfile")
        if not rootfiles:
            raise ValueError(f"EPUB container 缺少 rootfile: {path.name}")
        opf_path = rootfiles[0].get("full-path")
        if not opf_path:
            raise ValueError(f"EPUB container 缺少 OPF 路徑: {path.name}")
        package = ElementTree.fromstring(archive.read(opf_path))
        if package.tag.rsplit("}", 1)[-1] != "package":
            raise ValueError(f"EPUB OPF 不是 package: {path.name}")
        items = package.findall("{*}manifest/{*}item")
        spine = package.findall("{*}spine/{*}itemref")
        ids = {item.get("id") for item in items if item.get("href")}
        if not items or not spine or any(item.get("idref") not in ids for item in spine):
            raise ValueError(f"EPUB OPF manifest/spine 不完整: {path.name}")


def run_interactive_packer(
    exe_path, url, o1, o2, o3, cwd, cancel_event, *,
    on_process=None, on_line=None, on_tick=None,
    timeout_seconds=21600, stall_seconds=900,
) -> PackerResult:
    """執行一個下載工作，避免向 0.2.50 的常駐輸入迴圈送出 EOF。

    callback 應保持簡短，GUI 更新交回主執行緒。
    呼叫端負責封存 packer_console.log 與 bili_novel.log。
    收尾只停止本次啟動的子程序，不依名稱批次終止其他程序。
    """
    work = Path(cwd).resolve()
    process = None
    reader = None
    stop_reader = threading.Event()
    chunks = queue.Queue(maxsize=128)
    decoder = codecs.getincrementaldecoder("utf-8")("replace")
    protocol = None
    console_buffer = ""
    log_bytes = 0
    log_limit_reported = False
    result = PackerResult(False, "error")
    started = last_activity = time.monotonic()
    last_tick = -1
    last_log_stat = None
    exit_deadline = None
    controlled_stop = False
    eof = False

    def stop_process():
        if process is None:
            return
        if process.poll() is None:
            process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            if sys.platform == "win32":
                # Windows 的 kill() 與 terminate() 相同，重呼叫不會更強制
                raise RuntimeError("已要求停止下載器，但程序在 5 秒內仍未結束。")
            process.kill()
            process.wait(timeout=5)

    def read_output():
        try:
            while not stop_reader.is_set():
                data = os.read(process.stdout.fileno(), 4096)
                while not stop_reader.is_set():
                    try:
                        chunks.put(data if data else None, timeout=0.1)
                        break
                    except queue.Full:
                        continue
                if not data:
                    return
        except (OSError, ValueError) as error:
            while not stop_reader.is_set():
                try:
                    chunks.put(error, timeout=0.1)
                    break
                except queue.Full:
                    continue

    def send(replies):
        for reply in replies:
            process.stdin.write((reply + "\n").encode("utf-8"))
            process.stdin.flush()

    def emit_console(text, final=False):
        nonlocal console_buffer
        console_buffer += text
        while "\n" in console_buffer:
            line, console_buffer = console_buffer.split("\n", 1)
            if on_line and line.strip():
                on_line(_ANSI.sub("", line).rstrip("\r")[:4096])
        if len(console_buffer) > _MAX_BUFFER:
            if on_line:
                on_line(console_buffer[:4096] + " [長行已截斷]")
            console_buffer = console_buffer[-_MAX_BUFFER:]
        if final and console_buffer.strip():
            if on_line:
                on_line(_ANSI.sub("", console_buffer)[:4096])
            console_buffer = ""

    try:
        protocol = _PackerProtocol(url, o1, o2, o3, work)
        if cancel_event.is_set():
            return PackerResult(False, "cancelled", message="使用者已取消下載。")
        kwargs = {"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" else {}
        with (work / "packer_console.log").open("wb") as console_log:
            process = subprocess.Popen(
                [str(Path(exe_path).resolve())], cwd=str(work), stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, bufsize=0,
                shell=False, **kwargs,
            )
            write_packer_pid(work, process.pid)
            if on_process:
                on_process(process)
            reader = threading.Thread(target=read_output, name="packer-console-reader", daemon=True)
            reader.start()
            while True:
                now = time.monotonic()
                elapsed = now - started
                if cancel_event.is_set():
                    result = PackerResult(False, "cancelled", message="使用者已取消下載。")
                    break
                if elapsed >= timeout_seconds:
                    result = PackerResult(False, "timeout", message="下載超過總時間上限。")
                    break
                # 0.2.50 的章節與網路活動可能只寫入核心日誌，也要計入停滯判定
                try:
                    stat = (work / "bili_novel.log").stat()
                    signature = (stat.st_size, stat.st_mtime_ns)
                    if signature != last_log_stat:
                        last_log_stat, last_activity = signature, now
                except FileNotFoundError:
                    pass
                if now - last_activity >= stall_seconds:
                    result = PackerResult(False, "stall", message="下載器長時間沒有輸出或日誌活動。")
                    break
                if int(elapsed) != last_tick:
                    last_tick = int(elapsed)
                    if on_tick:
                        on_tick(elapsed)
                try:
                    data = chunks.get(timeout=0.1)
                except queue.Empty:
                    data = b""
                if isinstance(data, Exception):
                    raise data
                if data is None:
                    eof = True
                    text = decoder.decode(b"", final=True)
                    emit_console(text, final=True)
                    send(protocol.feed(text, final=True))
                elif data:
                    last_activity = time.monotonic()
                    if log_bytes < _CONSOLE_LOG_LIMIT:
                        retained = data[:_CONSOLE_LOG_LIMIT - log_bytes]
                        console_log.write(retained)
                        log_bytes += len(retained)
                    elif not log_limit_reported:
                        log_limit_reported = True
                        if on_line:
                            on_line("核心 console 日誌達 2 MiB 上限，後續仍持續讀取。")
                    text = decoder.decode(data)
                    emit_console(text)
                    send(protocol.feed(text))

                if protocol.boundary and exit_deadline is None:
                    if protocol.boundary == "idle":
                        # 回到網址提問只代表閒置；協定解析器已先核對全部成品記錄
                        if process.poll() is None:
                            process.terminate()
                            controlled_stop = True
                    exit_deadline = time.monotonic() + 5
                returncode = process.poll()
                if returncode is not None and eof:
                    if returncode != 0 and not controlled_stop:
                        raise PackerProtocolError(f"下載器異常退出，代碼: {returncode}")
                    if not protocol.boundary:
                        raise PackerProtocolError("下載器退出前沒有完整任務結束訊號。")
                    process.wait(timeout=5)
                    protocol._check_complete()
                    for output in protocol.output_files:
                        if cancel_event.is_set():
                            result = PackerResult(False, "cancelled", message="使用者已取消下載。")
                            break
                        _validate_epub(output)
                    else:
                        result = PackerResult(True, "success", output_files=list(protocol.output_files),
                                              message="核心打包完成，全部 EPUB 已驗證。")
                    break
                if returncode is not None and exit_deadline is None:
                    exit_deadline = time.monotonic() + 5
                if exit_deadline is not None and time.monotonic() >= exit_deadline:
                    raise PackerProtocolError("下載器結束或輸出管線收尾逾時。")
    except Exception as error:
        logging.error("下載器互動失敗: %s", error)
        result = PackerResult(False, "error", message=str(error))
    finally:
        try:
            stop_process()
        except Exception as error:
            result = PackerResult(False, "error", message=f"{result.message}\n下載器收尾失敗: {error}".strip())
        stop_reader.set()
        if process is not None:
            if process.stdin:
                try:
                    process.stdin.close()
                except OSError:
                    pass
            if reader:
                reader.join(timeout=2)
            if process.stdout:
                try:
                    process.stdout.close()
                except OSError:
                    pass
            if reader and reader.is_alive():
                reader.join(timeout=1)
            if reader and reader.is_alive():
                result = PackerResult(False, "error", message="下載器輸出讀取執行緒尚未結束。")
            result.returncode = process.poll()
            if result.returncode is not None:
                clear_packer_pid(work)
    return result


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
