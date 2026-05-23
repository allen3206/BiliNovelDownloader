# 核心下載器目錄

此資料夾為 `BiliNovelDownloader` 呼叫下載器工具的工作目錄。

由於 `bili_novel_packer.exe` 檔案較大且可能會較頻繁更新，因此 **此執行檔不會被包含在原始碼的 GitHub 儲存庫中**。

### 注意事項

如果您 clone 了本專案並希望在本地端直接執行 `BiliNovelDownloader.py` 測試：

1. 請前往 [bili_novel_packer Releases](https://github.com/Montaro2017/bili_novel_packer/releases) 下載最新的 Windows x64 版本執行檔。
2. 將下載的 `.exe` 檔案放入此 `tools/` 資料夾內。
3. 執行主程式，它會自動掃描此資料夾並找到該下載器。

*(注意：此資料夾內的 `.exe` 檔案已被 `.gitignore` 忽略，不會被 commit 到版本控制中。)*