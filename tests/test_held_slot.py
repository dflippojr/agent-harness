"""A hosted run's backend slot (#524), as the hosted half holds it (`harness/hosted_runs.py`, #991): given up while
the run waits on a person, taken back after a wait that returned, left released after one that raised."""
from __future__ import annotations

import asyncio

from harness.hosted_runs import _HeldSlot


def test_the_slot_is_taken_back_after_a_wait_that_returned():
    async def body():
        sem = asyncio.Semaphore(1)
        held = _HeldSlot(sem)
        async with held:
            async with held.released():
                assert not held.held and not sem.locked()  # another session can run meanwhile
            assert held.held and sem.locked()
        assert not sem.locked()
    asyncio.run(body())


def test_a_wait_that_raised_leaves_the_slot_released_once():
    async def body():
        sem = asyncio.Semaphore(1)
        held = _HeldSlot(sem)
        try:
            async with held:
                async with held.released():
                    raise TimeoutError("nobody decided in time")
        except TimeoutError:
            pass
        assert not held.held and not sem.locked()
        assert sem._value == 1  # the exit did not release it a second time
    asyncio.run(body())


def test_taking_the_slot_back_waits_its_turn_and_says_so():
    async def body():
        sem = asyncio.Semaphore(1)
        held = _HeldSlot(sem)
        waits = []

        async def on_wait():  # the slot is taken: the run shows queued, and the other run gives it up
            waits.append("queued")
            sem.release()
        async with held:
            async with held.released(on_wait):
                await sem.acquire()  # another session takes the slot meanwhile
            assert waits == ["queued"] and held.held
    asyncio.run(body())
