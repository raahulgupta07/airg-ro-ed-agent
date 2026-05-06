#!/usr/bin/env python3
"""Group management routes for RO-ED AI Agent (admin only)"""

from fastapi import APIRouter, Depends, HTTPException, Request, status
from typing import List

import database
import event_logger
from middleware import require_admin
from schemas import GroupRequest, GroupResponse, AssignGroupRequest

router = APIRouter()


def _ip(request: Request):
    try:
        return request.client.host if request and request.client else None
    except Exception:
        return None


@router.get("/", response_model=List[GroupResponse])
async def list_groups(admin: dict = Depends(require_admin)):
    """List all groups with member count."""
    groups = database.get_all_groups()
    return [GroupResponse(**g) for g in groups]


@router.post("/", status_code=status.HTTP_201_CREATED)
async def create_group(request_in: GroupRequest, request: Request, admin: dict = Depends(require_admin)):
    """Create a new group."""
    kwargs = request_in.model_dump(exclude={"name", "description", "member_ids"})
    gid = database.create_group(request_in.name, request_in.description, **kwargs)
    if gid is None:
        raise HTTPException(status_code=409, detail=f"Group '{request_in.name}' already exists")

    if request_in.member_ids:
        database.set_group_members(gid, request_in.member_ids, admin["username"])

    database.log_activity(admin["id"], admin["username"], "CREATE_GROUP", f"Created group: {request_in.name}")
    try:
        event_logger.log_event(
            action="GROUP_CREATE",
            user=admin.get("username"),
            status="OK",
            resource=f"Group:{gid}",
            details=f"created group {request_in.name}",
            ip=_ip(request),
            payload={"name": request_in.name, "member_ids": request_in.member_ids or []},
        )
        # Member adds at creation time
        for mid in (request_in.member_ids or []):
            try:
                event_logger.log_event(
                    action="GROUP_MEMBER_ADD",
                    user=admin.get("username"),
                    status="OK",
                    resource=f"Group:{gid}",
                    details=f"added user {mid} to group {gid}",
                    ip=_ip(request),
                    payload={"group_id": gid, "user_id": mid},
                )
            except Exception:
                pass
    except Exception:
        pass
    return {"id": gid, "message": f"Group '{request_in.name}' created"}


@router.get("/{group_id}", response_model=GroupResponse)
async def get_group(group_id: int, admin: dict = Depends(require_admin)):
    """Get a single group with members."""
    group = database.get_group(group_id)
    if not group:
        raise HTTPException(status_code=404, detail="Group not found")
    group["members"] = database.get_group_members(group_id)
    group["member_count"] = len(group["members"])
    return GroupResponse(**group)


@router.put("/{group_id}")
async def update_group(group_id: int, request_in: GroupRequest, request: Request, admin: dict = Depends(require_admin)):
    """Update a group's permissions and members."""
    group = database.get_group(group_id)
    if not group:
        raise HTTPException(status_code=404, detail="Group not found")

    # Snapshot prior members for diff
    prior_member_ids = set()
    try:
        prior_members = database.get_group_members(group_id) or []
        prior_member_ids = {m.get("id") if isinstance(m, dict) else m for m in prior_members}
    except Exception:
        prior_member_ids = set()

    kwargs = request_in.model_dump(exclude={"member_ids"})
    database.update_group(group_id, **kwargs)

    database.set_group_members(group_id, request_in.member_ids, admin["username"])

    database.log_activity(admin["id"], admin["username"], "UPDATE_GROUP", f"Updated group: {request_in.name}")
    try:
        event_logger.log_event(
            action="GROUP_UPDATE",
            user=admin.get("username"),
            status="OK",
            resource=f"Group:{group_id}",
            details=f"updated group {request_in.name}",
            ip=_ip(request),
            payload={"name": request_in.name},
        )
        new_ids = set(request_in.member_ids or [])
        added = new_ids - prior_member_ids
        removed = prior_member_ids - new_ids
        for mid in added:
            try:
                event_logger.log_event(
                    action="GROUP_MEMBER_ADD",
                    user=admin.get("username"),
                    status="OK",
                    resource=f"Group:{group_id}",
                    details=f"added user {mid} to group {group_id}",
                    ip=_ip(request),
                    payload={"group_id": group_id, "user_id": mid},
                )
            except Exception:
                pass
        for mid in removed:
            try:
                event_logger.log_event(
                    action="GROUP_MEMBER_REMOVE",
                    user=admin.get("username"),
                    status="OK",
                    resource=f"Group:{group_id}",
                    details=f"removed user {mid} from group {group_id}",
                    ip=_ip(request),
                    payload={"group_id": group_id, "user_id": mid},
                )
            except Exception:
                pass
    except Exception:
        pass
    return {"message": f"Group '{request_in.name}' updated"}


@router.delete("/{group_id}")
async def delete_group(group_id: int, request: Request, admin: dict = Depends(require_admin)):
    """Delete a group."""
    group = database.get_group(group_id)
    if not group:
        raise HTTPException(status_code=404, detail="Group not found")

    database.delete_group(group_id)
    database.log_activity(admin["id"], admin["username"], "DELETE_GROUP", f"Deleted group: {group['name']}")
    try:
        event_logger.log_event(
            action="GROUP_DELETE",
            user=admin.get("username"),
            status="OK",
            resource=f"Group:{group_id}",
            details=f"deleted group {group['name']}",
            ip=_ip(request),
            payload={"name": group.get("name")},
        )
    except Exception:
        pass
    return {"message": f"Group '{group['name']}' deleted"}


@router.put("/assign/{user_id}")
async def assign_user_group(user_id: int, request_in: AssignGroupRequest, request: Request, admin: dict = Depends(require_admin)):
    """Assign a user to a group (or remove from all groups)."""
    database.set_user_group(user_id, request_in.group_id, admin["username"])
    database.log_activity(
        admin["id"], admin["username"], "ASSIGN_GROUP",
        f"User {user_id} → group {request_in.group_id or 'none'}",
    )
    try:
        if request_in.group_id:
            event_logger.log_event(
                action="GROUP_MEMBER_ADD",
                user=admin.get("username"),
                status="OK",
                resource=f"Group:{request_in.group_id}",
                details=f"assigned user {user_id} to group {request_in.group_id}",
                ip=_ip(request),
                payload={"group_id": request_in.group_id, "user_id": user_id},
            )
        else:
            event_logger.log_event(
                action="GROUP_MEMBER_REMOVE",
                user=admin.get("username"),
                status="OK",
                resource=f"User:{user_id}",
                details=f"removed user {user_id} from all groups",
                ip=_ip(request),
                payload={"user_id": user_id},
            )
    except Exception:
        pass
    return {"message": "Group assignment updated"}
