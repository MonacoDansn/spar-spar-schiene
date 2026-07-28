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


if __name__ == "__main__":
    unittest.main()
