#!/usr/bin/env python3
"""RO-ED API client for batch validation tests."""

import json
import time
import requests
from pathlib import Path
from websocket import create_connection

BASE_URL = "http://localhost:9000"
WS_URL = "ws://localhost:9000/api/ws/batch"


def wait_healthy(timeout: int = 300) -> bool:
    start = time.time()
    while time.time() - start < timeout:
        try:
            r = requests.get(f"{BASE_URL}/api/health", timeout=3)
            if r.status_code == 200:
                return True
        except Exception:
            pass
        time.sleep(2)
    return False


def login(username: str = "admin", password: str = "admin123") -> str:
    r = requests.post(
        f"{BASE_URL}/api/auth/login",
        json={"username": username, "password": password},
        timeout=10,
    )
    r.raise_for_status()
    return r.json()["access_token"]


def upload(token: str, pdf_path: Path) -> dict:
    with open(pdf_path, "rb") as f:
        r = requests.post(
            f"{BASE_URL}/api/jobs/upload",
            headers={"Authorization": f"Bearer {token}"},
            files={"file": (pdf_path.name, f, "application/pdf")},
            timeout=60,
        )
    r.raise_for_status()
    return r.json()


def run_pipeline_ws(token: str, save_path: str, filename: str, log_cb=None) -> dict:
    """Open WS, run pipeline, return job_data."""
    ws = create_connection(WS_URL, timeout=600)
    ws.send(json.dumps({
        "token": token,
        "files": [{"path": save_path, "filename": filename}],
    }))

    job_data = None
    job_id = None
    while True:
        try:
            raw = ws.recv()
        except Exception:
            break
        if not raw:
            break
        msg = json.loads(raw)
        if msg.get("error"):
            ws.close()
            raise RuntimeError(f"WS error: {msg['error']}")
        if msg.get("job_created"):
            job_id = msg.get("job_id")
        if msg.get("log") and log_cb:
            log_cb(msg.get("log"))
        if msg.get("file_complete"):
            job_data = msg.get("job_data")
            break
        if msg.get("batch_complete"):
            break
    ws.close()
    return {"job_id": job_id, "job_data": job_data}


def get_job(token: str, job_id: str) -> dict:
    r = requests.get(
        f"{BASE_URL}/api/jobs/{job_id}",
        headers={"Authorization": f"Bearer {token}"},
        timeout=30,
    )
    r.raise_for_status()
    return r.json()
