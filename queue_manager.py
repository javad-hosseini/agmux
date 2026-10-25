import asyncio
from typing import Dict, List, Optional, Tuple


class QueueManager:
    def __init__(self):
        self._locks: Dict[int, asyncio.Lock] = {}
        self._queues: Dict[int, asyncio.Queue[Tuple[str, int]]] = {}
        self._btw_notes: Dict[int, List[str]] = {}

    def get_lock(self, session_id: int) -> asyncio.Lock:
        if session_id not in self._locks:
            self._locks[session_id] = asyncio.Lock()
        return self._locks[session_id]

    def is_running(self, session_id: int) -> bool:
        lock = self._locks.get(session_id)
        return lock.locked() if lock else False

    def get_queue(self, session_id: int) -> asyncio.Queue[Tuple[str, int]]:
        if session_id not in self._queues:
            self._queues[session_id] = asyncio.Queue()
        return self._queues[session_id]

    def queue_size(self, session_id: int) -> int:
        q = self._queues.get(session_id)
        return q.qsize() if q else 0

    async def enqueue(self, session_id: int, prompt: str, message_id: int) -> int:
        q = self.get_queue(session_id)
        await q.put((prompt, message_id))
        return q.qsize()

    def clear_queue(self, session_id: int) -> int:
        q = self._queues.get(session_id)
        if not q:
            return 0
        count = 0
        while not q.empty():
            try:
                q.get_nowait()
                q.task_done()
                count += 1
            except (asyncio.QueueEmpty, ValueError):
                break
        return count

    def add_btw_note(self, session_id: int, note: str) -> None:
        if session_id not in self._btw_notes:
            self._btw_notes[session_id] = []
        self._btw_notes[session_id].append(note)

    def pop_btw_notes(self, session_id: int) -> List[str]:
        notes = self._btw_notes.get(session_id, [])
        self._btw_notes[session_id] = []
        return notes

    def peek_btw_notes(self, session_id: int) -> List[str]:
        return list(self._btw_notes.get(session_id, []))


# Global queue manager singleton
queue_mgr = QueueManager()

