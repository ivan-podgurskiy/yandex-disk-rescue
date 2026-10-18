# Local Recovery UI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prepare a local web application that can reliably rescue the user's approximately 731 GB Yandex.Disk onto an external 2 TB disk, with development verified without personal data or that disk.

**Architecture:** A Python package owns catalog snapshots, SQLite job state, destination checks, and bounded transfer workers. A loopback HTTP service exposes commands and status to a static browser UI. Keep the existing CLI command syntax and comparison tools; share hardened transfer primitives where applicable without rewriting unrelated commands.

**Tech Stack:** Python 3.8+ standard library, SQLite, unittest, HTML/CSS/JavaScript. No runtime package installation or CDN. Browser verification uses available browser tools.

**Spec:** `docs/superpowers/specs/2026-10-18-local-recovery-ui-design.md`

## Global Constraints

- First release: personal use on macOS; Windows verification and desktop packaging remain roadmap items.
- Minimum free-space reserve: 1 GiB (`1073741824` bytes).
- Original and preview states are separate; previews never count as recovered originals.
- No cloud modification/deletion, no telemetry, no token or signed URL in SQLite, logs, or HTTP responses.
- Entered token lives only in process memory; preserve environment/file token sources without changing CLI arguments.
- All development recovery uses generated data and temporary directories. Real disk/API validation remains explicitly unverified until available.
- Preserve MIT license and original copyright. Do not push or create releases as part of implementation.
- Full-file restart is acceptable in v1; partial-file resume, full demo mode, desktop, Windows release packaging are deferred.

## Review Focus

- Zero-byte originals: recovered correctly without being mistaken for missing or failed transfers (Task 4).
- Catalog changes during pagination: retain incomplete observations and never mark a failed snapshot complete or delete older entries (Task 2).
- Two processes on one database/destination: reject the second recovery service before it can create duplicate writers (Task 5).
- Untrusted filenames/CSV formulas: render names as text and make exported spreadsheets safe without changing stored paths (Tasks 6–7).
- Destination replaced at the same mount path, including symlink replacement: pause rather than write to a new device or outside the root (Tasks 3–4).

## File map and ownership

- Keep `yd_rescue.py`: CLI entry point and existing scan/compare/watch behavior; delegate download safety to tested package primitives in Task 5.
- Create `rescue/__init__.py`, `rescue/models.py`: shared immutable records and state constants.
- Create `rescue/store.py`: schema, snapshots, observations, jobs, stable local mappings, atomic transitions.
- Create `rescue/api.py`: HTTPS client, pagination, error classification, streaming responses.
- Create `rescue/destination.py`: volume identity, capacity reservation, safe directory/file descriptors and mapped paths.
- Create `rescue/transfer.py`: verified downloads and preview copies; no scheduling policy.
- Create `rescue/engine.py`: bounded workers, retries, pause/resume, catalog operation, status aggregation.
- Create `rescue/server.py`, `rescue/__main__.py`: local HTTP application, process lock, launcher.
- Create `rescue/web/index.html`, `rescue/web/app.js`, `rescue/web/styles.css`: Russian-language UI.
- Create `tests/test_store.py`, `test_api.py`, `test_destination.py`, `test_transfer.py`, `test_engine.py`, `test_server.py`, `test_integration.py`, `tests/fakes.py`: behavioral tests and generated API/disk fixtures. Keep `tests/test_rescue.py` as the regression suite.
- Modify `.gitignore`, `README.md`, `SECURITY.md`: local database exclusion and operating instructions.

### Task 1: Durable catalog and job records

**Files:** Create package init, models, store, `tests/test_store.py`; modify `.gitignore` to exclude `*.sqlite*` and application state.

**Interfaces:** `CloudFile(path: str, size: int, md5: str, media_type: str, resource_id: str)`; `Store(db_path: str)` with `begin_snapshot() -> int`, `record_page(snapshot_id: int, files: Sequence[CloudFile], next_offset: int) -> None`, `finish_snapshot(snapshot_id: int) -> None`, `enqueue(snapshot_id: int, previews: bool) -> None`, `claim_job(now: float) -> Optional[Job]`, `complete_job(job_id: int, result: TransferResult) -> None`, `recover_interrupted() -> None`. `Job` exposes id, file, kind, local_rel, attempts; `TransferResult` exposes status, written_bytes, verification, error_code, retry_at. Store operations use independent connections/transactions, never a shared unlocked SQLite connection across workers.

