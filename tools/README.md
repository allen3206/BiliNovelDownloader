# 核心下載器目錄

此資料夾為 `BiliNovelDownloader` 呼叫下載器 `bili_novel_packer.exe` 的工作目錄。

下載器已附在此資料夾內。請保持 `tools` 資料夾與 `BiliNovelDownloader.exe` 在同一層目錄，且勿移動或刪除其中的 `.exe`。

### 自行 clone 原始碼

由於 `bili_novel_packer.exe` 檔案較大且可能會較頻繁更新，因此 **此執行檔不會被包含在原始碼的 GitHub 儲存庫中**。若您 clone 了本專案並希望在本地端直接執行 `BiliNovelDownloader.py`：

1. 請前往 [bili_novel_packer Releases](https://github.com/Montaro2017/bili_novel_packer/releases) 下載最新的 Windows x64 版本執行檔。
2. 將下載的 `.exe` 檔案放入此 `tools/` 資料夾內。
3. 執行主程式，它會自動掃描此資料夾並找到該下載器。

*(注意：此資料夾內的 `.exe` 檔案已被 `.gitignore` 忽略，不會被 commit 到版本控制中。)*

下載器為第三方開源工具（MIT 授權），授權全文見本資料夾的 `LICENSE-bili_novel_packer.txt`。