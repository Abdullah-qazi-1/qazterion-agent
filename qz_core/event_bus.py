"""Central event bus for Qazterion autonomous engine and CLI."""
from __future__ import annotations

import inspect
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional
from qz_storage import get_storage

EventHandler = Callable[[str, str, Dict[str, Any], str], Any]


class EventBus:
    def __init__(self) -> None:
        self._listeners: Dict[str, List[EventHandler]] = {}
        self._global_listeners: List[EventHandler] = []

    def subscribe(self, event_type: str, handler: EventHandler) -> None:
        """Subscribe to a specific event type or '*' for all events."""
        if event_type == "*":
            if handler not in self._global_listeners:
                self._global_listeners.append(handler)
        else:
            self._listeners.setdefault(event_type, [])
            if handler not in self._listeners[event_type]:
                self._listeners[event_type].append(handler)

    def unsubscribe(self, handler: EventHandler) -> None:
        """Unsubscribe handler from all events."""
        if handler in self._global_listeners:
            self._global_listeners.remove(handler)
        for handlers in self._listeners.values():
            if handler in handlers:
                handlers.remove(handler)

    def emit(self, event_type: str, task_id: str, payload: Optional[Dict[str, Any]] = None) -> None:
        """Emit an event to all subscribers and persist to storage."""
        now = datetime.now(timezone.utc).isoformat()
        data = payload or {}

        # 1. Persist to storage
        try:
            get_storage().log_event(task_id, event_type, data)
        except Exception:
            pass

        # 2. Dispatch to specific listeners
        handlers = list(self._listeners.get(event_type, [])) + list(self._global_listeners)
        for h in handlers:
            try:
                res = h(event_type, task_id, data, now)
                if inspect.isawaitable(res):
                    # For sync CLI loops, handle awaitables safely
                    import asyncio
                    try:
                        loop = asyncio.get_event_loop()
                        if loop.is_running():
                            asyncio.create_task(res)
                        else:
                            loop.run_until_complete(res)
                    except Exception:
                        pass
            except Exception:
                pass


_BUS: Optional[EventBus] = None


def get_event_bus() -> EventBus:
    global _BUS
    if _BUS is None:
        _BUS = EventBus()
    return _BUS
