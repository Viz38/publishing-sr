#!/bin/bash
cd "/Users/vishnu/Desktop/publishing-sr-tc-integrated/TypeA"
# Ensure project root is in PYTHONPATH for sr_common imports
export PYTHONPATH="/Users/vishnu/Desktop/publishing-sr-tc-integrated:$PYTHONPATH"
export PYTHONUNBUFFERED=1
"/Users/vishnu/.local/bin/uv" run uvicorn api:app --host 0.0.0.0 --port 8767 --workers 1 --log-level info >> "/Users/vishnu/Desktop/publishing-sr-tc-integrated/TypeA/Logs/api.logs" 2>&1
