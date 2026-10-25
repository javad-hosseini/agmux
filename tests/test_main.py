import pytest
from unittest.mock import AsyncMock, MagicMock
from aiogram.types import Message
from main import (
    OwnerOnlyMiddleware,
    approval_kb,
    dashboard_inline_kb,
    main_reply_keyboard,
    project_list_kb,
    session_list_kb,
    task_running_kb,
)


@pytest.mark.asyncio
async def test_owner_middleware_allows_owner():
    middleware = OwnerOnlyMiddleware(owner_id=12345)
    handler = AsyncMock(return_value="success")

    event = MagicMock()
    user = MagicMock()
    user.id = 12345
    data = {"event_from_user": user}

    result = await middleware(handler, event, data)
    assert result == "success"
    handler.assert_awaited_once_with(event, data)


@pytest.mark.asyncio
async def test_owner_middleware_drops_stranger():
    middleware = OwnerOnlyMiddleware(owner_id=12345)
    handler = AsyncMock(return_value="success")

    event = MagicMock()
    user = MagicMock()
    user.id = 99999  # Unauthorized ID
    data = {"event_from_user": user}

    result = await middleware(handler, event, data)
    assert result is None
    handler.assert_not_awaited()


@pytest.mark.asyncio
async def test_owner_middleware_auto_claim(tmp_path):
    temp_env = tmp_path / ".env"
    temp_env.write_text("ALLOWED_USER_ID=0\n")
    middleware = OwnerOnlyMiddleware(owner_id=0, env_path=temp_env)
    handler = AsyncMock(return_value="claimed")

    msg = MagicMock(spec=Message)
    msg.answer = AsyncMock()
    user = MagicMock()
    user.id = 55555
    user.full_name = "Javad Test"
    user.username = "javad"
    data = {"event_from_user": user}

    result = await middleware(handler, msg, data)
    assert result == "claimed"
    assert middleware.owner_id == 55555
    handler.assert_awaited_once_with(msg, data)
    msg.answer.assert_awaited_once()
    assert "ALLOWED_USER_ID=55555" in temp_env.read_text()


def test_keyboards_structure():
    kb_plan = approval_kb(1, "PLAN")
    assert len(kb_plan.inline_keyboard) == 1
    assert "Approve & Execute" in kb_plan.inline_keyboard[0][0].text

    kb_exec = approval_kb(1, "EXECUTE")
    assert len(kb_exec.inline_keyboard) == 1
    assert "Switch back to Plan Mode" in kb_exec.inline_keyboard[0][0].text

    kb_running = task_running_kb(1)
    assert len(kb_running.inline_keyboard[0]) == 2

    kb_proj = project_list_kb("agmux")
    assert len(kb_proj.inline_keyboard) >= 1

    kb_sess = session_list_kb([{"id": 1, "title": "Test", "mode": "PLAN"}], 1)
    assert len(kb_sess.inline_keyboard) == 1

    reply_kb = main_reply_keyboard()
    assert len(reply_kb.keyboard) == 3
    assert any("Projects" in btn.text for btn in reply_kb.keyboard[0])
    assert any("Sessions" in btn.text for btn in reply_kb.keyboard[0])
    assert any("Status" in btn.text for btn in reply_kb.keyboard[1])

    dash_kb = dashboard_inline_kb()
    assert len(dash_kb.inline_keyboard) == 3
    assert dash_kb.inline_keyboard[0][0].callback_data == "nav_projects"

