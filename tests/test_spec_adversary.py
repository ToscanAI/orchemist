"""Unit tests for orchestration_engine.spec_adversary.

Covers:
  - AdversaryFinding dataclass
  - AdversaryVerdict dataclass
  - parse_adversary_output
  - compute_reward
  - persist_reward
  - Module exports
"""

import json
import os
import tempfile
import unittest

from orchestration_engine.spec_adversary import (
    AdversaryFinding,
    AdversaryVerdict,
    compute_reward,
    parse_adversary_output,
    persist_reward,
)


class TestAdversaryFindingDataclass(unittest.TestCase):
    def test_fields_present(self):
        f = AdversaryFinding(category="vague", description="Too vague")
        self.assertEqual(f.category, "vague")
        self.assertEqual(f.description, "Too vague")

    def test_all_valid_categories(self):
        for cat in ("vague", "trivial", "missing_edge_case", "leakage", "divergence"):
            f = AdversaryFinding(category=cat, description="desc")
            self.assertEqual(f.category, cat)

    def test_description_stored_verbatim(self):
        desc = "  Leading spaces and CAPS preserved  "
        f = AdversaryFinding(category="trivial", description=desc)
        self.assertEqual(f.description, desc)


class TestAdversaryVerdictDataclass(unittest.TestCase):
    def test_fields_present(self):
        v = AdversaryVerdict(verdict="APPROVE", findings=[], raw_text="APPROVE")
        self.assertEqual(v.verdict, "APPROVE")
        self.assertEqual(v.findings, [])
        self.assertEqual(v.raw_text, "APPROVE")

    def test_findings_list(self):
        f = AdversaryFinding(category="vague", description="d")
        v = AdversaryVerdict(verdict="REQUEST_CHANGES", findings=[f], raw_text="RC")
        self.assertEqual(len(v.findings), 1)
        self.assertIs(v.findings[0], f)

    def test_raw_text_preserved(self):
        raw = "REQUEST_CHANGES\n[vague] something"
        v = AdversaryVerdict(verdict="REQUEST_CHANGES", findings=[], raw_text=raw)
        self.assertEqual(v.raw_text, raw)

    def test_default_findings_empty(self):
        v = AdversaryVerdict(verdict="APPROVE")
        self.assertEqual(v.findings, [])

    def test_default_raw_text_empty(self):
        v = AdversaryVerdict(verdict="APPROVE")
        self.assertEqual(v.raw_text, "")


class TestParseAdversaryOutput(unittest.TestCase):
    def test_approve_no_findings(self):
        result = parse_adversary_output("APPROVE\nAll contracts are tight.")
        self.assertEqual(result.verdict, "APPROVE")
        self.assertEqual(result.findings, [])

    def test_request_changes_with_findings(self):
        text = (
            "REQUEST_CHANGES\n"
            "[vague] Contract says 'handles errors' with no detail\n"
            "[trivial] All contracts pass if implementation returns empty dict\n"
        )
        result = parse_adversary_output(text)
        self.assertEqual(result.verdict, "REQUEST_CHANGES")
        self.assertEqual(len(result.findings), 2)
        self.assertEqual(result.findings[0].category, "vague")
        self.assertEqual(result.findings[1].category, "trivial")

    def test_all_five_categories_parsed(self):
        text = (
            "REQUEST_CHANGES\n"
            "[vague] A\n"
            "[trivial] B\n"
            "[missing_edge_case] C\n"
            "[leakage] D\n"
            "[divergence] E\n"
        )
        result = parse_adversary_output(text)
        cats = [f.category for f in result.findings]
        self.assertIn("vague", cats)
        self.assertIn("trivial", cats)
        self.assertIn("missing_edge_case", cats)
        self.assertIn("leakage", cats)
        self.assertIn("divergence", cats)

    def test_missing_verdict_defaults_to_request_changes(self):
        result = parse_adversary_output("No verdict here, just text.")
        self.assertEqual(result.verdict, "REQUEST_CHANGES")
        self.assertGreaterEqual(len(result.findings), 1)

    def test_empty_string_returns_request_changes(self):
        result = parse_adversary_output("")
        self.assertEqual(result.verdict, "REQUEST_CHANGES")
        self.assertGreaterEqual(len(result.findings), 1)

    def test_none_input_returns_request_changes(self):
        result = parse_adversary_output(None)
        self.assertEqual(result.verdict, "REQUEST_CHANGES")

    def test_non_string_input_returns_request_changes(self):
        result = parse_adversary_output(42)
        self.assertEqual(result.verdict, "REQUEST_CHANGES")

    def test_finding_lines_without_bracket_tags_skipped(self):
        text = "REQUEST_CHANGES\nThis line has no bracket tag and should be ignored\n"
        result = parse_adversary_output(text)
        self.assertEqual(result.verdict, "REQUEST_CHANGES")
        self.assertEqual(result.findings, [])

    def test_mixed_case_category_normalized(self):
        text = "REQUEST_CHANGES\n[VAGUE] Some vague thing\n[Missing_Edge_Case] Missing stuff\n"
        result = parse_adversary_output(text)
        cats = [f.category for f in result.findings]
        self.assertIn("vague", cats)
        self.assertIn("missing_edge_case", cats)

    def test_raw_text_preserved(self):
        text = "APPROVE\nLooks good"
        result = parse_adversary_output(text)
        self.assertEqual(result.raw_text, text)

    def test_never_raises(self):
        for bad_input in [None, 42, [], {}, object()]:
            try:
                parse_adversary_output(bad_input)
            except Exception as e:
                self.fail(f"parse_adversary_output raised {e} for input {bad_input!r}")

    def test_approve_case_insensitive(self):
        result = parse_adversary_output("approve\nAll good")
        self.assertEqual(result.verdict, "APPROVE")

    def test_request_changes_case_insensitive(self):
        result = parse_adversary_output("request_changes\n[vague] Something\n")
        self.assertEqual(result.verdict, "REQUEST_CHANGES")

    def test_unknown_category_skipped(self):
        text = "REQUEST_CHANGES\n[unknown_category] This should be skipped\n[vague] This should be kept\n"
        result = parse_adversary_output(text)
        cats = [f.category for f in result.findings]
        self.assertNotIn("unknown_category", cats)
        self.assertIn("vague", cats)


