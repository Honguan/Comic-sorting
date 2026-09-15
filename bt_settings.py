"""Native run options and font settings without exposing unrelated config fields."""
from copy import deepcopy
from datetime import datetime
import json
import math
from pathlib import Path
import queue
import re
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import font as tkfont
from tkinter import filedialog, messagebox, ttk

from app_logging import logger
from comic_core import save_json
from queue_worker import translator_command
from ui_language import tr


LABELS = {
    "module": "自動化模組", "llm_profiles": "LLM 配置卡", "global_fontformat": "字體與排版",
    "drawpanel": "繪圖工具", "package_manager": "套件管理", "mirrors": "下載鏡像",
    "auto_tate_chu_yoko": "自動縱中橫", "textdetector": "文字偵測器", "ocr": "OCR",
    "translator": "翻譯器", "inpainter": "修補器", "textdetector_params": "偵測器參數",
    "ocr_params": "OCR 參數", "translator_params": "翻譯器參數", "inpainter_params": "修補器參數",
    "enable_detect": "啟用文字偵測", "enable_ocr": "啟用 OCR", "enable_translate": "啟用翻譯",
    "enable_inpaint": "啟用修補", "translate_source": "來源語言", "translate_target": "目標語言",
    "translate_context": "翻譯分組", "llm_translate_context": "LLM 上下文",
    "llm_prior_context_token_budget": "上下文 Token 預算", "llm_glossary_path": "術語表路徑",
    "llm_glossary_mode": "術語表模式", "llm_translate_vision": "翻譯使用圖片",
    "llm_translate_summary_memory": "摘要與記憶", "llm_translate_overwrite_summary": "覆寫既有摘要",
    "translator_llm_id": "翻譯用 LLM", "ocr_llm_id": "OCR 用 LLM", "inpaint_llm_id": "修補用 LLM",
    "api_key": "API 金鑰", "base_url": "基礎 URL", "model": "文本模型", "vision_model": "視覺模型",
    "image_model": "圖像模型", "max_tokens": "Token 輸出上限", "temperature": "溫度",
    "frequency_penalty": "頻率懲罰", "presence_penalty": "存在懲罰", "thinking_level": "思考級別",
    "prompt": "翻譯提示詞", "vision_prompt": "視覺提示詞", "image_prompt": "圖像提示詞",
    "json_schema_response_format": "返回 JSON Schema", "require_api_key": "需要 API 金鑰",
    "support_text": "支援文本", "support_vision": "支援視覺", "support_image": "支援圖像",
    "font_family": "字體", "font_size": "字體大小", "vertical": "直排", "alignment": "對齊",
    "line_spacing": "行距", "letter_spacing": "字距", "text_effects": "文字特效",
    "text_transform": "文字變形", "imgsave_ext": "結果圖片格式", "imgsave_quality": "圖片品質",
    "intermediate_imgsave_ext": "中間圖片格式", "display_lang": "顯示語言", "darkmode": "深色模式",
    "ocr_sublist": "OCR 取代規則", "pre_mt_sublist": "翻譯前取代規則", "mt_sublist": "翻譯後取代規則",
    "spellcheck_enabled": "拼寫檢查", "run_pipeline_mode": "流程模式", "text_styles_path": "樣式檔路徑",
}


def field_name(key):
    label = LABELS.get(str(key))
    return f"{tr(label)} ({key})" if label else str(key)


def secret_path(path):
    return any(re.search(r"(?i)(api[ _-]?key|password|secret|access[ _-]?token|authorization)", str(p))
               for p in path if str(p).casefold() != "require_api_key")


def kind(value):
    return {dict: "object", list: "array", bool: "boolean", int: "integer",
            float: "number", str: "string", type(None): "null"}[type(value)]


def parse_json(text):
    def pairs(items):
        data = {}
        for key, value in items:
            if key in data:
                raise ValueError(tr("設定檔含有重複欄位"))
            data[key] = value
        return data
    def invalid(_value):
        raise ValueError(tr("數值必須是有限數字"))
    return json.loads(text, object_pairs_hook=pairs, parse_constant=invalid)


