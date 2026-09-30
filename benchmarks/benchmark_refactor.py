"""Repeatable local UI/statistics measurements; no models, network or user writes."""
import json
import importlib.util
from pathlib import Path
import statistics
import subprocess
import sys
import tempfile
import time
import tkinter as tk
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from queue_history import HistoryWindow, save_run
from translation_queue import ACTIONS, STATUSES
from queue_worker import Job
from queue_schedule import QueueSchedule

spec = importlib.util.spec_from_file_location('comic_benchmark', Path(__file__).resolve().parents[1] / 'Comic sorting.py')
comic = importlib.util.module_from_spec(spec)
spec.loader.exec_module(comic)


def record():
    return dict(total_tokens=1000, requests=1, missing_usage_requests=0,
                unpriced_requests=0, cost='0.01')


def history():
    with tempfile.TemporaryDirectory() as temp:
        path = Path(temp) / 'history.sqlite3'
        for index in range(3):
            save_run(path, dict(id=str(index), started_at='2026-10-01T00:00:00+08:00',
                               status='done', elapsed_seconds=10,
                               jobs=[dict(path=f'Manga/Chapter {i}', action='translate', status='done',
                                          usage={'total': record()}) for i in range(1000)]))
        samples, nodes = [], 0
        root = tk.Tk()
        root.withdraw()
        try:
            for _ in range(3):
                start = time.perf_counter()
                window = HistoryWindow(root, path, ACTIONS, STATUSES)
                root.update_idletasks()
                samples.append(time.perf_counter() - start)
                nodes = len(window.records)
                window.destroy()
        finally:
            root.destroy()
        return dict(seconds=statistics.median(samples), materialized_records=nodes)


def usage(size):
    root = tk.Tk()
    root.withdraw()
    with patch.object(comic, 'load_json', return_value={}):
        q = comic.FileAggregatorApp(root).translation_queue
    for index in range(size):
        q.usage_records[index, 'total'] = record()
    samples = []
    for _ in range(3):
        start = time.perf_counter()
        for index in range(100):
            q.usage_records[index, 'total'] = dict(record(), total_tokens=index)
            q.show_usage()
        samples.append(time.perf_counter() - start)
    root.destroy()
    return statistics.median(samples)


def schedule(cls):
    samples = []
    for _ in range(3):
        root = SimpleNamespace(after=lambda *args: 1, bind=lambda *args, **kwargs: None,
                               grab_current=lambda: None)
        variable = SimpleNamespace(get=lambda: '')
        q = SimpleNamespace(app=SimpleNamespace(root=root, manga_busy=False, komga_path=variable,
                                               skip_unchanged=variable), running=False,
                            jobs=[Job(Path(f'Manga/Chapter {i}'), 'translate') for i in range(5000)],
                            installation=variable, config=variable, python=variable, export=variable,
                            cleanup=variable, workflow_mode=lambda: 'normal')
        selected = []
        def start(**kwargs):
            selected.extend(kwargs['scheduled_jobs'])
            q.running = True
        q.start = start
        scheduler = cls(q)
        scheduler.entries.append(dict(due=datetime.now() - timedelta(seconds=1), deadline=None,
                                      jobs=[(job, scheduler.signature(job)) for job in q.jobs],
                                      options=scheduler.options(), status='等待'))
        start = time.perf_counter()
        scheduler.tick()
        samples.append(time.perf_counter() - start)
        assert selected == q.jobs
    return statistics.median(samples)


def baseline_schedule():
    source = subprocess.run(['git', 'show', '3b6decb:queue_schedule.py'], check=True,
                            capture_output=True, encoding='utf-8').stdout
    with tempfile.TemporaryDirectory() as temp:
        path = Path(temp) / 'schedule_baseline.py'
        path.write_text(source, encoding='utf-8')
        spec = importlib.util.spec_from_file_location('schedule_baseline', path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return schedule(module.QueueSchedule)


if __name__ == '__main__':
    print(json.dumps(dict(history=history(), usage_500=usage(500), usage_5000=usage(5000),
                         schedule_5000_before=baseline_schedule(), schedule_5000_after=schedule(QueueSchedule)), indent=2))
