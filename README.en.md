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

The Add manga and chapters panel at the top of the main queue is the primary entry point: choose a manga, enter chapter list indices starting at 1, choose translation, export, or cleanup, and click Add to queue. A blank end includes all remaining chapters; adding does not merge or start execution. With several chapters or manga selected in the library, Add to queue adds the selection and the range fields (including the merge window) are disabled; Add selected and double-clicking chapters still work. Merging is a secondary tool available from library Tools or queue Options in a separate window that preserves the selection and range when reopened.

The add form wraps in narrow panes. Options contains page ranges, follow-up processing, Add path, and chapter merging. History, Model statistics, and Progress open directly from the footer. Actions manages queue items; View provides LLM usage, Jev statistics, the full summary, and queue expansion/restoration. Common controls wrap when space is limited.

The main summary shows pending and completed items, plus attention counts when needed. View → Queue summary provides full totals, failures, and retries. The library shows manga/folder counts; selection details start with names, status, and pages, with full paths, sizes, and timestamps still available by scrolling. Unconfirmed page counts are not displayed as zero.

“Pending” means not started; “Not run” shows its reason separately. Page counts use pages and “Counting” while loading; remaining time uses “Estimating”. Incomplete usage and unknown costs have separate labels.

Larger default text and fewer visible columns keep names, pages, and status readable. Selecting a row still shows its full path and metadata. Stage progress and queue LLM usage open in separate windows, while the main view keeps the list and elapsed time. Queue expansion/restoration preserves velocity: rapid reversal decelerates before changing direction, resizing during animation retains the original proportion, and dragging the divider takes control immediately. Columns and text wrapping update after the animation settles to reduce repeated layout jumps. The library creates chapter rows on expansion and reuses directory entries for page counts and translation status during scanning, avoiding per-page path construction and showing progress counts and elapsed time.

The interface uses at least 12-point text and darker secondary text. Queue columns prioritize manga/chapter names and status, adding pages, action, and error reasons when space allows. Completed, warning, and failed rows use distinct text colors. A single library shows its root folder name; multiple sources retain their full paths. Selection details use two fixed-height lines, putting names, status, and errors before full paths and summaries. Scroll to see all details, use Ctrl+A to select all, and Ctrl+C to copy. Zero failure/retry counts are omitted when neither has occurred. The manga picker and chapter-range fields remain available when the queue is expanded.

Model statistics show scope, model, tokens, requests, and estimated cost by default. Select All fields for shares, per-request averages, input, output, cache, and completeness details. Positive costs below US$0.01 use six decimal places without changing stored amounts. Stage progress, accumulated time, and seconds per page appear on separate lines. ETA uses HH:MM:SS, retaining hours beyond 24; failed or stopped stages show their status instead.

| Operation | Behavior |
|---|---|
| Merge chapters | Secondary tool: open the merge window from Tools or Options and use chapters or a range in one series. Already merged folders are skipped. |
| Add to queue | Primary action: one manga uses the chapter range; several selected chapters or manga are added as selected. Add selected and adding a specific path are also available. |
| Chapter range | Use list indices starting at 1; a blank end includes the remaining chapters. Existing entries with the same path and action are skipped. |
| Merge and queue | Adds the merged output as a pending translation job. Start it manually. |
| Start | With a selection: runs pending jobs within the selected chapters or groups. Without a selection: runs all pending jobs in queue order. |
| Reorder | Move up, down, to the top, or to the bottom. Selected jobs retain their relative order. |
| Pause / resume | Finishes the current folder and its follow-up export/cleanup, then pauses. Resume continues the same batch. |
| Stop | Interrupts current processing and keeps later pending jobs. Restarting the app does not automatically resume work. |
| More actions → Retry | With a selection: resets failed, warning, stopped, and blocked jobs in that scope. Without a selection: resets all failed/warning jobs. A completed job can only be requeued when its individual row is selected alone. |
| Options → Translation pages | Select one translation job and set an inclusive page range. Pages start at 1 and follow natural image-filename order. |
| Search and view | Search manga names or paths. Groups show single/merged chapter counts and translation page counts. Expand the queue for more space. |
| Keyboard and feedback | Ctrl+F from a list or search box selects its search text. Enter applies the filter; Esc clears it and returns to the list. Enter in a chapter-range field enqueues it. Enqueueing reveals the item and preserves the existing selection. |
| Names and paths | Export all manga names, or use the context menu to copy manga names or full paths. |

