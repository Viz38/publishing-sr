#!/bin/bash
cd "/Users/vishnu/Documents/Tracxn/SR/Publishing/TypeC"
# Ensure project root is in PYTHONPATH for sr_common imports
export PYTHONPATH="/Users/vishnu/Documents/Tracxn/SR/Publishing:$PYTHONPATH"
export PYTHONUNBUFFERED=1
"/Users/vishnu/Documents/Tracxn/SR/Publishing/.venv/bin/python" -m uvicorn api:app --host 0.0.0.0 --port 8766 --workers 1 --log-level info >> "/Users/vishnu/Documents/Tracxn/SR/Publishing/TypeC/Logs/api.logs" 2>&1
