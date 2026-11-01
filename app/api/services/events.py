"""Bounded process-local broadcast. Events never write to SQLite."""

import asyncio
from collections import deque
from dataclasses import dataclass, field
from threading import Lock

from app.research.events import ProgressData, ProgressUpdate, ResearchEvent, make_event


@dataclass(eq=False)
class Subscription:
    run_id: str
    loop: asyncio.AbstractEventLoop
    wake: asyncio.Event = field(default_factory=asyncio.Event)
    cursor: int = 0
    notified: bool = False
    closed: bool = False


@dataclass
class Channel:
    buffer: deque
    sequence: int = 0
    active: bool = False
    tasks: dict = field(default_factory=dict)
    phases: dict = field(default_factory=dict)
    subscribers: set = field(default_factory=set)


class EventHub:
    def __init__(self, *, buffer_size=128, max_connections=64, redact=lambda value: value):
        self.buffer_size, self.max_connections, self.redact = buffer_size, max_connections, redact
        self._lock = Lock()
        self._channels: dict[str, Channel] = {}
        self._connections = 0

    def _channel(self, run_id):
        return self._channels.setdefault(run_id, Channel(buffer=deque(maxlen=self.buffer_size)))

    def start(self, run_id):
        with self._lock:
            channel = self._channel(run_id)
            channel.active = True
            channel.tasks.clear()
            channel.phases.clear()

    def finish(self, run_id):
        with self._lock:
            channel = self._channels.get(run_id)
            if channel:
                channel.active = False
                if not channel.subscribers:
                    del self._channels[run_id]

    def sink(self, run_id):
        return lambda update: self.publish(run_id, update)

    def event(self, run_id, update):
        event = make_event(run_id, update)
        return ResearchEvent.model_validate(self.redact(event.model_dump(mode="json")))

    def publish(self, run_id: str, update: ProgressUpdate):
        event = self.event(run_id, update)
        with self._lock:
            channel = self._channels.get(run_id)
            if channel is None:
                return  # No active worker or listeners: do not retain history.
            data = event.progress_data
            if event.task_id and data.task_status:
                if event.task_id in channel.tasks or len(channel.tasks) < 32:
                    channel.tasks[event.task_id] = data.task_status
            if data.phase and data.phase_status:
                channel.phases[data.phase] = data.phase_status
            for phase in data.phases:
                channel.phases[phase.phase] = phase.status
            channel.sequence += 1
            channel.buffer.append((channel.sequence, event))
            for sub in channel.subscribers:
                if not sub.closed and not sub.notified:
                    sub.notified = True
                    try:
                        sub.loop.call_soon_threadsafe(sub.wake.set)
                    except RuntimeError:
                        pass  # A closing HTTP loop must never fail research.

    def subscribe(self, run_id):
        with self._lock:
            if self._connections >= self.max_connections:
                raise OverflowError("SSE connection capacity exceeded.")
            channel = self._channel(run_id)
            sub = Subscription(run_id=run_id, loop=asyncio.get_running_loop(), cursor=channel.sequence)
            channel.subscribers.add(sub)
            self._connections += 1
            return sub

    def live(self, sub):
        with self._lock:
            channel = self._channels[sub.run_id]
            return ProgressData(tasks=[{"task_id": key, "status": value} for key, value in channel.tasks.items()],
                                phases=[{"phase": key, "status": value} for key, value in channel.phases.items()])

    def read(self, sub):
        with self._lock:
            channel = self._channels[sub.run_id]
            missed = bool(channel.buffer and sub.cursor < channel.buffer[0][0] - 1)
            events = [event for seq, event in channel.buffer if seq > sub.cursor]
            sub.cursor = channel.sequence
            sub.notified = False
            sub.wake.clear()
            return events, missed

    def unsubscribe(self, sub):
        with self._lock:
            if sub.closed:
                return
            sub.closed = True
            self._connections -= 1
            channel = self._channels.get(sub.run_id)
            if channel:
                channel.subscribers.discard(sub)
                if not channel.active and not channel.subscribers:
                    del self._channels[sub.run_id]