- [ ] Write tests `test_reopen_retains_jobs`, `test_changed_content_is_new_version`, `test_partial_snapshot_keeps_previous_files`, `test_original_and_preview_results_are_independent`, `test_claim_is_atomic` using temporary databases; assert exactly one claim per job and old results survive later scans.
- [ ] Run `python3 -m unittest discover -s tests -p test_store.py -v`; require failures for missing behavior.
- [ ] Implement schema version 1: snapshots and page cursor; file versions identified from path/size/MD5 and available resource metadata; snapshot membership; jobs; unique local mappings. Preserve original paths exactly. Completed jobs distinguish `verified_md5`, `verified_size`, `preview_saved`, and `preview_absent`. Separate attempts/errors from content state.
- [ ] Run store tests and `python3 -m unittest discover -s tests`; require all pass.
- [ ] Commit as `feat: persist recovery catalog and job state`.

### Task 2: API client and resumable catalog acquisition

**Files:** Create `rescue/api.py`, `tests/test_api.py`, `tests/fakes.py`.

**Interfaces:** `YandexClient(token: str, timeout: float = 12)` exposes `check_access() -> None`, `list_page(offset: int, limit: int = 1000) -> Sequence[CloudFile]`, `original_url(file: CloudFile) -> str`, `preview_url(file: CloudFile) -> Optional[str]`, `open_stream(url: str, authenticated: bool) -> BinaryIO`. `ApiError` exposes only safe `code`, `retry_after: Optional[float]`, and `requires_auth: bool`; never raw response bodies or request URLs. Catalog controller in Task 5 owns SQLite cursor and completion.

- [ ] Write tests `test_reject_http_and_foreign_redirect`, `test_auth_only_on_allowed_https_hosts`, `test_signed_download_has_no_auth`, `test_401_pauses_auth`, `test_429_parses_retry_after`, `test_invalid_page_does_not_mean_end`, `test_error_contains_no_token_or_signed_url`; stub transport without live network. Assert malformed JSON/items raises a safe error, not an empty successful page. Test both seconds and HTTP-date Retry-After.
- [ ] Run `python3 -m unittest discover -s tests -p test_api.py -v`; confirm failure.
- [ ] Implement explicit HTTPS allowlist checks on initial requests and redirects, bounded socket timeouts and safe error categories: auth, throttled, unavailable, timeout, invalid_response. Validate size as a nonnegative integer and hash syntax when present. Missing preview URL yields `None`, distinct from request failure.
- [ ] Run API and regression tests; require pass.
- [ ] Commit as `feat: add safe Yandex API client`.

### Task 3: Destination identity, capacity and collision-safe paths

**Files:** Create `rescue/destination.py`, `tests/test_destination.py`; extend Store mapping accessors.

**Interfaces:** `Destination.bind(root: str) -> Destination`; `identity` is serializable volume UUID, mount point and device id; `check() -> None`, `reserve(job_id: int, size: int) -> None`, `release(job_id: int) -> None`, `map_path(file: CloudFile, kind: str, store: Store) -> str`, `open_attempt(job: Job) -> Attempt`. `Attempt` exposes a binary writer, `check()`, `commit()`, `discard()`; root traversal and rename use held directory descriptors on macOS with no-follow operations. `DestinationError.code` distinguishes missing_volume, wrong_volume, unsafe_path, no_space. Inject identity/capacity providers for tests.

- [ ] Write tests `test_missing_volume_never_creates_root`, `test_replaced_mount_is_rejected`, `test_unicode_case_and_reserved_collisions_stable_on_reopen`, `test_symlink_swap_cannot_escape`, `test_reserved_capacity_keeps_one_gib`, `test_two_jobs_cannot_reserve_same_remaining_bytes`. Assert no file created outside temporary destination; capacity decisions use exact reserve `1073741824`.
- [ ] Run `python3 -m unittest discover -s tests -p test_destination.py -v`; confirm failure.
- [ ] Implement macOS volume lookup via `diskutil info -plist` with argument arrays and plistlib, verify current mount/device against captured identity, and fail closed if identification fails. Test targets use injected providers, not a production bypass. Only bind an existing destination; do not recreate a missing mount. Serialize reservations. Normalize comparison keys using Unicode NFC and casefold; persist a stable suffix derived from file version/kind when needed. Enforce component byte limits, separate originals/previews, and reject traversal/symlinks.
- [ ] Run destination tests and regression suite; require pass.
- [ ] Commit as `feat: guard external destination and local paths`.

### Task 4: Verified atomic transfer

**Files:** Create `rescue/transfer.py`, `tests/test_transfer.py`.

**Interfaces:** `transfer(job: Job, client: YandexClient, destination: Destination, cancel: threading.Event, on_bytes: Callable[[int], None]) -> TransferResult`; use Store-assigned mappings. Add `verify_existing(path: str, file: CloudFile) -> str` returning verified_md5, verified_size, missing, or conflict via safe destination file access, not unrestricted symlink-following opens. TransferResult statuses and fields match Task 1.

