"""Optional ntfy settings and background delivery; never controls translation state."""
import base64
import json
import queue
import re
import threading
import uuid
from datetime import datetime
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
import tkinter as tk
from tkinter import ttk

from windows_notifications import WindowsNotifications
from app_logging import logger, redact
from ui_language import tr


def validate_settings(settings):
    if not all(isinstance(settings.get(key), str) for key in ('server', 'topic', 'token')):
        raise ValueError(tr('通知設定欄位必須是文字'))
    server = settings['server'].strip().rstrip('/')
    try:
        url = urlsplit(server)
        valid = (url.scheme in ('https', 'http') and url.hostname and not url.username
                 and not url.password and not url.path and not url.query and not url.fragment
                 and not re.search(r'\s', server))
        url.port  # Reject malformed ports before dispatching background work.
    except ValueError:
        valid = False
    if not valid:
        raise ValueError(tr('請輸入 ntfy 伺服器網址，例如 https://ntfy.sh（不含主題或查詢參數）'))
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', settings['topic'].strip()):
        raise ValueError(tr('主題須為 1–64 個英文字母、數字、底線或連字號'))
    token = settings['token'].strip()
    if token and (url.scheme != 'https' or not re.fullmatch(r'[A-Za-z0-9._~+/=-]+', token)):
        raise ValueError(tr('存取權杖須使用 HTTPS，且不可包含空白或換行'))
    return dict(server=server, topic=settings['topic'].strip(), token=token)


def protect_token(token):
    if not token:
        return ''
    import win32crypt
    return base64.b64encode(win32crypt.CryptProtectData(
        token.encode('utf-8'), 'Comic sorting ntfy', None, None, None, 1)).decode('ascii')


def unprotect_token(value):
    if not value:
        return ''
    import win32crypt
    return win32crypt.CryptUnprotectData(base64.b64decode(value, validate=True), None, None, None, 1)[1].decode('utf-8')


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # Do not forward a private topic or bearer token to a redirected host.


def publish(settings, title, message, priority, stop):
    # Identify this client: some reverse proxies reject Python's default User-Agent.
    headers = {'Content-Type': 'application/json; charset=utf-8', 'User-Agent': 'Comic-sorting'}
    if settings['token']:
        headers['Authorization'] = 'Bearer ' + settings['token']
    # ntfy limits messages to 4096 bytes; leave room for UTF-8 and an ellipsis.
    message = redact(message)
    if len(message.encode('utf-8')) > 3500:
        message = message.encode('utf-8')[:3500].decode('utf-8', errors='ignore') + '…'
    payload = dict(topic=settings['topic'], title=title, message=message, priority=priority)
    request = Request(settings['server'] + '/', data=json.dumps(payload, ensure_ascii=False).encode('utf-8'), headers=headers)
    opener = build_opener(NoRedirect())
    for attempt in range(2):
        if stop.is_set():
            return 'NTFY_CANCELLED'
        delay = 2
        try:
            with opener.open(request, timeout=10) as response:
                result = json.loads(response.read(16384))
            if not isinstance(result, dict) or result.get('event') != 'message' or not result.get('id'):
                return 'NTFY_RESPONSE'
            return 'NTFY_OK'
        except HTTPError as error:
            code = f'NTFY_HTTP_{error.code}'
            retry = error.code == 429 or 500 <= error.code < 600
            retry_after = error.headers.get('Retry-After', '') if error.headers else ''
            if error.code == 403 and error.headers and 'cloudflare' in error.headers.get('Server', '').lower():
                try:
                    if re.search(rb'\berror code:\s*1010\b', error.read(16384), re.IGNORECASE):
                        code = 'NTFY_CLOUDFLARE_1010'
                except (OSError, ValueError):
                    pass  # Keep the HTTP status if the optional diagnostic body cannot be read.
            error.close()
            if retry_after:
                # Do not retry before a longer/date-formatted Retry-After expires.
                if not retry_after.isdigit() or int(retry_after) > 30:
                    return code
                delay = max(delay, int(retry_after))
            if not retry:
                return code
        except (TimeoutError, URLError, OSError):
            # Delivery may have succeeded before a connection dropped: do not resend blindly.
            return 'NTFY_NETWORK'
        except (ValueError, UnicodeError):
            return 'NTFY_RESPONSE'
        if attempt == 0 and stop.wait(delay):
            return 'NTFY_CANCELLED'
    return code


