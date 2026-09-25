"""BallonsTranslator subprocesses and UI-independent queue execution."""
from collections import deque
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from datetime import datetime
import os
import json
import math
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import threading
import time
import uuid

from app_logging import logger, log_path, redact

from comic_core import (clear_work_folders, export_chapter, image_files,
                        load_json, save_json, translation_status, translations_complete)
from ui_language import tr
from queue_errors import error_info


BT_STAGES = {"Text Detection": "enable_detect", "OCR": "enable_ocr",
             "Inpaint": "enable_inpaint", "Translation": "enable_translate"}


def parse_bt_usage(line):
    match = re.search(r"\[INFO\s*\]\s+module_manager:_finish_llm_usage_run:\d+ - LLM (?:(OCR|translation|Jev) )?run usage: (.*)", line)
    if not match:
        return None
    fields = dict(part.split('=', 1) for part in match[2].split(', ') if '=' in part)
    try:
        counts = {name: int(fields[name]) for name in
                  ('requests', 'total_tokens', 'missing_usage_requests', 'unpriced_requests')}
        cost = Decimal(fields['estimated_cost_usd']) if fields['estimated_cost_usd'] != 'unavailable' else None
        if cost is None and counts['unpriced_requests'] < counts['requests']:
            subtotal = fields.get('priced_subtotal_usd', 'unavailable')
            cost = Decimal(subtotal) if subtotal != 'unavailable' else None
        if any(value < 0 for value in counts.values()) or (cost is not None and (not cost.is_finite() or cost < 0)):
            return None
    except (KeyError, ValueError, InvalidOperation):
        return None
    return dict(scope=match[1] or 'total', cost=cost, price_basis=fields.get('price_basis'),
                rates_date=fields.get('rates_date'), **counts)


def parse_jev_stats(line):
    marker = ' - Jev OCR filter audit: '
    if marker not in line:
        return None
    try:
        data = json.loads(line.split(marker, 1)[1])
        if data.get('event') != 'finished' or not isinstance(data.get('audit_id'), str):
            return None
        blocks, usage = data['blocks'], data['usage']
        counts = dict(calls=1, changed_calls=int(any(b['action'] != 'kept' for b in blocks)),
                      cleared_blocks=sum(b['action'] == 'cleared' for b in blocks),
                      trimmed_blocks=sum(b['action'] == 'trimmed' for b in blocks),
                      kept_blocks=sum(b['action'] == 'kept' for b in blocks),
                      removed_characters=sum(b['removed_characters'] for b in blocks),
                      requests=usage['requests'])
        if 'input_tokens' in usage:
            counts.update(input_tokens=usage['input_tokens'], reported_input_requests=usage['reported_input_requests'],
                          missing_input_usage_requests=usage['requests'] - usage['reported_input_requests'])
        else:
            counts.update(reported_requests=usage['reported_requests'], total_tokens=usage['total_tokens'])
        if any(key in usage for key in ('cost_nano_usd', 'reported_cost_requests', 'estimated_cost_requests')):
            for key in ('cost_nano_usd', 'reported_cost_requests', 'estimated_cost_requests'):
                counts[key] = usage.get(key, 0)
            counts['unpriced_requests'] = counts['requests'] - counts['reported_cost_requests'] - counts['estimated_cost_requests']
        seconds = data.get('elapsed_seconds')
        if (any(type(v) is not int or v < 0 for v in counts.values())
                or counts.get('reported_requests', counts.get('reported_input_requests', 0)) > counts['requests']
                or any(b['action'] not in ('cleared', 'trimmed', 'kept') for b in blocks)
                or (seconds is not None and (type(seconds) not in (int, float) or not math.isfinite(seconds) or seconds < 0))):
            return None
        return dict(audit_id=data['audit_id'], elapsed_seconds=seconds, **counts)
    except (ValueError, KeyError, TypeError, AttributeError):
        return None


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
    start_page: int = 1
    end_page: int | None = None
    range_export: bool = False

    def should_export(self, default):
        if self.action != "translate":
            return self.action == "export"
        return self.range_export if self.start_page != 1 or self.end_page is not None else default

    def select_pages(self, images):
        end = len(images) if self.end_page is None else self.end_page
        if (type(self.start_page) is not int or type(end) is not int
                or not 1 <= self.start_page <= end <= len(images)):
            raise ValueError(tr("翻譯頁數必須介於 1 至 {0}，且起始頁不可大於結束頁").format(len(images)))
        return images[self.start_page - 1:end]