- [ ] Write tests `test_same_size_bad_hash_is_not_success`, `test_existing_conflict_is_preserved`, `test_zero_byte_original`, `test_interrupted_stream_not_published`, `test_absent_preview_is_not_saved`, `test_cancel_stops_before_commit`, `test_mount_swap_during_stream_pauses`, `test_no_space_during_chunk_pauses`; fake streams generate bytes on demand.
- [ ] Run `python3 -m unittest discover -s tests -p test_transfer.py -v`; confirm failure.
- [ ] Implement streaming in 1 MiB chunks; check cancellation and destination between chunks, validate expected length and MD5 before atomic publish, reject excess bytes. Assign each attempt a unique temporary name. Preserve any unrelated local file; obtain and persist a new mapping for conflicts before publishing. No destructive overwrite or full-file memory reads. Previews require a nonempty image response and a saved status separate from original verification. Release reservations in all outcomes.
- [ ] Run transfer and regression tests; require pass.
- [ ] Commit as `feat: verify and atomically save recovered files`.

### Task 5: Recovery engine, retries and CLI integration

**Files:** Create `rescue/engine.py`, `tests/test_engine.py`; modify `yd_rescue.py` download/previews and add regression coverage in `tests/test_rescue.py`.

**Interfaces:** `Engine(store: Store, client: YandexClient, destination: Optional[Destination], workers: int = 4)` exposes `scan_catalog()`, `set_destination(destination: Destination)`, `start(previews: bool = True)`, `pause()`, `resume()`, `status() -> dict`, `close()`. Catalog operations run outside request threads and checkpoint each page. `status()` includes counts by result/kind, verified original bytes, transferred attempt bytes, recent speed, active jobs, snapshot completeness, safe error codes, pause reason and free space. Constructor accepts clock/random/worker factory injection for tests.

- [ ] Write tests `test_restart_requeues_only_unfinished_jobs`, `test_stuck_worker_never_gets_duplicate_writer`, `test_pause_ack_waits_for_writer_exit`, `test_429_delays_all_network_work`, `test_auth_failure_pauses_network`, `test_partial_catalog_can_be_queued`, `test_failed_scan_retains_cursor`, `test_previews_off_keeps_originals`, `test_volume_resume_requires_confirmation`. Add CLI regression: known MD5 rejects same-size wrong content, sanitized-name collisions preserve both files.
- [ ] Run engine and CLI tests; confirm newly added assertions fail before changes.
- [ ] Implement bounded workers without replacing a still-live writer. Cancellation is cooperative; unresponsive jobs remain active and block their resource until completion rather than reporting safe pause. Recover persisted in-progress jobs only after exclusive application ownership. Use retry delay `min(300, 5 * 2**min(attempts - 1, 6))` seconds with 0–20% positive jitter and respect longer Retry-After; throttle globally when asked. Keep permanently unavailable items deferred, never mark lost. Expose worker/socket timeouts distinctly from failure.
- [ ] Refactor CLI download/previews to share safe verification, mapping and transfer primitives while retaining flags, CSV pending outputs and existing compare/scan/watch behavior. Use per-command adapters and destination-root lock; do not route all CLI commands through the new web server or change old watchdog tests for the legacy standalone helper. Add comments documenting that helper is not used for actual transfer retries.
- [ ] Run `python3 -m unittest discover -s tests -v`; require all pass, including the original 13 tests.
- [ ] Commit as `feat: add persistent recovery scheduling and safe pause`.

### Task 6: Loopback HTTP service and secure launcher

**Files:** Create `rescue/server.py`, `rescue/__main__.py`, `tests/test_server.py`.

**Interfaces:** `python3 -m rescue --host 127.0.0.1 --port 8765 --data-dir PATH` (non-loopback host rejected). `create_server(engine: Engine, data_dir: str, port: int) -> HTTPServer`. API routes: `GET /api/status`, `GET /api/files?offset=0&limit=100&query=&state=`, `GET /api/export.csv`; `POST /api/connect` with optional token/source, `/api/catalog`, `/api/destination` with path, `/api/start` with previews, `/api/pause`, `/api/resume` with confirm_destination. Async commands return 202 and an operation status visible through status; invalid state 409, invalid input 400, security failure 403. No filesystem enumeration route.