class NtfyNotifications:
    def __init__(self, app, settings):
        self.app = app
        self.saved = dict(enabled=settings.get('ntfy_enabled') is True,
                          server=settings.get('ntfy_server', 'https://ntfy.sh'),
                          topic=settings.get('ntfy_topic', 'comic-sorting-' + uuid.uuid4().hex),
                          token_protected=settings.get('ntfy_token_protected', ''),
                          done=settings.get('ntfy_done') is not False,
                          failed=settings.get('ntfy_failed') is not False,
                          success=settings.get('ntfy_success') is True)
        self.token_error = False
        try:
            self.saved_token = unprotect_token(self.saved['token_protected'])
        except Exception:
            self.saved_token = ''
            self.token_error = True
        self.enabled = tk.BooleanVar(value=self.saved['enabled'])
        self.server = tk.StringVar(value=self.saved['server'])
        self.topic = tk.StringVar(value=self.saved['topic'])
        self.token = tk.StringVar(value=self.saved_token)
        self.done = tk.BooleanVar(value=self.saved['done'])
        self.failed = tk.BooleanVar(value=self.saved['failed'])
        self.success = tk.BooleanVar(value=self.saved['success'])
        self.status = tk.StringVar(value=tr('權杖無法解密，請重新輸入並儲存；通知尚未發送') if self.token_error else tr('尚未發送通知'))
        self.tasks = queue.Queue(maxsize=100)
        self.results = queue.Queue()
        self.worker = None
        self.closed = threading.Event()
        self.pending = 0
        self.after_id = None
        self.tab = ttk.Frame(app.work_tabs)
        app.work_tabs.add(self.tab, text=tr('通知'))
        self.windows = WindowsNotifications(app, self.tab, settings)
        box = ttk.LabelFrame(self.tab, text=tr('ntfy 通知'), padding=8)
        box.pack(fill='x', padx=8, pady=8)
        row = ttk.Frame(box)
        row.pack(fill='x')
        for text, variable in ((tr('啟用通知'), self.enabled), (tr('整批結束'), self.done),
                               (tr('單項失敗／異常'), self.failed), (tr('單項成功'), self.success)):
            ttk.Checkbutton(row, text=text, variable=variable).pack(side='left', padx=(0, 12))
        for text, variable in ((tr('伺服器網址'), self.server), (tr('主題（Topic）'), self.topic),
                               (tr('存取權杖（選填）'), self.token)):
            row = ttk.Frame(box)
            row.pack(fill='x', pady=2)
            ttk.Label(row, text=text, width=22).pack(side='left')
            ttk.Entry(row, textvariable=variable, show='•' if variable is self.token else '').pack(side='left', fill='x', expand=True)
        row = ttk.Frame(box)
        row.pack(fill='x', pady=4)
        ttk.Button(row, text=tr('儲存通知設定'), command=self.save).pack(side='left')
        self.test_button = ttk.Button(row, text=tr('測試通知'), command=self.test)
        self.test_button.pack(side='left', padx=4)
        ttk.Button(row, text=tr('複製訂閱網址'), command=self.copy_url).pack(side='left')
        ttk.Label(box, text=tr('手機／其他設備訂閱相同伺服器與主題；公開主題請勿分享，權杖需搭配受保護主題。'), wraplength=720).pack(anchor='w')
        ttk.Label(box, textvariable=self.status, wraplength=720).pack(anchor='w')
        app.root.bind('<Destroy>', self.close, add='+')

    def settings(self):
        return dict({'ntfy_' + key: value for key, value in self.saved.items()}, **self.windows.settings())

    def read_fields(self):
        return validate_settings(dict(server=self.server.get(), topic=self.topic.get(), token=self.token.get()))

    def save(self):
        try:
            config = (self.read_fields() if self.enabled.get() else
                      dict(server=self.server.get().strip(), topic=self.topic.get().strip(), token=self.token.get().strip()))
        except ValueError as error:
            self.status.set('NTFY_CONFIG: ' + str(error))
            return False
        try:
            encrypted = protect_token(config['token'])
        except Exception:
            self.status.set('NTFY_TOKEN: ' + tr('權杖保護失敗，設定未儲存'))
            return False
        previous, previous_token = self.saved, self.saved_token
        self.saved = dict(enabled=self.enabled.get(), server=config['server'], topic=config['topic'],
                          token_protected=encrypted, done=self.done.get(), failed=self.failed.get(), success=self.success.get())
        if not self.app.save_settings():
            self.saved, self.saved_token = previous, previous_token
            self.status.set(tr('通知設定儲存失敗'))
            return False
        self.saved_token, self.token_error = config['token'], False
        self.status.set(tr('通知設定已儲存'))
        return True

    def copy_url(self):
        try:
            config = self.read_fields()
        except ValueError as error:
            self.status.set('NTFY_CONFIG: ' + str(error))
            return
        self.app.root.clipboard_clear()
        self.app.root.clipboard_append(config['server'] + '/' + config['topic'])
        self.status.set(tr('訂閱網址已複製；請在其他設備訂閱'))

    def test(self):
        try:
            config = self.read_fields()
        except ValueError as error:
            self.status.set('NTFY_CONFIG: ' + str(error))
            return
        self.enqueue(config, 'Comic sorting - ' + tr('測試通知'), tr('ntfy 連線測試成功，這是 Comic sorting 的測試通知。'), 3)

    def send(self, event, title, message, priority=3):
        self.windows.send(event, title, message, priority)
        if not self.saved['enabled'] or not self.saved[event]:
            return
        if self.token_error:
            self.status.set('NTFY_TOKEN: ' + tr('權杖無法解密，請重新輸入並儲存；通知尚未發送'))
            return
        try:
            config = validate_settings(dict(self.saved, token=self.saved_token))
        except ValueError as error:
            self.status.set('NTFY_CONFIG: ' + str(error))
            return
        self.enqueue(config, 'Comic sorting - ' + title, message, priority)

    def enqueue(self, config, title, message, priority):
        if self.closed.is_set():
            return
        if self.worker is None:
            try:
                self.worker = threading.Thread(target=self.deliver, daemon=True)
                self.worker.start()
            except RuntimeError:
                self.worker = None
                self.status.set('NTFY_INTERNAL: ' + tr('通知背景程序無法啟動'))
                logger.warning('ntfy_send_result code=NTFY_INTERNAL')
                return
        try:
            self.tasks.put_nowait((config, title, message, priority))
        except queue.Full:
            self.status.set('NTFY_QUEUE_FULL: ' + tr('通知等待過多，已略過本則通知'))
            logger.warning('ntfy_send_result code=NTFY_QUEUE_FULL')
            return
        self.pending += 1
        self.test_button.configure(state='disabled')
        self.status.set(tr('通知發送中…'))
        if self.after_id is None:
            self.after_id = self.app.root.after(100, self.poll)

    def deliver(self):
        # ponytail: in-memory delivery; add a persistent outbox only if shutdown recovery is needed.
        while not self.closed.is_set():
            task = self.tasks.get()
            if task is None or self.closed.is_set():
                break
            try:
                code = publish(*task, self.closed)
            except Exception:
                code = 'NTFY_INTERNAL'
            logger.log(20 if code == 'NTFY_OK' else 30, 'ntfy_send_result code=%s', code)
            self.results.put(code)

    def poll(self):
        self.after_id = None
        while True:
            try:
                code = self.results.get_nowait()
            except queue.Empty:
                break
            self.pending -= 1
            reason = (tr('通知已送達 ntfy 伺服器；設備需開啟通知權限') if code == 'NTFY_OK'
                      else tr('通知未確認送達；請檢查網址、訂閱、權限或網路，翻譯佇列不受影響'))
            reason = {'NTFY_HTTP_401': tr('ntfy 權杖無效或未提供'),
                      'NTFY_HTTP_403': tr('伺服器拒絕請求（HTTP 403）；請檢查 ntfy 權限或代理／防火牆規則'),
                      'NTFY_CLOUDFLARE_1010': tr('Cloudflare 阻擋用戶端（1010）；請檢查伺服器的 Cloudflare 規則，並非 ntfy 權杖權限判定'),
                      'NTFY_HTTP_404': tr('ntfy 伺服器網址不存在'),
                      'NTFY_HTTP_429': tr('ntfy 通知頻率或額度已達上限'),
                      'NTFY_NETWORK': tr('ntfy 連線失敗或逾時；為避免重複通知，未自動重送'),
                      'NTFY_RESPONSE': tr('伺服器未回傳有效的 ntfy 發布結果')}.get(code, reason)
            self.status.set(f'{datetime.now():%H:%M:%S} {code}: {reason}')
        if self.pending:
            self.after_id = self.app.root.after(100, self.poll)
        else:
            self.test_button.configure(state='normal')

    def close(self, event):
        if event.widget is not self.app.root:
            return
        self.closed.set()
        if self.after_id is not None:
            self.app.root.after_cancel(self.after_id)
        try:
            self.tasks.put_nowait(None)
        except queue.Full:
            pass
