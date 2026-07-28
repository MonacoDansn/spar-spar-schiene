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


if __name__ == "__main__":
    unittest.main()
