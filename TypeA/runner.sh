#!/bin/bash
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
export PYTHONPATH="/Users/vishnu/Documents/Tracxn/SR/Publishing:$PYTHONPATH"
cd "/Users/vishnu/Documents/Tracxn/SR/Publishing/TypeA"
exec "/Users/vishnu/Documents/Tracxn/SR/Publishing/TypeA/.venv/bin/uvicorn" api:app --host 0.0.0.0 --port 8767 --log-level info
