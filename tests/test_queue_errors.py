import unittest

from queue_errors import REASONS, error_details, error_info
from ui_language import LANGUAGES, TRANSLATIONS, set_language, tr


class QueueErrorTests(unittest.TestCase):
    def tearDown(self):
        set_language('zh-TW')

    def test_real_diagnostics_distinguish_causes_from_wrappers(self):
        cases = (
            ('Codex turn failed: Selected model is at capacity. Please try a different model.\n'
             '[INFO] module_manager:_imgtrans_pipeline:1142 - Image translation pipeline stopped by user', 'LLM_CAPACITY'),
            ('RateLimitError: Error code: 429 - {"code":"insufficient_quota"}', 'LLM_QUOTA'),
            ('RateLimitError: Error code: 429 - {"code":"credit_balance_exhausted"}', 'LLM_QUOTA'),
            ('Codex turn failed: You have hit your usage limit', 'LLM_QUOTA'),
            ('Error code: 429 - {"code":"rate_limit_exceeded"}', 'LLM_RATE_LIMIT'),
            ('Error code: 429', 'HTTP_429'),
            ('HTTP 503: server_is_overloaded', 'LLM_CAPACITY'),
            ('HTTP 503: service unavailable', 'HTTP_5XX'),
            ('HTTP/1.1 401 Unauthorized', 'LLM_AUTH'),
            ('Error code: 403', 'LLM_PERMISSION'),
            ('Codex request timed out. Check Codex usage before retrying.', 'REQUEST_TIMEOUT'),
            ('Codex App Server exited before completion.', 'CODEX_EXIT'),
            ('Cannot start Codex: [WinError 2]', 'CODEX_START'),
            ('Codex ChatGPT login is required. Run codex login, then retry.', 'LLM_AUTH'),
            ('Codex requested an interactive tool; translation stopped.', 'CODEX_INTERACTIVE'),
            ('Invalid Codex protocol response: missing turn', 'CODEX_PROTOCOL'),
            ('maximum context length exceeded', 'LLM_CONTEXT'),
            ('LLM output limit reached (8192)', 'LLM_OUTPUT_LIMIT'),
            ('LLM translation failed: count mismatch', 'LLM_RESPONSE'),
            ('ocr module LLMOCR failed to run: APIConnectionError: disconnected', 'NETWORK_ERROR'),
            ('ocr module LLMOCR failed to run: backend failed', 'OCR_FAILED'),
            ('textdetector module CTD failed to run: CUDA out of memory', 'GPU_MEMORY'),
            ('inpaint module LAMA failed to run: backend failed', 'INPAINT_FAILED'),
            ('Unable to read image: D:/Comics/948.jpg\nAttributeError: NoneType.shape', 'IMAGE_READ'),
            ('PIL.UnidentifiedImageError: cannot identify image file', 'IMAGE_READ'),
            ('ImportError: DLL load failed while importing QtCore', 'ENV_DEPENDENCY'),
            ('[WinError 112] There is not enough space on the disk', 'DISK_FULL'),
            ('[WinError 32] file is being used', 'FILE_LOCKED'),
            ('[WinError 5] 拒絕存取', 'FILE_PERMISSION'),
            ('[Errno 2] No such file or directory', 'FILE_MISSING'),
            ('[WinError 206] 路徑太長', 'PATH_TOO_LONG'),
            ('BallonsTranslator 未成功完成（exit=3221225477）', 'PROCESS_CRASH'),
            ('BallonsTranslator 未成功完成（exit=7）', 'PROCESS_EXIT'),
            ('BallonsTranslator 未成功完成（exit=0）', 'RUN_INCOMPLETE'),
            ('This BallonsTranslator version does not support selected pages; update BallonsTranslator.', 'BT_VERSION'),
            ('Selected pages are missing from the project: ["2.jpg"]', 'PAGE_RANGE'),
            ('[DEBUG] sample="at capacity"\n[WARNING] old rate_limit_exceeded\nTypeError: unexpected value', 'UNKNOWN_ERROR'),
            ('D:/Mangas/429.jpg', 'UNKNOWN_ERROR'),
            ('new unexpected error', 'UNKNOWN_ERROR'),
        )
        for message, code in cases:
            with self.subTest(message=message):
                self.assertEqual(error_info('failed', message), (code, REASONS[code]))

    def test_local_errors_classify_in_all_supported_languages(self):
        cases = {
            '請設定有效的 BallonsTranslator 安裝目錄及其 Python 執行檔': 'BT_INSTALL',
            '請選擇既有 BallonsTranslator config JSON': 'JSON_INVALID',
            '漫畫路徑不存在': 'MANGA_MISSING',
            '指定路徑沒有圖片，請選擇章節或整合輸出': 'NO_IMAGES',
            '翻譯頁數必須介於 1 至 {0}，且起始頁不可大於結束頁': 'PAGE_RANGE',
            '請至少啟用一個 BallonsTranslator 處理階段': 'NO_STAGE',
            '翻譯結果不完整，未執行後續動作': 'RESULT_INCOMPLETE',
            '匯出與漫畫路徑不可互相包含': 'EXPORT_PATH',
            'CBZ 內容損壞': 'CBZ_INVALID',
            '不允許刪除系列目錄外或符號連結資料夾': 'UNSAFE_PATH',
        }
        for language in LANGUAGES:
            set_language(language)
            for source, code in cases.items():
                with self.subTest(language=language, code=code):
                    self.assertEqual(error_info('failed', tr(source).format(20))[0], code)
            for reason in REASONS.values():
                self.assertIn(reason, TRANSLATIONS)
                self.assertEqual(error_info('failed', 'at capacity')[1], tr(REASONS['LLM_CAPACITY']))

    def test_status_retry_and_unknown_error_keep_truthful_details(self):
        for status in ('pending', 'running', 'done'):
            self.assertEqual(error_info(status, 'at capacity'), ('', ''))
        self.assertEqual(error_info('blocked', 'at capacity')[0], 'QUEUE_BLOCKED')
        self.assertEqual(error_info('cancelled', '已停止')[0], 'USER_CANCELLED')
        self.assertEqual(error_info('cancelled', '上次執行中斷，請確認結果後重試')[0], 'RUN_INTERRUPTED')
        self.assertEqual(error_info('done_warning', 'exit=3221225477')[0], 'PROCESS_CRASH')
        self.assertEqual(error_info('done_warning', '')[0], 'RUN_WARNING')
        raw = 'Something unexpected: ' + 'sk-or-v1-' + 'a' * 40
        details = error_details('failed', raw)
        self.assertIn('UNKNOWN_ERROR', details)
        self.assertIn('Something unexpected:', details)
        self.assertNotIn('a' * 40, details)
        self.assertEqual(error_details('done', 'partial page range'), 'partial page range')
