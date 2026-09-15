"""Light Windows desktop theme with native controls and a blue primary action."""
from tkinter import font, ttk


def apply_theme(root):
    background, surface = "#f7f9fc", "#ffffff"
    ink, muted, accent = "#192b40", "#607086", "#0067c0"
    selected = "#dceeff"
    family = font.nametofont("TkDefaultFont", root=root).cget("family")
    heading = (family, font.nametofont("TkDefaultFont", root=root).cget("size"), "bold")
    root.configure(background=background)
    root.option_add("*Toplevel.background", background)
    root.option_add("*Text.background", surface)
    root.option_add("*Text.foreground", ink)
    root.option_add("*Text.selectBackground", selected)
    root.option_add("*Text.selectForeground", ink)
    style = ttk.Style(root)
    style.theme_use("vista" if "vista" in style.theme_names() else "clam")
    style.configure(".", background=background, foreground=ink, font="TkDefaultFont")
    style.configure("TLabel", padding=0)
    style.configure("Muted.TLabel", foreground=muted)
    style.configure("TLabelframe", borderwidth=1)
    style.configure("TLabelframe.Label", font=heading, foreground=accent)
    style.configure("TButton", padding=(3, 1))
    # Share a drawable border so hover colors are visible on native Windows themes.
    if "Primary.border" not in style.element_names():
        style.element_create("Primary.border", "from", "clam", "Button.border")
    style.layout("TButton", [("Primary.border", {"sticky": "nswe", "children": [
        ("Button.focus", {"sticky": "nswe", "children": [
            ("Button.padding", {"sticky": "nswe", "children": [
                ("Button.label", {"sticky": "nswe"})]})]})]})])
    style.configure("TButton", background=surface, foreground=ink,
                    bordercolor="#bac7d6", lightcolor=surface, darkcolor=surface,
                    relief="flat", borderwidth=1, focusthickness=1, focuscolor=accent)
    style.map("TButton",
              background=[("disabled", "#edf0f4"), ("pressed", "#badcff"), ("active", "#dceeff")],
              foreground=[("disabled", "#8793a3"), ("active", "#004e92")],
              bordercolor=[("disabled", "#d6dfe9"), ("pressed", "#004e92"), ("active", accent), ("focus", accent)])
    style.configure("Accent.TButton", background=accent, foreground=surface,
                    bordercolor=accent, lightcolor=accent, darkcolor=accent,
                    relief="flat", borderwidth=1, focusthickness=1, focuscolor=surface)
    style.map("Accent.TButton", background=[("disabled", "#e6ebf1"), ("pressed", "#004e92"), ("active", "#1979ca")],
              foreground=[("disabled", "#778599"), ("!disabled", surface)],
              bordercolor=[("disabled", "#d6dfe9")])
    for name in ("TEntry", "TCombobox", "TSpinbox"):
        style.configure(name, padding=1)
    style.configure("TNotebook", tabmargins=(0, 2, 0, 0))
    style.configure("TNotebook.Tab", padding=(10, 2))
    style.map("TNotebook.Tab", foreground=[("selected", accent)])
    style.configure("Treeview", background=surface, fieldbackground=surface,
                    rowheight=font.nametofont("TkDefaultFont", root=root).metrics("linespace") + 8)
    style.map("Treeview", background=[("selected", selected)], foreground=[("selected", ink)])
    style.configure("Treeview.Heading", font=heading, padding=(4, 3))
