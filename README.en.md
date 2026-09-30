<div align="center">
  <img src="assets/comic-sorting.png" width="120" alt="Comic Sorting">
  <h1>Comic Sorting</h1>
  <p>A Windows tool for merging manga chapters, queuing BallonsTranslator jobs, and exporting CBZ files for Komga.</p>
</div>

[繁體中文](README.md) | **English**

## Install and get started

1. Download the Windows x64 EXE from [GitHub Releases](https://github.com/Honguan/Comic-sorting/releases/latest), place it in a writable folder, and launch it.
2. Choose your manga root folder and rescan to load series and chapters.
   Scanning builds an in-memory page-count index shared with the queue. Rescanning or restarting rebuilds it; rescan after adding or removing images externally.
3. For translation, configure a separately installed BallonsTranslator and its `config/config.json` in Settings. Leave Python blank to use that installation's bundled environment or `.venv`.
4. For CBZ export, set the output folder in the Komga export tab.
5. Select chapters and an action, add them to the queue, then start.

The EXE includes this tool's dependencies; no separate Python or archive utility is needed. Install BallonsTranslator, its models, and translation services separately. The interface supports Traditional Chinese, English, and Japanese; restart after changing languages.

## Common operations

| Operation | Behavior |
|---|---|
| Merge chapters | Select chapters and a range within one series. Already merged folders are skipped. |
| Add to queue | Choose translation, export, or cleanup, then add selected items or a specific path. Double-clicking a chapter in the upper list also adds it. |
| Add by number range | Uses list start/end indices; a blank end means the last item. Adds jobs without merging or starting them. |
| Merge and queue | Adds the merged output as a pending translation job. Start it manually. |
| Start | With a selection: runs pending jobs within the selected chapters or groups. Without a selection: runs all pending jobs in queue order. |
| Reorder | Move up, down, to the top, or to the bottom. Selected jobs retain their relative order. |
| Pause / resume | Finishes the current folder and its follow-up export/cleanup, then pauses. Resume continues the same batch. |
| Stop | Interrupts current processing and keeps later pending jobs. Restarting the app does not automatically resume work. |
| Requeue | With a selection: resets failed, warning, stopped, and blocked jobs in that scope. Without a selection: resets all failed/warning jobs. A completed job can only be requeued when its individual row is selected alone. |
| Translation pages | Select one translation job and set an inclusive page range. Pages start at 1 and follow natural image-filename order. |
| Search and view | Search manga names or paths. Groups show single/merged chapter counts and translation page counts. Expand the queue for more space. |
| Names and paths | Export all manga names, or use the context menu to copy manga names or full paths. |

- **Search only filters the display.** Select results to run only those jobs; starting without a selection still runs all pending jobs.
- Starting one selected manga with multiple pending single chapters asks for a chapter range, defaulting to all; merged chapters remain included. Selecting chapter rows directly skips this question. Multiple manga instead show a draggable priority list with up/down controls. The default preserves queue order; changes apply only to this batch, and Cancel does not start it.
- Adding the same path and action again resets the existing job to pending. Requeuing does not start work automatically. Adding and reordering jobs are disabled while running or paused.
- Lists support Ctrl/Shift selection, Ctrl+A to select all, Esc to clear selection, and Ctrl+C to copy paths. Selecting a group while searching includes only visible matches.
- Click manga-list column headers to sort. Maximize the window or drag the divider to resize the upper and lower areas.

### Translation, export, and records

- Main queue → Scheduled start supports multiple one-shot schedules. Countdown accepts hours/minutes with shortcuts; Date and time has separate date/hour/minute fields and Today/Tomorrow buttons. Preview the local start time and confirm scope, chapters, and order when adding. Only unchanged, still-pending selected jobs run; busy or paused queues wait until idle. Changes to translation/cleanup/output settings require rescheduling. Keep the app open: closing cancels schedules; schedules do not wake or boot the computer.
- The **▼ menu beside the Start button** at the bottom of the main queue offers Standard, Preprocess only, Translate preprocessed chapters, and Preprocess all then translate. Preprocessing runs detection, OCR, and inpainting without rendering results, exporting, or cleaning up. Models stay loaded across chapters within each pass; the process closes between passes to release models. Selecting a mode does not start work; click the main button (Start / Preprocess / Translate / Run two passes). Selection scope is unchanged.
- Run the passes separately, including after restarting. Each chapter stores `.comic-sorting-prepared.json`; translation-only validates source images, OCR, and artifacts and skips missing or changed preprocessing. Existing data without this completion record must be preprocessed first. Completed queue jobs are not rerun automatically; requeue failed/stopped jobs. Cloud OCR or inpainting still uses the configured service during preprocessing.
- Multi-chapter translation reuses one BallonsTranslator process. Each chapter finishes result checks, export, cleanup, and saving before the next is submitted. Pausing retains models and GPU memory; stopping or finishing the batch closes the process. After a translation failure, the next chapter gets a new process. BallonsTranslator source files are unchanged; its per-request Codex connection behavior is unchanged.
- The settings editor exposes run options, current module parameters, and translation fonts. Hidden fields are preserved; saving validates, backs up, and checks for external changes.
- Settings editor → Jev abnormal character cleanup (Experimental) supports the new experimental switch (off by default), provider and TypeSafe key; OpenRouter shares the existing profile key (ID `openrouter`). Only the first full-page Codex timeout triggers cleanup and one retry with the same model; saved OCR is preserved. Keys can also be supplied through `TYPESAFE_API_KEY` / `OPENROUTER_API_KEY`. Existing Codex timeouts are retained; new profiles use native defaults. Older translators retain the OCR cleanup switch.
- Jev statistics update after each Jev cleanup call: processed/changed calls, cleared/trimmed/kept blocks, removed characters, and reported elapsed time. New Jev input tokens (K/M/B), request counts, and requests missing input usage are tracked separately, along with translator-reported cost, its reported/estimated source, and unpriced requests; these stay outside LLM totals. Legacy Jev usage records remain available. Jev time is already part of translation rescue (OCR in older versions) and is not added to batch elapsed time again. Missing historical data shows “—” without inferred backfilling.
- Newer BallonsTranslator versions offer `HTTP`, `Python SDK`, and `CLI` for Codex App Server profiles; HTTP needs no Codex executable. Independent Codex HTTP profiles expose text/OCR/inpainting capabilities, native model catalogs, reasoning levels, and image prompts. Sign in/out, refresh catalogs, and initialize profiles in the native BallonsTranslator UI first, save, then reload this editor. Existing profiles are preserved; changes apply to the next worker process. Inpainting token usage is not inferred when the translator does not report it.
- When the independent Codex catalog is empty, model and reasoning choices follow the installed version's native defaults (including `gpt-6.1-sol` in newer versions); a refreshed account catalog takes precedence. `Auto` uses the model default; `Disabled` maps to `none` and appears only for supported models. Default choices do not establish account access.
- Page ranges require BallonsTranslator support for native page selection. Completing a range does not mean the whole chapter is complete; incomplete chapter results skip automatic export and cleanup. Range jobs require a separate export opt-in.
- CBZ files use `result` images and are saved as `<Komga folder>/<series>/<chapter>.cbz`. Export checks completeness and can skip unchanged content incrementally.
- Stage displays include progress, remaining time, and average seconds per page. Usage and history use structured fields with K/M/B token counts. Costs are estimates of reported usage, **not actual bills**; missing and incomplete data are marked.
- Enable Windows and ntfy separately in Notifications. Notification failures do not stop jobs. Server acceptance does not guarantee delivery to your phone.

## Data and precautions

| Data | Location |
|---|---|
| Settings, queue, and preferences | `comic-sorting.settings.json` beside the EXE |
| History | `comic-sorting.history.sqlite3` beside the EXE |
| Logs | `logs/comic-sorting.log` beside the EXE |
| Komga incremental state | `<Komga folder>/.comic-sorting-state.json` |

- Keep settings and history when moving the app. Only one Windows instance may use the same settings file; launching again activates the existing window. Close all older versions before switching: they do not enforce this guard. Separate installations can still run independently; do not process the same manga or export directory simultaneously.
- Removing queue items keeps manga files. Deleting folders through the upper list's context menu moves them to the Windows Recycle Bin.
- **Removing merge sources and cleaning `mask`/`inpainted` permanently deletes data.** Merging can preserve the last source. Manual Komga export cleans successful chapters' working data after saving export state; queued jobs follow their cleanup option.
- Types are inferred from names: `Chapter 1` is a single chapter; `Chapter 1-30` is a merged folder. Renaming may affect classification.
- Invalid settings or state files stop related operations and remain untouched; repair them or restore a backup. Logs may contain paths and translator output, so review them before sharing.

Troubleshooting: [Error codes](ERROR_CODES.md) · [Project audit](docs/audit-2026-09-18.md) (both in Traditional Chinese)

## Development and build

Use Windows and Python 3.10 with the project's pinned dependencies:

```powershell
py -3.10 -m pip install -r requirements.txt pyinstaller==6.22.2
py -3.10 -B -m unittest discover -s tests -v
py -3.10 -m PyInstaller --clean --noconfirm "Comic sorting.spec"
```

Output: `dist/Comic sorting.exe`. GitHub Actions tests and builds `main` and pull requests; `v*` tags trigger releases.

Core files: `Comic sorting.py` (main window), `comic_core.py` (file operations), `queue_worker.py` (job execution), `translation_queue.py` (queue UI), and `queue_history.py` (history).

## License

[MIT License](LICENSE) · Copyright (c) 2026 CHEN HONG GUAN. Third-party dependencies and external tools retain their own licenses.

- **Preprocess and translate concurrently** runs two persistent processes. Translation consumes fully saved preprocessing results in queue order while preprocessing advances to the next chapter. Valid preparation is reused; failed preparation is not translated. Duplicate paths wait for the preceding translation to finish. Pause waits for both current chapters; stop cancels both workers. Saved preparation can be resumed after restart. Only the translation worker exports/cleans completed results. Keeping both processes alive requires more memory.
