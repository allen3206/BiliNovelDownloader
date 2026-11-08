# 核心下載器目錄

此資料夾為 `BiliNovelDownloader` 呼叫下載器 `bili_novel_packer.exe` 的工作目錄。

下載器已附在此資料夾內。請保持 `tools` 資料夾與 `BiliNovelDownloader.exe` 在同一層目錄，且勿移動或刪除其中的 `.exe`。

### 自行 clone 原始碼

由於 `bili_novel_packer.exe` 檔案較大且可能會較頻繁更新，因此 **此執行檔不會被包含在原始碼的 GitHub 儲存庫中**。若您 clone 了本專案並希望在本地端直接執行 `BiliNovelDownloader.py`：

1. 請前往 [bili_novel_packer v0.2.50](https://github.com/Montaro2017/bili_novel_packer/releases/tag/v0.2.50) 下載 `bili_novel_packer-0.2.50-x86_64-windows.exe`。
2. 將下載的 `.exe` 檔案放入此 `tools/` 資料夾內；舊核心請先備份到其他目錄，這裡只保留一個核心執行檔。
3. 執行主程式，它會自動掃描此資料夾並找到該下載器。

本版已實測核心下載器 0.2.50，並保留 0.2.44 的互動流程處理，但未重新驗證其實際下載功能。不建議未備份就替換核心；使用未驗證版本時，程式會在執行日誌提醒，並依實際互動格式嘗試執行，而非僅因版本號不同就停止。若提問、順序或完成訊號不相容，程式會停止並保留診斷；成品仍須通過檢查才會進入繁體轉換。這不代表保證相容所有新版核心。

*(注意：此資料夾內的 `.exe` 檔案已被 `.gitignore` 忽略，不會被 commit 到版本控制中。)*

下載器為第三方開源工具（MIT 授權），授權全文見本資料夾的 `LICENSE-bili_novel_packer.txt`。
