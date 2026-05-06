"""Activity log v2 API: filters, stats, security view, CSV export."""
import csv
import io
from typing import Optional
from fastapi import APIRouter, Depends, Query, Response
import database

try:
    from middleware import require_admin
except ImportError:
    from middleware import get_current_user
    async def require_admin(user=Depends(get_current_user)):
        from fastapi import HTTPException
        if not user or user.get("role") != "admin":
            raise HTTPException(403, "admin only")
        return user


router = APIRouter()


SECURITY_ACTIONS = (
    "LOGIN_FAILED", "LDAP_BIND_FAIL", "LDAP_TEST", "LDAP_DELETE",
    "DELETE_USER", "ROLE_CHANGE", "CONFIG_CHANGE",
    "LOCKOUT", "MFA_FAIL", "TOKEN_REVOKED",
)


@router.get("/")
async def list_activity(
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    action: Optional[str] = None,
    action_in: Optional[str] = None,
    status: Optional[str] = None,
    user: Optional[str] = None,
    severity: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    search: Optional[str] = None,
    _=Depends(require_admin),
):
    rows = database.list_activity_log_v2(
        limit=limit, offset=offset,
        action=action, action_in=action_in, status=status, user=user,
        severity=severity, date_from=date_from, date_to=date_to,
        search=search,
    )
    total = database.count_activity_log_v2(
        action=action, action_in=action_in, status=status, user=user,
        severity=severity, date_from=date_from, date_to=date_to, search=search,
    )
    return {"events": rows, "total": total, "limit": limit, "offset": offset}


@router.get("/security")
async def list_security_events(
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    _=Depends(require_admin),
):
    """Security-relevant events only."""
    all_rows = []
    for act in SECURITY_ACTIONS:
        all_rows += database.list_activity_log_v2(limit=limit, offset=0, action=act)
    all_rows.sort(key=lambda r: r.get("timestamp", ""), reverse=True)
    paged = all_rows[offset:offset + limit]
    return {"events": paged, "total": len(all_rows), "limit": limit, "offset": offset}


@router.get("/stats")
async def activity_stats(
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    _=Depends(require_admin),
):
    return database.activity_log_stats(date_from=date_from, date_to=date_to)


@router.get("/{event_id}")
async def get_event(event_id: int, _=Depends(require_admin)):
    """Single event detail (for drawer)."""
    import sqlite3
    conn = database._connect()
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute("SELECT * FROM activity_log WHERE id = ?", (event_id,))
    row = cur.fetchone()
    conn.close()
    if not row:
        from fastapi import HTTPException
        raise HTTPException(404, "event not found")
    event = dict(row)
    related = []
    try:
        conn = database._connect()
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        if event.get("user"):
            cur.execute("""SELECT * FROM activity_log
                            WHERE user = ? AND id != ?
                            ORDER BY timestamp DESC LIMIT 5""",
                        (event["user"], event_id))
            related = [dict(r) for r in cur.fetchall()]
        conn.close()
    except Exception:
        pass
    return {"event": event, "related": related}


@router.get("/export/csv")
async def export_csv(
    action: Optional[str] = None,
    status: Optional[str] = None,
    user: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    _=Depends(require_admin),
):
    rows = database.list_activity_log_v2(
        limit=10000, offset=0,
        action=action, status=status, user=user,
        date_from=date_from, date_to=date_to,
    )
    output = io.StringIO()
    if rows:
        keys = ["timestamp", "user", "action", "status", "auth_source", "ip_address",
                "user_agent", "duration_ms", "resource", "details", "error_message", "severity"]
        writer = csv.DictWriter(output, fieldnames=keys, extrasaction="ignore")
        writer.writeheader()
        for r in rows:
            writer.writerow({k: r.get(k, "") for k in keys})
    csv_data = output.getvalue()
    return Response(content=csv_data, media_type="text/csv",
                    headers={"Content-Disposition": "attachment; filename=activity_log.csv"})