class TestComputeReward(unittest.TestCase):
    def test_request_changes_with_n_findings(self):
        findings = [AdversaryFinding("vague", f"issue {i}") for i in range(3)]
        v = AdversaryVerdict(verdict="REQUEST_CHANGES", findings=findings)
        self.assertEqual(compute_reward(v), 3)

    def test_approve_returns_zero(self):
        v = AdversaryVerdict(verdict="APPROVE", findings=[])
        self.assertEqual(compute_reward(v), 0)

    def test_request_changes_with_zero_findings_returns_zero(self):
        v = AdversaryVerdict(verdict="REQUEST_CHANGES", findings=[])
        self.assertEqual(compute_reward(v), 0)

    def test_approve_with_findings_returns_zero(self):
        # Edge case: APPROVE verdict with findings (shouldn't happen, but be safe)
        findings = [AdversaryFinding("vague", "x")]
        v = AdversaryVerdict(verdict="APPROVE", findings=findings)
        self.assertEqual(compute_reward(v), 0)

    def test_reward_equals_findings_count(self):
        for n in range(5):
            findings = [AdversaryFinding("trivial", f"t{i}") for i in range(n)]
            v = AdversaryVerdict(verdict="REQUEST_CHANGES", findings=findings)
            self.assertEqual(compute_reward(v), n)


class TestPersistReward(unittest.TestCase):
    def _make_verdict(self, verdict="APPROVE", findings=None):
        return AdversaryVerdict(
            verdict=verdict,
            findings=findings or [],
            raw_text=verdict,
        )

    def test_writes_correct_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            v = self._make_verdict(
                "REQUEST_CHANGES",
                [AdversaryFinding("vague", "too vague")],
            )
            persist_reward(tmpdir, v, 1)
            reward_file = os.path.join(tmpdir, "adversary_reward.json")
            self.assertTrue(os.path.exists(reward_file))
            data = json.loads(open(reward_file).read())
            self.assertEqual(data["verdict"], "REQUEST_CHANGES")
            self.assertEqual(data["reward_score"], 1)
            self.assertEqual(data["findings_count"], 1)
            self.assertEqual(len(data["findings"]), 1)
            self.assertIn("persisted_at", data)

    def test_all_required_fields_present(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            v = self._make_verdict("APPROVE")
            persist_reward(tmpdir, v, 0)
            data = json.loads(open(os.path.join(tmpdir, "adversary_reward.json")).read())
            for field in ("verdict", "reward_score", "findings_count", "findings", "persisted_at"):
                self.assertIn(field, data, f"Missing field: {field}")

    def test_none_output_dir_does_not_raise(self):
        v = self._make_verdict()
        try:
            persist_reward(None, v, 0)
        except Exception as e:
            self.fail(f"persist_reward raised {e} with output_dir=None")

    def test_nonexistent_output_dir_does_not_raise(self):
        v = self._make_verdict()
        try:
            persist_reward("/tmp/nonexistent_path_12345xyz", v, 0)
        except Exception as e:
            self.fail(f"persist_reward raised {e} with nonexistent dir")

    def test_findings_serialized_correctly(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            findings = [
                AdversaryFinding("vague", "Contract A"),
                AdversaryFinding("leakage", "Contract B"),
            ]
            v = self._make_verdict("REQUEST_CHANGES", findings)
            persist_reward(tmpdir, v, 2)
            data = json.loads(open(os.path.join(tmpdir, "adversary_reward.json")).read())
            self.assertEqual(data["findings"][0]["category"], "vague")
            self.assertEqual(data["findings"][0]["description"], "Contract A")
            self.assertEqual(data["findings"][1]["category"], "leakage")


class TestModuleExports(unittest.TestCase):
    def test_all_public_names_importable(self):
        from orchestration_engine import spec_adversary
        for name in ("AdversaryFinding", "AdversaryVerdict", "parse_adversary_output",
                     "compute_reward", "persist_reward"):
            self.assertTrue(
                hasattr(spec_adversary, name),
                f"spec_adversary missing export: {name}",
            )

    def test_all_in_dunder_all(self):
        from orchestration_engine.spec_adversary import __all__
        for name in ("AdversaryFinding", "AdversaryVerdict", "parse_adversary_output",
                     "compute_reward", "persist_reward"):
            self.assertIn(name, __all__)


if __name__ == "__main__":
    unittest.main()
