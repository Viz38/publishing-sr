#!/bin/bash
cd "/home/tracxn-lp-477/Desktop/publishing-sr/techcrwler"
# Ensure project root is in PYTHONPATH for sr_common imports
export PYTHONPATH="/home/tracxn-lp-477/Desktop/publishing-sr:$PYTHONPATH"
export PYTHONUNBUFFERED=1
"/home/tracxn-lp-477/Desktop/publishing-sr/.venv/bin/uvicorn" api:app --host 0.0.0.0 --port 8768 --workers 1 --log-level info >> "/home/tracxn-lp-477/Desktop/publishing-sr/techcrwler/Logs/api.logs" 2>&1
