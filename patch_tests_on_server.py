
from pathlib import Path
src = Path("test_person_unified.py").read_text(encoding="utf-8")
needle = 'self.assertIn("Отвязать", body)\n        listing = self.client.get("/api/people").get_json()'
insert = (
    'self.assertIn("Отвязать", body)\n'
    '        self.assertIn("sheet-backdrop", body)\n'
    "        self.assertIn('id=\"search\"', body)\n"
    '        listing = self.client.get("/api/people").get_json()'
)
if needle not in src:
    raise SystemExit("needle1 missing")
src = src.replace(needle, insert, 1)
needle2 = (
    'self.assertEqual(names[0], "Антонина Саганова")\n'
    '        self.assertIn("Юлия Головина бухгалтер", names)'
)
insert2 = (
    'self.assertEqual(names[0], "Антонина Саганова")\n'
    '        antonina = next(p for p in listing["people"] if p["slug"] == "antonina-saganova")\n'
    '        self.assertTrue(antonina.get("last_message_at"))\n'
    '        self.assertIn("Юлия Головина бухгалтер", names)'
)
if needle2 not in src:
    raise SystemExit("needle2 missing: " + repr(src[src.find("names[0]"):src.find("names[0]")+200]))
src = src.replace(needle2, insert2, 1)
extra = """

class SearchMatchTests(unittest.TestCase):
    def test_translit_antonina(self):
        self.assertTrue(person_unified.search_match("Antonina", "Антонина Саганова"))
        self.assertTrue(person_unified.search_match("antonina", "Антонина Саганова", "antonina-saganova"))
        self.assertTrue(person_unified.search_match("Саганова", "Антонина Саганова"))
        self.assertTrue(person_unified.search_match("saganova", "Антонина Саганова"))
        self.assertTrue(person_unified.search_match("Алёна", "Алена Менеджер"))
        self.assertTrue(person_unified.search_match("alena", "Алёна Менеджер"))
        self.assertFalse(person_unified.search_match("xyzzy", "Антонина Саганова"))

    def test_normalize_strips_punct(self):
        self.assertEqual(
            person_unified.normalize_search("Антонина, Саганова!"),
            person_unified.normalize_search("антонина саганова"),
        )


class ListPilotRecencyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "archive.db"
        build_db(self.path)
        self.con = connect(self.path)
        self.allow = person_unified.load_allowlist()
        add_msg(self.con, 306905419, 1, "2025-07-08T10:00:00+00:00", "старое")
        add_msg(self.con, 1073347845, 1, "2026-09-20T15:00:00+00:00", "свежее")
        self.con.commit()

    def tearDown(self):
        self.con.close()
        self.tmp.cleanup()

    def test_sorted_by_last_message_at(self):
        listing = person_unified.list_pilot(self.con, self.allow)
        people = listing["people"]
        with_date = [p for p in people if p.get("last_message_at")]
        self.assertGreaterEqual(len(with_date), 2)
        self.assertEqual(with_date[0]["slug"], "kirill-svinarev")
        self.assertEqual(with_date[1]["slug"], "antonina-saganova")
        self.assertTrue(with_date[0]["last_message_at"] >= with_date[1]["last_message_at"])
        nulls = [p for p in people if not p.get("last_message_at")]
        self.assertTrue(nulls)
        first_null_idx = next(i for i, p in enumerate(people) if not p.get("last_message_at"))
        last_dated_idx = max(i for i, p in enumerate(people) if p.get("last_message_at"))
        self.assertGreater(first_null_idx, last_dated_idx)

    def test_filter_q(self):
        listing = person_unified.list_pilot(self.con, self.allow, q="Antonina")
        names = [p["display_name"] for p in listing["people"]]
        self.assertEqual(len(names), 1)
        self.assertIn("Антонина", names[0])
"""
marker = 'if __name__ == "__main__":'
if marker not in src:
    raise SystemExit("main missing")
src = src.replace(marker, extra + "\n\n" + marker, 1)
Path("test_person_unified.py").write_text(src, encoding="utf-8")
print("patched tests ok", len(src))
