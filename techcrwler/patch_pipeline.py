"""
Patches run_batch_pipeline in main.py to run the background poller concurrently
with the generation loop, so polling fires at 45-min regardless of generation phase.
"""

NEW_FUNCTION = '''async def run_batch_pipeline(
    session: aiohttp.ClientSession,
    httpx_client: httpx.AsyncClient,
    rows: List[List[str]],
    category_heuristics: Dict[str, List[Dict]],
    tracxn_sem: asyncio.Semaphore,
    local_sem: asyncio.Semaphore,
    start_row: int = 2
):
    total_rows = len(rows) - 1
    if total_rows <= 0:
        logger.info("No domain rows to process.")
        return

    domain_sem = asyncio.Semaphore(CONFIG["DOMAIN_CONCURRENCY"])
    next_row_idx = max(2, start_row)
    batch_num = 1
    processed_count = 0
    success_count = 0
    fail_count = 0

    update_progress_file(processed_count, total_rows, success_count, fail_count)

    while next_row_idx <= len(rows):
        if os.path.exists(STOP_FILE):
            logger.info("Stop requested (.stop_requested found). Terminating pipeline loop.")
            break

        batch_tasks: Dict[str, DomainTask] = {}
        gen_start_time = time.time()
        gen_window_sec = CONFIG["GENERATE_WINDOW_MINUTES"] * 60

        logger.info("\\n=======================================================")
        logger.info(f"BATCH #{batch_num}: Starting concurrent Generation + Polling...")
        logger.info(f"Starting at sheet row {next_row_idx} of {len(rows)}")
        logger.info("=======================================================\\n")

        active_gen_tasks = set()

        # Signal set once generation finishes so the poller knows to wrap up
        generation_done_ev = asyncio.Event()

        # Capture batch_num for async closures
        _bn = batch_num
        _sweep_box = [0]

        async def _sweep_one_pass():
            nonlocal success_count, fail_count
            sweep_sem = asyncio.Semaphore(50)

            async def _sweep_domain(task):
                nonlocal success_count, fail_count
                async with sweep_sem:
                    was_processing = task_needs_polling(task)
                    await check_single_task_jobs(session, task)
                    still_processing = task_needs_polling(task)

                    # Always push sheet update after poll (updates poll-count column)
                    await sheet_update_queue.put(task)

                    if was_processing and not still_processing:
                        if task.home_status == "SUCCESS" or any(
                            s == "SUCCESS" for s in task.category_statuses.values()
                        ):
                            success_count += 1
                        else:
                            fail_count += 1
                        update_progress_file(processed_count, total_rows, success_count, fail_count)

                        home_about_text = (
                            (task.home_content or "") + "\\n\\n" +
                            (task.category_contents.get("About") or "")
                        )
                        rest_parts = [
                            task.category_contents[c]
                            for c in OTHER_PAGES if task.category_contents.get(c)
                        ]
                        rest_text = "\\n\\n".join(rest_parts) if rest_parts else None
                        pages_val = "home,about," + ",".join(
                            [c.lower() for c in OTHER_PAGES if task.category_contents.get(c)]
                        )
                        await save_to_supabase(task.domain, home_about_text.strip(), rest_text, pages_val)
                        logger.info(
                            f"REAL-TIME PUSH: Streamed final update for {task.domain} to Google Sheet queue."
                        )

            ready = [t for t in batch_tasks.values() if is_task_ready_to_poll(t)]
            if ready:
                _sweep_box[0] += 1
                pending_count = len([t for t in batch_tasks.values() if task_needs_polling(t)])
                logger.info(
                    f"BATCH #{_bn} Sweep #{_sweep_box[0]}: "
                    f"{len(ready)} domains ready to poll "
                    f"({pending_count - len(ready)} still waiting for scheduled poll time)..."
                )
                await asyncio.gather(*[_sweep_domain(t) for t in ready])

        async def _background_poller():
            """
            Runs CONCURRENTLY with the generation loop.
            Wakes every 30 s, polls any task whose next_poll_time has arrived.
            Exits only after generation is done AND all tasks have exhausted their polls.
            """
            logger.info(
                f"BATCH #{_bn}: Background poller started "
                f"(wakes every 30 s; fires at 45 min / 105 min from job creation)."
            )
            while True:
                if os.path.exists(STOP_FILE):
                    break

                await _sweep_one_pass()

                still_pending = [t for t in batch_tasks.values() if task_needs_polling(t)]

                # Done: generation finished and all tasks resolved
                if generation_done_ev.is_set() and not still_pending:
                    logger.info(f"BATCH #{_bn}: All domain jobs reached terminal status!")
                    break

                # Done: generation finished and no more polls possible
                if generation_done_ev.is_set() and still_pending:
                    due_times = [
                        t.next_poll_time for t in still_pending if t.poll_count < MAX_POLLS_PER_JOB
                    ]
                    if not due_times:
                        break
                    remaining = min(due_times) - time.time()
                    if remaining > 30:
                        logger.info(
                            f"BATCH #{_bn}: Next poll due in {round(remaining / 60, 1)} min. "
                            "Sleeping 30 s then rechecking..."
                        )

                await asyncio.sleep(30)

            logger.info(f"BATCH #{_bn}: Background poller finished.")

        # Launch the background poller concurrently with generation
        poller_task = asyncio.create_task(_background_poller())

        # Generation loop (runs at the same time as the poller)
        while next_row_idx <= len(rows):
            if os.path.exists(STOP_FILE):
                break

            elapsed_gen = time.time() - gen_start_time
            if elapsed_gen >= gen_window_sec:
                logger.info(
                    f"Generation window limit ({CONFIG[\'GENERATE_WINDOW_MINUTES\']} mins) "
                    f"reached for Batch #{batch_num}."
                )
                break

            row = rows[next_row_idx - 1]
            idx = next_row_idx
            next_row_idx += 1

            domain_name = row[0].strip() if row else ""
            if not domain_name:
                continue

            async def _process_and_register(r=row, i=idx):
                nonlocal processed_count
                task = await process_single_domain_producer(
                    session, httpx_client, domain_sem, tracxn_sem, local_sem,
                    category_heuristics, r, i
                )
                processed_count += 1
                update_progress_file(processed_count, total_rows, success_count, fail_count)

                if task and task_needs_polling(task):
                    batch_tasks[task.domain] = task

            coro = asyncio.create_task(_process_and_register())
            active_gen_tasks.add(coro)
            coro.add_done_callback(active_gen_tasks.discard)

            if len(active_gen_tasks) >= CONFIG["DOMAIN_CONCURRENCY"] * 2:
                await asyncio.sleep(0.1)

        if active_gen_tasks:
            await asyncio.gather(*active_gen_tasks)

        logger.info(
            f"BATCH #{batch_num}: Generation Phase complete! "
            f"Generated/queued {len(batch_tasks)} active domains."
        )

        # Signal the poller that no new tasks will arrive
        generation_done_ev.set()

        if not batch_tasks:
            poller_task.cancel()
            try:
                await poller_task
            except asyncio.CancelledError:
                pass
            if next_row_idx > len(rows):
                logger.info("No more domains remaining to process.")
                break
            continue

        logger.info("\\n-------------------------------------------------------")
        logger.info(
            f"BATCH #{batch_num}: Generation done. "
            f"Poller tracking {len(batch_tasks)} domains until all resolved..."
        )
        logger.info("-------------------------------------------------------\\n")

        # Wait for the background poller to finish all remaining polls
        await poller_task

        logger.info(f"BATCH #{batch_num} complete! Moving to next batch...\\n")
        batch_num += 1

    await sheet_update_queue.put(None)
'''

