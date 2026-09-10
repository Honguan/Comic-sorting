"""Pass selected image names to BallonsTranslator's native page-range pipeline."""
import inspect
import json
from pathlib import Path
import sys


def install_page_selection(window_class, shared, pages):
    original = window_class.on_run_imgtrans
    if "pages_to_process" not in inspect.signature(original).parameters:
        raise RuntimeError("This BallonsTranslator version does not support selected pages; update BallonsTranslator.")

    def run_selected(window, *args, **kwargs):
        missing = set(pages).difference(window.imgtrans_proj.pages)
        if not pages or missing:
            raise ValueError(f"Selected pages are missing from the project: {sorted(missing)}")
        for bar in shared.pbar.values():
            bar.reset(total=len(pages))
        kwargs["pages_to_process"] = pages
        return original(window, *args, **kwargs)

    window_class.on_run_imgtrans = run_selected


def main():
    pages = json.loads(Path(sys.argv.pop(1)).read_text(encoding="utf-8"))
    from ballontranslator import launch
    launch.args.exec_dirs = [launch.args.exec_dirs]
    setup_locks = launch.setup_locks

    def prepare():
        # This native startup hook runs after configuration, Qt and fonts are ready.
        setup_locks()
        from ballontranslator.ui.mainwindow import MainWindow
        install_page_selection(MainWindow, launch.shared, pages)

    launch.setup_locks = prepare
    launch.main()


if __name__ == "__main__":
    main()