def bridge(settings, operation, data=None):
    command, root, env = translator_command(settings["bt_path"], settings["bt_config"], settings.get("bt_python", ""))
    script = Path(getattr(sys, "_MEIPASS", Path(__file__).parent)) / "bt_config_bridge.py"
    # Do not put the EXE bundle's incompatible Python extensions on the child import path.
    process = subprocess.run([command[0], "-u", "-c",
                              "import runpy, sys; runpy.run_path(sys.argv[1], run_name='__main__')", str(script)], cwd=root, env=env,
                             input=json.dumps({"operation": operation, "data": data}, ensure_ascii=False, allow_nan=False),
                             capture_output=True, text=True, encoding="utf-8", timeout=45,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    # Never report child output: native validation diagnostics can contain secrets.
    if process.returncode:
        raise ValueError(tr("無法讀取 BallonsTranslator 設定定義，請檢查安裝與 Python 路徑"))
    try:
        result = parse_json(process.stdout)
    except ValueError:
        raise ValueError(tr("BallonsTranslator 設定工具回傳無效資料")) from None
    if "bridge_error" in result:
        raise ValueError(tr("BallonsTranslator 設定檢查失敗：{0}").format(result["bridge_error"]))
    return result


def merge_defaults(default, value):
    """Add native fields without overwriting saved values or dropping unknown ones."""
    if isinstance(default, dict) and isinstance(value, dict):
        result = deepcopy(default)
        for key, child in value.items():
            result[key] = merge_defaults(default.get(key), child)
        return result
    return deepcopy(value)


class ConfigDocument:
    def __init__(self, path, metadata):
        self.path = Path(path).resolve()
        self.original = self.path.read_bytes()
        raw = parse_json(self.original.decode("utf-8-sig"))
        if not isinstance(raw, dict):
            raise ValueError(tr("設定檔根層必須是物件"))
        self.metadata = metadata
        self.data = merge_defaults(metadata["defaults"], raw)
        prototype = metadata["templates"]["llm_profiles"]["LLM"]
        profiles = self.data.get("module", {}).get("llm_profiles", [])
        if isinstance(profiles, list):
            self.data["module"]["llm_profiles"] = [merge_defaults(prototype, p) for p in profiles]
        self.initial = deepcopy(self.data)

    @property
    def dirty(self):
        return self.data != self.initial

    def get(self, path):
        value = self.data
        for key in path:
            value = value[key]
        return value

    def schema(self, path):
        node = self.metadata["schema"]
        for key in path:
            node = node.get("item", {}) if isinstance(key, int) else node.get("children", {}).get(key, {})
        return node

    def choices(self, path):
        if not path:
            return []
        key = str(path[-1])
        parent = self.get(path[:-1])
        if path[:-1] == ("module",) and key in ("translate_source", "translate_target"):
            choices = self.metadata.get("translator_languages", {}).get(parent.get("translator"), {}).get(key)
            if choices:
                return choices
        if key in ("translator_llm_id", "ocr_llm_id", "inpaint_llm_id"):
            support = {"translator_llm_id": "support_text", "ocr_llm_id": "support_vision", "inpaint_llm_id": "support_image"}[key]
            return [p["id"] for p in self.data["module"]["llm_profiles"] if isinstance(p, dict) and p.get(support)]
        if isinstance(parent, dict) and isinstance(parent.get(key + "_options"), list):
            return parent[key + "_options"]
        return self.schema(path).get("choices", [])

    def set(self, path, value):
        if not path:
            raise ValueError(tr("不能移除或取代設定檔根層"))
        expected = self.schema(path).get("type", "any")
        actual = kind(value)
        if self.schema(path).get("nullable") and value is None:
            expected = "any"
        if expected not in ("any", "secret", actual) and not (expected == "number" and actual == "integer"):
            raise ValueError(tr("欄位型別應為 {0}").format(expected))
        if actual == "number" and not math.isfinite(value):
            raise ValueError(tr("數值必須是有限數字"))
        schema = self.schema(path)
        if schema.get("strict_choices") and value not in schema["choices"]:
            raise ValueError(tr("請選擇支援的選項"))
        if actual in ("integer", "number") and not schema.get("minimum", -math.inf) <= value <= schema.get("maximum", math.inf):
            raise ValueError(tr("數值超出允許範圍"))
        parent = self.get(path[:-1])
        parent[path[-1]] = value
        if len(path) == 4 and path[:2] == ("module", "llm_profiles") and path[-1] in ("model", "vision_model", "image_model"):
            options = parent.get(path[-1] + "_options")
            if value and isinstance(options, list) and value not in options:
                options.append(value)

    def validate(self):
        def visit(value, before, path):
            if value == before:
                return
            if path:
                self.set(path, value)
            if isinstance(value, dict) and not secret_path(path):
                for key, child in value.items():
                    visit(child, before.get(key) if isinstance(before, dict) else None, path + (key,))
            elif isinstance(value, list):
                for index, child in enumerate(value):
                    previous = before[index] if isinstance(before, list) and index < len(before) else None
                    visit(child, previous, path + (index,))
        visit(self.data, self.initial, ())
        module = self.data.get("module", {})
        profiles = module.get("llm_profiles", [])
        ids = [p.get("id", "") for p in profiles]
        if any(not isinstance(identifier, str) or not identifier.strip() for identifier in ids) or len(set(ids)) != len(ids):
            raise ValueError(tr("LLM 配置卡 ID 必須非空白且不可重複"))
        for key in ("translator_llm_id", "ocr_llm_id", "inpaint_llm_id"):
            if module.get(key) and module[key] not in ids:
                raise ValueError(tr("選用的 LLM 配置卡不存在：{0}").format(key))

    def save(self, prepare):
        if self.path.read_bytes() != self.original:
            raise ValueError(tr("設定檔已被其他程式修改，請重新載入後再編輯"))
        if not self.dirty:
            return None
        self.validate()
        candidate = prepare(deepcopy(self.data))
        # Recheck after the native validator, before creating a backup or replacing.
        if self.path.read_bytes() != self.original:
            raise ValueError(tr("設定檔已被其他程式修改，請重新載入後再編輯"))
        backup = self.path.with_name(self.path.name + "." + datetime.now().strftime("%Y%m%d-%H%M%S-%f") + ".bak")
        with backup.open("xb") as file:
            file.write(self.original)
        save_json(self.path, candidate)
        self.original = self.path.read_bytes()
        self.data = candidate
        self.initial = deepcopy(candidate)
        logger.info("translator_config_saved path=%s backup=%s", self.path, backup)
        return backup


class ConfigEditor(tk.Toplevel):
    """Focused editor; hidden native settings remain in ConfigDocument."""
    def __init__(self, parent, settings):
        super().__init__(parent)
        self.title(tr("BallonsTranslator 運行設定"))
        self.geometry("980x780")
        self.minsize(760, 540)
        self.transient(parent)
        self.grab_set()
        self.settings = dict(settings)
        self.document = None
        self.fields = {}
        self.events = queue.Queue()
        self.working = False
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.bind("<MouseWheel>", self.scroll)
        self.bind("<Control-s>", lambda _e: self.save())
        self.bind("<Escape>", lambda _e: self.close())
        footer = ttk.Frame(self, padding=10)
        footer.pack(side="bottom", fill="x")
        self.status = tk.StringVar(value=tr("正在讀取設定定義…"))
        self.save_button = ttk.Button(footer, text=tr("儲存設定檔"), command=self.save)
        self.save_button.pack(side="right")
        ttk.Button(footer, text=tr("關閉"), command=self.close).pack(side="right", padx=5)
        ttk.Button(footer, text=tr("重新載入"), command=self.reload).pack(side="right")
        self.status_label = ttk.Label(footer, textvariable=self.status, wraplength=380)
        self.status_label.pack(side="left", fill="x", expand=True)
        self.status_label.bind("<Configure>", lambda e: self.status_label.configure(wraplength=max(100, e.width)))
        ttk.Label(self, text=tr("儲存後供下一次佇列運行使用；頁數範圍請在主佇列設定。"), padding=10).pack(anchor="w")
        self.panel = ttk.Notebook(self)
        self.panel.pack(fill="both", expand=True, padx=10, pady=5)
        self.run_async(lambda: ConfigDocument(settings["bt_config"], bridge(settings, "metadata")), self.loaded)

    def loaded(self, document):
        self.document = document
        self.fields = {}
        self.rebuild()

    def scroll(self, event):
        if self.panel.select():
            tab = self.nametowidget(self.panel.select())
            canvas = next(w for w in tab.winfo_children() if isinstance(w, tk.Canvas))
            canvas.yview_scroll(-int(event.delta / 120), "units")

    def section(self, title):
        tab = ttk.Frame(self.panel)
        self.panel.add(tab, text=tr(title))
        canvas = tk.Canvas(tab, highlightthickness=0)
        scroll = ttk.Scrollbar(tab, command=canvas.yview)
        canvas.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        frame = ttk.Frame(canvas, padding=15)
        window = canvas.create_window(0, 0, anchor="nw", window=frame)
        frame.bind("<Configure>", lambda _e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda e: canvas.itemconfigure(window, width=e.width))
        frame.columnconfigure(1, weight=1)
        return frame

    def field(self, frame, path, label=None, choices=None, refresh=False):
        try:
            value = self.document.get(path)
        except (KeyError, IndexError):
            return
        schema = self.document.schema(path)
        if isinstance(value, (dict, list)) and not secret_path(path):
            return
        row = frame.grid_size()[1]
        ttk.Label(frame, text=tr(label or LABELS.get(path[-1], schema.get("title", path[-1])))).grid(row=row, column=0, sticky="w", padx=(0, 20), pady=6)
        options = choices if choices is not None else self.document.choices(path)
        mapping = {tr(k): v for k, v in options.items()} if isinstance(options, dict) else {str(v): v for v in options}
        if isinstance(value, bool):
            var = tk.BooleanVar(value=value)
            widget = ttk.Checkbutton(frame, variable=var)
            read = var.get
        else:
            var = tk.StringVar(value="" if secret_path(path) else next((k for k, v in mapping.items() if v == value), str(value)))
            if mapping or path[-1] == "font_family":
                widget = ttk.Combobox(frame, textvariable=var, values=tuple(mapping), state="readonly" if choices is not None or schema.get("strict_choices") else "normal")
            else:
                widget = ttk.Entry(frame, textvariable=var, show="•" if secret_path(path) else "")
            def read():
                text = var.get()
                if secret_path(path):
                    return text if text else value
                if text in mapping:
                    return mapping[text]
                if isinstance(value, int):
                    return int(text)
                if isinstance(value, float):
                    return float(text)
                return text
        widget.grid(row=row, column=1, sticky="ew", pady=6)
        self.fields[path] = (var, read)
        if refresh:
            widget.bind("<<ComboboxSelected>>", lambda _e: self.rebuild())
        if path[-1] == "font_family":
            widget.configure(values=sorted(set(tkfont.families(self))), state="normal")
        if path[-1] == "llm_glossary_path":
            ttk.Button(frame, text=tr("瀏覽"), command=lambda: self.browse(var)).grid(row=row, column=2)

    def rebuild(self):
        if not self.document or self.working or not self.apply():
            return
        tab = self.panel.index(self.panel.select()) if self.panel.tabs() else 0
        for child in self.panel.winfo_children():
            child.destroy()
        self.fields = {}
        run = self.section("運行")
        self.field(run, ("run_pipeline_mode",), "原生介面運行模式", {"自動化流程": "pipeline", "僅排版 / 渲染": "rendering"})
        module = self.document.data.get("module", {})
        for key, enabled, label in (("textdetector", "enable_detect", "文字偵測"), ("ocr", "enable_ocr", "OCR"), ("inpainter", "enable_inpaint", "圖像修復"), ("translator", "enable_translate", "翻譯")):
            self.field(run, ("module", enabled), "啟用" + label)
            self.field(run, ("module", key), label + "模組", refresh=True)
        for key, label in (("keep_exist_textlines", "保留已有文本"), ("ocr_font_detect", "字體檢測"), ("ocr_text_postprocess", "大小寫轉換"), ("check_need_inpaint", "跳過簡單區域"), ("filter_mask_by_bboxes", "僅修復文本框內區域"), ("translate_source", "來源語言"), ("translate_target", "目標語言"), ("llm_glossary_path", "術語表"), ("llm_glossary_mode", "術語表模式"), ("llm_translate_vision", "LLM 上下文：視覺"), ("llm_translate_summary_memory", "LLM 上下文：摘要"), ("llm_translate_context", "LLM 上下文"), ("translate_context", "翻譯分組"), ("llm_prior_context_token_budget", "Token 預算"), ("llm_translate_overwrite_summary", "覆寫已有摘要")):
            self.field(run, ("module", key), label)
        self.field(run, ("restore_ocr_empty",), "移除空文本塊")
        self.field(run, ("render_without_text_style_update",), "原生渲染時保留文本樣式")
        params = self.section("目前模組參數")
        for key in ("textdetector", "ocr", "inpainter", "translator"):
            name = module.get(key)
            for param in module.get(key + "_params", {}).get(name, {}):
                self.field(params, ("module", key + "_params", name, param), str(name) + " / " + field_name(param))
        selected = set()
        for key, capability in (("translator_llm_id", "support_text"), ("ocr_llm_id", "support_vision"), ("inpaint_llm_id", "support_image")):
            native = {"translator_llm_id": ("translator", "LLMTranslator"), "ocr_llm_id": ("ocr", "LLMOCR"), "inpaint_llm_id": ("inpainter", "LLMInpaint")}
            module_key, llm_name = native[key]
            if module_key in module and module[module_key] != llm_name:
                continue
            profiles = module.get("llm_profiles", [])
            choices = {str(p.get("name") or p["id"]) + " (" + p["id"] + ")": p["id"] for p in profiles if p.get(capability)}
            self.field(params, ("module", key), choices=choices, refresh=True)
            selected.add(module.get(key))
        for index, profile in enumerate(module.get("llm_profiles", [])):
            if profile.get("id") in selected:
                for key in ("model", "vision_model", "image_model", "base_url", "api_key", "max_tokens", "temperature", "thinking_level", "prompt", "vision_prompt"):
                    self.field(params, ("module", "llm_profiles", index, key), str(profile.get("name", "LLM")) + " / " + field_name(key))
        font = self.section("翻譯字體")
        for key, label in (("let_family_flag", "字體來源"), ("let_fntsize_flag", "字號來源"), ("let_alignment_flag", "對齊來源"), ("let_writing_mode_flag", "書寫方向來源")):
            self.field(font, (key,), label, {"依原文 / 自動": 0, "使用下方全域設定": 1})
        for key, label, choices in (("font_family", "字體", {}), ("font_size", "字號（像素）", None), ("vertical", "直排", None), ("alignment", "對齊", {"靠左": 0, "置中": 1, "靠右": 2}), ("line_spacing", "行距", None), ("letter_spacing", "字距", None), ("italic", "斜體", None), ("underline", "底線", None)):
            self.field(font, ("global_fontformat", key), label, choices)
        self.panel.select(tab)
        self.status.set(tr("尚未儲存") if self.document.dirty else tr("設定已載入"))

    def apply(self):
        if not self.document:
            return True
        before = deepcopy(self.document.data)
        try:
            for path, (_var, read) in self.fields.items():
                self.document.set(path, read())
            return True
        except (ValueError, TypeError):
            self.document.data = before
            messagebox.showerror(tr("設定檔"), tr("欄位值無效：") + field_name(path[-1]), parent=self)
            return False

    def has_changes(self):
        if not self.document:
            return False
        try:
            return self.document.dirty or any(read() != self.document.get(path) for path, (_var, read) in self.fields.items())
        except (ValueError, TypeError):
            return True

    def browse(self, variable):
        selected = filedialog.askopenfilename(parent=self)
        if selected:
            variable.set(selected)

    def run_async(self, work, complete):
        if self.working:
            return
        self.working = True
        self.save_button.configure(state="disabled")
        self.disabled_widgets = []
        def disable(parent):
            for widget in parent.winfo_children():
                if isinstance(widget, (ttk.Entry, ttk.Button, ttk.Checkbutton, ttk.Combobox, tk.Text)):
                    self.disabled_widgets.append((widget, str(widget.cget("state"))))
                    widget.configure(state="disabled")
                disable(widget)
        disable(self.panel)
        def worker():
            try:
                self.events.put((complete, work(), None))
            except Exception as error:
                self.events.put((complete, None, error))
        threading.Thread(target=worker, daemon=True).start()
        self.after(50, self.poll)

    def poll(self):
        try:
            complete, result, error = self.events.get_nowait()
        except queue.Empty:
            self.after(50, self.poll)
            return
        self.working = False
        self.save_button.configure(state="normal")
        for widget, state in self.disabled_widgets:
            if widget.winfo_exists():
                widget.configure(state=state)
        if error:
            self.status.set(tr("操作失敗，原設定檔未被取代"))
            messagebox.showerror(tr("設定檔"), str(error), parent=self)
        else:
            complete(result)

    def save(self):
        if self.working or not self.document or not self.apply():
            return
        self.status.set(tr("正在檢查並儲存設定…"))
        self.run_async(lambda: self.document.save(lambda data: bridge(self.settings, "prepare", data)), self.saved)

    def saved(self, backup):
        self.fields = {}
        self.rebuild()
        self.status.set(tr("已儲存；備份：{0}").format(backup) if backup else tr("沒有變更"))

    def reload(self):
        if self.working:
            return
        if self.has_changes() and not messagebox.askyesno(tr("設定檔"), tr("放棄未儲存的變更並重新載入？"), parent=self):
            return
        self.run_async(lambda: ConfigDocument(self.settings["bt_config"], bridge(self.settings, "metadata")), self.loaded)

    def close(self):
        if self.working:
            return
        if self.has_changes():
            save = messagebox.askyesnocancel(tr("設定檔"), tr("是否儲存設定變更？"), parent=self)
            if save is None:
                return
            if save:
                if not self.apply():
                    return
                self.status.set(tr("正在檢查並儲存設定…"))
                self.run_async(lambda: self.document.save(lambda data: bridge(self.settings, "prepare", data)), lambda _backup: self.destroy())
                return
        self.destroy()
