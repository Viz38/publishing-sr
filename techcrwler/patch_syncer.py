"""
Patches sheet_syncer in main.py:
- Single full-row write always (B:BG)
- col B = task.created_at (NEVER overwritten)
- col BG = poll_count
- Correct column mapping for all statuses
"""
import re

FILE = '/home/tracxn-lp-477/Desktop/publishing-sr/techcrwler/main.py'

NEW_CODE = '''# 3. REAL-TIME SHEET STREAMING SYNCER
# Column layout (B=col2 to BG=col59, 58 cols written as row_vals[1:])
# [1]  B  = created_at      (NEVER overwritten -- always use task.created_at)
# [2]  C  = homepage_job_id
# [3]  D  = about_job_id
# [4]  E  = about_link
# [5]  F  = home_s3
# [6]  G  = home_content
# [7]  H  = about_s3
# [8]  I  = about_content
# [9]  J  = home_status
# [10] K  = about_status
# [11] L  = last_updated
# [12] M  = elapsed_seconds
# [13..57] N..BF = OTHER_PAGES (job_id, link, s3, content, status) x9 = 45 cols
# [58] BG = poll_count
def _build_row_vals(task):
    row_vals = [""] * 59       # indices 0-58 -> cols A-BG; only [1:] written -> B:BG
    row_vals[0]  = task.domain
    row_vals[1]  = task.created_at                                  # col B -- NEVER changes
    row_vals[2]  = task.homepage_job_id
    row_vals[3]  = task.category_job_ids.get("About", "")
    row_vals[4]  = task.category_links.get("About") or "No About Link"
    row_vals[5]  = task.home_s3
    row_vals[6]  = task.home_content[:20000] if task.home_content else ""
    row_vals[7]  = task.category_s3s.get("About", "")
    row_vals[8]  = task.category_contents.get("About", "")[:20000] if task.category_contents.get("About") else ""
    row_vals[9]  = task.home_status                                 # col J
    row_vals[10] = task.category_statuses.get("About", "N/A")      # col K
    from datetime import datetime
    import time as _time
    row_vals[11] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")    # col L -- last updated
    row_vals[12] = str(round(_time.time() - task.start_time, 2))   # col M -- elapsed

    curr = 13
    for cat in OTHER_PAGES:
        row_vals[curr]   = task.category_job_ids.get(cat, "")
        row_vals[curr+1] = task.category_links.get(cat) or "N/A"
        row_vals[curr+2] = task.category_s3s.get(cat, "")
        row_vals[curr+3] = task.category_contents.get(cat, "")[:20000] if task.category_contents.get(cat) else ""
        row_vals[curr+4] = task.category_statuses.get(cat, "N/A")
        curr += 5
    # curr ends at 13 + 9*5 = 58

    row_vals[58] = str(task.poll_count)                             # col BG -- poll count
    return row_vals

async def sheet_syncer(db_ws):
    logger.info("REAL-TIME SHEET SYNCER: Starting live streaming spreadsheet updater...")

    while True:
        task = await sheet_update_queue.get()
        if task is None:
            sheet_update_queue.task_done()
            break

        async with sheet_lock:
            try:
                row_vals = _build_row_vals(task)
                # Always write B:BG -- col B always = task.created_at (never datetime.now())
                await db_ws.update(
                    range_name=f"B{task.row_idx}:BG{task.row_idx}",
                    values=[row_vals[1:]]    # 58 values -> cols B through BG
                )
                logger.info(
                    f"SHEET SYNC (poll #{task.poll_count}): "
                    f"Row {task.row_idx} ({task.domain}) written -- "
                    f"home={task.home_status}, about={task.category_statuses.get('About','')}"
                )
            except Exception as e:
                logger.error(f"SHEET SYNCER: Failed row update for {task.domain}: {e}")

        sheet_update_queue.task_done()
        await asyncio.sleep(0.05)

    logger.info("REAL-TIME SHEET SYNCER: All live updates written to Google Sheet.")

'''

with open(FILE, 'r') as f:
    content = f.read()

lines = content.split('\n')

# Find "# 3. REAL-TIME SHEET STREAMING SYNCER"
start_idx = None
for i, line in enumerate(lines):
    if line.strip() == '# 3. REAL-TIME SHEET STREAMING SYNCER':
        start_idx = i
        break

if start_idx is None:
    print("ERROR: Could not find '# 3. REAL-TIME SHEET STREAMING SYNCER'")
    exit(1)

# Find the end: next top-level def/class/async def after sheet_syncer
# Also need to remove col_idx_to_letter if it's there
end_idx = None
in_syncer = False
for i, line in enumerate(lines):
    if i < start_idx:
        continue
    if i == start_idx:
        in_syncer = True
        continue
    if in_syncer and line and not line.startswith(' ') and not line.startswith('#'):
        # top-level definition
        # If it's col_idx_to_letter, include it in removal
        if line.startswith('def col_idx_to_letter') or line.startswith('async def safe_get_all_values'):
            if end_idx is None:
                end_idx = i
        if line.startswith('async def safe_get_all_values'):
            end_idx = i
            break
        if end_idx is None:
            end_idx = i
            break

if end_idx is None:
    print("ERROR: Could not find end of sheet_syncer section")
    exit(1)

print(f"Replacing lines {start_idx+1} to {end_idx} (inclusive start, exclusive end)")
print(f"Start line: {repr(lines[start_idx])}")
print(f"End line:   {repr(lines[end_idx])}")

new_lines = lines[:start_idx] + [NEW_CODE] + lines[end_idx:]

with open(FILE, 'w') as f:
    f.write('\n'.join(new_lines))

print("Written! Verifying syntax...")
import subprocess
result = subprocess.run(
    ['python3', '-m', 'py_compile', FILE],
    capture_output=True, text=True
)
if result.returncode == 0:
    print("Syntax OK")
else:
    print(f"Syntax ERROR:\n{result.stderr}")
