from __future__ import annotations

import asyncio

from app.realtime.broker import EventBroker


def test_slow_subscriber_receives_resync_marker_without_blocking_publisher() -> None:
    async def scenario() -> None:
        broker = EventBroker(queue_size=1, history_size=4)
        async with broker.subscribe() as queue:
            first = await broker.publish("session.activated")
            second = await broker.publish("safety.check_in_requested")

            assert first.sequence == 1
            assert second.sequence == 2
            marker = queue.get_nowait()
            assert marker.type == "stream.resync_required"
            assert marker.sequence == 2
            assert marker.data == {"latest_sequence": 2}

        assert await broker.current_sequence() == 2
        assert [event.type for event in await broker.recent()] == [
            "session.activated",
            "safety.check_in_requested",
        ]

    asyncio.run(scenario())
