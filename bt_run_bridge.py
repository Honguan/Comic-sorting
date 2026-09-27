"""Adapt native headless jobs and page ranges without changing the installation."""
import inspect
import json
from pathlib import Path
import sys
import time


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


def install_session(window_class, shared, request):
    """Keep native models alive, accepting one JSON job after saved output."""
    original_run = window_class.on_run_imgtrans
    original_next = window_class.run_next_dir
    if request.get('phase') == 'prepare':
        # Preprocessing must not render untranslated pages over existing results.
        # Keep native per-page OCR/font formatting and project saves intact.
        def save_preprocessing(window, *args, **kwargs):
            return None
        window_class.saveCurrentPage = save_preprocessing

    def run_selected(window, *args, **kwargs):
        pages = request['pages']
        if pages is not None:
            if "pages_to_process" not in inspect.signature(original_run).parameters:
                print('[ERROR] module_manager:bridge:0 - Image translation pipeline stopped: selected pages unsupported', flush=True)
                raise RuntimeError("This BallonsTranslator version does not support selected pages.")
            missing = set(pages).difference(window.imgtrans_proj.pages)
            if not pages or missing:
                print('[ERROR] module_manager:bridge:0 - Image translation pipeline stopped: selected pages missing', flush=True)
                raise ValueError(f"Selected pages are missing from the project: {sorted(missing)}")
            for bar in shared.pbar.values():
                bar.reset(total=len(pages))
            kwargs['pages_to_process'] = pages
        return original_run(window, *args, **kwargs)

    def next_dir(window):
        if window.exec_dirs:
            return original_next(window)
        while window.imsave_thread.isRunning():
            time.sleep(.1)
        if request.get('phase') == 'prepare':
            window.imgtrans_proj.save()
        print('COMIC_SORTING_FINISHED:' + request['token'] + ':' +
              str(int(window.module_manager.proj_finished())), flush=True)
        print('finished translating all dirs', flush=True)
        print('COMIC_SORTING_DONE:' + request['token'], flush=True)
        line = sys.stdin.readline()
        if not line or line.strip() == 'exit':
            window.app.quit()
            return
        following = json.loads(line)
        request.clear()
        request.update(following)
        window.run_batch([request['path']])

    window_class.on_run_imgtrans = run_selected
    window_class.run_next_dir = next_dir


def main():
    pages = json.loads(Path(sys.argv.pop(1)).read_text(encoding="utf-8"))
    from ballontranslator import launch
    launch.args.exec_dirs = [launch.args.exec_dirs]
    setup_locks = launch.setup_locks

    def prepare():
        # This native startup hook runs after configuration, Qt and fonts are ready.
        setup_locks()
        from ballontranslator.ui.mainwindow import MainWindow
        if isinstance(pages, dict):
            install_session(MainWindow, launch.shared, pages)
        else:
            install_page_selection(MainWindow, launch.shared, pages)

    launch.setup_locks = prepare
    launch.main()


if __name__ == "__main__":
    main()
