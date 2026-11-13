import io
import unittest

import requests

from tgcompress.errors import BotError
from tgcompress.telegram_api import BotApi, UploadMeter


class Response:
    def __init__(self, payload, status_code: int = 200, headers: dict | None = None):
        self.payload = payload
        self.status_code = status_code
        self.headers = headers or {}

    def json(self):
        return self.payload


class FakeHttp:
    def __init__(self) -> None:
        self.captured = None
        self.post_calls = 0
        self.get_calls = 0

    def post(self, url, data=None, files=None, timeout=None):
        self.captured = data
        self.post_calls += 1
        return Response({"ok": True, "result": {}})

    def get(self, url, params=None, timeout=None):
        self.get_calls += 1
        return Response({"ok": True, "result": []})


class ApiTest(unittest.TestCase):
    def test_persistent_session_created_by_default(self):
        api = BotApi("token", "https://api.telegram.org")
        self.assertIsInstance(api.http, requests.Session)
        api.close()

    def test_form_encoding(self):
        http = FakeHttp()
        api = BotApi("token", "https://api.telegram.org", http=http)
        api.call(
            "sendMessage",
            chat_id=1,
            text="hi",
            reply_parameters='{"message_id": 2}',
            drop_pending_updates=False,
        )
        self.assertEqual(http.captured["drop_pending_updates"], "false")
        self.assertEqual(http.captured["reply_parameters"], '{"message_id": 2}')

    def test_telegram_error_becomes_bot_error(self):
        class FailingHttp(FakeHttp):
            def post(self, url, data=None, files=None, timeout=None):
                return Response({"ok": False, "description": "Bad Request: nope"})

        api = BotApi("token", "https://api.telegram.org", http=FailingHttp())
        with self.assertRaises(BotError) as caught:
            api.call("sendMessage", chat_id=1, text="hi")
        self.assertIn("Bad Request: nope", str(caught.exception))

    def test_http_429_retries_with_parameters_retry_after(self):
        responses = [
            Response(
                {
                    "ok": False,
                    "error_code": 429,
                    "description": "Too Many Requests: retry after 4",
                    "parameters": {"retry_after": 4},
                },
                status_code=429,
            ),
            Response({"ok": True, "result": {"message_id": 42}}),
        ]
        sleeps = []

        class RateLimitedHttp:
            def __init__(self):
                self.calls = 0

            def post(self, url, data=None, files=None, timeout=None):
                res = responses[self.calls]
                self.calls += 1
                return res

        api = BotApi(
            "token",
            "https://api.telegram.org",
            http=RateLimitedHttp(),
            sleep_fn=sleeps.append,
        )
        res = api.call("sendMessage", chat_id=1, text="hello")
        self.assertEqual(res, {"message_id": 42})
        self.assertEqual(sleeps, [4])

    def test_http_429_exhaustion_raises_bot_error(self):
        sleeps = []

        class Always429Http:
            def __init__(self):
                self.calls = 0

            def post(self, url, data=None, files=None, timeout=None):
                self.calls += 1
                return Response(
                    {
                        "ok": False,
                        "error_code": 429,
                        "description": "Too Many Requests",
                        "parameters": {"retry_after": 2},
                    },
                    status_code=429,
                )

        http = Always429Http()
        api = BotApi(
            "token",
            "https://api.telegram.org",
            http=http,
            sleep_fn=sleeps.append,
            max_retries=3,
        )
        with self.assertRaises(BotError) as caught:
            api.call("sendMessage", chat_id=1, text="hello")
        self.assertIn("Too Many Requests", str(caught.exception))
        self.assertEqual(http.calls, 4)  # 1 initial + 3 retries
        self.assertEqual(sleeps, [2, 2, 2])

    def test_rewinds_files_on_retry(self):
        stream = io.BytesIO(b"file content")
        stream.seek(5)  # simulate partially read stream

        responses = [
            Response(
                {
                    "ok": False,
                    "error_code": 429,
                    "parameters": {"retry_after": 1},
                },
                status_code=429,
            ),
            Response({"ok": True, "result": {"ok": True}}),
        ]

        class FileRetryingHttp:
            def __init__(self):
                self.calls = 0

            def post(self, url, data=None, files=None, timeout=None):
                self.calls += 1
                return responses[self.calls - 1]

        api = BotApi(
            "token",
            "https://api.telegram.org",
            http=FileRetryingHttp(),
            sleep_fn=lambda _: None,
        )
        api.call(
            "sendDocument",
            files={"document": ("test.mp4", stream, "video/mp4")},
        )
        self.assertEqual(stream.tell(), 0)

    def test_get_updates_retries_on_429(self):
        responses = [
            Response(
                {"ok": False, "parameters": {"retry_after": 3}},
                status_code=429,
            ),
            Response({"ok": True, "result": [{"update_id": 100}]}),
        ]
        sleeps = []

        class Updates429Http:
            def __init__(self):
                self.calls = 0

            def get(self, url, params=None, timeout=None):
                res = responses[self.calls]
                self.calls += 1
                return res

        api = BotApi(
            "token",
            "https://api.telegram.org",
            http=Updates429Http(),
            sleep_fn=sleeps.append,
        )
        updates = api.get_updates(offset=None)
        self.assertEqual(updates, [{"update_id": 100}])
        self.assertEqual(sleeps, [3])


class UploadMeterTest(unittest.TestCase):
    def test_reports_sent_percent(self):
        percents = []
        meter = UploadMeter(io.BytesIO(b"x" * 1000), 1000, percents.append)
        meter.read(300)
        meter.read(300)
        meter.read(400)
        self.assertIn(30, percents)
        self.assertIn(60, percents)
        self.assertEqual(percents[-1], 100)


if __name__ == "__main__":
    unittest.main()
