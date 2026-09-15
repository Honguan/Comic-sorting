"""Keyboard actions scoped to the focused table, never to text inputs."""


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
