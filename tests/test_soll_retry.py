import unittest
from unittest import mock

import oebb
import scanner


class FakeClient:
    """Liefert beim n-ten Aufruf Erfolg, davor OeBB-Ablehnungen."""

    def __init__(self, fehler_vorab):
        self.fehler_vorab = fehler_vorab
        self.aufrufe = 0

    def connection_search(self, *a, **kw):
        self.aufrufe += 1
        if self.aufrufe <= self.fehler_vorab:
            raise oebb.OebbError("OeBB-Server ueberlastet (HTTP 429) bei /hafas/v4/timetable")
        conn = {"id": "c1", "from": {"departure": "2026-08-13T08:00", "name": "A"},
                "to": {"arrival": "2026-08-13T10:00", "name": "B"},
                "sections": [{"type": "journey",
                              "category": {"displayName": "RJ", "number": "551"}}]}
        return [conn], {"c1": {"price": 29.9, "sparschiene": True}}


class TestSollRetry(unittest.TestCase):
    """Sporadische 429 duerfen den ersten Schritt nicht scheitern lassen."""

    def test_zweiter_versuch_rettet_das_laden(self):
        client = FakeClient(fehler_vorab=1)
        with mock.patch.object(scanner.time, "sleep"):
            soll = scanner.build_soll_list(client, {"id": 1, "name": "A"},
                                           {"id": 2, "name": "B"}, "2026-08-13T08:00")
        self.assertEqual(client.aufrufe, 2)
        self.assertEqual(soll[0]["trains"], ["RJ 551"])

    def test_dauerhafte_ablehnung_meldet_fehler(self):
        client = FakeClient(fehler_vorab=99)
        with mock.patch.object(scanner.time, "sleep"):
            with self.assertRaises(oebb.OebbError):
                scanner.build_soll_list(client, {"id": 1, "name": "A"},
                                        {"id": 2, "name": "B"}, "2026-08-13T08:00")
        self.assertEqual(client.aufrufe, 3)


if __name__ == "__main__":
    unittest.main()
