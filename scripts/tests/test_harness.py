import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import harness as h  # noqa: E402

MEMBERS = {"p1": {}, "p2": {}, "p3": {}}
PROTECTED = ["contracts/**", ".env.example", "scripts/**", "CLAUDE.md"]


def entry(key, touches, blocked=(), type="feature", owner="p2"):
    return {"key": key, "title": key, "type": type, "owner": owner, "touches": touches,
            "blocked_by": list(blocked), "verify": "true"}


class GlobTests(unittest.TestCase):
    def test_star_stays_in_segment(self):
        self.assertTrue(h.matches("a/b.py", "a/*.py"))
        self.assertFalse(h.matches("a/c/b.py", "a/*.py"))

    def test_double_star(self):
        self.assertTrue(h.matches("a/b/c/d.py", "a/**"))
        self.assertTrue(h.matches("a/x", "a/**/x"))
        self.assertTrue(h.matches("a/b/x", "a/**/x"))
        self.assertTrue(h.matches("package.json", "**/package.json"))
        self.assertTrue(h.matches("web/package.json", "**/package.json"))
        self.assertTrue(h.matches("anything/at/all", "**"))

    def test_question_mark(self):
        self.assertTrue(h.matches("a1", "a?"))
        self.assertFalse(h.matches("a/", "a?"))

    def test_exact(self):
        self.assertTrue(h.matches("CLAUDE.md", "CLAUDE.md"))
        self.assertFalse(h.matches("x/CLAUDE.md", "CLAUDE.md"))
        self.assertFalse(h.matches("CLAUDEXmd", "CLAUDE.md"))

    def test_violations(self):
        bad = h.touches_violations(["web/a.ts", "api/b.py", "uv.lock"], ["web/**"], ["uv.lock"])
        self.assertEqual(bad, ["api/b.py"])


class BodyTests(unittest.TestCase):
    def test_roundtrip(self):
        body = h.build_body(["a/**", "b.py"], [1, 2], "pnpm test && pnpm lint", "obj",
                            ["ref"], ["one", "two"], "my-key")
        p = h.parse_body(body)
        self.assertEqual(p["touches"], ["a/**", "b.py"])
        self.assertEqual(p["blocked_by"], [1, 2])
        self.assertEqual(p["verify"], "pnpm test && pnpm lint")
        self.assertEqual(p["key"], "my-key")

    def test_none_blockers(self):
        p = h.parse_body(h.build_body(["a"], [], "true", "", [], [], "k"))
        self.assertEqual(p["blocked_by"], [])

    def test_missing_block(self):
        p = h.parse_body("just text")
        self.assertEqual(p["touches"], [])
        self.assertIsNone(p["key"])


class ManifestTests(unittest.TestCase):
    def check(self, entries, tracked=(), allow=False):
        return h.validate_manifest(entries, MEMBERS, PROTECTED, tracked, allow)

    def test_valid(self):
        errs, _ = self.check([entry("a", ["web/**"]), entry("b", ["api/**"], ["a"])])
        self.assertEqual(errs, [])

    def test_duplicate_unknown_dep_owner_empty_touches(self):
        errs, _ = self.check([entry("a", ["x/**"]), entry("a", ["y/**"])])
        self.assertTrue(any("duplicate" in e for e in errs))
        errs, _ = self.check([entry("a", ["x/**"], ["zzz"])])
        self.assertTrue(any("unknown key" in e for e in errs))
        errs, _ = self.check([entry("a", ["x/**"], owner="p9")])
        self.assertTrue(any("not in team.json" in e for e in errs))
        errs, _ = self.check([entry("a", [])])
        self.assertTrue(any("touches is empty" in e for e in errs))

    def test_cycle(self):
        errs, _ = self.check([entry("a", ["x/**"], ["b"]), entry("b", ["y/**"], ["a"])])
        self.assertTrue(any("cycle" in e for e in errs))

    def test_protected_path_rejected_for_feature_not_contract(self):
        errs, _ = self.check([entry("a", ["contracts/**"])])
        self.assertTrue(any("protected" in e for e in errs))
        errs, _ = self.check([entry("a", ["contracts/openapi.yaml"], type="integration")])
        self.assertTrue(any("protected" in e for e in errs))
        errs, _ = self.check([entry("a", ["contracts/**"], type="contract")])
        self.assertEqual(errs, [])

    def test_overlap_blocked_vs_unblocked(self):
        a, b = entry("a", ["web/**"]), entry("b", ["web/pages/**"])
        errs, _ = self.check([a, b])
        self.assertTrue(any("overlap" in e for e in errs))
        errs, _ = self.check([a, dict(b, blocked_by=["a"])])
        self.assertEqual(errs, [])

    def test_overlap_transitive(self):
        entries = [entry("a", ["web/**"]), entry("m", ["api/**"], ["a"]),
                   entry("b", ["web/x/**"], ["m"])]
        errs, _ = self.check(entries)
        self.assertEqual(errs, [])

    def test_allow_overlap_downgrades(self):
        errs, warns = self.check([entry("a", ["web/**"]), entry("b", ["web/**"])], allow=True)
        self.assertEqual(errs, [])
        self.assertEqual(len(warns), 1)

    def test_overlap_by_tracked_file(self):
        tracked = ["src/app.py"]
        errs, _ = self.check([entry("a", ["src/app.py"]), entry("b", ["src/*.py"])], tracked)
        self.assertTrue(errs)

    def test_distinct_exact_files_do_not_overlap(self):
        errs, _ = self.check([entry("a", ["src/a"]), entry("b", ["src/ab"])])
        self.assertEqual(errs, [])


class MiscTests(unittest.TestCase):
    def test_pick_slot(self):
        self.assertEqual(h.pick_slot([]), 1)
        self.assertEqual(h.pick_slot([1, 2, 4]), 3)
        self.assertEqual(h.pick_slot([2]), 1)

    def test_codeowners(self):
        t = h.codeowners_text(["contracts/**", "CLAUDE.md"], ["a", "b"])
        self.assertIn("/contracts/ @a @b", t)
        self.assertIn("/CLAUDE.md @a @b", t)

    def test_stale(self):
        now = h.parse_ts("2026-01-01T12:00:00Z")
        old, recent = h.parse_ts("2026-01-01T10:00:00Z"), h.parse_ts("2026-01-01T11:30:00Z")
        self.assertTrue(h.is_stale(old, old, now, 60))
        self.assertFalse(h.is_stale(old, recent, now, 60))
        self.assertFalse(h.is_stale(recent, None, now, 60))

    def test_cycle_finder(self):
        self.assertIsNone(h.find_cycle({"a": ["b"], "b": []}))
        self.assertIsNotNone(h.find_cycle({"a": ["a"]}))


if __name__ == "__main__":
    unittest.main()
