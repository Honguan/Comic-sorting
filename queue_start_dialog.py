"""Chapter range and manga priority selection for a pending batch."""
import tkinter as tk
from tkinter import messagebox, simpledialog, ttk
from ui_language import tr


class QueueStartDialog(simpledialog.Dialog):
    def __init__(self, parent, paths, *, reorder=False):
        self.paths = list(paths)
        self.reorder = reorder
        super().__init__(parent, tr("漫畫優先順序" if reorder else "翻譯章節範圍"))

    def body(self, master):
        ttk.Label(master, text=tr("拖曳調整漫畫順序；取消不會開始。" if self.reorder else
                                 "選擇單一章節起訖；整合章節仍照原佇列執行。"), wraplength=600).pack(anchor="w", pady=6)
        self.list = ttk.Treeview(master, columns=("path",), show="tree headings", height=12, selectmode="browse")
        self.list.heading("#0", text=tr("漫畫／章節"))
        self.list.heading("path", text=tr("完整路徑"))
        self.list.column("#0", width=320)
        self.list.column("path", width=320)
        scroll = ttk.Scrollbar(master, command=self.list.yview)
        self.list.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.list.pack(fill="both", expand=True)
        for index, path in enumerate(self.paths):
            self.list.insert("", "end", iid=str(index), text=path.name if self.reorder else f"{index + 1}. {path.name}", values=(str(path),))
        controls = ttk.Frame(master)
        controls.pack(fill="x", pady=8)
        if self.reorder:
            self.dragged = None
            self.list.bind("<ButtonPress-1>", self.begin_drag)
            self.list.bind("<B1-Motion>", self.drag)
            self.list.bind("<ButtonRelease-1>", lambda _event: setattr(self, "dragged", None))
            for label, offset in (("上移", -1), ("下移", 1)):
                ttk.Button(controls, text=tr(label), command=lambda step=offset: self.move(step)).pack(side="left")
        else:
            self.first = tk.StringVar(self, "1")
            self.last = tk.StringVar(self, str(len(self.paths)))
            for label, variable in (("起始編號：", self.first), ("結束編號：", self.last)):
                ttk.Label(controls, text=tr(label)).pack(side="left")
                ttk.Spinbox(controls, from_=1, to=len(self.paths), textvariable=variable, width=8).pack(side="left")
        self.resizable(True, True)
        return self.list

    def buttonbox(self):
        box = ttk.Frame(self, padding=8)
        box.pack(fill="x")
        ttk.Button(box, text=tr("確認開始"), command=self.ok).pack(side="right")
        ttk.Button(box, text=tr("取消"), command=self.cancel).pack(side="right", padx=6)
        self.bind("<Escape>", self.cancel)

    def begin_drag(self, event):
        self.dragged = self.list.identify_row(event.y)

    def drag(self, event):
        target = self.list.identify_row(event.y)
        if self.dragged and target:
            self.list.move(self.dragged, "", self.list.index(target))
            self.list.selection_set(self.dragged)
        if event.y < 25:
            self.list.yview_scroll(-1, "units")
        elif event.y > self.list.winfo_height() - 25:
            self.list.yview_scroll(1, "units")

    def move(self, offset):
        selected = self.list.selection()
        if selected:
            item = selected[0]
            self.list.move(item, "", max(0, min(len(self.paths) - 1, self.list.index(item) + offset)))
            self.list.see(item)

    def validate(self):
        if not self.reorder:
            try:
                first, last = int(self.first.get()), int(self.last.get())
                if not 1 <= first <= last <= len(self.paths):
                    raise ValueError
            except ValueError:
                messagebox.showerror(tr("翻譯章節範圍"), tr("請輸入有效的章節起訖編號。"), parent=self)
                return False
        return True

    def apply(self):
        self.result = ([self.paths[int(item)] for item in self.list.get_children()]
                       if self.reorder else self.paths[int(self.first.get()) - 1:int(self.last.get())])
