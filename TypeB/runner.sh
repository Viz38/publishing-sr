#!/bin/bash
cd "/Users/vishnu/Documents/Tracxn/SR/Publishing-Caching/TypeB"
# Ensure project root is in PYTHONPATH for sr_common imports
export PYTHONPATH="/Users/vishnu/Documents/Tracxn/SR/Publishing-Caching:$PYTHONPATH"
export PYTHONUNBUFFERED=1
"/Users/vishnu/Documents/Tracxn/SR/Publishing-Caching/.venv/bin/python" -m uvicorn api:app --host 0.0.0.0 --port 8765 --workers 1 --log-level info >> "/Users/vishnu/Documents/Tracxn/SR/Publishing-Caching/TypeB/Logs/api.logs" 2>&1