- [ ] Write tests `test_second_process_rejected`, `test_foreign_host_and_origin_rejected`, `test_mutation_requires_session_secret`, `test_token_not_in_any_response_or_log`, `test_request_size_limit`, `test_catalog_command_returns_before_scan`, `test_pagination_is_bounded`, `test_csv_formula_is_escaped`. Assert request bodies larger than 16 KiB are rejected and file page limit is at most 200.
- [ ] Run `python3 -m unittest discover -s tests -p test_server.py -v`; confirm failure.
- [ ] Implement a per-data-directory POSIX file lock and destination ownership lock acquired before work; fail clearly on conflicts. Local HTML bootstrap receives a random session secret kept in browser memory; require it and same-origin checks for JSON mutations. Read APIs and CSV also require session secret via header. Require JSON content type for POST, reject malformed lengths, strip request-body logging, set no-store/CSP/nosniff/frame protections, and serve only enumerated static assets. Persist data under `~/Library/Application Support/YandexDiskRescue` with directory mode 0700 and private state files. No secret in launcher URL; bootstrap available only to checked loopback Host requests.
- [ ] Export original/preview state, raw path as CSV text escaped with leading apostrophe when beginning with formula-triggering characters; retain unmodified raw paths in database. Client downloads export with authenticated fetch and blob, not token-bearing query parameters.
- [ ] Run server and regression suite; require pass.
- [ ] Commit as `feat: expose secure local recovery API`.

### Task 7: Russian-language working UI

**Files:** Create three static assets under `rescue/web/`; extend server asset allowlist; add UI-related integration assertions.

**Interfaces:** Use Task 6 endpoints, poll status every 2 seconds while active and stop overlapping requests. No browser token persistence. UI sections: connection, catalog, destination, recovery summary, file results. Status errors use Russian explanations and actionable controls.

- [ ] Define acceptance fixtures for disconnected account, partial scan, absent destination, active download, pausing, wrong disk, no space, auth failure and completed-with-deferred-files. Add server/fixture integration tests asserting matching status fields without exposing secrets.
- [ ] Implement semantic HTML with labelled inputs, keyboard controls, progress indicators and separate original/preview counters. Default action queues the whole known catalog; filters allow viewing/selecting a subset by explicit file ids bound to a snapshot, with server validation. If subset support requires API additions, add `file_ids: Optional[list[int]]` to `/api/start`, Store.enqueue and Engine.start with tests rejecting ids outside that snapshot; do not silently expand selection.
- [ ] Implement file search/pagination and CSV export, text-only rendering via textContent, network failure banner and controls disabled during incompatible operations. Destination is an explicit absolute path entry in v1; native picker is desktop-roadmap work. Show that closing the browser does not stop the process. Never claim safe disk ejection until active writers are zero.
- [ ] Run full unit suite, launch with generated fixture engine and temporary database/destination through a test-only harness, then inspect in available browser tools: connect, scan, start, pause, resume, filter and export. Verify a filename containing HTML renders literally. Capture UI screenshot artifact for user review; do not connect a real account.
- [ ] Commit as `feat: add local recovery web interface`.

### Task 8: End-to-end recovery proof and operating documentation

**Files:** Create `tests/test_integration.py`; update `tests/fakes.py`, `README.md`, `SECURITY.md`.

**Interfaces:** Test-only fake transport supplies deterministic content, HTTP failures, and injected volume/capacity providers. Real server API and Engine/Store/transfer integration run against this transport; no production fake-mode flag or real disk mutation required.

- [ ] Write `test_recovery_survives_restart_and_preserves_conflicts`: generated original, zero-byte file, missing preview, flaky file, conflicting names and pre-existing bad same-size copy; assert verified content and MD5, separate previews, original local conflict unchanged, retry persisted across restart. Write `test_disk_loss_creates_nothing_at_replacement_path` and `test_incomplete_scan_then_rescan_preserves_old_observations`.
- [ ] Run `python3 -m unittest discover -s tests -p test_integration.py -v`; confirm missing end-to-end behavior fails, implement only necessary integration fixes, rerun to pass.
- [ ] Document launch/stop, token sources, disk path, confirmation after remount, partial-catalog meaning, original verification levels, interrupted-file restart, CSV privacy, no deletion, and roadmap. Mark Windows and real 731 GB recovery as not yet validated. Explain laptop sleep stops progress and process must stay running. Retain MIT notices.
- [ ] Run `python3 -m unittest discover -s tests -v`, `python3 -m compileall -q rescue yd_rescue.py`, `python3 yd_rescue.py --help`, `python3 -m rescue --help`, and `git diff --check`; require exit code 0. Review changed code for token exposure, symlink handling, concurrent writers and capacity checks. Repeat browser smoke only if UI/integration fixes affect it.
- [ ] Commit as `test: verify recovery lifecycle and document local operation`.

## Delivery and remaining validation

Deliver local commits, launch command, screenshot and exact test results. Keep real token out of chat and artifacts. When the disk arrives, validate its detected identity and free space, perform a small user-authorized real recovery, verify results, then start the full catalog. This later hardware/network check is not replaced by fake tests. Reviewers should focus especially on filesystem race boundaries and whether the public UI accurately distinguishes saved previews from verified originals.
