#!/bin/bash
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
export PYTHONPATH="/Users/vishnu/Documents/Tracxn/SR/Publishing:$PYTHONPATH"
cd "/Users/vishnu/Documents/Tracxn/SR/Publishing/TypeC"
exec "/Users/vishnu/Documents/Tracxn/SR/Publishing/TypeC/.venv/bin/uvicorn" api:app --host 0.0.0.0 --port 8766 --log-level info