with open('/home/tracxn-lp-477/Desktop/publishing-sr/techcrwler/main.py', 'r') as f:
    lines = f.readlines()

# Find start/end line numbers (0-indexed)
start_idx = None
end_idx = None
for i, line in enumerate(lines):
    if line.strip() == 'async def run_batch_pipeline(':
        start_idx = i
    if start_idx is not None and i > start_idx and line.strip() == 'await sheet_update_queue.put(None)':
        end_idx = i
        break

if start_idx is None or end_idx is None:
    print(f"ERROR: Could not find boundaries: start={start_idx}, end={end_idx}")
else:
    print(f"Replacing lines {start_idx+1} to {end_idx+1}")
    new_lines = lines[:start_idx] + [NEW_FUNCTION + '\n'] + lines[end_idx + 1:]
    with open('/home/tracxn-lp-477/Desktop/publishing-sr/techcrwler/main.py', 'w') as f:
        f.writelines(new_lines)
    print("Done! Verifying syntax...")
    import subprocess
    result = subprocess.run(
        ['python3', '-m', 'py_compile', '/home/tracxn-lp-477/Desktop/publishing-sr/techcrwler/main.py'],
        capture_output=True, text=True
    )
    if result.returncode == 0:
        print("Syntax OK")
    else:
        print(f"Syntax ERROR: {result.stderr}")
