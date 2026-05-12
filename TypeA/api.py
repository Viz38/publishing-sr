import asyncio
import json
import logging
import os
import sys
from datetime import datetime, timezone
from typing import Optional, Dict, Any

from fastapi import FastAPI, HTTPException, BackgroundTasks, Depends, status, Request
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from fastapi.middleware.cors import CORSMiddleware
from sr_common.models import RunRequest
from sr_common.config import settings

# Configure logging
LOGS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'Logs')
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

app = FastAPI(title="SR Publishing Type A API")

@app.middleware("http")
async def log_requests(request: Request, call_next):
    # Capture Request Body safely
    body = await request.body()
    # Reset receive to allow app to read body again
    async def receive():
        return {"type": "http.request", "body": body}
    
    client_ip = request.headers.get("x-forwarded-for") or request.client.host
    logger.info(f"REQ FROM {client_ip}: {request.method} {request.url.path} | Body: {body.decode()[:500]}")
    
    # We pass the modified request (with reset receive)
    response = await call_next(Request(request.scope, receive=receive))
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


state = {
    "status": "idle",
    "current_task": None,
    "start_time": None,
    "active_pid": None,
    "progress": {"current": 0, "total": 0, "success": 0, "fail": 0}
}

def utc_now():
    return datetime.now(timezone.utc).isoformat()

async def run_pipeline_task(request: RunRequest):
    state["status"] = "running"
    state["start_time"] = utc_now()
    state["progress"] = {"current": 0, "total": 0, "success": 0, "fail": 0}
    
    try:
        if os.path.exists(".progress.json"):
            os.remove(".progress.json")
            
        cmd = [sys.executable, "main.py", str(request.start_row), request.mode]
        if request.sheet_id:
            cmd.extend(["--sheet_id", request.sheet_id])
        logger.info(f"Starting Type A pipeline: {' '.join(cmd)}")
        
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=os.path.dirname(os.path.abspath(__file__))
        )
        state["active_pid"] = process.pid
        logger.info(f"Pipeline started with PID: {process.pid}")
        
        while process.returncode is None:
            try:
                if os.path.exists(".progress.json"):
                    with open(".progress.json", "r") as f:
                        state["progress"] = json.load(f)
            except: pass
            await asyncio.sleep(2)
            if process.returncode is not None: break
        
        stdout, stderr = await process.communicate()
        if process.returncode == 0:
            logger.info("Type A pipeline completed successfully")
            state["status"] = "succeeded"
        else:
            err = stderr.decode() if stderr else "Unknown error"
            logger.error(f"Type A pipeline failed: {err}")
            state["status"] = "failed"
            
    except Exception as e:
        logger.error(f"Pipeline error: {e}")
        state["status"] = "failed"
    finally:
        state["current_task"] = None
        state["active_pid"] = None

@app.get("/typea/status", dependencies=[Depends(verify_token)])
async def get_status():
    # Sync with progress file
    try:
        if os.path.exists(".progress.json"):
            with open(".progress.json", "r") as f:
                state["progress"] = json.load(f)
    except:
        pass
        
    return {
        "status": state["status"],
        "active": state["status"] == "running",
        "progress_current": state["progress"].get("current", 0),
        "progress_total": state["progress"].get("total", 0),
        "progress_success": state["progress"].get("success", 0),
        "progress_fail": state["progress"].get("fail", 0),
        "workerName": "Vishnu-TypeA-Pipeline"
    }

@app.get("/typea/health", dependencies=[Depends(verify_token)])
async def health_check():
    return {"status": "ok", "timestamp": utc_now()}

@app.post("/typea/start", dependencies=[Depends(verify_token)])
async def start_pipeline(req: RunRequest, background_tasks: BackgroundTasks):
    if state["status"] == "running":
        return {"status": "error", "message": "Already running"}
    
    background_tasks.add_task(run_pipeline_task, req)
    return {"status": "accepted"}

@app.post("/typea/cancel", dependencies=[Depends(verify_token)])
async def cancel_pipeline():
    if state["active_pid"]:
        try:
            import signal
            os.kill(state["active_pid"], signal.SIGTERM)
            logger.info(f"Killed process {state['active_pid']}")
        except Exception as e:
            logger.error(f"Failed to kill process {state['active_pid']}: {e}")
    state["status"] = "idle"
    state["active_pid"] = None
    return {"status": "ok"}

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8767))
    uvicorn.run(app, host="0.0.0.0", port=port)
