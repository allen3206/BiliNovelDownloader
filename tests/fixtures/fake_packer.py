"""Issue #2 使用的離線假核心，不連線至外部網站。

提問文字依照已核對的 v0.2.50 原始碼，僅模擬互動行為，不是官方執行檔。
EOF 重複輸出刻意限速，保留缺陷行為供重現，同時避免耗盡 CPU 與磁碟。
"""
import argparse
import json
import sys
import time
import zipfile
from pathlib import Path

PROMPTS = {
    "url": "请输入链接(多个链接使用空格隔开):\n",
    "volumes": "请选择要下载的分卷: ",
    "merge": "是否合并选择的分卷为一个文件? ",
    "title": "是否在每章开头添加章节标题? ",
}
ROOT = Path.cwd()
ARGS = None


def record(event, **fields):
    with (ROOT / "fixture-events.jsonl").open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({"event": event, "monotonic": time.monotonic(), **fields}, ensure_ascii=False) + "\n")


def emit(text, fragmented=False):
    data = text.encode("utf-8")
    if fragmented:
        # 每段兩個 bytes，刻意切開佔三個 bytes 的中文 UTF-8 字元
        for offset in range(0, len(data), 2):
            sys.stdout.buffer.write(data[offset:offset + 2])
            sys.stdout.buffer.flush()
            time.sleep(0.001)
    else:
        sys.stdout.buffer.write(data)
        sys.stdout.buffer.flush()


def ask(kind):
    record("prompt", kind=kind)
    emit(PROMPTS[kind], ARGS.fragment_prompts)
    data = sys.stdin.buffer.readline()
    if not data:
        record("eof", kind=kind)
        return None
    value = data.decode("utf-8").rstrip("\r\n")
    record("input", kind=kind, value=value)
    return value


def log_line(text):
    with (ROOT / "bili_novel.log").open("a", encoding="utf-8") as stream:
        stream.write(text + "\n")
    emit(text + "\n")


def make_epub(name, volumes, title_enabled):
    folder = ROOT / "FixtureBook"
    folder.mkdir(exist_ok=True)
    path = folder / name
    if ARGS.scenario == "invalid":
        path.write_bytes(b"This is deliberately not a ZIP or EPUB.")
        record("epub_closed", path=str(path), volumes=volumes, title_enabled=title_enabled, invalid=True)
        log_line("打包完成: " + str(path))
        return path
    chapter = "<h1>测试章节标题</h1>" if title_enabled else ""
    chapter += "<p>这是离线测试。书里有汉字和图片。</p>"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        archive.writestr("META-INF/container.xml", '<?xml version="1.0" encoding="UTF-8"?><container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles></container>')
        archive.writestr("OEBPS/content.opf", '<?xml version="1.0" encoding="UTF-8"?><package xmlns="http://www.idpf.org/2007/opf" unique-identifier="uid" version="3.0"><metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:identifier id="uid">urn:uuid:fixture</dc:identifier><dc:title>离线测试</dc:title><dc:language>zh-CN</dc:language><meta property="dcterms:modified">2026-11-06T00:00:00Z</meta></metadata><manifest><item id="chapter" href="chapter.xhtml" media-type="application/xhtml+xml"/><item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/></manifest><spine><itemref idref="chapter"/></spine></package>')
        archive.writestr("OEBPS/chapter.xhtml", '<?xml version="1.0" encoding="UTF-8"?><html xmlns="http://www.w3.org/1999/xhtml"><head><title>测试章节</title></head><body>' + chapter + "</body></html>")
        archive.writestr("OEBPS/nav.xhtml", '<?xml version="1.0" encoding="UTF-8"?><html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops"><head><title>目录</title></head><body><nav epub:type="toc"><ol><li><a href="chapter.xhtml">测试章节</a></li></ol></nav></body></html>')
    record("epub_closed", path=str(path), volumes=volumes, title_enabled=title_enabled)
    log_line("打包完成: " + str(path))
    return path


def selected_volumes(raw):
    maximum = 2 if ARGS.scenario in ("multi", "error") else 1
    if raw == "0":
        return list(range(1, maximum + 1))
    chosen = []
    for piece in raw.replace("，", ",").split(","):
        if "-" in piece:
            start, end = map(int, piece.split("-", 1))
            chosen.extend(range(start, end + 1))
        else:
            chosen.append(int(piece))
    if not chosen or any(value < 1 or value > maximum for value in chosen):
        raise ValueError("invalid fixture volume selection")
    return list(dict.fromkeys(chosen))