def translator_command(installation, config, python_path="", chapter=None, page_manifest=None):
    root = Path(installation).resolve()
    runtimes = (root / "ballontrans_pylibs_win" / "python.exe",
                root / ".venv" / "Scripts" / "python.exe")
    python = (Path(python_path) if python_path else
              next((path for path in runtimes if path.is_file()), runtimes[0])).resolve()
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
        if page_manifest is not None:
            script_dir = Path(getattr(sys, "_MEIPASS", Path(__file__).parent))
            # Keep the bundled Python extensions off the external runtime's import path.
            command[3] = "import runpy, sys; runpy.run_path(sys.argv.pop(1), run_name='__main__')"
            command[4:4] = [str(script_dir / "bt_run_bridge.py"), str(page_manifest)]
    env = os.environ.copy()
    env["PATH"] = os.pathsep.join((str(python.parent), str(python.parent / "Scripts"), env.get("PATH", "")))
    env["PYTHONIOENCODING"] = "utf-8"
    return command, root, env


class TranslationSession:
    """Own one translator process and its descendants for a batch."""
    def __init__(self, stop):
        self.stop = stop
        self.process = None
        self.process_job = None
        self.watcher = None
        self.finished = threading.Event()
        self.fatal = threading.Event()

    def open(self, command, root, env, request, log_context):
        if self.stop.is_set():
            raise RuntimeError(tr("已停止"))
        if self.process is not None and self.process.poll() is None:
            self.process.stdin.write(json.dumps(request, ensure_ascii=False) + "\n")
            self.process.stdin.flush()
            logger.info("[%s] translator_reuse pid=%s", log_context, self.process.pid)
            return self.process
        self.close()
        self.finished.clear()
        self.fatal.clear()
        logger.info("[%s] translator_start command=%s cwd=%s", log_context, subprocess.list2cmdline(command), root)
        process = self.process = subprocess.Popen(
            command, cwd=root, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        logger.info("[%s] translator_pid=%s", log_context, process.pid)
        try:
            if os.name == "nt":
                import win32api
                import win32con
                import win32job
                self.process_job = win32job.CreateJobObject(None, "")
                limits = win32job.QueryInformationJobObject(self.process_job, win32job.JobObjectExtendedLimitInformation)
                limits['BasicLimitInformation']['LimitFlags'] |= win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
                win32job.SetInformationJobObject(self.process_job, win32job.JobObjectExtendedLimitInformation, limits)
                handle = win32api.OpenProcess(win32con.PROCESS_SET_QUOTA | win32con.PROCESS_TERMINATE, False, process.pid)
                try:
                    win32job.AssignProcessToJobObject(self.process_job, handle)
                finally:
                    handle.Close()
            self.watcher = threading.Thread(target=self.watch, daemon=True)
            self.watcher.start()
        except BaseException:
            self.close()
            raise
        return process

    def terminate(self):
        process = self.process
        if process is None:
            return
        try:
            if self.process_job is not None:
                import win32job
                win32job.TerminateJobObject(self.process_job, 1)
            elif process.poll() is None:
                process.terminate()
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        except OSError:
            pass  # The child can exit between poll and terminate.

    def watch(self):
        while not self.finished.wait(.2):
            if self.stop.is_set() or self.fatal.is_set() or self.process.poll() is not None:
                self.terminate()
                return

    def close(self, graceful=False):
        if self.process is None:
            return
        try:
            if graceful and not self.stop.is_set() and self.process.poll() is None:
                try:
                    self.process.stdin.write("exit\n")
                    self.process.stdin.flush()
                    self.process.wait(timeout=5)
                except (OSError, subprocess.TimeoutExpired):
                    pass
        finally:
            self.finished.set()
            self.terminate()
            if self.watcher is not None:
                self.watcher.join()
                self.watcher = None
            try:
                self.process.stdin.close()
            except OSError:
                pass  # The child may already have closed its input pipe.
            self.process.stdout.close()
            if self.process_job is not None:
                self.process_job.Close()
                self.process_job = None
            self.process = None


def run_translation(command, root, env, stop, progress, log_context="", usage=None, timing=None, *, session=None, request=None, jev=None):
    completed = False
    failures = deque(maxlen=5)
    fatal_count = 0
    retries = 0
    recent = deque(maxlen=20)
    stages = {}
    jev_seen, jev_totals = set(), {}
    ansi = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\))")
    owned = session is None
    session = session or TranslationSession(stop)
    try:
        process = session.open(command, root, env, request, log_context)
        if owned:
            try:
                process.stdin.write("exit\n")
                process.stdin.flush()
            except (BrokenPipeError, OSError):
                pass  # Read startup diagnostics even if the child already exited.
        boundary = False
        for line in process.stdout:
            line = redact(ansi.sub("", line).rstrip())
            if request is not None and line == "COMIC_SORTING_DONE:" + request["token"]:
                boundary = True
                break
            if line:
                logger.info("[%s] BT %s", log_context, line)
            recent.append(line)
            consumption = parse_bt_usage(line)
            if consumption is not None and usage is not None:
                usage(consumption)
            stats = parse_jev_stats(line)
            if stats is not None and stats['audit_id'] not in jev_seen:
                jev_seen.add(stats.pop('audit_id'))
                for key, value in stats.items():
                    if value is not None:
                        jev_totals[key] = jev_totals.get(key, 0) + value
                if jev is not None:
                    jev(dict(jev_totals))
                if usage is not None and 'total_tokens' in jev_totals:
                    usage(dict(scope='Jev', cost=None, requests=jev_totals['requests'],
                               total_tokens=jev_totals['total_tokens'],
                               missing_usage_requests=jev_totals['requests'] - jev_totals['reported_requests'],
                               unpriced_requests=jev_totals['requests']))
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
                if "Image translation pipeline stopped" in diagnostic:
                    session.fatal.set()
            stage_progress = parse_bt_progress(line)
            if stage_progress:
                stages[stage_progress[0]] = stage_progress[1]
                progress(*stage_progress)
                elapsed = re.search(r"\[(\d+(?::\d{2}){1,2})<", line)
                if elapsed and timing is not None:
                    parts = [int(part) for part in elapsed[1].split(':')]
                    if all(part < 60 for part in parts[1:]):
                        seconds = 0
                        for part in parts:
                            seconds = seconds * 60 + part
                        timing(stage_progress[0], seconds)
        code = 0 if boundary else process.wait()
        logger.info("[%s] translator_%s=%s completed=%s fatal_errors=%s retry_diagnostics=%s stopped=%s",
                    log_context, "boundary" if boundary else "exit", code, completed, fatal_count, retries, stop.is_set())
        if stop.is_set():
            raise RuntimeError(tr("已停止；未執行此項目的後續動作"))
        if session.fatal.is_set():
            raise RuntimeError("\n".join(failures) + tr("；紀錄：{0}").format(log_path()))
        if code or not completed or (request is not None and not boundary) or failures or any(value < 100 for value in stages.values()):
            finished_with_anomalies = (completed and stages and all(value == 100 for value in stages.values())
                                       and (request is None or boundary))
            detail = (tr("流程已完成但發現異常（exit={0}）") if finished_with_anomalies else
                      tr("BallonsTranslator 未成功完成（exit={0}）；請查看其 logs")).format(code)
            detail += tr("；紀錄：{0}").format(log_path())
            for failure in failures:
                if failure not in recent:
                    detail += "\n" + failure
            if recent:
                detail += "\n" + "\n".join(recent)
            if finished_with_anomalies:
                logger.warning("[%s] completed_with_anomalies %s", log_context, detail)
                return detail
            raise RuntimeError(detail)
    except BaseException:
        session.close()
        raise
    finally:
        if owned:
            session.close()


