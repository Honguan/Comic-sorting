"""Stable display codes for queue diagnostics; original errors remain persisted."""
import re

from app_logging import redact
from ui_language import TRANSLATIONS, tr


# Specific causes precede wrapper errors and generic HTTP/exit codes.
ERRORS = (
    ('LLM_CAPACITY', '模型服務暫時滿載', r'at capacity|server_is_overloaded|model.{0,40}(?:overloaded|over capacity)'),
    ('LLM_QUOTA', '帳號額度、餘額或支出上限不足', r'insufficient_quota|credit_balance_exhausted|(?:spend|usage)_limit_exceeded|UsageLimitExceeded|(?:exceeded|reached|hit).{0,30}(?:quota|usage limit)|insufficient credits|credit balance.{0,20}(?:low|exhausted|insufficient)'),
    ('LLM_AUTH', '登入已失效或 API 金鑰無效／未設定', r'LLMApiKeyRequiredError|API key is required|AuthenticationError|invalid_api_key|incorrect api key|unauthorized|Codex ChatGPT login is required|not authenticated|token.{0,20}(?:expired|revoked)'),
    ('LLM_MODEL', '模型未設定、不存在或不可用', r'LLMModelRequiredError|model_not_found|(?:vision |image )?model is required for LLM|model.{0,60}(?:does not exist|not found|not supported)'),
    ('LLM_CONFIG', '服務網址或請求設定無效', r'LLMBaseURLRequiredError|base URL is required|Codex Timeout must be|Save Codex Sessions must|unsupported codex message|Codex requires (?:text or image|a final user)|does not have text translation enabled'),
    ('LLM_CONTEXT', '輸入超過模型上下文上限', r'ContextWindowExceeded|context_length_exceeded|maximum context length|context (?:window|length).{0,25}exceed'),
    ('LLM_OUTPUT_LIMIT', '模型輸出達到 Token 上限', r'LLMOutputLimitError|LLM output limit reached|max_output_tokens.{0,10}(?:exceeded|reached)'),
    ('LLM_REFUSAL', '服務拒絕處理此內容', r'content_policy_violation|content_filter|safety.{0,15}(?:blocked|rejected)|request.{0,15}refused'),
    ('CODEX_START', 'Codex 執行檔不存在或無法啟動', r'Cannot start Codex|Install the official Codex CLI'),
    ('CODEX_INTERACTIVE', 'Codex 要求互動操作，無法自動繼續', r'Codex requested an interactive tool'),
    ('CODEX_PROTOCOL', 'Codex 回應格式或通訊協定異常', r'Invalid Codex protocol response|Expected a JSON-RPC object'),
    ('CODEX_EXIT', 'Codex 子程序提前結束', r'Codex App Server exited before completion'),
    ('REQUEST_TIMEOUT', '等待服務回應逾時', r'APITimeoutError|TimeoutError|ReadTimeout|ConnectTimeout|request timed out|read timed out|connect timeout'),
    ('NETWORK_ERROR', '網路、代理伺服器或 TLS 連線失敗', r'APIConnectionError|ConnectionError|ConnectionResetError|ConnectError|ProxyError|SSLError|CERTIFICATE_VERIFY_FAILED|NameResolutionError|getaddrinfo failed|ResponseStreamDisconnected|ResponseStreamConnectionFailed|stream disconnected'),
    ('LLM_RATE_LIMIT', '請求頻率過高，服務已限流', r'rate_limit_exceeded|rate_limit_error|slow_down|too many requests|rate limit reached'),
    ('LLM_PERMISSION', '服務拒絕存取，帳號或地區權限不足', r'PermissionDeniedError|unsupported_country_region_territory|IP not authorized|country.{0,30}not supported'),
    ('GPU_MEMORY', 'GPU 顯示記憶體不足', r'CUDA out of memory|CUDAOutOfMemory|torch\.OutOfMemoryError|CUDA error: out of memory'),
    ('SYSTEM_MEMORY', '系統記憶體不足', r'MemoryError|cannot allocate memory|not enough memory|\[WinError (?:8|14|1455)\]'),
    ('ENV_DEPENDENCY', 'Python 套件或 DLL 載入失敗', r'ModuleNotFoundError|ImportError|DLL load failed|No module named|\[WinError 193\]'),
    ('IMAGE_READ', '圖片損壞、格式不支援或無法讀取', r'Unable to read (?:page )?image:|cannot identify image file|UnidentifiedImageError|image file is truncated|broken data stream when reading image'),
    ('DISK_FULL', '磁碟空間不足', r'No space left on device|disk (?:is )?full|\[Errno 28\]|\[WinError (?:39|112)\]'),
    ('FILE_LOCKED', '檔案正在被其他程式使用', r'\[WinError (?:32|33)\]|used by another process|sharing violation'),
    ('FILE_PERMISSION', '檔案存取權限不足', r'PermissionError|Permission denied|Access is denied|\[WinError 5\]'),
    ('FILE_MISSING', '檔案或資料夾不存在', r'FileNotFoundError|No such file or directory|\[WinError (?:2|3)\]'),
    ('PATH_TOO_LONG', '檔案路徑超過系統限制', r'\[WinError 206\]|file name too long|filename or extension is too long'),
    ('MEMORY_SUMMARY', '翻譯摘要記憶整理失敗', r'LLMMemoryCompactionError|LLM memory compaction failed'),
    ('LLM_RESPONSE', '模型回覆格式、翻譯數量或內容不符合要求', r'LLM translation failed|translation count mismatch|Failed to parse matching translation count|matching translation count|invalid JSON response|empty (?:model |LLM )?response'),
    ('DETECT_FAILED', '文字偵測模組執行失敗', r'textdetector module.{0,120}failed to run|Text Detection Failed'),
    ('OCR_FAILED', 'OCR 模組執行失敗', r'ocr module.{0,120}failed to run|OCR Failed'),
    ('INPAINT_FAILED', '修補模組執行失敗', r'inpaint(?:er)? module.{0,120}failed to run|Inpaint(?:ing)? Failed'),
    ('PROCESS_CRASH', '翻譯程序異常終止', r'exit=(?:3221225477|-1073741819|3221226505|-1073740791)|access violation|Fatal Python error|segmentation fault'),
)

