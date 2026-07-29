import time
import unittest
from unittest import mock

import oebb


class ScriptedClient(oebb.OebbClient):
    """Testclient: _raw liefert ein Drehbuch aus (status, body, headers)-Tupeln."""

    def __init__(self, script):
        super().__init__(max_rps=1000)
        self.script = list(script)
        self._local.token = "test-token"
        self._local.token_time = time.time()

    def _raw(self, method, pathq, headers, body, timeout=15):
        return self.script.pop(0)

    def _throttle(self):
        pass


class TestRateLimitRetry(unittest.TestCase):
    def test_429_wird_geduldig_wiederholt_und_retry_after_respektiert(self):
        client = ScriptedClient([
            (429, b"", {"Retry-After": "7"}),
            (429, b"", {}),
            (200, b'{"ok": true}', {}),
        ])
        with mock.patch.object(oebb.time, "sleep") as fake_sleep:
            result = client._request("GET", "/test")
        self.assertEqual(result, {"ok": True})
        waits = [c.args[0] for c in fake_sleep.call_args_list]
        self.assertIn(7.0, waits)  # Retry-After-Header der OeBB wird respektiert

    def test_bremse_kann_weit_unter_2_rps(self):
        # Der alte Deckel bei 0.5s (= 2 Anfragen/s) war zu hoch: bei strenger
        # OeBB-Drosselung konnte der Client nie genug abbremsen (Debug e1b6a34b02f4).
        client = ScriptedClient([])
        for _ in range(20):
            client._slow_down()
        self.assertEqual(client._min_interval, 5.0)

    def test_429_drosselt_das_tempo(self):
        client = ScriptedClient([
            (429, b"", {}),
            (200, b'{"ok": true}', {}),
        ])
        before = client._min_interval
        with mock.patch.object(oebb.time, "sleep"):
            client._request("GET", "/test")
        self.assertGreater(client._min_interval, before)

    def test_429_dauerhaft_bricht_mit_klarer_meldung_ab(self):
        client = ScriptedClient([(429, b"", {})] * 10)
        with mock.patch.object(oebb.time, "sleep"):
            with self.assertRaises(oebb.OebbError) as ctx:
                client._request("GET", "/test")
        self.assertIn("429", str(ctx.exception))

    def test_5xx_weiterhin_begrenzte_wiederholung(self):
        client = ScriptedClient([
            (503, b"", {}),
            (200, b'{"ok": true}', {}),
        ])
        with mock.patch.object(oebb.time, "sleep"):
            result = client._request("GET", "/test")
        self.assertEqual(result, {"ok": True})


if __name__ == "__main__":
    unittest.main()
