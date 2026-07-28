import unittest

from scanner import EtaTracker


class TestEtaTracker(unittest.TestCase):
    def test_zu_wenige_messungen_ergibt_none(self):
        t = EtaTracker(min_samples=5)
        for ts in (1.0, 2.0, 3.0, 4.0):
            t.record(ts)
        self.assertIsNone(t.estimate(10))

    def test_konstante_rate(self):
        t = EtaTracker(min_samples=5)
        for ts in (0.0, 1.0, 2.0, 3.0, 4.0):  # 1 Item/s
            t.record(ts)
        self.assertAlmostEqual(t.estimate(10), 10.0, places=3)

    def test_fenster_begrenzt_alte_messungen(self):
        t = EtaTracker(window=5, min_samples=5)
        for ts in (0.0, 100.0, 101.0, 102.0, 103.0, 104.0):  # 0.0 faellt raus
            t.record(ts)
        self.assertAlmostEqual(t.estimate(4), 4.0, places=3)

    def test_nichts_mehr_offen_ergibt_none(self):
        t = EtaTracker(min_samples=2)
        t.record(1.0)
        t.record(2.0)
        self.assertIsNone(t.estimate(0))


from scanner import ScanJob


class TestPhaseState(unittest.TestCase):
    def test_emit_progress_setzt_phase_state_und_event(self):
        job = ScanJob({"maxRps": 1})
        job.pending_phases = {"B": 10}
        for ts in (0.0, 1.0, 2.0, 3.0, 4.0):
            job.eta_tracker.record(ts)
        job._emit_progress("A", "Einstiegs-Bahnhoefe testen", done=5, total=10, found=2)

        st = job.phase_state
        self.assertEqual(st["name"], "A")
        self.assertEqual(st["done"], 5)
        self.assertEqual(st["total"], 10)
        self.assertEqual(st["found"], 2)
        # 5 offen in A + 10 in B = 15 Items bei 1 Item/s
        self.assertEqual(st["eta"], 15)
        self.assertTrue(st["etaMin"])  # Phase C noch unbekannt

        ev = job.events[-1]
        self.assertEqual(ev["type"], "progress")
        self.assertEqual(ev["data"]["eta"], 15)
        self.assertTrue(ev["data"]["etaMin"])

    def test_eta_min_faellt_weg_sobald_c_bekannt(self):
        job = ScanJob({"maxRps": 1})
        job.c_known = True
        for ts in (0.0, 1.0, 2.0, 3.0, 4.0):
            job.eta_tracker.record(ts)
        job._emit_progress("C", "Kombinationen testen", done=1, total=3, found=0)
        self.assertFalse(job.phase_state["etaMin"])


import server


class TestSnapshot(unittest.TestCase):
    def _job(self):
        job = ScanJob({"maxRps": 1})
        job.phase_state = {"name": "A", "label": "x", "done": 1, "total": 2,
                           "found": 0, "eta": 30, "etaMin": True}
        job.results = {"k": {"price": 10.0}}
        return job

    def test_light_snapshot_ohne_results(self):
        snap = server.job_snapshot(self._job(), light=True)
        self.assertNotIn("results", snap)
        self.assertEqual(snap["resultCount"], 1)
        self.assertEqual(snap["phase"]["eta"], 30)
        self.assertFalse(snap["finished"])

    def test_voller_snapshot_mit_results_und_phase(self):
        snap = server.job_snapshot(self._job(), light=False)
        self.assertEqual(len(snap["results"]), 1)
        self.assertEqual(snap["phase"]["name"], "A")

    def test_snapshot_meldet_fehler_und_abbruch(self):
        job = self._job()
        snap = server.job_snapshot(job, light=True)
        self.assertIsNone(snap["error"])
        self.assertFalse(snap["cancelled"])

        job.error = "Die OeBB-API antwortet nicht mehr"
        job.cancelled = True
        snap = server.job_snapshot(job, light=True)
        self.assertEqual(snap["error"], "Die OeBB-API antwortet nicht mehr")
        self.assertTrue(snap["cancelled"])

    def test_run_safe_setzt_error(self):
        job = ScanJob({"maxRps": 1})
        job._run = lambda: (_ for _ in ()).throw(RuntimeError("kaputt"))
        job._run_safe()
        self.assertEqual(job.error, "kaputt")
        self.assertTrue(job.finished)


import importlib
import os


class TestEnvOverrides(unittest.TestCase):
    """Fuer die lokale Android-App: Daten-/Public-Pfade per Env uebersteuerbar."""

    def _reload_all(self):
        import scanner as sc
        import server as sv
        import stations as st
        importlib.reload(st)
        importlib.reload(sc)
        importlib.reload(sv)
        return st, sc, sv

    def test_env_overrides_wirken_und_defaults_bleiben(self):
        os.environ["SPAR_DATA_DIR"] = "/tmp/spar-data"
        os.environ["SPAR_PUBLIC_DIR"] = "/tmp/spar-public"
        try:
            st, sc, sv = self._reload_all()
            self.assertEqual(st.DATA_DIR, "/tmp/spar-data")
            self.assertEqual(sc.PLACES_CACHE_FILE,
                             os.path.join("/tmp/spar-data", "places_cache.json"))
            self.assertEqual(sc.BUS_CACHE_FILE,
                             os.path.join("/tmp/spar-data", "bus_cache.json"))
            self.assertEqual(sv.PUBLIC, "/tmp/spar-public")
        finally:
            del os.environ["SPAR_DATA_DIR"]
            del os.environ["SPAR_PUBLIC_DIR"]
            st, sc, sv = self._reload_all()
        self.assertTrue(st.DATA_DIR.endswith("data"))
        self.assertTrue(sv.PUBLIC.endswith("public"))


if __name__ == "__main__":
    unittest.main()
