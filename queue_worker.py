"""BallonsTranslator subprocesses and UI-independent queue execution."""
from collections import deque
from dataclasses import dataclass
import os
from pathlib import Path
import re
import subprocess
import threading

from comic_core import (clear_work_folders, export_chapter, image_files,
                        load_json, save_json, translation_status)
from ui_language import tr


@dataclass
class Job:
    path: Path
    action: str
    status: str = "pending"
    error: str = ""


def translator_command(installation, config, python_path="", chapter=None):
    root = Path(installation).resolve()
    python = (Path(python_path) if python_path else root / "ballontrans_pylibs_win" / "python.exe").resolve()
    if not (root / "ballontranslator" / "__main__.py").is_file() or not python.is_file():
        raise ValueError(tr("請設定有效的 BallonsTranslator 安裝目錄及其 Python 執行檔"))
    config = Path(config).resolve()
    if not config.is_file():
        raise ValueError(tr("請選擇既有 BallonsTranslator config JSON"))
    command = [str(python), "-u", "-m", "ballontranslator", "--config", str(config)]
    if chapter is not None:
        if "," in str(chapter):
            raise ValueError(tr("BallonsTranslator 的 --exec_dirs 不支援含逗號的路徑"))
        command += ["--headless", "--exec_dirs", str(Path(chapter).resolve())]
    env = os.environ.copy()
    env["PATH"] = os.pathsep.join((str(python.parent), str(python.parent / "Scripts"), env.get("PATH", "")))
    env["PYTHONIOENCODING"] = "utf-8"
    return command, root, env


def run_translation(command, root, env, stop, progress):
    completed = False
    errors = False
    recent = deque(maxlen=20)
    ansi = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\))")
    process = subprocess.Popen(command, cwd=root, env=env, stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               text=True, encoding="utf-8", errors="replace",
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    finished = threading.Event()

    def cancel():
        while not finished.wait(.2) and process.poll() is None:
            if stop.is_set():
                try:
                    process.terminate()
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                except OSError:
                    pass  # The process may exit between poll() and terminate().
                return

    watcher = threading.Thread(target=cancel, daemon=True)
    watcher.start()
    try:
        try:
            process.stdin.write("exit\n")
            process.stdin.flush()
        except (BrokenPipeError, OSError):
            pass  # Read startup diagnostics even if the child already exited.
        for line in process.stdout:
            line = ansi.sub("", line).rstrip()
            recent.append(line)
            completed |= "finished translating all dirs" in line
            errors |= bool(re.search(r"\bERROR\b|Traceback \(most recent call last\)", line))
            match = re.search(r"(Text Detection|OCR|Translation|Inpaint).*?(\d+)%", line)
            if match:
                progress(match[1], int(match[2]))
        code = process.wait()
        if stop.is_set():
            raise RuntimeError(tr("已停止；未執行此項目的後續動作"))
        if code or not completed or errors:
            detail = tr("BallonsTranslator 未成功完成（exit={0}）；請查看其 logs").format(code)
            if recent:
                detail += "\n" + "\n".join(recent)
            raise RuntimeError(detail)
    finally:
        finished.set()
        try:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
        finally:
            try:
                process.stdin.close()
            except OSError:
                pass
            process.stdout.close()
            watcher.join()


def run_jobs(jobs, settings, output, skip, stop, emit):
    """Run a pending-job snapshot; only the event consumer mutates Job state."""
    def export_progress(count, total):
        if stop.is_set():
            raise RuntimeError(tr("已停止；未執行此項目的後續動作"))
        emit(("stage", "匯出", int(count * 100 / total)))

    failed_paths = set()
    try:
        for index, job in enumerate(jobs):
            if stop.is_set():
                break
            path, action = Path(job.path).resolve(), job.action
            if path in failed_paths:
                emit(("status", index, "blocked", tr("此路徑前置工作失敗，跳過後續動作")))
                emit(("total", index + 1))
                continue
            emit(("status", index, "running", ""))
            try:
                if action not in ("translate", "export", "cleanup"):
                    raise ValueError(tr("不支援的佇列動作：{0}").format(action))
                if not path.is_dir():
                    raise ValueError(tr("漫畫路徑不存在"))
                if action == "translate":
                    if not image_files(path):
                        raise ValueError(tr("指定路徑沒有圖片，請選擇章節或整合輸出"))
                    run_translation(*translator_command(settings["bt_path"], settings["bt_config"],
                                                        settings.get("bt_python", ""), path), stop,
                                    lambda name, value: emit(("stage", name, value)))
                    sources = {p.stem for p in image_files(path)}
                    _, translated = translation_status(path)
                    if not sources.issubset({p.stem for p in translated}):
                        raise RuntimeError(tr("翻譯結果不完整，未執行後續動作"))
                if stop.is_set():
                    raise RuntimeError(tr("已停止"))
                if action == "export" or (action == "translate" and settings.get("bt_export", False)):
                    if not str(output).strip():
                        raise ValueError(tr("請設定 Komga 輸出路徑"))
                    target = Path(output).resolve()
                    if target.is_relative_to(path) or path.is_relative_to(target):
                        raise ValueError(tr("匯出與漫畫路徑不可互相包含"))
                    state_file = target / ".comic-sorting-state.json"
                    state = load_json(state_file, {})
                    emit(("stage", "匯出", 0))
                    export_chapter(path.parent, path, target, state, skip,
                                   progress=export_progress)
                    save_json(state_file, state)
                    emit(("stage", "匯出", 100))
                if stop.is_set():
                    raise RuntimeError(tr("已停止，未執行後續清理"))
                if action == "cleanup" or (settings.get("bt_cleanup", False) and action in ("translate", "export")):
                    _, _, errors = clear_work_folders(path)
                    if errors:
                        raise RuntimeError("\n".join(errors))
                if stop.is_set():
                    raise RuntimeError(tr("已停止"))
                emit(("status", index, "done", ""))
            except Exception as error:
                failed_paths.add(path)
                emit(("status", index, "cancelled" if stop.is_set() else "failed", str(error)))
            emit(("total", index + 1))
    finally:
        emit(("done",))
