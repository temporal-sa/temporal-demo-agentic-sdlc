"""Shared test setup.

Agents run on the scripted fake model (fake_model.py), installed before any test module can import
the agents. ``redis_url`` is a throwaway Redis for the demo machinery the web and worker share.
"""

import asyncio
import contextlib
import socket
import subprocess

import pytest
from redis.asyncio import Redis

import fake_model

fake_model.install()


@pytest.fixture
async def redis_url():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    server = subprocess.Popen(["redis-server", "--port", str(port), "--save", "", "--appendonly", "no"],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    url = f"redis://127.0.0.1:{port}"
    redis = Redis.from_url(url)
    for _ in range(50):
        with contextlib.suppress(Exception):
            if await redis.ping():
                break
        await asyncio.sleep(0.1)
    yield url
    await redis.aclose()
    server.terminate()
    server.wait(timeout=10)
