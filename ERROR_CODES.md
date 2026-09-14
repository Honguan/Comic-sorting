# 佇列錯誤碼與原因

這些是 Comic sorting 的分類碼，不是服務端原生 HTTP／Codex 錯誤碼。主佇列與歷史資料夾列會顯示分類；雙擊主佇列或選取歷史資料夾，可查看原始錯誤、檔案路徑、HTTP／程序退出碼與現有日誌。

## 分析範圍與判斷限制

已檢查主佇列、翻譯程序、頁數橋接、JSON 設定、圖片讀取、OCR／偵測／修補、匯出與清理的既有錯誤路徑，並對照 BallonsTranslator 的 LLM／Codex 例外及實際日誌。以下分類涵蓋可辨識的錯誤；不能預先窮舉外部服務、作業系統與第三方套件的所有未來錯誤。無明確證據時顯示 `UNKNOWN_ERROR`，原始訊息仍保留，沒有錯誤回報的持續等待也不會被誤判為某一種已知故障。

- 分類只在失敗、已停止、前置工作失敗、完成但有異常時顯示；等待、執行中與完成列保持空白。
- 以具體原因優先，略過 INFO／DEBUG／WARNING 與 traceback 中的程式碼行，避免將建議文字或被取消的其他請求當成根因。同時存在多個明確問題時顯示優先分類，所有原始訊息留在詳情。
- `cancelled` 表示主佇列停止（包含使用者停止或歷史儲存失敗），不由翻譯器日誌中的「stopped by user」文字推斷使用者操作。
- 429 可以表示頻率限制、額度或支出限制，必須檢查更具體的訊息；只有狀態碼時保留 `HTTP_429`。參考 [OpenAI Docs：Error codes](https://developers.openai.com/api/docs/guides/error-codes)。
- Codex 失敗事件可包含原因與上游 HTTP 狀態，但目前的 BallonsTranslator 轉接層不一定將所有原生欄位寫入日誌，因此不能補造未回報的原生代碼。參考 [OpenAI Docs：App Server errors](https://learn.chatgpt.com/docs/app-server#errors)。
- 單項失敗仍繼續其他資料夾；相同資料夾的後續匯出／清理受前置失敗阻擋。此功能不更改既有重試、停止或成功判斷。
- 設定與歷史保存原始錯誤，讀取時用同一套分類器顯示；既有歷史不需搬移。重試只清除當前列的舊錯誤。

## 錯誤碼對照

| 錯誤碼 | 顯示原因 |
|---|---|
| `LLM_CAPACITY` | 模型服務暫時滿載 |
| `LLM_QUOTA` | 帳號額度、餘額或支出上限不足 |
| `LLM_AUTH` | 登入已失效或 API 金鑰無效／未設定 |
| `LLM_MODEL` | 模型未設定、不存在或不可用 |
| `LLM_CONFIG` | 服務網址或請求設定無效 |
| `LLM_CONTEXT` | 輸入超過模型上下文上限 |
| `LLM_OUTPUT_LIMIT` | 模型輸出達到 Token 上限 |
| `LLM_REFUSAL` | 服務拒絕處理此內容 |
| `CODEX_START` | Codex 執行檔不存在或無法啟動 |
| `CODEX_INTERACTIVE` | Codex 要求互動操作，無法自動繼續 |
| `CODEX_PROTOCOL` | Codex 回應格式或通訊協定異常 |
| `CODEX_EXIT` | Codex 子程序提前結束 |
| `REQUEST_TIMEOUT` | 等待服務回應逾時 |
| `NETWORK_ERROR` | 網路、代理伺服器或 TLS 連線失敗 |
| `LLM_RATE_LIMIT` | 請求頻率過高，服務已限流 |
| `LLM_PERMISSION` | 服務拒絕存取，帳號或地區權限不足 |
| `GPU_MEMORY` | GPU 顯示記憶體不足 |
| `SYSTEM_MEMORY` | 系統記憶體不足 |
| `ENV_DEPENDENCY` | Python 套件或 DLL 載入失敗 |
| `IMAGE_READ` | 圖片損壞、格式不支援或無法讀取 |
| `DISK_FULL` | 磁碟空間不足 |
| `FILE_LOCKED` | 檔案正在被其他程式使用 |
| `FILE_PERMISSION` | 檔案存取權限不足 |
| `FILE_MISSING` | 檔案或資料夾不存在 |
| `PATH_TOO_LONG` | 檔案路徑超過系統限制 |
| `MEMORY_SUMMARY` | 翻譯摘要記憶整理失敗 |
| `LLM_RESPONSE` | 模型回覆格式、翻譯數量或內容不符合要求 |
| `DETECT_FAILED` | 文字偵測模組執行失敗 |
| `OCR_FAILED` | OCR 模組執行失敗 |
| `INPAINT_FAILED` | 修補模組執行失敗 |
| `PROCESS_CRASH` | 翻譯程序異常終止 |
| `BT_INSTALL` | BallonsTranslator 安裝路徑或 Python 無效 |
| `JSON_INVALID` | 設定檔或專案 JSON 無法讀取 |
| `MANGA_MISSING` | 漫畫資料夾不存在 |
| `NO_IMAGES` | 資料夾內沒有可處理的圖片 |
| `PAGE_RANGE` | 翻譯頁數範圍無效或頁面已不存在 |
| `NO_STAGE` | 未啟用任何處理階段 |
| `RESULT_INCOMPLETE` | 翻譯結果不完整，已略過後續動作 |
| `EXPORT_PATH` | 匯出路徑未設定、互相包含或已被其他來源使用 |
| `CBZ_INVALID` | CBZ 壓縮檔驗證失敗 |
| `UNSAFE_PATH` | 操作路徑或符號連結不符合限制 |
| `HTTP_429` | 服務回報 429；需查看原始訊息確認頻率或額度限制 |
| `HTTP_4XX` | 服務拒絕請求，請查看原始 HTTP 錯誤 |
| `HTTP_5XX` | 遠端服務或閘道發生錯誤 |
| `REQUEST_INVALID` | 請求參數無效或不受支援 |
| `BT_VERSION` | BallonsTranslator 版本不支援此功能 |
| `PIPELINE_STOPPED` | 翻譯流程提前停止，未回報更明確原因 |
| `PROCESS_EXIT` | 翻譯程序以非零代碼結束 |
| `RUN_INCOMPLETE` | 翻譯程序未完成所有階段 |
| `QUEUE_BLOCKED` | 同一資料夾的前置工作失敗，已略過此項 |
| `RUN_INTERRUPTED` | 上次執行中斷，需確認成果後重試 |
| `USER_CANCELLED` | 使用者或主佇列要求停止 |
| `UNKNOWN_ERROR` | 未能分類的錯誤，請查看原始訊息 |
| `RUN_WARNING` | 流程完成但有異常，請查看原始訊息 |

## 排查方向

- 模型滿載或服務 5xx：稍後重試；日誌本身不能證明一定是平行數造成。
- 額度／權限／登入：先確認帳號與服務設定，持續重試不能補足額度。
- 限流／逾時／連線：核對原始服務回覆、等待時間與網路；不自行替換模型或自動增加請求。
- 壞圖：查看詳情中的頁面檔案；副檔名為 JPG 不代表內容一定是圖片。
- 本機檔案、記憶體、Python：依具體錯誤檢查空間、權限、檔案占用或所選執行環境。
- 結果／CBZ 不完整：先確認來源與已保存成果，再決定是否重試；不以舊成果存在視為本次一定成功。

分類規則由 `queue_errors.py` 統一管理。新增外部錯誤格式時，先取得原始診斷與回歸案例，再新增規則，避免只憑數字或漫畫文字誤分類。
