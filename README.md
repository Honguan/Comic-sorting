<div align="center">
  <img src="assets/comic-sorting.png" width="160" alt="Comic Sorting 圖標">
  <h1>Comic Sorting</h1>
  <p>Windows 漫畫章節整合、翻譯狀態檢查與 Komga CBZ 匯出工具。</p>
</div>

## 功能

- 從高層級漫畫目錄自動建立「系列 → 章節」樹狀列表。
- 系列依更新時間由新到舊排列，章節依最小章號由小到大排列。
- 顯示章節狀態、資料夾大小、更新時間及順序編號。
- 選擇父系列或單一章節後，將指定範圍圖片連續編號並整合成 `Chapter X-Y`。
- 偵測章節 `result` 資料夾，將翻譯圖片原樣封裝為 Komga 可讀取的 CBZ。
- 支援自然排序、Unicode 路徑、增量跳過／更新、原子替換及批次錯誤隔離。
- 手動清空 `mask`、`inpainted` 內容；Komga 整批匯出成功後自動清理。
- 大型目錄掃描、漫畫整合、CBZ 匯出與清理皆在背景執行。

## 下載

從 [GitHub Releases](https://github.com/Honguan/Comic-sorting/releases/latest) 下載最新的 Windows x64 EXE。程式不依賴 WinRAR、7-Zip 或外部壓縮工具。

## 使用方式

1. 啟動 `Comic sorting.exe`。
2. 選擇漫畫路徑；可直接選擇包含多個系列的高層級目錄。
3. 在樹狀列表選擇父系列或章節，確認起始與結束編號後執行整合。
4. 翻譯工具完成 `result` 圖片後，按「重新掃描」。
5. 設定 Komga 輸出路徑，匯出選取項目或所有已翻譯項目。

輸出結構：

```text
D:\Komga\
└─ Series Name\
   ├─ Chapter 1.cbz
   └─ Chapter 2-10.cbz
```

CBZ 根目錄只包含 `result` 內的 `.png`、`.jpg`、`.jpeg`、`.webp` 圖片，不會重新編碼或壓縮圖片。

## 清理注意事項

「清理所有 mask / inpainted」會清空目前漫畫路徑下所有同名資料夾的內容，但保留資料夾本身。手動清理前會要求確認，操作無法復原。

Komga 匯出只有在整批 CBZ 與狀態檔都成功後才會自動清理；任何匯出失敗都會保留工作資料。

## 路徑綁定與增量狀態

- 可攜式設定：EXE 同目錄的 `comic-sorting.settings.json`
- Komga 增量狀態：`<Komga 路徑>\.comic-sorting-state.json`

首次啟動時漫畫與 Komga 路徑皆為空白，必須手動設定。設定後會自動保存並在後續啟動時讀取；重新瀏覽即可改綁其他路徑。移動 EXE 時請連同設定檔一起移動。

增量狀態依圖片名稱、大小及修改時間判斷是否需要更新 CBZ。

## 開發與建置

需求：Python 3.10、PyInstaller 6.14.1。

```powershell
py -3.10 -B -m unittest discover -s tests -v
py -3.10 -m PyInstaller --clean --noconfirm "Comic sorting.spec"
```

建置輸出位於 `dist\Comic sorting.exe`。推送 `v*` 標籤時，GitHub Actions 會執行測試、建置並建立 Release。
