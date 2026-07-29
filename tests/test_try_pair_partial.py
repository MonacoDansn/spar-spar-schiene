import unittest

import oebb
import scanner
from scanner import ScanJob

SOLL = [{"trains": ["RJ 551"], "dep": "2026-08-13T08:00", "arr": "2026-08-13T10:00",
         "price": 40.0}]
STATION_A = {"id": 1, "name": "A", "lat": 47.8, "lon": 13.0}
STATION_B = {"id": 2, "name": "B", "lat": 48.1, "lon": 11.5}


def _conn(cid):
    return {"id": cid,
            "from": {"departure": "2026-08-13T07:40", "name": "A"},
            "to": {"arrival": "2026-08-13T10:00", "name": "B"},
            "sections": [{"type": "journey",
                          "category": {"displayName": "RJ", "number": "551"}}]}


class TestTryPairTeilerfolg(unittest.TestCase):
    """Sporadische Ablehnungen duerfen bereits gefundene Treffer nicht entwerten -
    sonst fehlt der Bahnhof als Kandidat in Phase C (gemessen: 'Treffer 0' trotz
    dutzender Ergebnisse)."""

    def _job(self):
        job = ScanJob({"maxRps": 1000})
        job.skip_travel_action = True
        return job

    def test_treffer_bleiben_wenn_ein_anker_scheitert(self):
        job = self._job()
        aufrufe = {"n": 0}

        def fake_search(from_st, to_st, dep=None, arr=None):
            aufrufe["n"] += 1
            if aufrufe["n"] == 2:
                raise oebb.OebbError("OeBB-Server ueberlastet (HTTP 429) bei /x")
            return [_conn("c1")]

        job._search = fake_search
        job._match_and_price = lambda conns, soll: [
            (c, SOLL[0], {"price": 25.0, "sparschiene": True, "error": False,
                          "reduced": False, "reducedScope": None}) for c in conns]

        hits = job._try_pair(STATION_A, STATION_B, soll_list=SOLL, phase="A",
                             arrs=["2026-08-13T10:01:00.000", "2026-08-13T09:01:00.000"])
        self.assertEqual(len(hits), 1)            # Treffer des ersten Ankers erhalten
        self.assertEqual(job.stats["partial"], 1)  # ehrlich als teilweise vermerkt
        self.assertEqual(len(job.results), 1)

    def test_alle_anker_gescheitert_wirft(self):
        job = self._job()

        def fake_search(from_st, to_st, dep=None, arr=None):
            raise oebb.OebbError("OeBB-Server ueberlastet (HTTP 429) bei /x")

        job._search = fake_search
        with self.assertRaises(oebb.OebbError):
            job._try_pair(STATION_A, STATION_B, soll_list=SOLL, phase="A",
                          arrs=["2026-08-13T10:01:00.000"])
        self.assertEqual(job.stats["partial"], 0)


if __name__ == "__main__":
    unittest.main()
