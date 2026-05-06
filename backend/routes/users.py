#!/usr/bin/env python3
"""User management routes for RO-ED AI Agent (admin only)"""

from fastapi import APIRouter, Depends, HTTPException, Request, status
from typing import List

import config
import database
import event_logger
from middleware import require_admin, get_current_user
from schemas import (
    CreateUserRequest, UpdateUserRequest, UserListResponse,
    ActivityLogResponse,
)

router = APIRouter()


def _ip(request: Request):
    try:
        return request.client.host if request and request.client else None
    except Exception:
        return None


@router.get("/")
async def list_users(admin: dict = Depends(require_admin)):
    """List all users with group info (admin only)."""
    users = database.get_all_users_with_groups()
    return users


@router.post("/", status_code=status.HTTP_201_CREATED)
async def create_user(request_in: CreateUserRequest, request: Request, admin: dict = Depends(require_admin)):
    """Create a new user (admin only). Blocked when Keycloak is enabled."""
    if config.get_keycloak_config():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="User management is handled in Keycloak",
        )
    success = database.create_user(
        request_in.username, request_in.password, request_in.display_name, request_in.role
    )
    if not success:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Username '{request_in.username}' already exists",
        )
    database.log_activity(
        admin["id"], admin["username"], "CREATE_USER",
        f"Created user: {request_in.username} ({request_in.role})"
    )
    try:
        event_logger.log_event(
            action="USER_CREATE",
            user=admin.get("username"),
            status="OK",
            resource=f"User:{request_in.username}",
            details=f"created user {request_in.username} ({request_in.role})",
            ip=_ip(request),
            payload={"username": request_in.username, "role": request_in.role},
        )
    except Exception:
        pass
    return {"message": f"User '{request_in.username}' created"}


@router.put("/{user_id}")
async def update_user(user_id: int, request_in: UpdateUserRequest, request: Request, admin: dict = Depends(require_admin)):
    """Update a user (admin only). Blocked when Keycloak is enabled."""
    if config.get_keycloak_config():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="User management is handled in Keycloak",
        )
    # Snapshot prior state to detect role/active changes for richer event types
    prior = None
    try:
        prior = database.get_user_by_id(user_id) if hasattr(database, "get_user_by_id") else None
    except Exception:
        prior = None

    database.update_user(
        user_id,
        display_name=request_in.display_name,
        role=request_in.role,
        is_active=request_in.is_active if request_in.is_active is not None else None,
        password=request_in.password,
    )
    database.log_activity(
        admin["id"], admin["username"], "UPDATE_USER",
        f"Updated user ID {user_id}"
    )

    actor = admin.get("username")
    ip = _ip(request)

    # Fire fine-grained events when applicable
    try:
        if request_in.password:
            event_logger.log_event(
                action="PASSWORD_RESET",
                user=actor,
                status="OK",
                resource=f"User:{user_id}",
                details=f"password reset for user {user_id}",
                ip=ip,
            )
    except Exception:
        pass

    try:
        if request_in.role is not None:
            old_role = prior.get("role") if isinstance(prior, dict) else None
            if old_role != request_in.role:
                event_logger.log_event(
                    action="ROLE_CHANGE",
                    user=actor,
                    status="OK",
                    resource=f"User:{user_id}",
                    details=f"role change for user {user_id}: {old_role} -> {request_in.role}",
                    ip=ip,
                    payload={"old_role": old_role, "new_role": request_in.role},
                )
    except Exception:
        pass

    try:
        if request_in.is_active is not None:
            old_active = prior.get("is_active") if isinstance(prior, dict) else None
            if old_active != request_in.is_active:
                event_logger.log_event(
                    action="USER_ENABLE" if request_in.is_active else "USER_DISABLE",
                    user=actor,
                    status="OK",
                    resource=f"User:{user_id}",
                    details=f"user {user_id} {'enabled' if request_in.is_active else 'disabled'}",
                    ip=ip,
                )
    except Exception:
        pass

    # Generic update event for name/email-style updates
    try:
        event_logger.log_event(
            action="USER_UPDATE",
            user=actor,
            status="OK",
            resource=f"User:{user_id}",
            details=f"updated user {user_id}",
            ip=ip,
            payload={
                "display_name": request_in.display_name,
                "role": request_in.role,
                "is_active": request_in.is_active,
                "password_changed": bool(request_in.password),
            },
        )
    except Exception:
        pass

    return {"message": "User updated"}


@router.delete("/{user_id}")
async def delete_user(user_id: int, request: Request, admin: dict = Depends(require_admin)):
    """Delete a user (admin only). Blocked when Keycloak is enabled."""
    if config.get_keycloak_config():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="User management is handled in Keycloak",
        )
    if user_id == admin["id"]:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot delete your own account",
        )
    success = database.delete_user(user_id)
    if not success:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    database.log_activity(
        admin["id"], admin["username"], "DELETE_USER",
        f"Deleted user ID {user_id}"
    )
    try:
        event_logger.log_event(
            action="USER_DELETE",
            user=admin.get("username"),
            status="OK",
            resource=f"User:{user_id}",
            details=f"deleted user {user_id}",
            ip=_ip(request),
        )
    except Exception:
        pass
    return {"message": "User deleted"}


@router.get("/activity-logs", response_model=List[ActivityLogResponse])
async def get_activity_logs(
    limit: int = 200,
    user_id: int = None,
    admin: dict = Depends(require_admin),
):
    """Get activity logs (admin only)."""
    logs = database.get_activity_logs(limit=limit, user_id=user_id)
    return [ActivityLogResponse(**log) for log in logs]
