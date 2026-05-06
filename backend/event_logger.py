"""Centralized event logger. Single sink for all activity events.

Wraps database.insert_activity_log_v2 with sane defaults + JSON serialization.
"""
import json
import time
from datetime import datetime
from typing import Any, Optional

import database


_SECURITY_ACTIONS = {
    "LOGIN_FAILED", "LDAP_BIND_FAIL", "LDAP_TEST_FAIL",
    "DELETE_USER", "ROLE_CHANGE", "CONFIG_CHANGE",
    "LOCKOUT", "MFA_FAIL", "TOKEN_REVOKED",
}

_JOB_ACTIONS = {"JOB_START", "JOB_SUCCESS", "JOB_FAIL", "JOB_REVIEWED",
                "JOB_CORRECTED", "EXPORT_EXCEL", "RUN_JOB"}


def _ts() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def log_event(action: str, *, user: Optional[str] = None,
              status: str = "OK", details: Optional[str] = None,
              ip: Optional[str] = None, user_agent: Optional[str] = None,
              auth_source: Optional[str] = None, duration_ms: Optional[int] = None,
              resource: Optional[str] = None, error: Optional[str] = None,
              payload: Optional[Any] = None):
    """Single entry point for activity logging.

    Severity is auto-assigned: error if status=FAILED, warn if action in security set,
    else info.
    """
    severity = "info"
    if status == "FAILED" or status == "ERROR":
        severity = "error"
    elif action in _SECURITY_ACTIONS:
        severity = "warn"

    payload_json = None
    if payload is not None:
        try:
            payload_json = json.dumps(payload, default=str)[:8000]
        except Exception:
            payload_json = str(payload)[:8000]

    try:
        database.insert_activity_log_v2(
            timestamp=_ts(),
            user=user,
            action=action,
            details=details,
            ip_address=ip,
            user_agent=user_agent,
            auth_source=auth_source,
            status=status,
            duration_ms=duration_ms,
            resource=resource,
            severity=severity,
            error_message=error,
            payload_json=payload_json,
        )
    except Exception as e:
        # Best effort — never raise from logger
        print(f"[event_logger] insert failed: {e}")


# Convenience wrappers
def log_login(user, status, ip=None, user_agent=None, auth_source="LOCAL",
              duration_ms=None, error=None):
    action = "LOGIN" if status == "OK" else "LOGIN_FAILED"
    log_event(action, user=user, status=status, ip=ip, user_agent=user_agent,
              auth_source=auth_source, duration_ms=duration_ms, error=error,
              details="Session started" if status == "OK" else (error or "auth failed"))


def log_job(action, user, job_id, status="OK", duration_ms=None, **kwargs):
    log_event(action, user=user, status=status, duration_ms=duration_ms,
              resource=job_id, payload=kwargs,
              details=kwargs.get("details") or f"{action.lower()} {job_id}")


def log_ldap(action, user, ldap_id, status="OK", error=None, **kwargs):
    log_event(action, user=user, status=status, error=error,
              resource=f"LDAP:{ldap_id}" if ldap_id else None,
              payload=kwargs,
              details=kwargs.get("details") or action.lower())
