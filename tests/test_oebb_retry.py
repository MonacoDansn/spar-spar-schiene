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

    def test_parallele_429_drosseln_nur_einmal(self):
        """10 Worker melden gleichzeitig dasselbe Limit -> EIN Bremsschritt.
        Ohne Cooldown kollabierte die Rate sofort auf den Deckel (App war
        dadurch unbenutzbar langsam)."""
        client = ScriptedClient([])
        start = client._min_interval
        with mock.patch.object(oebb.time, "time", return_value=1000.0):
            for _ in range(10):
                client._slow_down()
        self.assertEqual(client._min_interval, start * 2)

    def test_erneutes_429_nach_cooldown_bremst_weiter(self):
        client = ScriptedClient([])
        start = client._min_interval
        with mock.patch.object(oebb.time, "time", return_value=1000.0):
            client._slow_down()
        with mock.patch.object(oebb.time, "time", return_value=1000.0 + oebb.THROTTLE_COOLDOWN + 1):
            client._slow_down()
        self.assertEqual(client._min_interval, start * 4)

    def test_erholung_ist_zuegig_nach_ruhiger_phase(self):
        """Nach RECOVER_AFTER Sekunden ohne 429 halbiert sich die Wartezeit
        schrittweise - frueher brauchte das ~183 erfolgreiche Anfragen."""
        client = ScriptedClient([])
        client._min_interval = client._base_interval = 0.125  # wie App: 8 Anfragen/s
        t = 1000.0
        for _ in range(20):  # bis zum Deckel bremsen (mit Cooldown)
            t += oebb.THROTTLE_COOLDOWN + 1
            with mock.patch.object(oebb.time, "time", return_value=t):
                client._slow_down()
        self.assertEqual(client._min_interval, 5.0)

        t += oebb.RECOVER_AFTER + 1
        schritte = 0
        while client._min_interval > client._base_interval and schritte < 30:
            t += oebb.RECOVER_STEP_EVERY + 0.1
            with mock.patch.object(oebb.time, "time", return_value=t):
                client._recover_speed()
            schritte += 1
        self.assertEqual(client._min_interval, client._base_interval)
        self.assertLessEqual(schritte, 7)  # 5.0 -> 0.125 = 6 Halbierungen

    def test_keine_erholung_direkt_nach_drosselung(self):
        client = ScriptedClient([])
        with mock.patch.object(oebb.time, "time", return_value=1000.0):
            client._slow_down()
            gebremst = client._min_interval
            client._recover_speed()
        self.assertEqual(client._min_interval, gebremst)

    def test_bremse_kann_weit_unter_2_rps(self):
        # Der alte Deckel bei 0.5s (= 2 Anfragen/s) war zu hoch: bei strenger
        # OeBB-Drosselung konnte der Client nie genug abbremsen (Debug e1b6a34b02f4).
        client = ScriptedClient([])
        for i in range(20):
            with mock.patch.object(oebb.time, "time",
                                   return_value=1000.0 + i * (oebb.THROTTLE_COOLDOWN + 1)):
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
