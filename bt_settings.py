"""Complete, typed editing of a selected BallonsTranslator config file."""
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
from tkinter import filedialog, messagebox, simpledialog, ttk
import uuid

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
    return any(re.search(r"(?i)(api[ _-]?key|password|secret|access[ _-]?token|authorization)", str(p)) for p in path)


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
    process = subprocess.run([command[0], "-u", str(script)], cwd=root, env=env,
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


class NewFieldDialog(simpledialog.Dialog):
    TYPES = {"文字": "", "整數": 0, "小數": 0.0, "布林值": False,
             "物件": {}, "清單": [], "空值": None}

    def __init__(self, parent, named):
        self.named = named
        super().__init__(parent, tr("新增欄位／項目"))

    def body(self, master):
        self.name = ttk.Entry(master)
        if self.named:
            ttk.Label(master, text=tr("欄位名稱")).pack(anchor="w")
            self.name.pack(fill="x", pady=5)
        ttk.Label(master, text=tr("欄位型別")).pack(anchor="w")
        self.type_choice = ttk.Combobox(master, state="readonly", values=[tr(k) for k in self.TYPES])
        self.type_choice.current(0)
        self.type_choice.pack(fill="x", pady=5)
        return self.name if self.named else self.type_choice

    def validate(self):
        return bool(self.name.get().strip()) if self.named else True

    def apply(self):
        value = next(value for label, value in self.TYPES.items() if tr(label) == self.type_choice.get())
        self.result = (self.name.get(), deepcopy(value))


class ConfigEditor(tk.Toplevel):
    def __init__(self, parent, settings):
        super().__init__(parent)
        self.title(tr("編輯 BallonsTranslator 設定檔"))
        self.geometry("1050x700")
        self.minsize(760, 540)
        self.transient(parent)
        self.grab_set()
        self.settings = dict(settings)
        self.document = None
        self.current = None
        self.edit_value = None
        self.events = queue.Queue()
        self.working = False
        self.selecting = False
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.bind("<Control-s>", lambda _e: self.save())
        self.bind("<Escape>", lambda _e: self.close())
        self.bind("<Control-f>", lambda _e: self.search_entry.focus_set())

        ttk.Label(self, text=str(Path(settings["bt_config"]).resolve()), wraplength=980).pack(anchor="w", padx=10, pady=8)
        top = ttk.Frame(self, padding=(10, 0))
        top.pack(fill="x")
        ttk.Label(top, text=tr("搜尋設定欄位")).pack(side="left")
        self.search = tk.StringVar()
        self.search_entry = ttk.Entry(top, textvariable=self.search)
        self.search_entry.pack(side="left", fill="x", expand=True, padx=8)
        self.search_entry.bind("<Return>", lambda _e: self.rebuild())
        ttk.Button(top, text=tr("搜尋"), command=self.rebuild).pack(side="left")
        ttk.Button(top, text=tr("全部欄位"), command=self.clear_search).pack(side="left", padx=4)
        footer = ttk.Frame(self, padding=10)
        footer.pack(side="bottom", fill="x")
        self.status = tk.StringVar(value=tr("正在讀取設定定義…"))
        self.save_button = ttk.Button(footer, text=tr("儲存設定檔"), command=self.save)
        self.save_button.pack(side="right")
        ttk.Button(footer, text=tr("關閉"), command=self.close).pack(side="right", padx=5)
        ttk.Button(footer, text=tr("重新載入"), command=self.reload).pack(side="right")
        self.status_label = ttk.Label(footer, textvariable=self.status, wraplength=650)
        self.status_label.pack(side="left", fill="x", expand=True)
        self.status_label.bind("<Configure>", lambda event: self.status_label.configure(wraplength=max(100, event.width)))

        self.panes = ttk.Panedwindow(self, orient="horizontal")
        self.panes.pack(fill="both", expand=True, padx=10, pady=10)
        left = ttk.Frame(self.panes)
        self.panes.add(left, weight=3)
        self.tree = ttk.Treeview(left, columns=("value",), selectmode="browse")
        self.tree.heading("#0", text=tr("設定欄位"))
        self.tree.heading("value", text=tr("目前值"))
        self.tree.column("#0", width=300)
        self.tree.column("value", width=130)
        scroll = ttk.Scrollbar(left, command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        scroll.grid(row=0, column=1, sticky="ns")
        left.rowconfigure(0, weight=1)
        left.columnconfigure(0, weight=1)
        self.tree.bind("<<TreeviewSelect>>", self.select)
        self.panel = ttk.Frame(self.panes, padding=10)
        self.panes.add(self.panel, weight=2)
        self.paths = {}
        self.run_async(lambda: ConfigDocument(settings["bt_config"], bridge(settings, "metadata")), self.loaded)

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

    def loaded(self, document):
        self.document = document
        self.current = None
        self.edit_value = None
        self.rebuild()

    def clear_search(self):
        self.search.set("")
        self.rebuild()

    def rebuild(self, select_path=None):
        if not self.document or self.working or not self.apply():
            return
        selected = self.current if select_path is None else select_path
        self.selecting = True
        self.tree.delete(*self.tree.get_children())
        self.paths = {}
        query = self.search.get().strip().casefold()
        def insert(path, value, parent, inherited=False):
            name = field_name(path[-1]) if path else tr("全部設定")
            if isinstance(path[-1] if path else None, int) and isinstance(value, dict):
                name += " " + str(value.get("name") or value.get("id") or value.get("effect_type") or value.get("transform_type") or "")
            matches = inherited or not query or query in name.casefold() or query in ".".join(map(str, path)).casefold()
            secret = secret_path(path)
            summary = tr("已設定（遮蔽）") if secret and value else ""
            if not secret:
                summary = tr("{0} 個欄位／項目").format(len(value)) if isinstance(value, (dict, list)) else str(value)
                summary = summary.replace("\n", " ")[:90]
            item = self.tree.insert(parent, "end", text=name, values=(summary,), open=bool(query) or len(path) < 2)
            self.paths[item] = path
            children = value.items() if isinstance(value, dict) else enumerate(value) if isinstance(value, list) else ()
            count = 0
            if not secret:
                for key, child in children:
                    count += insert(path + (key,), child, item, matches)
            if not matches and not count:
                self.tree.delete(item)
                del self.paths[item]
                return 0
            return 1
        insert((), self.document.data, "")
        self.selecting = False
        self.current = None
        self.edit_value = None
        target = next((i for i, path in self.paths.items() if path == selected), next(iter(self.paths), None))
        if target:
            self.tree.selection_set(target)
            self.tree.focus(target)
            self.tree.see(target)
            self.select()
        self.status.set(tr("{0} 個可見欄位；{1}").format(len(self.paths), tr("尚未儲存") if self.document.dirty else tr("設定已載入")))

    def select(self, _event=None):
        if self.selecting or self.working or not self.tree.selection():
            return
        path = self.paths.get(self.tree.selection()[0])
        if path is None or path == self.current:
            return
        if not self.apply():
            previous = next((i for i, p in self.paths.items() if p == self.current), None)
            if previous:
                self.selecting = True
                self.tree.selection_set(previous)
                self.selecting = False
            return
        self.current = path
        self.edit_value = None
        for widget in self.panel.winfo_children():
            widget.destroy()
        value = self.document.get(path)
        schema = self.document.schema(path)
        ttk.Label(self.panel, text=field_name(path[-1]) if path else tr("全部設定"), wraplength=330).pack(anchor="w")
        ttk.Label(self.panel, text=" / ".join(map(str, path)), wraplength=330, foreground="#666666").pack(anchor="w", pady=(4, 12))
        if schema.get("description"):
            ttk.Label(self.panel, text=schema["description"], wraplength=330).pack(anchor="w", pady=(0, 8))
        if path == ("module", "finish_code"):
            ttk.Label(self.panel, text=tr("此值由偵測、OCR、翻譯與修補開關自動計算。"), wraplength=330).pack(anchor="w")
            return
        if schema.get("read_only"):
            ttk.Label(self.panel, text=tr("舊版相容欄位；請展開「文字特效」修改對應設定。"), wraplength=330).pack(anchor="w")
            return
        if secret_path(path):
            ttk.Label(self.panel, text=tr("金鑰留白會保留原值；輸入新值可取代。"), wraplength=330).pack(anchor="w")
            variable = tk.StringVar()
            entry = ttk.Entry(self.panel, textvariable=variable, show="•")
            entry.pack(fill="x", pady=8)
            clear = tk.BooleanVar()
            ttk.Checkbutton(self.panel, text=tr("清除已保存的金鑰"), variable=clear).pack(anchor="w")
            self.edit_value = lambda: "" if clear.get() else variable.get() if variable.get() else value
        elif isinstance(value, (dict, list)):
            ttk.Label(self.panel, text=tr("展開左側項目即可編輯每個欄位。"), wraplength=330).pack(anchor="w")
            ttk.Button(self.panel, text=tr("新增欄位／項目"), command=self.add).pack(anchor="w", pady=8)
            template_key = str(path[-1]) if path else ""
            templates = self.document.metadata["templates"].get(template_key, {})
            if templates:
                choice = ttk.Combobox(self.panel, state="readonly", values=tuple(templates))
                choice.current(0)
                choice.pack(fill="x", pady=4)
                ttk.Button(self.panel, text=tr("加入／套用範本"), command=lambda: self.template(templates[choice.get()])).pack(anchor="w")
        elif isinstance(value, bool):
            variable = tk.BooleanVar(value=value)
            ttk.Checkbutton(self.panel, text=tr("啟用"), variable=variable).pack(anchor="w")
            self.edit_value = variable.get
        else:
            current_type = schema.get("type", kind(value))
            if current_type in ("any", "secret"):
                current_type = kind(value)
            choices = self.document.choices(path)
            if isinstance(value, str) and ("prompt" in str(path[-1]) or "\n" in value or len(value) > 160):
                entry = tk.Text(self.panel, height=10, wrap="word", undo=True)
                entry.insert("1.0", value)
                entry.pack(fill="both", expand=True)
                self.edit_value = lambda: entry.get("1.0", "end-1c")
            else:
                variable = tk.StringVar(value="null" if value is None else str(value))
                if choices:
                    entry = ttk.Combobox(self.panel, textvariable=variable, values=choices,
                                         state="readonly" if schema.get("strict_choices") else "normal")
                else:
                    entry = ttk.Entry(self.panel, textvariable=variable)
                entry.pack(fill="x", pady=8)
                def read():
                    text = variable.get()
                    if current_type == "string":
                        return text
                    if current_type == "integer":
                        return int(text)
                    if current_type == "number":
                        return float(text)
                    # Nullable settings (e.g. mirrors) accept a string or null.
                    return None if text == "null" else text
                self.edit_value = read
                if isinstance(value, str) and any(p in str(path[-1]).lower() for p in ("path", "file", "directory")):
                    ttk.Button(self.panel, text=tr("選擇檔案"), command=lambda: self.browse(variable, False)).pack(anchor="w")
                    ttk.Button(self.panel, text=tr("選擇資料夾"), command=lambda: self.browse(variable, True)).pack(anchor="w", pady=3)
            if schema.get("nullable") or value is None:
                null = tk.BooleanVar(value=value is None)
                ttk.Checkbutton(self.panel, text=tr("使用空值（null）"), variable=null).pack(anchor="w")
                read_value = self.edit_value
                self.edit_value = lambda: None if null.get() else read_value()
            ttk.Label(self.panel, text=tr("型別：{0}").format(schema.get("type", current_type))).pack(anchor="w", pady=4)
        if self.edit_value:
            ttk.Button(self.panel, text=tr("套用欄位"), command=self.rebuild).pack(anchor="w", pady=10)
        if path:
            ttk.Button(self.panel, text=tr("移除欄位／項目"), command=self.remove).pack(anchor="w", pady=4)
            if isinstance(path[-1], int):
                row = ttk.Frame(self.panel)
                row.pack(anchor="w")
                ttk.Button(row, text=tr("複製項目"), command=self.duplicate).pack(side="left")
                ttk.Button(row, text=tr("上移"), command=lambda: self.move(-1)).pack(side="left")
                ttk.Button(row, text=tr("下移"), command=lambda: self.move(1)).pack(side="left")

    def apply(self):
        if self.current is None or not self.edit_value:
            return True
        try:
            self.document.set(self.current, self.edit_value())
            return True
        except (ValueError, TypeError):
            messagebox.showerror(tr("設定檔"), tr("欄位值無效，請檢查型別與數值"), parent=self)
            return False

    def has_changes(self):
        if not self.document:
            return False
        try:
            return self.document.dirty or bool(self.edit_value and self.edit_value() != self.document.get(self.current))
        except (ValueError, TypeError):
            return True

    def browse(self, variable, directory):
        selected = filedialog.askdirectory(parent=self) if directory else filedialog.askopenfilename(parent=self)
        if selected:
            variable.set(selected)

    def template(self, value):
        if self.working or not self.apply():
            return
        value = deepcopy(value)
        parent = self.document.get(self.current)
        if isinstance(parent, list):
            if self.current[-1] == "llm_profiles":
                value.update(id="custom-" + uuid.uuid4().hex[:8], name=tr("新 LLM 配置卡"), built_in=False)
            parent.append(value)
            self.rebuild(self.current + (len(parent) - 1,))
        elif messagebox.askyesno(tr("設定檔"), tr("以範本取代此物件？尚未儲存前可以重新載入還原。"), parent=self):
            self.document.set(self.current, value)
            self.rebuild()

    def add(self):
        if self.working or not self.apply():
            return
        parent = self.document.get(self.current)
        if not isinstance(parent, (dict, list)):
            return
        templates = self.document.metadata["templates"].get(str(self.current[-1]) if self.current else "", {})
        if isinstance(parent, list) and templates:
            self.template(next(iter(templates.values())))
            return
        key = len(parent)
        dialog = NewFieldDialog(self, isinstance(parent, dict))
        self.grab_set()
        if dialog.result is None:
            return
        name, value = dialog.result
        if isinstance(parent, dict):
            key = name
            if key in parent:
                messagebox.showerror(tr("設定檔"), tr("欄位已存在"), parent=self)
                return
        if isinstance(parent, list):
            parent.append(value)
        else:
            parent[key] = value
        self.rebuild(self.current + (key,))

    def remove(self):
        if self.working or not self.current or not self.apply():
            return
        if not messagebox.askyesno(tr("設定檔"), tr("移除此欄位或項目？變更會在儲存後生效。"), parent=self):
            return
        path = self.current
        del self.document.get(path[:-1])[path[-1]]
        self.current = None
        self.edit_value = None
        self.rebuild(path[:-1])

    def duplicate(self):
        if self.working or not self.apply():
            return
        value = deepcopy(self.document.get(self.current))
        parent = self.document.get(self.current[:-1])
        if self.current[:-1] == ("module", "llm_profiles"):
            value.update(id="custom-" + uuid.uuid4().hex[:8], built_in=False)
        parent.insert(self.current[-1] + 1, value)
        self.rebuild(self.current[:-1] + (self.current[-1] + 1,))

    def move(self, delta):
        if self.working or not self.apply():
            return
        parent = self.document.get(self.current[:-1])
        source, target = self.current[-1], self.current[-1] + delta
        if 0 <= target < len(parent):
            parent.insert(target, parent.pop(source))
            self.edit_value = None
            self.rebuild(self.current[:-1] + (target,))

    def save(self):
        if self.working or not self.document or not self.apply():
            return
        self.status.set(tr("正在檢查並儲存設定…"))
        self.run_async(lambda: self.document.save(lambda data: bridge(self.settings, "prepare", data)), self.saved)

    def saved(self, backup):
        self.edit_value = None
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
