import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from fastapi import FastAPI
from fastapi.testclient import TestClient
from services.public_website_stats import PublicStatsCache, build_router, collect_counts


class FakeSource:
    def __init__(self, rows, cap=100, fail_at=None):
        self.rows, self.cap, self.fail_at = rows, cap, fail_at
        self.requests = []
    def table(self, name):
        assert name == "users"
        return self
    def select(self, columns):
        assert columns == "total_questions"  # No user identifiers or personal data.
        return self
    def lte(self, key, value):
        assert key == "joined_at"
        return self
    def order(self, key):
        assert key == "user_id"
        return self
    def range(self, start, end):
        self.start, self.end = start, end
        return self
    async def execute(self):
        self.requests.append((self.start, self.end))
        if self.fail_at is not None and self.start >= self.fail_at:
            raise RuntimeError("private provider error")
        return SimpleNamespace(data=self.rows[self.start:min(self.end+1, self.start+self.cap)])


class AggregateTests(unittest.IsolatedAsyncioTestCase):
    async def test_more_than_one_thousand_users_and_smaller_server_page(self):
        source = FakeSource([{"total_questions": 7}] * 1251)
        self.assertEqual(await collect_counts(source, datetime.now(timezone.utc)),
                         {"students": 1251, "questions": 8757})
        self.assertEqual(source.requests[-1][0], 1251)

    async def test_partial_failure_keeps_the_previous_complete_snapshot(self):
        cache = PublicStatsCache()
        cache.publish({"students": 20, "questions": 500}, datetime.now(timezone.utc))
        before = cache.read()
        source = FakeSource([{"total_questions": 7}] * 250, fail_at=100)
        with self.assertRaises(RuntimeError):
            await cache.refresh(source)
        self.assertEqual(cache.read(), before)

    async def test_null_is_zero_invalid_counter_never_published(self):
        now = datetime.now(timezone.utc)
        self.assertEqual(await collect_counts(FakeSource([{"total_questions": None}]), now),
                         {"students": 1, "questions": 0})
        for value in (-1, True, "5", 2.5):
            with self.assertRaises(ValueError):
                await collect_counts(FakeSource([{"total_questions": value}]), now)


class PublicRouteTests(unittest.TestCase):
    def setUp(self):
        self.cache = PublicStatsCache()
        app = FastAPI()
        app.include_router(build_router(self.cache, True))
        self.client = TestClient(app)

    def test_response_is_projected_and_cors_is_exact(self):
        self.cache.publish({"students": 800, "questions": 16000, "secret": "must not leak"}, datetime.now(timezone.utc))
        response = self.client.get("/api/public-stats", headers={"Origin": "https://abiad.me"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(set(response.json()), {"students", "questions", "updated_at"})
        self.assertEqual(response.headers["access-control-allow-origin"], "https://abiad.me")
        self.assertNotIn("access-control-allow-credentials", response.headers)
        hostile = self.client.get("/api/public-stats", headers={"Origin": "https://abiad.me.evil.example"})
        self.assertEqual(hostile.status_code, 200)  # Public data, independent of CORS.
        self.assertNotIn("access-control-allow-origin", hostile.headers)

    def test_unavailable_expired_or_disabled_never_claims_zero(self):
        self.assertEqual(self.client.get("/api/public-stats").status_code, 503)
        self.cache.publish({"students": 4, "questions": 50}, datetime.now(timezone.utc)-timedelta(days=2))
        self.assertEqual(self.client.get("/api/public-stats").status_code, 503)
        app = FastAPI()
        app.include_router(build_router(self.cache, False))
        self.assertEqual(TestClient(app).get("/api/public-stats").status_code, 404)

    def test_cannot_filter_write_or_refresh_from_http(self):
        self.cache.publish({"students": 4, "questions": 50}, datetime.now(timezone.utc))
        self.cache.refresh = AsyncMock(side_effect=AssertionError("HTTP triggered database query"))
        for _ in range(100):
            self.assertEqual(self.client.get("/api/public-stats").status_code, 200)
        self.cache.refresh.assert_not_called()
        self.assertEqual(self.client.get("/api/public-stats?user_id=1").status_code, 400)
        self.assertEqual(self.client.get("/api/public-stats?refresh=true").status_code, 400)
        self.assertEqual(self.client.post("/api/public-stats", json={"students": 0}).status_code, 405)


if __name__ == "__main__":
    unittest.main()
