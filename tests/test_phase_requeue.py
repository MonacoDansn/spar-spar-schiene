import unittest
from unittest import mock

import oebb
import scanner
from scanner import ScanJob


def _job():
    return ScanJob({"maxRps": 1000, "workers": 2})


class TestRateLimitRequeue(unittest.TestCase):
    """429-gedrosselte Kandidaten werden zurueckgestellt und nach einer Pause
    erneut versucht statt verworfen (Ursache: Debug-Bericht e1b6a34b02f4)."""

    def test_429_kandidat_bekommt_zweiten_versuch(self):
        job = _job()
        calls = {}

        def worker(item):
            name = item["name"]
            calls[name] = calls.get(name, 0) + 1
            if name == "b" and calls[name] == 1:
                raise oebb.OebbError("OeBB-Server ueberlastet (HTTP 429) bei /x")
            return []

        items = [{"name": n} for n in ("a", "b", "c")]
        with mock.patch.object(scanner.time, "sleep"):
            job._phase_scan("A", "Test", items, worker, None)

        self.assertEqual(calls["b"], 2)   # zurueckgestellt + zweiter Versuch
        self.assertEqual(calls["a"], 1)
        self.assertEqual(job.stats["throttled"], 0)
        # Der Wiederholungsversuch zaehlt sichtbar mit (3 + 1 Wiederholung),
        # sonst wirkt der Fortschrittsbalken eingefroren
        self.assertEqual(job.phase_state["done"], 4)
        self.assertEqual(job.phase_state["total"], 4)

    def test_dauerhaft_gedrosselt_wird_ehrlich_gezaehlt(self):
        job = _job()

        def worker(item):
            raise oebb.OebbError("OeBB-Server ueberlastet (HTTP 429) bei /x")

        items = [{"name": n} for n in ("a", "b")]
        with mock.patch.object(scanner.time, "sleep"):
            job._phase_scan("A", "Test", items, worker, None)

        self.assertEqual(job.stats["throttled"], 2)
        logs = " | ".join(e["data"].get("msg", "") for e in job.events if e["type"] == "log")
        self.assertIn("ungeprüft", logs)
        self.assertIn("ungeprüft", job._stats_line())

    def test_andere_fehler_werden_nicht_zurueckgestellt(self):
        job = _job()
        calls = {}

        def worker(item):
            calls[item["name"]] = calls.get(item["name"], 0) + 1
            raise oebb.OebbError("nicht buchbar")

        with mock.patch.object(scanner.time, "sleep"):
            job._phase_scan("A", "Test", [{"name": "a"}], worker, None)

        self.assertEqual(calls["a"], 1)   # kein zweiter Versuch
        self.assertEqual(job.stats["throttled"], 0)


if __name__ == "__main__":
    unittest.main()
