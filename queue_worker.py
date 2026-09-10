"""BallonsTranslator subprocesses and UI-independent queue execution."""
from collections import deque
from dataclasses import dataclass
import os
from pathlib import Path
import re
import subprocess
import threading
import uuid

from app_logging import logger, log_path, redact

from comic_core import (clear_work_folders, export_chapter, image_files,
                        load_json, save_json, translation_status)
from ui_language import tr


BT_STAGES = {"Text Detection": "enable_detect", "OCR": "enable_ocr",
             "Inpaint": "enable_inpaint", "Translation": "enable_translate"}


def parse_bt_progress(line):
    """Read tqdm's completed-page counters and ETA without inferring page counts."""
    match = re.match(r"(Text Detection|OCR|Inpaint|Translation):\s*(\d{1,3})%", line.lstrip())
    if not match or int(match[2]) > 100:
        return None
    current = total = eta = None
    detail = re.match(r"\|[^|]*\|\s*(\d+)/(\d+)\s*\[([^\]]*)\]", line.lstrip()[match.end():])
    percent = int(match[2])
    if detail:
        current, total = int(detail[1]), int(detail[2])
        if total <= 0 or current > total:
            return None
        percent = current * 100 // total
        remaining = re.search(r"<((?:\d+:)?\d{2}:\d{2})(?:,|$)", detail[3])
        eta = "00:00" if current == total else remaining[1] if remaining else None
    return match[1], percent, current, total, eta


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
        # run_batch accepts a list; its string form splits literal path commas.
        command[2:4] = ["-c", "from ballontranslator.launch import args, main; "
                         "args.exec_dirs = [args.exec_dirs]; main()"]
        command += ["--headless", "--exec_dirs", str(Path(chapter).resolve())]
    env = os.environ.copy()
    env["PATH"] = os.pathsep.join((str(python.parent), str(python.parent / "Scripts"), env.get("PATH", "")))
    env["PYTHONIOENCODING"] = "utf-8"
    return command, root, env


def run_translation(command, root, env, stop, progress, log_context=""):
    completed = False
    failures = deque(maxlen=5)
    fatal_count = 0
    retries = 0
    recent = deque(maxlen=20)
    ansi = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\))")
    logger.info("[%s] translator_start command=%s cwd=%s", log_context, subprocess.list2cmdline(command), root)
    process = subprocess.Popen(command, cwd=root, env=env, stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               text=True, encoding="utf-8", errors="replace",
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    logger.info("[%s] translator_pid=%s", log_context, process.pid)
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
            line = redact(ansi.sub("", line).rstrip())
            if line:
                logger.info("[%s] BT %s", log_context, line)
            recent.append(line)
            completed |= "finished translating all dirs" in line
            record = re.search(r"\[(?:ERROR|WARNING|INFO|DEBUG)\s*\]\s+\w+:\w+:\d+ - ", line)
            diagnostic = line[record.start():] if record else line
            # This diagnostic is emitted BEFORE the translator's built-in retry.
            # Exhausted retries still emit a separate fatal error and traceback.
            if re.match(r"\[ERROR\s*\]\s+trans_llm:_translate:\d+ - Failed to parse matching translation count for prompt:", diagnostic):
                retries += 1
            elif (re.match(r"(?:\[ERROR\s*\]|ERROR\b|Traceback \(most recent call last\))", diagnostic)
                  or re.search(r"module_manager:.* - Image translation pipeline stopped", diagnostic)):
                fatal_count += 1
                failures.append(line)
            stage_progress = parse_bt_progress(line)
            if stage_progress:
                progress(*stage_progress)
        code = process.wait()
        logger.info("[%s] translator_exit=%s completed=%s fatal_errors=%s retry_diagnostics=%s stopped=%s",
                    log_context, code, completed, fatal_count, retries, stop.is_set())
        if stop.is_set():
            raise RuntimeError(tr("已停止；未執行此項目的後續動作"))
        if code or not completed or failures:
            detail = tr("BallonsTranslator 未成功完成（exit={0}）；請查看其 logs").format(code)
            detail += tr("；紀錄：{0}").format(log_path())
            for failure in failures:
                if failure not in recent:
                    detail += "\n" + failure
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
    run_id = uuid.uuid4().hex[:10]
    logger.info("[%s] queue_start jobs=%s export=%s cleanup=%s", run_id, len(jobs),
                settings.get("bt_export", False), settings.get("bt_cleanup", False))
    try:
        for index, job in enumerate(jobs):
            if stop.is_set():
                break
            path, action = Path(job.path).resolve(), job.action
            job_id = f"{run_id}/{index + 1}"
            logger.info("[%s] job_start action=%s path=%s", job_id, action, path)
            if path in failed_paths:
                logger.warning("[%s] job_blocked predecessor_failed", job_id)
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
                    sources = image_files(path)
                    if not sources:
                        raise ValueError(tr("指定路徑沒有圖片，請選擇章節或整合輸出"))
                    module = load_json(settings["bt_config"], {}).get("module", {})
                    emit(("bt_reset", len(sources), {name: module.get(flag, True) is not False
                                                   for name, flag in BT_STAGES.items()}))
                    run_translation(*translator_command(settings["bt_path"], settings["bt_config"],
                                                        settings.get("bt_python", ""), path), stop,
                                    lambda *values: emit(("bt_progress", *values)), log_context=job_id)
                    status, translated = translation_status(path)
                    if status != tr("可匯出"):
                        raise RuntimeError(tr("翻譯結果不完整，未執行後續動作"))
                    logger.info("[%s] result_verified sources=%s results=%s", job_id, len(image_files(path)), len(translated))
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
                    export_action, archive = export_chapter(path.parent, path, target, state, skip,
                                                            progress=export_progress)
                    save_json(state_file, state)
                    logger.info("[%s] export_%s output=%s", job_id, export_action, archive)
                    emit(("stage", "匯出", 100))
                if stop.is_set():
                    raise RuntimeError(tr("已停止，未執行後續清理"))
                if action == "cleanup" or (settings.get("bt_cleanup", False) and action in ("translate", "export")):
                    folders, removed, errors = clear_work_folders(path)
                    logger.info("[%s] cleanup_result folders=%s removed=%s errors=%s", job_id, folders, removed, errors)
                    if errors:
                        raise RuntimeError("\n".join(errors))
                if stop.is_set():
                    raise RuntimeError(tr("已停止"))
                emit(("status", index, "done", ""))
                logger.info("[%s] job_done", job_id)
            except Exception as error:
                logger.exception("[%s] job_%s action=%s path=%s", job_id,
                                 "cancelled" if stop.is_set() else "failed", action, path)
                failed_paths.add(path)
                emit(("status", index, "cancelled" if stop.is_set() else "failed", redact(str(error))))
            emit(("total", index + 1))
    finally:
        logger.info("[%s] queue_end stopped=%s failed_paths=%s", run_id, stop.is_set(), len(failed_paths))
        emit(("done",))
