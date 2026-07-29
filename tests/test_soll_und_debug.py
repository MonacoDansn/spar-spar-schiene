import unittest

import scanner
import server


SOLL = [
    {"trains": ["RJ 551"], "dep": "d1", "arr": "a1", "price": 59.9},
    {"trains": ["IC 547"], "dep": "d2", "arr": "a2", "price": 66.4},
    {"trains": ["S3 3", "RJX 19975"], "dep": "d3", "arr": "a3", "price": 66.4},
]


class TestSollFilter(unittest.TestCase):
    def test_leere_auswahl_bedeutet_alle(self):
        self.assertEqual(scanner.filter_soll(SOLL, []), SOLL)
        self.assertEqual(scanner.filter_soll(SOLL, None), SOLL)

    def test_auswahl_filtert_nach_zugfolge(self):
        out = scanner.filter_soll(SOLL, ["RJ 551", "S3 3+RJX 19975"])
        self.assertEqual([s["trains"] for s in out], [["RJ 551"], ["S3 3", "RJX 19975"]])

    def test_unbekannte_auswahl_ergibt_leer(self):
        self.assertEqual(scanner.filter_soll(SOLL, ["ICE 999"]), [])


class TestDebugStore(unittest.TestCase):
    def test_speichern_und_abrufen(self):
        rid = server.store_debug_report("Testbericht 1")
        self.assertEqual(len(rid), 12)
        self.assertEqual(server.get_debug_report(rid), "Testbericht 1")

    def test_unbekannte_kennung(self):
        self.assertIsNone(server.get_debug_report("gibtsnicht123"))

    def test_maximal_20_berichte(self):
        ids = [server.store_debug_report(f"Bericht {i}") for i in range(25)]
        self.assertIsNone(server.get_debug_report(ids[0]))   # aeltester verdraengt
        self.assertIsNotNone(server.get_debug_report(ids[-1]))

    def test_grosse_berichte_werden_gekuerzt(self):
        rid = server.store_debug_report("x" * 200_000)
        self.assertLessEqual(len(server.get_debug_report(rid)), 100_000)


if __name__ == "__main__":
    unittest.main()