def run_jobs(jobs, settings, output, skip, stop, emit, *, pause=None, checkpoint=None, blocked_jobs=()):
    """Run a pending-job snapshot; only the event consumer mutates Job state."""
    run_started = time.monotonic()
    paused_seconds = 0
    run_timestamp = datetime.now().astimezone().isoformat(timespec='seconds')
    def export_progress(count, total):
        if stop.is_set():
            raise RuntimeError(tr("已停止；未執行此項目的後續動作"))
        emit(("stage", "匯出", int(count * 100 / total)))

    session = TranslationSession(stop) if sum(job.action == "translate" for job in jobs) > 1 else None
    failed_paths = set()
    run_id = uuid.uuid4().hex[:10]
    logger.info("[%s] queue_start jobs=%s export=%s cleanup=%s", run_id, len(jobs),
                settings.get("bt_export", False), settings.get("bt_cleanup", False))
    try:
        for index, job in enumerate(jobs):
            if pause is not None and pause.is_set() and not stop.is_set():
                paused_at = time.monotonic()
                logger.info("[%s] queue_paused next_job=%s", run_id, index + 1)
                emit(("paused", paused_at))
                while pause.is_set() and not stop.wait(.1):
                    pass
                duration = time.monotonic() - paused_at
                paused_seconds += duration
                emit(("resumed", duration))
                logger.info("[%s] queue_resume pause_seconds=%.3f stopped=%s", run_id, duration, stop.is_set())
            if stop.is_set():
                break
            path, action = Path(job.path).resolve(), job.action
            job_id = f"{run_id}/{index + 1}"
            logger.info("[%s] job_start action=%s path=%s", job_id, action, path)
            if path in failed_paths or index in blocked_jobs:
                failed_paths.add(path)
                logger.warning("[%s] job_blocked predecessor_failed", job_id)
                emit(("status", index, "blocked", tr("此路徑前置工作失敗，跳過後續動作")))
                emit(("total", index + 1))
                if checkpoint is not None and not checkpoint():
                    break
                continue
            emit(("status", index, "running", ""))
            if checkpoint is not None and not checkpoint():
                emit(("status", index, "cancelled", tr("已停止；未執行此項目的後續動作")))
                break
            job_started = time.monotonic()
            job_timestamp = datetime.now().astimezone().isoformat(timespec='seconds')
            try:
                whole_chapter = True
                completion_note = ""
                diagnostic = None
                if action not in ("translate", "export", "cleanup"):
                    raise ValueError(tr("不支援的佇列動作：{0}").format(action))
                if not path.is_dir():
                    raise ValueError(tr("漫畫路徑不存在"))
                if job.should_export(settings.get("bt_export", False)) and str(output).strip():
                    target = Path(output).resolve()
                    if target.is_relative_to(path) or path.is_relative_to(target):
                        raise ValueError(tr("匯出與漫畫路徑不可互相包含"))
                if action == "translate":
                    sources = image_files(path)
                    if not sources:
                        raise ValueError(tr("指定路徑沒有圖片，請選擇章節或整合輸出"))
                    selected = job.select_pages(sources)
                    module = load_json(settings["bt_config"], {}, strict=True).get("module", {})
                    enabled = {name: module.get(flag, True) is not False for name, flag in BT_STAGES.items()}
                    if not any(enabled.values()):
                        raise ValueError(tr("請至少啟用一個 BallonsTranslator 處理階段"))
                    emit(("bt_reset", len(selected), enabled))
                    logger.info("[%s] translation_pages start=%s end=%s selected=%s total=%s", job_id,
                                job.start_page, job.end_page or len(sources), len(selected), len(sources))
                    with tempfile.TemporaryDirectory(prefix="comic-sorting-pages-") as temporary:
                        page_manifest = None
                        request = None
                        if len(selected) != len(sources):
                            page_manifest = Path(temporary) / "pages.json"
                            save_json(page_manifest, [p.name for p in selected])
                        if session is not None:
                            request = dict(path=str(path), pages=[p.name for p in selected] if len(selected) != len(sources) else None,
                                           token=uuid.uuid4().hex)
                            page_manifest = Path(temporary) / "session.json"
                            save_json(page_manifest, request)
                        command_options = {"page_manifest": page_manifest} if page_manifest else {}
                        session_options = dict(session=session, request=request) if session is not None else {}
                        diagnostic = run_translation(*translator_command(settings["bt_path"], settings["bt_config"],
                                                            settings.get("bt_python", ""), path, **command_options), stop,
                                        lambda *values: emit(("bt_progress", *values)), log_context=job_id,
                                        usage=lambda values: emit(("usage", index, values)),
                                        jev=lambda values: emit(("jev_stats", index, values)),
                                        timing=lambda name, seconds: emit(("stage_time", index, name, seconds)), **session_options)
                        if isinstance(diagnostic, str):
                            completion_note = diagnostic
                    status, translated = translation_status(path)
                    if not translations_complete(sources, translated, selected):
                        raise RuntimeError(tr("翻譯結果不完整，未執行後續動作"))
                    whole_chapter = status == tr("可匯出")
                    if not whole_chapter:
                        completion_note = "\n".join(filter(None, (completion_note, tr("指定頁面已完成；全章結果尚未齊全，略過自動匯出與清理"))))
                        logger.info("[%s] partial_range_completed export_cleanup_skipped", job_id)
                    result_path = translated[0].parent
                    logger.info("[%s] result_verified sources=%s results=%s", job_id, len(image_files(path)), len(translated))
                if stop.is_set():
                    raise RuntimeError(tr("已停止"))
                if whole_chapter and job.should_export(settings.get("bt_export", False)):
                    if not str(output).strip():
                        raise ValueError(tr("請設定 Komga 輸出路徑"))
                    target = Path(output).resolve()
                    if target.is_relative_to(path) or path.is_relative_to(target):
                        raise ValueError(tr("匯出與漫畫路徑不可互相包含"))
                    state_file = target / ".comic-sorting-state.json"
                    state = load_json(state_file, {}, strict=True)
                    emit(("stage", "匯出", 0))
                    export_action, archive = export_chapter(path.parent, path, target, state, skip,
                                                            progress=export_progress)
                    save_json(state_file, state)
                    result_path = archive
                    logger.info("[%s] export_%s output=%s", job_id, export_action, archive)
                    emit(("stage", "匯出", 100))
                if stop.is_set():
                    raise RuntimeError(tr("已停止，未執行後續清理"))
                if action == "cleanup" or (whole_chapter and settings.get("bt_cleanup", False) and action in ("translate", "export")):
                    folders, removed, errors = clear_work_folders(path)
                    logger.info("[%s] cleanup_result folders=%s removed=%s errors=%s", job_id, folders, removed, errors)
                    if errors:
                        raise RuntimeError("\n".join(errors))
                if stop.is_set():
                    raise RuntimeError(tr("已停止"))
                emit(("status", index, "done_warning" if action == "translate" and isinstance(diagnostic, str) and diagnostic else "done", completion_note))
                if action == "translate":
                    emit(("result", result_path))
                logger.info("[%s] job_done", job_id)
            except Exception as error:
                if action == "translate" and session is not None:
                    session.close()
                status = "cancelled" if stop.is_set() else "failed"
                code, reason = error_info(status, str(error))
                logger.exception("[%s] job_%s action=%s path=%s error_code=%s reason=%s", job_id,
                                 status, action, path, code, reason)
                failed_paths.add(path)
                emit(("status", index, status, redact(str(error))))
                if code == "LLM_QUOTA":
                    # Preserve the failed job's quota diagnostic; leave later jobs pending.
                    stop.set()
                    logger.error("[%s] queue_stopped error_code=%s reason=%s", run_id, code, reason)
            emit(("job_time", index, job_timestamp, datetime.now().astimezone().isoformat(timespec='seconds'),
                  time.monotonic() - job_started))
            emit(("total", index + 1))
            if checkpoint is not None and not checkpoint():
                break
    finally:
        if session is not None:
            session.close(graceful=True)
        logger.info("[%s] queue_end stopped=%s failed_paths=%s", run_id, stop.is_set(), len(failed_paths))
        emit(("run_time", run_timestamp, datetime.now().astimezone().isoformat(timespec='seconds'),
              time.monotonic() - run_started - paused_seconds))
        emit(("done",))
