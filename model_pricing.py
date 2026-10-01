"""Token rates, public model catalog, and per-model user overrides."""
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from functools import lru_cache
import json
from urllib.request import urlopen

from app_logging import logger

CATALOG_URL = 'https://openrouter.ai/api/v1/models'
# Standard USD per million tokens, verified on each OpenAI model page 2026-10-01.
DEFAULT_RATES = {
    'gpt-6.1-sol': ('2', '0.10', '2.50', '10'),
    'gpt-6-astra': ('10', '1', '12.50', '50'),
    'gpt-6-sol': ('2', '0.20', '2.50', '10'),
    'gpt-6-luna': ('0.10', '0.01', '0.125', '0.50'),
    'gpt-5.6-sol': ('4', '0.40', '5', '20'),
    'gpt-5.6-terra': ('2', '0.20', '2.50', '12'),
    'gpt-5.6-luna': ('0.20', '0.02', '0.25', '1.20'),
    'gpt-5.5': ('5', '0.50', None, '30'),
    'gpt-5.4': ('2.50', '0.25', None, '15'),
    'gpt-5.4-mini': ('0.75', '0.075', None, '4.50'),
    'gpt-4.1': ('2', '0.50', None, '8'),
    'gpt-4.1-mini': ('0.40', '0.10', None, '1.60'),
    'gpt-4o': ('2.50', '1.25', None, '10'),
    'gpt-4o-mini': ('0.15', '0.075', None, '0.60'),
}
LONG_CONTEXT = set(DEFAULT_RATES) - {'gpt-5.4-mini', 'gpt-4.1', 'gpt-4.1-mini', 'gpt-4o', 'gpt-4o-mini'}
RATE_FIELDS = ('prompt', 'input_cache_read', 'input_cache_write', 'completion')


def validate_rates(values):
    if not isinstance(values, (list, tuple)) or len(values) != 4:
        raise ValueError('Four token rates are required')
    rates = tuple(Decimal(value) if value is not None else None for value in values)
    if any(value is not None and (not value.is_finite() or value < 0) for value in rates) or rates[0] is None or rates[3] is None:
        raise ValueError('Token rates must be finite and nonnegative')
    return rates


@lru_cache(maxsize=1)
def fetch_catalog():
    try:
        with urlopen(CATALOG_URL, timeout=10) as response:
            data = json.load(response)['data']
        return {model['id']: model['pricing'] for model in data if isinstance(model.get('id'), str)}
    except (OSError, ValueError, KeyError, TypeError) as error:
        logger.warning('model_pricing_catalog_unavailable %s', error)
        return {}  # Retry explicitly or after restarting; never fetch once per response.


def get_price(model, custom=None, *, local=False, catalog=False):
    if not model:
        return None
    custom = custom or {}
    if model in custom:
        return dict(rates=validate_rates(custom[model]), source='custom', date=datetime.now().date().isoformat())
    if local:
        return dict(rates=(Decimal(0),) * 4, source='local', date=datetime.now().date().isoformat())
    base_model = model  # Snapshot IDs can have different prices; never strip dates.
    if base_model in DEFAULT_RATES:
        return dict(rates=validate_rates(DEFAULT_RATES[base_model]), source='OpenAI Standard API equivalent',
                    date='2026-10-01', long_context=base_model in LONG_CONTEXT,
                    url='https://developers.openai.com/api/docs/models/' + base_model)
    if not catalog:
        return None
    models = fetch_catalog()
    aliases = {'gemini-flash-latest': '~google/gemini-flash-latest',
               'gemini-pro-latest': '~google/gemini-pro-latest'}
    key = aliases.get(model, model)
    matches = [name for name in models if name == key or name.endswith('/' + key)]
    if key in models:
        matches = [key]
    if len(matches) != 1:
        return None
    pricing = models[matches[0]]
    price = dict(pricing=pricing, source='OpenRouter catalog API equivalent',
                 date=datetime.now(timezone.utc).date().isoformat(), url=CATALOG_URL)
    try:
        effective_rates(price, 0)
    except (ValueError, InvalidOperation, TypeError, KeyError):
        return None
    return price


def effective_rates(price, prompt):
    if 'rates' in price:
        rates = price['rates']
        if price.get('long_context') and prompt > 272_000:
            return tuple(rate * Decimal('1.5' if index == 3 else '2') if rate is not None else None
                         for index, rate in enumerate(rates))
        return rates
    base = price['pricing']
    now = datetime.now(timezone.utc)
    clock = now.hour * 100 + now.minute
    selected = dict(base)
    for override in base.get('overrides', []):
        if prompt <= override.get('min_prompt_tokens', -1):
            continue
        if 'utc_days' in override and now.strftime('%A').lower() not in override['utc_days']:
            continue
        start, end = override.get('utc_start', 0), override.get('utc_end', 2400)
        if not (start <= clock < end if start < end else clock >= start or clock < end):
            continue
        selected = dict(base, **override)  # Missing override rates inherit from base.
    return validate_rates([str(Decimal(selected.get(field, selected['prompt'])) * 1_000_000)
                           for field in RATE_FIELDS])


def open_pricing(parent, custom, save):
    import tkinter as tk
    from tkinter import messagebox, ttk
    from ui_language import tr
    window = tk.Toplevel(parent)
    window.title(tr('模型單價'))
    window.geometry('680x390')
    frame = ttk.Frame(window, padding=12)
    frame.pack(fill='both', expand=True)
    ttk.Label(frame, text=tr('每百萬 Token 的美元單價；自訂單價優先。快取留白使用輸入單價。'), wraplength=640).pack(fill='x')
    values = [tk.StringVar() for _ in range(5)]
    known = sorted(set(DEFAULT_RATES) | set(custom))
    for index, title in enumerate(('模型', '輸入 Token', '快取讀取', '快取寫入', '輸出 Token')):
        row = ttk.Frame(frame)
        row.pack(fill='x', pady=4)
        ttk.Label(row, text=tr(title), width=16).pack(side='left')
        entry = ttk.Combobox(row, textvariable=values[index], values=known) if index == 0 else ttk.Entry(row, textvariable=values[index])
        entry.pack(side='left', fill='x', expand=True)
    def load(*_args):
        price = get_price(values[0].get().strip(), custom, catalog=False)
        rates = effective_rates(price, 0) if price else ('',) * 4
        for variable, rate in zip(values[1:], rates):
            variable.set('' if rate is None else str(rate))
    values[0].trace_add('write', load)
    def apply(remove=False):
        model = values[0].get().strip()
        if not model:
            return
        if not remove:
            try:
                rates = validate_rates([values[1].get(), values[2].get() or values[1].get(),
                                        values[3].get() or values[1].get(), values[4].get()])
            except (ValueError, InvalidOperation):
                messagebox.showerror(tr('模型單價'), tr('單價必須是大於或等於零的有限數字。'), parent=window)
                return
        old = dict(custom)
        if remove:
            custom.pop(model, None)
        else:
            custom[model] = [str(rate) for rate in rates]
        if not save():
            custom.clear()
            custom.update(old)
            return
        load()
        messagebox.showinfo(tr('模型單價'), tr('已儲存'), parent=window)
    buttons = ttk.Frame(frame)
    buttons.pack(fill='x', pady=8)
    ttk.Button(buttons, text=tr('儲存'), command=apply).pack(side='left')
    ttk.Button(buttons, text=tr('恢復預設'), command=lambda: apply(True)).pack(side='left', padx=8)
    ttk.Label(frame, text=tr('其他模型會自動查詢 OpenRouter 公開費率；查無單價時可在此新增任何模型。'), wraplength=640).pack(fill='x')