- **Search only filters the display.** Select results to run only those jobs; starting without a selection still runs all pending jobs.
- Start runs the selected scope directly, or all pending jobs when nothing is selected. For a temporary chapter range or manga order, use Actions → Adjust range and order…; changes affect only that batch, and Cancel does not start it.
- Adding the same path and action again resets the existing job to pending. Requeuing does not start work automatically. Adding and reordering jobs are disabled while running or paused.
- Lists support Ctrl/Shift selection, Ctrl+A to select all, Esc to clear selection, and Ctrl+C to copy paths. Selecting a group while searching includes only visible matches.
- Click the manga name header to sort by name, or use Tools → Sort for updated time, name, size, or chapter count. Selecting the same field again reverses its direction. Drag the divider to resize the left and right areas.

### Translation, export, and records

- Main queue → More actions → Scheduled start supports multiple one-shot schedules. Countdown accepts hours/minutes with shortcuts; Date and time has separate date/hour/minute fields and Today/Tomorrow buttons. Preview the local start time and confirm scope, chapters, and order when adding. Only unchanged, still-pending selected jobs run; busy or paused queues wait until idle. Changes to translation/cleanup/output settings require rescheduling. Keep the app open: closing cancels schedules; schedules do not wake or boot the computer.
The **Workflow selector beside Start** shows the current mode. Choose one of five workflows, then click Start; selection scope is unchanged.

| Workflow | Processing |
| --- | --- |
| Standard | Completes each chapter using the configured native pipeline. |
| Preprocess only | Saves detection, OCR, and inpainting checkpoints without rendering, exporting, or cleanup. |
| Translate preprocessed chapters | Validates sources and artifacts before translation; missing or changed preparation is skipped. |
| Preprocess all then translate | Completes preparation, closes the process to release models, then translates. |
| Preprocess and translate concurrently | Two persistent workers overlap preparation of the next chapter with translation of a saved chapter. |

