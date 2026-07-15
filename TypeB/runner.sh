#!/bin/bash
cd "/home/tracxn-lp-477/Desktop/Publishing_SR/TypeB"
# Ensure project root is in PYTHONPATH for sr_common imports
export PYTHONPATH="/home/tracxn-lp-477/Desktop/Publishing_SR:$PYTHONPATH"
export PYTHONUNBUFFERED=1
"/home/tracxn-lp-477/.local/bin/uv" run uvicorn api:app --host 0.0.0.0 --port 8765 --workers 1 --log-level info >> "/home/tracxn-lp-477/Desktop/Publishing_SR/TypeB/Logs/api.logs" 2>&1
