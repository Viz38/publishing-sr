#!/bin/bash
cd "/Users/vishnu/Documents/Tracxn/SR/Publishing_Copy/TypeB"
# Ensure project root is in PYTHONPATH for sr_common imports
export PYTHONPATH="/Users/vishnu/Documents/Tracxn/SR/Publishing_Copy:$PYTHONPATH"
export PYTHONUNBUFFERED=1
./.venv/bin/python -m uvicorn api:app --host 0.0.0.0 --port 8765 --workers 1 --log-level info >> "/Users/vishnu/Documents/Tracxn/SR/Publishing_Copy/TypeB/Logs/api.logs" 2>&1
