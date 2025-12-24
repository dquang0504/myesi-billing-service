import asyncio
from collections import deque
from dataclasses import dataclass
from typing import Any, Deque, Dict, Iterable, List, Optional

import httpx

import pytest
from fastapi.testclient import TestClient
from httpx import AsyncClient

from app.main import app
from app.db import session as db_session


@dataclass
class FakeRow:
    data: Dict[str, Any]

    def __getattr__(self, item):
        return self.data.get(item)


class FakeResult:
    def __init__(
        self,
        fetchone: Optional[Any] = None,
        fetchall: Optional[List[Any]] = None,
        scalar: Optional[Any] = None,
    ):
        self._fetchone = fetchone
        self._fetchall = fetchall or []
        self._scalar = scalar

    def fetchone(self):
        return self._fetchone

    def scalar(self):
        return self._scalar

    def fetchall(self):
        return self._fetchall

    def scalars(self):
        return self

    def first(self):
        return self._fetchone

    def all(self):
        return self._fetchall

    def scalar_one_or_none(self):
        return self._scalar if self._scalar is not None else self._fetchone


class FakeDB:
    def __init__(self):
        self.execute_results = []
        self.added = []
        self.commits = 0

    def queue_result(self, result):
        self.execute_results.append(result)

    async def execute(self, *args, **kwargs):
        if self.execute_results:
            return self.execute_results.pop(0)
        return FakeResult()

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        self.commits += 1

class FakeResult:
    def __init__(self, scalar=None, fetchone=None, rows=None):
        self._scalar = scalar
        self._fetchone = fetchone
        self._rows = rows or []

    def scalar(self):
        return self._scalar

    def fetchone(self):
        return self._fetchone

    def fetchall(self):
        return self._rows


@pytest.fixture
def fake_db():
    return FakeDB()


@pytest.fixture(autouse=True)
def override_db(fake_db):
    async def _fake_dependency():
        yield fake_db

    app.dependency_overrides[db_session.get_db] = _fake_dependency
    yield
    app.dependency_overrides.pop(db_session.get_db, None)


@pytest.fixture
def sync_client():
    return TestClient(app)


@pytest.fixture
async def async_client():
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield client