- Run the passes separately, including after restarting. Each chapter stores `.comic-sorting-prepared.json`; translation-only validates source images, OCR, and artifacts and skips missing or changed preprocessing. Existing data without this completion record must be preprocessed first. Completed queue jobs are not rerun automatically; requeue failed/stopped jobs. Cloud OCR or inpainting still uses the configured service during preprocessing.
- Multi-chapter translation reuses one BallonsTranslator process. Each chapter finishes result checks, export, cleanup, and saving before the next is submitted. Pausing retains models and GPU memory; stopping or finishing the batch closes the process. After a translation failure, the next chapter gets a new process. BallonsTranslator source files are unchanged; its per-request Codex connection behavior is unchanged.
- **Preprocess and translate concurrently** runs two persistent processes. Translation consumes fully saved preprocessing results in queue order while preprocessing advances to the next chapter. Valid preparation is reused; failed preparation is not translated. Duplicate paths wait for the preceding translation to finish. Pause waits for both current chapters; stop cancels both workers. Saved preparation can be resumed after restart. Only the translation worker exports/cleans completed results. Keeping both processes alive requires more memory.
- Use Open native settings for modules, models, concurrency, prompts, fonts, and sign-in. Save and close BallonsTranslator before starting the queue. This tool no longer edits its configuration file.
- Jev statistics remain supported when the installed version reports them. Without Jev or reported data, fields stay empty or show “—” without affecting processing or inferring usage. Configure Jev and its keys in that version's native interface.
- Jev statistics update after each Jev cleanup call: processed/changed calls, cleared/trimmed/kept blocks, removed characters, and reported elapsed time. New Jev input tokens (K/M/B), request counts, and requests missing input usage are tracked separately, along with translator-reported cost, its reported/estimated source, and unpriced requests; these stay outside LLM totals. Legacy Jev usage records remain available. Jev time is already part of translation rescue (OCR in older versions) and is not added to batch elapsed time again. Missing historical data shows “—” without inferred backfilling.
- Queue jobs use the installed native pipeline, including concurrent Codex OCR, inpainting, and translation pages in `D:\AI\Ballonstranslator_win_minium`. Native modules own concurrency, RPM/delay, and model catalogs. Chapter-level preprocessing/translation overlap can coexist with native page concurrency.
- Tokens follow official per-request logs: use `total`, or input plus output only when both are reported; add each OCR `Used` value, not its persistent `Total`. Cached and reasoning tokens are subsets and are not added again. Count each reported retry and memory-compaction response once. Legacy run summaries replace request snapshots; Jev remains separate.
- All models share the pricing path. The 14 native OpenAI models have official Standard rates verified on `2026-10-01`; other models automatically use API-equivalent rates from the [public OpenRouter catalog](https://openrouter.ai/api/v1/models), including latest aliases, long-context, and peak/off-peak overrides. Settings → Model token rates accepts any model ID with custom USD-per-million-token rates, which take precedence; blank cache rates use the input rate. Local Ollama/LM Studio API fees are zero, excluding hardware and electricity.
- OCR, translation, and memory-compaction requests record the requested model and returned input/output/cache usage, including output-limited responses. Failed requests without usage retain their model and missing-usage flag. Bridge records replace native request logs to prevent duplicate counting. Mixed-model snapshots retain every model name; live usage and history details display them even when pricing is unavailable. Snapshot IDs are never silently renamed or assigned another version's price.
- Estimates are not actual bills and exclude unreported inpainting usage, tool fees, and unreported extra charges. Cache/reasoning subsets are not added twice; missing cache counts are assumed zero. History preserves model identities, pricing basis/source/date, and applied rates. Use custom prices if the catalog is offline or a model is unlisted; absent usage remains incomplete and old history is not guessed. Native reported amounts take precedence; unpriced summaries retain local estimates only when request, token, and missing-usage counts match.
- Page ranges require BallonsTranslator support for native page selection. Completing a range does not mean the whole chapter is complete; incomplete chapter results skip automatic export and cleanup. Range jobs require a separate export opt-in.
- CBZ files use `result` images and are saved as `<Komga folder>/<series>/<chapter>.cbz`. Export checks completeness and can skip unchanged content incrementally.
- Stage displays include progress, remaining time, and average seconds per page. Usage and history use structured fields with K/M/B token counts. Costs are estimates of reported usage, **not actual bills**; missing and incomplete data are marked.
- Queue Statistics → Model statistics and the historical Model statistics tab show per-model OCR/translation/total tokens, requests, estimated costs, input/output/cache breakdowns, and unpriced/missing-usage request counts. The live queue and selected historical batch/chapter share the same columns. Costs display six decimal places while storage preserves the original precision. Totals already include OCR and translation; cache tokens are input subsets. Legacy usage can be attributed when exactly one model is known; mixed-model records without breakdowns remain unallocated.
- Model statistics include token/request/known-cost shares and average tokens/cost per request. Each OCR, translation, or total scope has its own denominator across all models, including unallocated usage. Shares use original values before rounding to two decimal places; zero denominators show an em dash. Average tokens exclude requests with missing usage, and average cost is shown only for complete costs so unknown charges are not treated as zero.
- History separates Run records, Selected record details, and All-history totals. Detailed totals start collapsed; click Detailed statistics to expand. History displays 200 batches per page; use Previous/Next for other records. Expand batches and chapters to load their children. Refresh preserves expanded rows and selection on the current page; changing filters returns to the first page. Grand totals include all history and reported usage from failed/stopped jobs, retaining aggregated summaries rather than every usage record.
- Completed counts include successful jobs and completed jobs with warnings; pending, failed, stopped, and prepared-only jobs are excluded. A known cost subtotal stays marked as partial whenever any contributing record lacks a cost. Chapter and stage timings can overlap and are not added to batch wall time again.
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
- **Completed preprocessing does not authorize cleanup.** Manual Komga export retains `mask`/`inpainted`; use Clean exported caches when needed. Automatic queue cleanup requires CBZ export to be enabled. Every cleanup entry point requires complete translation, a matching export record, unchanged results and CBZ metadata, and a successful archive-content and CRC check. Source or preprocessing files newer than the archive are also retained. Re-export chapters with missing legacy verification records before cleanup.
- **Removing merge sources and cleaning `mask`/`inpainted` permanently deletes data.** Source removal is off by default and can retain the last chapter. Cleanup still requires confirmation.
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

Core files: `Comic sorting.py` (main window and events), `comic_core.py` (scanning, merging, and file operations), `queue_worker.py` (execution), `translation_queue.py` (queue UI), `queue_stats.py` (snapshot merging and incremental totals), and `queue_history.py` (history queries and pagination). All five workflows use the existing executor; settings and history formats remain unchanged.

Run local measurements with `py -3.10 -X utf8 benchmarks/benchmark_refactor.py`. It uses synthetic data and does not translate. The [refactoring and validation report](docs/refactor-2026-10-01.md) records feature coverage, measurements, and limitations in Traditional Chinese.

## License

[MIT License](LICENSE) · Copyright (c) 2026 CHEN HONG GUAN. Third-party dependencies and external tools retain their own licenses.
