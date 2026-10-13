"""Remove the duplicate (old) sheet_syncer function (lines 1192-1317) and col_idx_to_letter."""

FILE = '/home/tracxn-lp-477/Desktop/publishing-sr/techcrwler/main.py'

with open(FILE, 'r') as f:
    lines = f.readlines()

# Lines to remove: old sheet_syncer starts at line 1192 (1-indexed) and col_idx_to_letter ends at line 1317
# (safe_get_all_values starts at 1318)
# 0-indexed: 1191 to 1317 (inclusive)

remove_start = 1191   # 0-indexed, line 1192 in file
remove_end   = 1317   # 0-indexed, line 1318 is safe_get_all_values (keep from here)

print(f"Removing lines {remove_start+1} to {remove_end+1} (1-indexed)")
print(f"  First removed: {repr(lines[remove_start].rstrip())}")
print(f"  Last removed:  {repr(lines[remove_end-1].rstrip())}")
print(f"  Kept after:    {repr(lines[remove_end].rstrip())}")

new_lines = lines[:remove_start] + lines[remove_end:]

with open(FILE, 'w') as f:
    f.writelines(new_lines)

print("Written!")

import subprocess
result = subprocess.run(['python3', '-m', 'py_compile', FILE], capture_output=True, text=True)
if result.returncode == 0:
    print("Syntax OK")
else:
    print(f"Syntax ERROR:\n{result.stderr}")
