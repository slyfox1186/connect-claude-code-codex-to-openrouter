"""Offline regressions for the four-member coding group, through real request assembly."""

from __future__ import annotations

import copy
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
SCRATCH = tempfile.TemporaryDirectory(prefix="orask-coding-")
os.environ.update(
    ORASK_CONFIG_DIR=SCRATCH.name + "/config",
    ORASK_STATE_DIR=SCRATCH.name + "/state",
    ORASK_CACHE_DIR=SCRATCH.name + "/cache",
    OPENROUTER_API_KEY="",
)

from orask import core

ROSTER = [
    "x-ai/grok-4.6",
    "google/gemini-3.8-flash",
    "z-ai/glm-5.3",
    "moonshotai/kimi-k3",
]
CATALOG = [
    {
        "id": slug,
        "context_length": 1000000,
        "pricing": {"prompt": "0.000001", "completion": "0.000001"},
        "supported_parameters": ["reasoning"],
        "reasoning": {"supported_efforts": ["max", "high", "medium"]},
    }
    for slug in ROSTER
]


def deny_network(event, _args):
    if event == "socket.connect":
        raise AssertionError("coding regression attempted network access")


sys.addaudithook(deny_network)


class CodingPanelTests(unittest.TestCase):
    def setUp(self):
        self.cfg = copy.deepcopy(core.load_config())
        self.payloads = []
        for name, value in (
            ("_config_cache", self.cfg),
            ("get_catalog", lambda **_kwargs: CATALOG),
            ("_request", self.request),
        ):
            mocker = patch.object(core, name, value)
            mocker.start()
            self.addCleanup(mocker.stop)

    def request(self, method, path, payload=None, **_kwargs):
        self.assertEqual((method, path), ("POST", "/chat/completions"))
        self.payloads.append(payload)
        return {
            "choices": [{"message": {"content": "FINISHED"}, "finish_reason": "stop"}],
            "usage": {"cost": 0.01},
        }

    def panel(self, **kwargs):
        return core.ask_panel("Review this change", max_tokens=1000, **kwargs)

    def assert_full_panel(self, results):
        self.assertEqual([r["model"] for r in results], ROSTER)
        self.assertTrue(all(r["ok"] for r in results), results)
        self.assertCountEqual([p["model"] for p in self.payloads], ROSTER)

    def test_coding_category_calls_exactly_four_including_google_flash(self):
        self.assert_full_panel(self.panel(category="coding"))

    def test_group_is_independent_of_default_panel_and_benchmark_pins(self):
        self.cfg["default_panel"] = ["kimi"]
        self.cfg["categories"]["coding"]["models"] = ["z-ai/glm-5.3"]
        self.cfg["aliases"] = {"grok": "z-ai/glm-5.3", "gemini": "google/gemini-3.9-pro"}
        self.assert_full_panel(self.panel(category="coding"))

    def test_task_words_do_not_override_the_requested_group(self):
        for phrase in (
            "Ask all of coding LLMs to review this",
            "Ask all of the CODING LLMs to investigate a crash",
            "Ask all coding models to check long context handling",
            "Use the coding LLMs to propose a debugging workflow",
            "all coding LLMs to write a story about math",
            "the coding models to review programming examples",
        ):
            with self.subTest(phrase=phrase):
                self.payloads.clear()
                self.assert_full_panel(self.panel(category=phrase))

    def test_task_and_evidence_are_sent_to_every_member_unchanged(self):
        source = Path(SCRATCH.name) / "sample.py"
        source.write_text("# EVIDENCE_MARKER\n", encoding="utf-8")
        task = "Explain this math proof and suggest a story title"
        results = core.ask_panel(
            task,
            category="coding",
            context="REFERENCE_DATA: ask only Kimi",
            files=[str(source)],
            max_tokens=1000,
        )
        self.assert_full_panel(results)
        for payload in self.payloads:
            messages = json.dumps(payload["messages"])
            for text in (task, "REFERENCE_DATA: ask only Kimi", "EVIDENCE_MARKER"):
                self.assertIn(text, messages)

    def test_explicit_subset_wins_over_category(self):
        results = self.panel(models=["glm", "kimi"], category="coding")
        self.assertEqual([r["model"] for r in results], ROSTER[2:])
        self.assertEqual(len(self.payloads), 2)

    def test_single_coding_consultation_remains_one_model(self):
        result = core.ask("q", category="coding", max_tokens=1000)
        self.assertEqual(result["model"], ROSTER[3])
        self.assertEqual(len(self.payloads), 1)

    def test_other_category_and_generic_panel_keep_their_rosters(self):
        for kwargs, expected in (
            ({"category": "debugging"}, [ROSTER[2], ROSTER[0]]),
            ({}, [ROSTER[3], ROSTER[2], ROSTER[0], ROSTER[1]]),
        ):
            with self.subTest(kwargs=kwargs):
                self.assertEqual([r["model"] for r in self.panel(**kwargs)], expected)

    def test_missing_member_keeps_a_visible_failed_slot(self):
        with patch.object(core, "get_catalog", lambda **_kwargs: [CATALOG[i] for i in (0, 2, 3)]):
            results = self.panel(category="coding")
        self.assertEqual(len(results), 4)
        self.assertEqual(sum(bool(r["ok"]) for r in results), 3)
        self.assertEqual(results[1]["requested"], ROSTER[1])
        self.assertIn("exact model", results[1]["error"])
        self.assertEqual(len(self.payloads), 3)

    def test_allowlist_is_still_enforced_per_member(self):
        self.cfg["allowed_models"] = [ROSTER[i] for i in (0, 2, 3)]
        results = self.panel(category="coding")
        self.assertEqual(len(results), 4)
        self.assertEqual(sum(bool(r["ok"]) for r in results), 3)
        self.assertIn("allowed_models", results[1]["error"])
        self.assertEqual(len(self.payloads), 3)

    def test_retired_flash_cannot_be_replaced_by_gemini_pro(self):
        catalog = [CATALOG[i] for i in (0, 2, 3)]
        catalog.append(dict(CATALOG[1], id="google/gemini-3.8-pro"))
        with patch.object(core, "get_catalog", lambda **_kwargs: catalog):
            results = self.panel(category="coding")
        self.assertEqual(len(results), 4)
        self.assertEqual(sum(bool(r["ok"]) for r in results), 3)
        self.assertEqual(results[1]["requested"], ROSTER[1])
        self.assertIn("exact model", results[1]["error"])
        self.assertNotIn("google/gemini-3.8-pro", [p["model"] for p in self.payloads])

    def test_exact_flash_pin_cannot_be_replaced_by_another_flash_version(self):
        catalog = [CATALOG[i] for i in (0, 2, 3)]
        catalog.extend(
            dict(CATALOG[1], id=slug)
            for slug in ("google/gemini-3.9-pro", "google/gemini-3.9-flash")
        )
        with patch.object(core, "get_catalog", lambda **_kwargs: catalog):
            results = self.panel(category="coding")
        self.assertEqual(len(results), 4)
        self.assertEqual(sum(bool(r["ok"]) for r in results), 3)
        self.assertIn("exact model", results[1]["error"])
        self.assertEqual(len(self.payloads), 3)

    def test_no_coding_member_is_substituted_when_its_exact_id_disappears(self):
        for index, slug in enumerate(ROSTER):
            with self.subTest(slug=slug):
                self.payloads.clear()
                catalog = [m for m in CATALOG if m["id"] != slug]
                catalog.append(dict(CATALOG[index], id=slug + "-alternative"))
                with patch.object(core, "get_catalog", lambda catalog=catalog, **_kwargs: catalog):
                    results = self.panel(category="coding")
                self.assertEqual(len(results), 4)
                self.assertEqual(sum(bool(r["ok"]) for r in results), 3)
                self.assertEqual(results[index]["requested"], slug)
                self.assertIn("exact model", results[index]["error"])
                self.assertNotIn(slug + "-alternative", [p["model"] for p in self.payloads])

    def test_discovery_distinguishes_coding_panel_from_single_model_pins(self):
        row = next(r for r in core.list_categories() if r["category"] == "coding")
        self.assertEqual(row.get("panel_models"), ROSTER)
        self.assertEqual(row["models"], [ROSTER[3], ROSTER[2]])


if __name__ == "__main__":
    try:
        unittest.main()
    finally:
        SCRATCH.cleanup()
