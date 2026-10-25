import pytest
from db import (
    create_session,
    get_or_create_context,
    get_session,
    list_sessions,
    log_audit,
    set_active_project,
    set_active_session,
    update_session,
)


@pytest.mark.asyncio
async def test_get_or_create_context(temp_db):
    user_id = 12345
    ctx = await get_or_create_context(user_id, "default_proj")
    assert ctx["telegram_user_id"] == user_id
    assert ctx["active_project"] == "default_proj"
    assert ctx["active_session_id"] is None

    # Fetch again, should return existing row
    ctx2 = await get_or_create_context(user_id, "other_proj")
    assert ctx2["telegram_user_id"] == user_id
    assert ctx2["active_project"] == "default_proj"


@pytest.mark.asyncio
async def test_session_lifecycle(temp_db):
    user_id = 99999
    sid = await create_session(user_id, "my_repo", "Initial test session", mode="PLAN")
    assert sid > 0

    session = await get_session(sid)
    assert session is not None
    assert session["project_slug"] == "my_repo"
    assert session["title"] == "Initial test session"
    assert session["mode"] == "PLAN"
    assert session["status"] == "IDLE"

    # Context should be updated
    ctx = await get_or_create_context(user_id, "default")
    assert ctx["active_project"] == "my_repo"
    assert ctx["active_session_id"] == sid

    # Update session
    await update_session(sid, mode="EXECUTE", status="RUNNING", agy_conv_id="test-conv-123")
    updated = await get_session(sid)
    assert updated["mode"] == "EXECUTE"
    assert updated["status"] == "RUNNING"
    assert updated["agy_conv_id"] == "test-conv-123"


@pytest.mark.asyncio
async def test_list_sessions(temp_db):
    user_id = 11111
    s1 = await create_session(user_id, "project_a", "Session 1")
    s2 = await create_session(user_id, "project_a", "Session 2")
    s3 = await create_session(user_id, "project_b", "Session 3")

    sessions_a = await list_sessions("project_a")
    assert len(sessions_a) == 2
    # Ordered descending by id
    assert sessions_a[0]["id"] == s2
    assert sessions_a[1]["id"] == s1

    sessions_b = await list_sessions("project_b")
    assert len(sessions_b) == 1
    assert sessions_b[0]["id"] == s3


@pytest.mark.asyncio
async def test_log_audit(temp_db):
    user_id = 22222
    sid = await create_session(user_id, "audit_test", "Audit Session")
    await log_audit(sid, "TEST_EVENT", {"detail": "something happened"})
    # Verify no error raised on serialization and insertion

