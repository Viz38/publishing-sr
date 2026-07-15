#!/bin/bash
cd "/home/tracxn-lp-477/Desktop/Publishing_SR/TypeA"
# Ensure project root is in PYTHONPATH for sr_common imports
export PYTHONPATH="/home/tracxn-lp-477/Desktop/Publishing_SR:$PYTHONPATH"
export PYTHONUNBUFFERED=1
"/home/tracxn-lp-477/.local/bin/uv" run uvicorn api:app --host 0.0.0.0 --port 8767 --workers 1 --log-level info >> "/home/tracxn-lp-477/Desktop/Publishing_SR/TypeA/Logs/api.logs" 2>&1