LOCAL_ERRORS = (
    ('BT_INSTALL', 'BallonsTranslator 安裝路徑或 Python 無效', ('請設定有效的 BallonsTranslator 安裝目錄及其 Python 執行檔',)),
    ('JSON_INVALID', '設定檔或專案 JSON 無法讀取', ('請選擇既有 BallonsTranslator config JSON', '無法讀取 JSON：{0}。請檢查權限與內容，或還原備份後再試；原檔未變更。')),
    ('MANGA_MISSING', '漫畫資料夾不存在', ('漫畫路徑不存在',)),
    ('NO_IMAGES', '資料夾內沒有可處理的圖片', ('指定路徑沒有圖片，請選擇章節或整合輸出',)),
    ('PAGE_RANGE', '翻譯頁數範圍無效或頁面已不存在', ('翻譯頁數必須介於 1 至 {0}，且起始頁不可大於結束頁',)),
    ('NO_STAGE', '未啟用任何處理階段', ('請至少啟用一個 BallonsTranslator 處理階段',)),
    ('RESULT_INCOMPLETE', '翻譯結果不完整，已略過後續動作', ('翻譯結果不完整，未執行後續動作',)),
    ('EXPORT_PATH', '匯出路徑未設定、互相包含或已被其他來源使用', ('請設定 Komga 輸出路徑', '匯出與漫畫路徑不可互相包含', '輸出已屬於其他來源，請改用不同的 Komga 路徑或系列名稱：{0}')),
    ('CBZ_INVALID', 'CBZ 壓縮檔驗證失敗', ('CBZ 內容損壞', 'CBZ 圖片數量、順序或名稱不符', 'CBZ 含有子資料夾或非圖片檔案')),
    ('UNSAFE_PATH', '操作路徑或符號連結不符合限制', ('不允許刪除系列目錄外或符號連結資料夾',)),
)

