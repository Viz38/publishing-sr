import asyncio
import json
import logging
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from datetime import datetime, timezone
from typing import Optional, Dict, Any

from fastapi import FastAPI, HTTPException, BackgroundTasks, Depends, status, Request
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from fastapi.middleware.cors import CORSMiddleware
from sr_common.models import RunRequest
from sr_common.config import settings

# Configure logging
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOGS_DIR = os.path.join(BASE_DIR, 'Logs')
PROGRESS_FILE = os.path.join(BASE_DIR, ".progress.json")
STOP_FILE = os.path.join(BASE_DIR, ".stop_requested")
os.makedirs(LOGS_DIR, exist_ok=True)
api_log_path = os.path.join(LOGS_DIR, 'api.logs')

# Ensure we can write to the log file
if not os.path.exists(api_log_path):
    with open(api_log_path, 'w') as f: pass
os.chmod(api_log_path, 0o666)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(api_log_path, mode="a"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger("api")

app = FastAPI(title="SR Publishing Type C API")

@app.middleware("http")
async def log_requests(request: Request, call_next):
    client_ip = request.headers.get("x-forwarded-for") or request.client.host
    logger.info(f"REQ FROM {client_ip}: {request.method} {request.url.path}")
    
    response = await call_next(request)
    return response

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

security = HTTPBearer()

def verify_token(credentials: HTTPAuthorizationCredentials = Depends(security)):
    if credentials.credentials != settings.SERVICE_AUTH_TOKEN:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authentication credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )

state_lock = asyncio.Lock()
state = {
    "status": "idle",
    "is_stopping": False,
    "current_task": None,
    "start_time": None,
    "active_pid": None,
    "progress": {"current": 0, "total": 0, "success": 0, "fail": 0}
}

def utc_now():
    return datetime.now(timezone.utc).isoformat()

async def run_pipeline_task(request: RunRequest):
    async with state_lock:
        state["status"] = "running"
        state["is_stopping"] = False
        state["start_time"] = utc_now()
        state["progress"] = {"current": 0, "total": 0, "success": 0, "fail": 0}
    
    if os.path.exists(STOP_FILE):
        try: os.remove(STOP_FILE)
        except: pass

    try:
        if os.path.exists(PROGRESS_FILE):
            try: os.remove(PROGRESS_FILE)
            except: pass
            
        cmd = [sys.executable, "main.py", str(request.start_row), request.mode]
        if request.sheet_id:
            cmd.extend(["--sheet_id", request.sheet_id])
        logger.info(f"Starting Type C pipeline: {' '.join(cmd)}")
        
        stdout_path = os.path.join(LOGS_DIR, "stdout.log")
        stderr_path = os.path.join(LOGS_DIR, "stderr.log")
        with open(stdout_path, "a") as out_f, open(stderr_path, "a") as err_f:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=out_f,
                stderr=err_f,
                cwd=BASE_DIR
            )
            state["active_pid"] = process.pid
            logger.info(f"Pipeline started with PID: {process.pid}")
            
            while process.returncode is None:
                try:
                    if os.path.exists(PROGRESS_FILE):
                        with open(PROGRESS_FILE, "r") as f:
                            state["progress"] = json.load(f)
                except: pass
                await asyncio.sleep(2)
                if process.returncode is not None: break
            
            await process.wait()

        if state.get("is_stopping"):
            logger.info("Type C pipeline stopped gracefully")
            state["status"] = "stopped"
        elif process.returncode == 0:
            logger.info("Type C pipeline completed successfully")
            state["status"] = "succeeded"
        else:
            logger.error(f"Type C pipeline exited with code {process.returncode}")
            state["status"] = "failed"
            
    except Exception as e:
        logger.error(f"Pipeline error: {e}")
        state["status"] = "failed"
    finally:
        state["current_task"] = None
        state["active_pid"] = None
        state["is_stopping"] = False
        if os.path.exists(STOP_FILE):
            try: os.remove(STOP_FILE)
            except: pass

@app.get("/typec/status", dependencies=[Depends(verify_token)])
async def get_status():
    try:
        if os.path.exists(PROGRESS_FILE):
            with open(PROGRESS_FILE, "r") as f:
                state["progress"] = json.load(f)
    except: pass
    
    is_stopping = state.get("is_stopping", False) or state["progress"].get("is_stopping", False)
    current_status = "stopping" if (is_stopping and state["status"] in ("running", "stopping")) else state["status"]
        
    return {
        "status": current_status,
        "active": state["status"] in ("running", "stopping"),
        "is_stopping": is_stopping,
        "progress_current": state["progress"].get("current", 0),
        "progress_total": state["progress"].get("total", 0),
        "progress_success": state["progress"].get("success", 0),
        "progress_fail": state["progress"].get("fail", 0),
        "rate_limit_paused": state["progress"].get("rate_limit_paused", False),
        "sleep_remaining_sec": state["progress"].get("sleep_remaining_sec", 0.0),
        "rate_limit_message": state["progress"].get("rate_limit_message", ""),
        "backlog": state["progress"].get("backlog", 0),
        "workerName": f"{os.environ.get('WORKER_IDENTITY', os.uname().nodename)}-TypeC-Pipeline"
    }

@app.get("/typec/health", dependencies=[Depends(verify_token)])
async def health_check():
    return {"status": "ok", "timestamp": utc_now()}

@app.post("/typec/start", dependencies=[Depends(verify_token)])
async def start_pipeline(req: RunRequest, background_tasks: BackgroundTasks):
    if state["status"] in ("running", "stopping"):
        return {"status": "error", "message": "Pipeline already running or stopping"}
    
    background_tasks.add_task(run_pipeline_task, req)
    return {"status": "accepted"}

@app.post("/typec/cancel", dependencies=[Depends(verify_token)])
async def cancel_pipeline(request: Request):
    headers = dict(request.headers)
    logger.info(f"CANCEL REQUEST RECEIVED. Headers: {headers}")
    if state["active_pid"]:
        # If already in stopping state, a second cancel request triggers force kill
        if state.get("is_stopping"):
            try:
                import signal
                os.kill(state["active_pid"], signal.SIGKILL)
                logger.info(f"Force killed process {state['active_pid']}")
            except Exception as e:
                logger.error(f"Failed to force kill process {state['active_pid']}: {e}")
            state["status"] = "idle"
            state["is_stopping"] = False
            state["active_pid"] = None
            if os.path.exists(STOP_FILE):
                try: os.remove(STOP_FILE)
                except: pass
            return {"status": "ok", "message": "Force terminated"}

        # Graceful stop: first request
        state["is_stopping"] = True
        state["status"] = "stopping"
        try:
            with open(STOP_FILE, "w") as f:
                f.write("stop")
        except Exception as e:
            logger.error(f"Failed to create stop file: {e}")

        try:
            import signal
            os.kill(state["active_pid"], signal.SIGTERM)
            logger.info(f"Sent SIGTERM to process {state['active_pid']} for graceful stop")
        except Exception as e:
            logger.error(f"Failed to send SIGTERM to {state['active_pid']}: {e}")

        return {"status": "stopping", "message": "Graceful stop requested. Draining Tracxn queue and saving progress."}

    state["status"] = "idle"
    state["is_stopping"] = False
    return {"status": "ok"}

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8766))
    uvicorn.run(app, host="0.0.0.0", port=port)
