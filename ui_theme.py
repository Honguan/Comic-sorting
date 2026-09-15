"""Compact light desktop theme inspired by Linear's restrained accent and surfaces."""
from tkinter import font, ttk


def apply_theme(root):
    background, surface = "#f5f6f8", "#ffffff"
    ink, muted, border = "#20232d", "#626774", "#d7dbe3"
    accent, selected = "#5e6ad2", "#e9ecff"
    family = font.nametofont("TkDefaultFont", root=root).cget("family")
    heading = (family, font.nametofont("TkDefaultFont", root=root).cget("size"), "bold")
    root.configure(background=background)
    root.option_add("*Toplevel.background", background)
    root.option_add("*Text.background", surface)
    root.option_add("*Text.foreground", ink)
    root.option_add("*Text.selectBackground", selected)
    root.option_add("*Text.selectForeground", ink)
    style = ttk.Style(root)
    style.theme_use("clam")
    style.configure(".", background=background, foreground=ink, font="TkDefaultFont",
                    bordercolor=border, lightcolor=border, darkcolor=border)
    style.configure("TLabel", padding=0)
    style.configure("Muted.TLabel", foreground=muted)
    style.configure("TLabelframe", relief="solid", borderwidth=1)
    style.configure("TLabelframe.Label", font=heading, foreground=ink)
    style.configure("TButton", background=surface, padding=(4, 2), relief="flat", focusthickness=1, focuscolor=accent)
    style.map("TButton", background=[("disabled", background), ("pressed", selected), ("active", "#eef0f6")],
              foreground=[("disabled", "#8c919c")], bordercolor=[("focus", accent)])
    style.configure("Accent.TButton", background=accent, foreground=surface)
    style.map("Accent.TButton", background=[("disabled", "#e2e4ed"), ("pressed", "#4753b4"), ("active", "#515dc4")],
              foreground=[("disabled", "#727887"), ("!disabled", surface)])
    for name in ("TEntry", "TCombobox", "TSpinbox"):
        style.configure(name, fieldbackground=surface, padding=2, arrowsize=12)
        style.map(name, bordercolor=[("focus", accent)],
                  fieldbackground=[("disabled", background), ("readonly", "#f0f2f6")],
                  foreground=[("disabled", muted), ("readonly", ink)])
    for name in ("TCheckbutton", "TRadiobutton"):
        style.map(name, background=[("active", background)], foreground=[("disabled", muted)])
    style.configure("TNotebook", borderwidth=0, tabmargins=(0, 4, 0, 0))
    style.configure("TNotebook.Tab", padding=(10, 3), background=background)
    style.map("TNotebook.Tab", background=[("selected", surface), ("active", selected)],
              foreground=[("selected", "#4652b5")])
    style.configure("Treeview", background=surface, fieldbackground=surface, borderwidth=1,
                    rowheight=font.nametofont("TkDefaultFont", root=root).metrics("linespace") + 8)
    style.map("Treeview", background=[("selected", selected)], foreground=[("selected", ink)])
    style.configure("Treeview.Heading", background="#eef0f5", font=heading, padding=(4, 4), relief="flat")
    style.map("Treeview.Heading", background=[("active", "#e3e6f0")])
    style.configure("Horizontal.TProgressbar", background=accent, troughcolor="#e6e9f0", borderwidth=0)
    style.configure("TScrollbar", background="#c5cad6", troughcolor=background, borderwidth=0, arrowsize=12)
    style.map("TScrollbar", background=[("active", "#a5adbf")])