OTHER_ERRORS = {
    'HTTP_429': '服務回報 429；需查看原始訊息確認頻率或額度限制',
    'HTTP_4XX': '服務拒絕請求，請查看原始 HTTP 錯誤',
    'HTTP_5XX': '遠端服務或閘道發生錯誤',
    'REQUEST_INVALID': '請求參數無效或不受支援',
    'BT_VERSION': 'BallonsTranslator 版本不支援此功能',
    'PIPELINE_STOPPED': '翻譯流程提前停止，未回報更明確原因',
    'PROCESS_EXIT': '翻譯程序以非零代碼結束',
    'RUN_INCOMPLETE': '翻譯程序未完成所有階段',
    'QUEUE_BLOCKED': '同一資料夾的前置工作失敗，已略過此項',
    'RUN_INTERRUPTED': '上次執行中斷，需確認成果後重試',
    'USER_CANCELLED': '使用者或主佇列要求停止',
    'UNKNOWN_ERROR': '未能分類的錯誤，請查看原始訊息',
    'RUN_WARNING': '流程完成但有異常，請查看原始訊息',
}
REASONS = {code: reason for code, reason, _ in ERRORS + LOCAL_ERRORS} | OTHER_ERRORS
_RULES = [(code, re.compile(pattern, re.I)) for code, _, pattern in ERRORS]


def error_info(status: str, error: str) -> tuple[str, str]:
    """Classify terminal diagnostics, never progress text or a successful job's notes."""
    if status not in ('failed', 'cancelled', 'blocked', 'done_warning'):
        return '', ''
    if status == 'blocked':
        code = 'QUEUE_BLOCKED'
    elif status == 'cancelled':
        key = '上次執行中斷，請確認結果後重試'
        code = ('RUN_INTERRUPTED' if error in (key, *TRANSLATIONS.get(key, {}).values())
                else 'USER_CANCELLED')
    else:
        # Ignore info/warning logs and Python source lines in tracebacks: they may
        # mention limits, cancelled siblings, or examples unrelated to the failure.
        lines = [line.strip() for line in redact(str(error)).splitlines()
                 if not re.search(r'\[(?:INFO|DEBUG|WARNING)\s*\]', line)
                 and not line.lstrip().startswith(('File "', 'raise ', 'return ', 'if ', 'self.'))]
        text = '\n'.join(lines)
        code = next((code for code, pattern in _RULES if pattern.search(text)), '')
        if not code:
            for local_code, _, keys in LOCAL_ERRORS:
                if any(prefix and prefix in text for key in keys
                       for wording in (key, *TRANSLATIONS.get(key, {}).values())
                       for prefix in [wording.split('{', 1)[0]]):
                    code = local_code
                    break
        if not code:
            if re.search(r'JSONDecodeError|Unexpected JSON root type', text):
                code = 'JSON_INVALID'
            elif re.search(r'BadZipFile|File is not a zip file|Bad CRC-32', text, re.I):
                code = 'CBZ_INVALID'
            elif re.search(r'Selected pages are missing', text, re.I):
                code = 'PAGE_RANGE'
            elif 'does not support selected pages' in text:
                code = 'BT_VERSION'
            elif re.search(r'BadRequestError|UnprocessableEntityError|invalid_request_error|BadRequest\b', text):
                code = 'REQUEST_INVALID'
            elif match := re.search(r'(?:HTTP(?:/\d(?:\.\d)?)?\s+|(?:status(?:_code)?|httpStatusCode|error code)["\x27]?\s*[:=]?\s*)([45]\d\d)\b', text, re.I):
                http = int(match[1])
                code = {401: 'LLM_AUTH', 403: 'LLM_PERMISSION', 429: 'HTTP_429'}.get(http, 'HTTP_5XX' if http >= 500 else 'HTTP_4XX')
            elif re.search(r'exit=-?[1-9]\d*', text):
                code = 'PROCESS_EXIT'
            elif re.search(r'BallonsTranslator .*(?:exit=0)', text):
                code = 'RUN_INCOMPLETE'
            elif re.search(r'Image translation pipeline stopped', str(error)):
                code = 'PIPELINE_STOPPED'
            else:
                code = 'RUN_WARNING' if status == 'done_warning' else 'UNKNOWN_ERROR'
    return code, tr(REASONS[code])


def error_details(status: str, error: str) -> str:
    code, reason = error_info(status, error)
    original = redact(str(error))
    if not code:
        return original
    return f'{tr("錯誤碼")}: {code}\n{tr("錯誤原因")}: {reason}\n\n{tr("原始訊息")}:\n{original}'
