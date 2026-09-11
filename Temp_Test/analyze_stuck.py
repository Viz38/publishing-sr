import re
from collections import defaultdict

starts = {}
ends = {}
datasources = {}

with open('811-TypeB-Logs/pipeline.logs', 'r') as f:
    for line in f:
        match_start = re.search(r'PROCESS START: ([\w.-]+)', line)
        if match_start:
            starts[match_start.group(1)] = line[:19]
            continue
            
        match_ds = re.search(r'DATASOURCE: ([\w.-]+)', line)
        if match_ds:
            datasources[match_ds.group(1)] = line[:19]
            continue
            
        match_success = re.search(r'PROCESS SUCCESS: ([\w.-]+)', line)
        if match_success:
            ends[match_success.group(1)] = "SUCCESS"
            continue
            
        match_fail = re.search(r'PROCESS FAILED: ([\w.-]+)', line)
        if match_fail:
            ends[match_fail.group(1)] = "FAILED"
            continue
            
        match_fail2 = re.search(r'PIPELINE FAILED: ([\w.-]+)', line)
        if match_fail2:
            ends[match_fail2.group(1)] = "PIPELINE_FAILED"
            continue

stuck = []
for domain in starts:
    if domain not in ends:
        stuck.append(domain)

print(f"Total domains started: {len(starts)}")
print(f"Total domains finished: {len(ends)}")
print(f"Total domains stuck: {len(stuck)}")
print("First 20 stuck domains:")
for d in stuck[:20]:
    print(f"{d} (Started: {starts[d]}, Datasource logged: {d in datasources})")
