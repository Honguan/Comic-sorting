"""Two persistent phase workers, joined through durable chapter checkpoints."""
from datetime import datetime
from decimal import Decimal
import threading
import time

from queue_phases import run_phased_jobs


def run_parallel_jobs(jobs, settings, output, skip, stop, emit, *, pause=None,
                      checkpoint=None, blocked_jobs=()):
    started = datetime.now().astimezone().isoformat(timespec='seconds')
    clock = time.monotonic()
    lock = threading.RLock()
    ready = [threading.Event() for _ in jobs]
    consumed = [threading.Event() for _ in jobs]
    states = {}
    previous, paths = {}, {}
    for index, job in enumerate(jobs):
        if job.action == 'translate':
            path = job.path.resolve()
            previous[index] = paths.get(path)
            paths[path] = index
    active = {'prepare': False, 'translate': False}
    totals = {'prepare': 0, 'translate': 0}
    snapshots, timings = {}, {}
    paused_at = None
    paused_seconds = 0

    def update_pause():
        nonlocal paused_at, paused_seconds
        if pause is not None and pause.is_set() and not any(active.values()) and not stop.is_set():
            if paused_at is None:
                paused_at = time.monotonic()
                emit(('paused', paused_at))
        elif paused_at is not None:
            duration = time.monotonic() - paused_at
            paused_seconds += duration
            paused_at = None
            emit(('resumed', duration))

    def worker(phase):
        terminal = None

        def before(index):
            with lock:
                active[phase] = False
                update_pause()
            dependency = ready[index] if phase == 'translate' else (
                consumed[previous[index]] if previous[index] is not None else None)
            while not stop.is_set():
                with lock:
                    update_pause()
                    if (pause is None or not pause.is_set()) and (dependency is None or dependency.is_set()):
                        if phase == 'translate' and states.get(index) != 'prepared':
                            consumed[index].set()
                            return False
                        active[phase] = True
                        return True
                stop.wait(.05)
            return False

        def persist():
            nonlocal terminal
            if checkpoint is not None and not checkpoint():
                stop.set()
                return False
            with lock:
                if terminal is not None:
                    (ready if phase == 'prepare' else consumed)[terminal].set()
                    terminal = None
                    active[phase] = False
                update_pause()
            return not stop.is_set()

        def forward(event):
            nonlocal terminal
            kind = event[0]
            with lock:
                if kind in ('done', 'run_time'):
                    return
                if kind == 'status':
                    index, status = event[1:3]
                    if phase == 'prepare':
                        states[index] = status
                    if status != 'running':
                        terminal = index
                if kind == 'total':
                    totals[phase] = event[1]
                    emit(('total', sum(totals.values()) / 2))
                    return
                if kind == 'job_time':
                    old = timings.get(event[1])
                    if old:
                        event = (kind, event[1], min(old[2], event[2]), event[3], old[4] + event[4])
                    timings[event[1]] = event
                if kind in ('usage', 'jev_stats'):
                    key = (kind, event[1], event[2].get('scope', ''))
                    values = snapshots.setdefault(key, {})
                    values[phase] = event[2]
                    combined = {}
                    for snapshot in values.values():
                        for name, value in snapshot.items():
                            if name == 'cost' and value is not None:
                                combined[name] = str(Decimal(combined.get(name) or '0') + Decimal(value))
                            elif value is not None and not isinstance(value, str):
                                combined[name] = (combined.get(name) or 0) + value
                            elif name not in combined:
                                combined[name] = value
                    event = (kind, event[1], combined)
                emit(('phase_event', phase, event))

        try:
            run_phased_jobs(jobs, dict(settings, bt_workflow=phase), output, skip, stop,
                            forward, checkpoint=persist, blocked_jobs=blocked_jobs,
                            before_job=before)
        except Exception:
            stop.set()
            from app_logging import logger
            logger.exception('parallel_phase_failed phase=%s', phase)
        finally:
            with lock:
                active[phase] = False
                # Release consumers when a phase aborts before reaching later jobs.
                if phase == 'prepare':
                    for signal in ready:
                        signal.set()
                else:
                    for signal in consumed:
                        signal.set()
                update_pause()

    threads = [threading.Thread(target=worker, args=(phase,), daemon=True)
               for phase in ('prepare', 'translate')]
    try:
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
    finally:
        if any(thread.is_alive() for thread in threads):
            stop.set()
            for thread in threads:
                if thread.ident is not None:
                    thread.join()
        if paused_at is not None:
            paused_seconds += time.monotonic() - paused_at
        emit(('run_time', started, datetime.now().astimezone().isoformat(timespec='seconds'),
              time.monotonic() - clock - paused_seconds))
        emit(('done',))
