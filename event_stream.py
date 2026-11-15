"""Cursor-based event batches shared by independent browser tabs.

Responses finish immediately. EventSource reconnects using Last-Event-ID,
so an idle browser does not reserve a Flask/Gunicorn thread.
"""

from collections import deque
import json
import threading


def event_cursor(value, latest):
    try:
        return max(0, min(int(value), latest)) if value is not None else latest
    except (TypeError, ValueError):
        return latest


def encode_event_batch(records, cursor):
    # A named cursor event also records an initial/recovered cursor when the
    # batch is empty. Native reconnects retain it without custom JS timers.
    chunks = [f'retry: 2000\nid: {cursor}\nevent: cursor\ndata: {{}}\n\n']
    for event_id, payload in records:
        if not isinstance(payload, str):
            payload = json.dumps(payload, ensure_ascii=False)
        chunks.append(f'id: {event_id}\ndata: {payload}\n\n')
    return ''.join(chunks)


class EventHistory:
    def __init__(self, capacity=1000):
        self._events = deque(maxlen=capacity)
        self._latest = 0
        self._lock = threading.Lock()

    def publish(self, event):
        with self._lock:
            self._latest += 1
            self._events.append((self._latest, event))

    def read(self, after=None, limit=50):
        with self._lock:
            cursor = event_cursor(after, self._latest)
            return cursor, [(key, event) for key, event in self._events if key > cursor][:limit]
