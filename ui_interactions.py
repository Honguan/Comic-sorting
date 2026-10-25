"""Selection details and keyboard actions scoped to the focused widget."""
from tkinter import Text, ttk
from ui_theme import px


def details_text(parent, variable, **grid):
    """Read-only details box gridded with `grid`; hidden while empty so tables keep the space."""
    frame = ttk.Frame(parent)
    frame.grid(**grid)
    text = Text(frame, width=1, height=2, wrap='word', font='TkDefaultFont',
                relief='flat', borderwidth=0, padx=px(4), pady=px(4), state='disabled', takefocus=True)
    scrollbar = ttk.Scrollbar(frame, command=text.yview)
    text.configure(yscrollcommand=scrollbar.set)
    text.pack(side='left', fill='both', expand=True)
    scrollbar.pack(side='right', fill='y')
    def refresh(*_args):
        value = variable.get()
        if value:
            frame.grid()
        else:
            frame.grid_remove()
        if text.get('1.0', 'end-1c') != value:
            text.configure(state='normal')
            text.delete('1.0', 'end')
            text.insert('1.0', value)
            text.configure(state='disabled')
            text.yview_moveto(0)
    def select_all(_event):
        text.tag_add('sel', '1.0', 'end-1c')
        return 'break'
    text.bind('<Control-a>', select_all)
    variable.trace_add('write', refresh)
    refresh()
    return frame, text


class LibraryTree(ttk.Treeview):
    """Materialize chapter rows when expanded or requested by existing callers."""
    def __init__(self, parent, load_children, **options):
        super().__init__(parent, **options)
        self.load_children = load_children
        self.bind('<<TreeviewOpen>>', lambda _event: load_children(self.focus()))

    def get_children(self, item=None):
        if item:
            self.load_children(item)
        return super().get_children(item)

    def item(self, item, option=None, **options):
        if options.get('open'):
            self.load_children(item)
        return super().item(item, option, **options)


def copy_manga_names(tree, series_paths):
    names = list(dict.fromkeys(path.name for path in series_paths))
    if names:
        tree.clipboard_clear()
        tree.clipboard_append("\n".join(names))


def bind_table_shortcuts(tree, selected_paths):
    def select_all(_event=None):
        tree.selection_set(tree.get_children())
        return "break"

    def clear_selection(_event=None):
        tree.selection_remove(tree.selection())
        return "break"

    def copy_paths(_event=None):
        paths = list(dict.fromkeys(str(path) for path in selected_paths()))
        if paths:
            tree.clipboard_clear()
            tree.clipboard_append("\n".join(paths))
        return "break"

    tree.bind("<Control-a>", select_all)
    tree.bind("<Escape>", clear_selection)
    tree.bind("<Control-c>", copy_paths)
    return copy_paths


def bind_search_shortcuts(entry, variable, apply_filter, tree):
    def focus_search(_event=None):
        entry.focus_set()
        entry.selection_range(0, 'end')
        return 'break'

    def search(_event=None):
        apply_filter()
        tree.focus_set()
        return 'break'

    def clear_search(_event=None):
        if variable.get():
            variable.set('')
            apply_filter()
        tree.focus_set()
        return 'break'

    for widget in (entry, tree):
        widget.bind('<Control-f>', focus_search)
    entry.bind('<Return>', search)
    entry.bind('<Escape>', clear_search)
    return focus_search
