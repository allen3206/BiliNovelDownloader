import re
import time
import shutil
import zipfile
import logging
from pathlib import Path

import opencc

from core import DIR_DOWNLOADS, DIR_SIMP, DIR_TRAD, sanitize_filename

# 設定全域 OpenCC 實例，避免重複載入字典檔
S2TW_CONVERTER = opencc.OpenCC('s2tw')


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

def convert_epub_with_opencc(input_epub: Path, output_epub: Path, converter: opencc.OpenCC, target_lang: str = None):
    if not input_epub.exists() or input_epub.stat().st_size < 100:
        raise ValueError(f"EPUB 檔案無效或疑似損壞（路徑：{input_epub}，大小：{input_epub.stat().st_size if input_epub.exists() else '不存在'} bytes）")
    text_extensions = ('.html', '.xhtml', '.xml', '.opf', '.ncx', '.txt', '.css')
    with zipfile.ZipFile(input_epub, 'r') as zin:
        with zipfile.ZipFile(output_epub, 'w', zipfile.ZIP_DEFLATED) as zout:
            for item in zin.infolist():
                content = zin.read(item.filename)
                if item.filename.lower().endswith(text_extensions):
                    try:
                        text = content.decode('utf-8')
                        converted_text = converter.convert(text)
                        if target_lang and item.filename.lower().endswith('.opf'):
                            converted_text = re.sub(
                                r'(<dc:language[^>]*>)\s*(?:zh-CN|zh-Hans|zh)\s*(</dc:language>)',
                                rf'\g<1>{target_lang}\g<2>', converted_text, flags=re.IGNORECASE)
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
            convert_epub_with_opencc(epub_file, temp_output, S2TW_CONVERTER, target_lang='zh-TW')

            if not temp_output.exists() or temp_output.stat().st_size == 0:
                raise Exception("轉換輸出檔不存在或大小為零，取消覆蓋原檔")

            epub_file.unlink()
            temp_output.rename(epub_file)
            logger.info(f"轉換成功: {epub_file.name}")
        except Exception as e:
            logger.error(f"轉換失敗: {epub_file.name}, 錯誤: {e}")
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