def main():
    global ARGS
    parser = argparse.ArgumentParser()
    parser.add_argument("--scenario", choices=("single", "multi", "error", "invalid", "zero", "hang", "quietlog", "flood"), default="single")
    parser.add_argument("--legacy", action="store_true")
    parser.add_argument("--version")
    parser.add_argument("--unknown-prompt", action="store_true")
    parser.add_argument("--unknown-boundary", action="store_true")
    parser.add_argument("--fragment-prompts", action="store_true")
    parser.add_argument("--between-volumes", type=float, default=0.4)
    ARGS = parser.parse_args()
    record("started", scenario=ARGS.scenario, fragmented=ARGS.fragment_prompts)
    version = ARGS.version or ("0.2.44" if ARGS.legacy else "0.2.50")
    if ARGS.legacy:
        PROMPTS['url'] = '请输入URL(支持哔哩轻小说&轻小说文库):\n'
        PROMPTS['volumes'] = '请选择需要下载的分卷(可输入如1-9进行范围选择以及如2,5单独选择):'
    emit("欢迎使用轻小说打包器!\n作者: Spark\n当前版本: " + version + "\n")
    while True:
        url = ask("url")
        if url is None:
            time.sleep(0.03)
            continue
        if not url.startswith("https://fixture.invalid/"):
            record("invalid_url", value=url)
            continue
        if ARGS.unknown_prompt:
            emit("请选择输出格式:")
            record("unknown_prompt_reply", value=sys.stdin.buffer.readline().decode("utf-8"))
            return
        emit("[ 1] 测试卷一\n" + ("[ 2] 测试卷二\n" if ARGS.scenario in ("multi", "error") else "") + "[ 0] 选择全部\n")
        selection = ask("volumes")
        if selection is None:
            record("input_eof_before_job")
            continue
        try:
            volumes = selected_volumes(selection)
            emit("\n")
            merge = ask("merge") if len(volumes) > 1 else None
            if merge is not None:
                emit("\n")
            title = ask("title")
            emit("\n")
            if title not in ("1", "2") or (merge is not None and merge not in ("1", "2")):
                raise ValueError("fixture option mismatch")
            record("job_started", selected=volumes, merge=merge, title=title)
            log_line("开始打包: FixtureBook")
            if ARGS.scenario == 'quietlog':
                for _ in range(10):
                    with (ROOT / 'bili_novel.log').open('a', encoding='utf-8') as f:
                        f.write('native log activity only\n')
                    time.sleep(.1)
            if ARGS.scenario == 'flood':
                data = (b'ordinary stderr diagnostic\n' * 2048)
                for _ in range(64):
                    sys.stderr.buffer.write(data)
                    sys.stderr.buffer.flush()
            if ARGS.scenario == "hang":
                while True:
                    time.sleep(0.05)
            if ARGS.scenario == "zero":
                record("task_boundary", outcome="success_without_output")
                continue
            if merge == "1":
                if ARGS.scenario == "error":
                    raise RuntimeError("fixture download failed")
                time.sleep(ARGS.between_volumes)
                make_epub("FixtureBook-merged.epub", volumes, title == "1")
            else:
                for index, volume in enumerate(volumes):
                    if index:
                        time.sleep(ARGS.between_volumes)
                    if ARGS.scenario == "error" and index == 1:
                        raise RuntimeError("fixture second volume failed")
                    make_epub("FixtureBook-%s.epub" % volume, [volume], title == "1")
            record("task_boundary", outcome="success")
            if ARGS.unknown_boundary:
                emit("所有文件处理完毕\n")
                return
            if ARGS.legacy:
                emit('全部任务已完成，按回车键退出.\n')
                value = sys.stdin.buffer.readline()
                record('legacy_final_enter', value=value.decode('utf-8'))
                return
        except Exception as exc:
            log_line(("运行出错，按回车键退出." if ARGS.legacy else "运行出错.") + "(" + version + ")")
            record("fatal_error", message=str(exc))
            record("task_boundary", outcome="failure")
            if ARGS.legacy:
                return  # upstream errors may also produce exit code zero


if __name__ == "__main__":
    main()
