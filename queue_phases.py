"""Disk-backed, chapter-level preprocessing checkpoints and two-pass execution."""
from copy import deepcopy
from contextlib import ExitStack
from datetime import datetime
import hashlib
import json
from pathlib import Path
import tempfile

from comic_core import load_json, save_json
from ui_language import tr
from queue_stats import merge_snapshots

MODES = {"normal": "原本流程", "prepare": "只跑前處理", "translate": "只翻譯已前處理", "two_pass": "先全部前處理再翻譯", "parallel": "前處理與翻譯並行"}
READY = ("pending", "prepared", "skipped")
MARKER = ".comic-sorting-prepared.json"


def signature(path):
    stat = path.stat()
    if not stat.st_size:
        raise ValueError("Empty preprocessing artifact")
    return [stat.st_size, stat.st_mtime_ns]


def page_record(path, source, project):
    blocks = project["pages"][source.name]
    # Translation/rendering changes other block fields; OCR and geometry must remain intact.
    ocr = [{key: block.get(key) for key in ("text", "lines", "xyxy")} for block in blocks]
    digest = hashlib.sha256(json.dumps(ocr, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    assets = {}
    for folder in ("mask", "inpainted"):
        candidates = [path / folder / (source.stem + suffix) for suffix in (".png", ".jxl")]
        candidates = [p for p in candidates if p.is_file()]
        if not candidates and blocks and folder == "inpainted":
            raise ValueError("Missing preprocessing artifact")
        for asset in candidates:
            assets[str(asset.relative_to(path))] = signature(asset)
    return dict(source=signature(source), ocr=digest, assets=assets)


def prepared(path, selected):
    try:
        marker = load_json(path / MARKER, {}, strict=True)
        project = load_json(path / f"imgtrans_{path.name}.json", {}, strict=True)
        return bool(selected) and marker.get("version") == 1 and all(
            marker.get("pages", {}).get(source.name) == page_record(path, source, project) for source in selected)
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return False


def mark_prepared(path, selected):
    project = load_json(path / f"imgtrans_{path.name}.json", {}, strict=True)
    marker = load_json(path / MARKER, {}, strict=True)
    pages = marker.get("pages", {}) if marker.get("version") == 1 else {}
    pages.update({source.name: page_record(path, source, project) for source in selected})
    save_json(path / MARKER, dict(version=1, pages=pages))


def invalidate_prepared(path, selected):
    marker = load_json(path / MARKER, {}, strict=True)
    if marker.get("version") == 1:
        for source in selected:
            marker.get("pages", {}).pop(source.name, None)
        save_json(path / MARKER, marker)


def run_phased_jobs(jobs, settings, output, skip, stop, emit, *, pause=None, checkpoint=None, blocked_jobs=(), before_job=None):
    if settings["bt_workflow"] == "parallel":
        from queue_parallel import run_parallel_jobs
        return run_parallel_jobs(jobs, settings, output, skip, stop, emit,
                                 pause=pause, checkpoint=checkpoint, blocked_jobs=blocked_jobs)
    from queue_worker import run_jobs
    mode = settings["bt_workflow"]
    phases = ("prepare", "translate") if mode == "two_pass" else (mode,)
    failed = set()
    finished = set()
    started = datetime.now().astimezone().isoformat(timespec="seconds")
    elapsed = 0
    aborted = False
    timings, usage, jev = {}, {}, {}

    def save_checkpoint():
        nonlocal aborted
        aborted = not checkpoint()
        return not aborted

    try:
        config = load_json(settings["bt_config"], {}, strict=True)
        if mode not in MODES:
            raise ValueError("Unknown queue workflow: " + str(mode))
        with ExitStack() as cleanup:
            for phase in phases:
                if stop.is_set() or aborted:
                    break
                indices = [i for i, job in enumerate(jobs) if i not in failed and job.action == "translate"]
                if not indices:
                    break
                phase_config = deepcopy(config)
                module = phase_config.setdefault("module", {})
                for key in ("enable_detect", "enable_ocr", "enable_inpaint"):
                    module[key] = phase == "prepare"
                module["enable_translate"] = phase == "translate"
                module["finish_code"] = 7 if phase == "prepare" else 8
                # Native accounts and keyring IDs are relative to the config directory.
                with tempfile.NamedTemporaryFile(dir=Path(settings["bt_config"]).resolve().parent,
                        prefix=".comic-sorting-" + phase + "-", suffix=".json", delete=False) as file:
                    config_path = Path(file.name)
                cleanup.callback(config_path.unlink, missing_ok=True)
                cleanup.callback(Path(str(config_path) + '.tmp').unlink, missing_ok=True)
                save_json(config_path, phase_config)
                options = dict(settings, bt_workflow="normal", bt_phase=phase, bt_config=str(config_path))
                if phase == "prepare":
                    options.update(bt_export=False, bt_cleanup=False, bt_open_after_completion=False)

                def forward(event):
                    nonlocal elapsed
                    kind = event[0]
                    if kind == "done":
                        return
                    if kind == "run_time":
                        elapsed += event[3]
                        return
                    if kind == "total":
                        # Both passes contribute equally to the visible queue progress.
                        offset = len(jobs) if mode == "two_pass" and phase == "translate" else 0
                        emit(("total", (offset + event[1] * len(jobs) / len(indices)) / len(phases)))
                        return
                    if kind in ("status", "job_time", "stage_time", "usage", "jev_stats"):
                        index = indices[event[1]]
                        event = (kind, index, *event[2:])
                        if kind == "status" and event[2] in ("failed", "cancelled", "blocked"):
                            failed.add(index)
                        if kind == "status" and event[2] in ("done", "done_warning"):
                            finished.add(index)
                        if kind == "job_time":
                            old = timings.get(index)
                            event = (kind, index, old[2] if old else event[2], event[3], event[4] + (old[4] if old else 0))
                            timings[index] = event
                        elif kind in ("usage", "jev_stats"):
                            records = usage if kind == "usage" else jev
                            scope = event[2].get("scope", "")
                            snapshots = records.setdefault((index, scope), {})
                            snapshots[phase] = event[2]
                            combined = merge_snapshots(snapshots.values())
                            event = (kind, index, combined)
                    emit(event)

                run_jobs(tuple(jobs[i] for i in indices), options, output, skip, stop, forward,
                         pause=pause, checkpoint=save_checkpoint if checkpoint else None,
                         before_job=(lambda index: before_job(indices[index])) if before_job else None,
                         blocked_jobs=tuple(i for i, original in enumerate(indices) if original in blocked_jobs))
        for index, job in enumerate(jobs):
            if job.action != "translate" and not stop.is_set() and not aborted:
                emit(("status", index, "skipped", tr("兩階段模式只處理翻譯項目")))
        if not stop.is_set() and not aborted:
            emit(("total", len(jobs)))
    except Exception as error:
        from app_logging import logger, redact
        logger.exception("phased_queue_failed")
        for index in range(len(jobs)):
            if index not in failed and index not in finished:
                emit(("status", index, "failed", redact(str(error))))
    finally:
        emit(("run_time", started, datetime.now().astimezone().isoformat(timespec="seconds"), elapsed))
        emit(("done",))
