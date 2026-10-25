import pytest
from queue_manager import QueueManager


@pytest.mark.asyncio
async def test_queue_lifecycle():
    qm = QueueManager()
    session_id = 42

    assert not qm.is_running(session_id)
    assert qm.queue_size(session_id) == 0

    # Add items to queue
    len1 = await qm.enqueue(session_id, "first prompt", 101)
    len2 = await qm.enqueue(session_id, "second prompt", 102)
    assert len1 == 1
    assert len2 == 2
    assert qm.queue_size(session_id) == 2

    # Pop item
    q = qm.get_queue(session_id)
    p1, m1 = await q.get()
    assert p1 == "first prompt"
    assert m1 == 101
    assert qm.queue_size(session_id) == 1

    # Clear remaining
    cleared = qm.clear_queue(session_id)
    assert cleared == 1
    assert qm.queue_size(session_id) == 0


def test_btw_notes():
    qm = QueueManager()
    sid = 7

    qm.add_btw_note(sid, "Remember to check docs")
    qm.add_btw_note(sid, "Don't touch db.py")

    assert len(qm.peek_btw_notes(sid)) == 2

    popped = qm.pop_btw_notes(sid)
    assert popped == ["Remember to check docs", "Don't touch db.py"]
    assert len(qm.peek_btw_notes(sid)) == 0

