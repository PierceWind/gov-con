"""Provider failure handling and proposal input/output regressions; no live API calls."""
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import proposal


class ProviderTests(unittest.TestCase):
    def test_claude_success_does_not_call_codex(self):
        metadata = {}
        with patch.object(proposal, "provider", return_value="claude-code"), \
             patch.object(proposal, "_call_provider", return_value="draft") as call:
            self.assertEqual(proposal._call("task", [], metadata=metadata), "draft")
        self.assertEqual(call.call_count, 1)
        self.assertEqual(metadata, {"ai": "claude-code", "requested_ai": "claude-code", "fallback": False})

    def test_both_claude_providers_fall_back_with_same_inputs(self):
        blocks = [{"type": "text", "text": "bid pack"}]
        schema = {"required": ["answer"]}
        for primary in ("claude", "claude-code"):
            with self.subTest(primary=primary):
                metadata = {}
                with patch.object(proposal, "provider", return_value=primary), \
                     patch.object(proposal, "codex_cli", return_value="codex.exe"), \
                     patch.object(proposal, "_call_provider", side_effect=[RuntimeError("runtime failure"), '{"answer": 1}']) as call:
                    proposal._call("task", blocks, schema, 123, metadata)
                self.assertEqual(call.call_args_list[0].args, (primary, "task", blocks, schema, 123))
                self.assertEqual(call.call_args_list[1].args, ("codex", "task", blocks, schema, 123))
                self.assertEqual(metadata, {"ai": "codex", "requested_ai": primary, "fallback": True})

    def test_empty_or_invalid_claude_response_falls_back(self):
        for response in ("", "not JSON", "{}", '{"score": "high"}'):
            with self.subTest(response=response), \
                 patch.object(proposal, "provider", return_value="claude-code"), \
                 patch.object(proposal, "codex_cli", return_value="codex.exe"), \
                 patch.object(proposal, "_call_provider", side_effect=[response, '{"score": 80}']):
                result = proposal._call("task", [], {"required": ["score"], "properties": {"score": {"type": "integer"}}})
                self.assertEqual(json.loads(result)["score"], 80)

    def test_unavailable_fallback_preserves_original_error(self):
        with patch.object(proposal, "provider", return_value="claude"), \
             patch.object(proposal, "codex_cli", return_value=None), \
             patch.object(proposal, "_call_provider", side_effect=RuntimeError("original error")) as call:
            with self.assertRaisesRegex(RuntimeError, "original error"):
                proposal._call("task", [])
        self.assertEqual(call.call_count, 1)

    def test_double_failure_reports_both_errors(self):
        with patch.object(proposal, "provider", return_value="claude-code"), \
             patch.object(proposal, "codex_cli", return_value="codex.exe"), \
             patch.object(proposal, "_call_provider", side_effect=[RuntimeError("Claude runtime error"), RuntimeError("Codex login required")]) as call:
            with self.assertRaisesRegex(RuntimeError, "(?s)Claude runtime error.*Codex login required"):
                proposal._call("task", [])
        self.assertEqual(call.call_count, 2)

    def test_non_claude_failure_is_not_retried(self):
        for primary in ("gemini", "codex"):
            with self.subTest(primary=primary), \
                 patch.object(proposal, "provider", return_value=primary), \
                 patch.object(proposal, "_call_provider", side_effect=RuntimeError("failed")) as call:
                with self.assertRaises(RuntimeError):
                    proposal._call("task", [])
                self.assertEqual(call.call_count, 1)

    def test_forced_codex_and_default_detection(self):
        with patch.object(proposal, "env_value", side_effect=lambda name: "codex" if name == "AI_PROVIDER" else None):
            self.assertEqual(proposal.provider(), "codex")
        with patch.object(proposal, "env_value", return_value=None), \
             patch.object(proposal, "claude_cli", return_value=None), \
             patch.object(proposal, "codex_cli", return_value="codex.exe"):
            self.assertEqual(proposal.provider(), "codex")

    def test_codex_final_output_schema_and_cleanup(self):
        workdirs = []

        def run(cmd, **kwargs):
            workdirs.append(kwargs["cwd"])
            self.assertIn("read-only", cmd)
            self.assertIn("features.shell_tool=false", cmd)
            self.assertEqual(cmd[-1], "-")
            self.assertIn("supplied pack", kwargs["input"])
            schema_path = Path(cmd[cmd.index("--output-schema") + 1])
            self.assertEqual(json.loads(schema_path.read_text()), {"type": "object"})
            Path(cmd[cmd.index("--output-last-message") + 1]).write_text('{"ok":true}', encoding="utf-8")
            return subprocess.CompletedProcess(cmd, 0, stdout="unrelated log", stderr="")

        with tempfile.TemporaryDirectory() as folder, \
             patch.object(proposal, "DRAFTS", Path(folder)), \
             patch.object(proposal, "codex_cli", return_value="codex.exe"), \
             patch.object(proposal, "env_value", return_value=None), \
             patch.object(proposal.subprocess, "run", side_effect=run):
            for _ in range(2):
                self.assertEqual(proposal._call_codex("task", "supplied pack", {"type": "object"}), '{"ok":true}')
            self.assertNotEqual(workdirs[0], workdirs[1])
            self.assertTrue(all(not p.exists() for p in workdirs))

    def test_codex_failure_empty_output_and_timeout(self):
        for outcome, message in ((subprocess.CompletedProcess([], 1, "", "login required"), "login required"),
                                 (subprocess.CompletedProcess([], 0, "", ""), "no final response"),
                                 (subprocess.TimeoutExpired("codex", 1800), "30 minutes")):
            with self.subTest(message=message), tempfile.TemporaryDirectory() as folder, \
                 patch.object(proposal, "DRAFTS", Path(folder)), \
                 patch.object(proposal, "codex_cli", return_value="codex.exe"), \
                 patch.object(proposal, "env_value", return_value=None), \
                 patch.object(proposal.subprocess, "run") as run:
                if isinstance(outcome, Exception):
                    run.side_effect = outcome
                else:
                    run.return_value = outcome
                with self.assertRaisesRegex(RuntimeError, message):
                    proposal._call_codex("task", "pack")


class DraftTests(unittest.TestCase):
    def test_both_benchmarks_are_loaded(self):
        _, template, _ = proposal._company()
        self.assertIn("PROPOSAL BENCHMARK GUIDE", template)
        self.assertIn("70Z028-26-Q-0000001", template)
        self.assertIn("IFB No. 11213", template)
        self.assertIn("3.4 Emergency Repair and Downtime Response", template)

    def test_draft_metadata_and_market_research_format(self):
        for market in (False, True):
            with self.subTest(market=market), tempfile.TemporaryDirectory() as folder:
                folder = Path(folder)
                opp = {"opp_type": {"description": "Sources Sought" if market else "Solicitation"}}

                def respond(task, blocks, **kwargs):
                    self.assertIn("proposal draft" if not market else "market research", task)
                    # Uploaded/source document text stays out of system instructions.
                    self.assertNotIn("PROPOSAL BENCHMARK GUIDE", task)
                    if not market:
                        self.assertIn("PROPOSAL BENCHMARK GUIDE", blocks[0]["text"])
                        self.assertIn("IFB No. 11213", blocks[0]["text"])
                    kwargs["metadata"].update(ai="codex", requested_ai="claude-code", fallback=True)
                    return "# Draft\n\n[CONFIRM PRICE]"

                with patch.object(proposal, "build_bid_pack", return_value=(opp, "pack", folder)), \
                     patch.object(proposal, "attach_capability"), \
                     patch.object(proposal, "_call", side_effect=respond):
                    result = proposal.draft_proposal("key")
                output = folder / ("sources-sought-response.md" if market else "proposal-draft.md")
                self.assertEqual(output.read_text(), result["markdown"])
                self.assertEqual(json.loads(output.with_suffix(".meta.json").read_text())["ai"], "codex")
                self.assertTrue(result["fallback"])

    def test_failed_generation_preserves_existing_draft(self):
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            output = folder / "proposal-draft.md"
            output.write_text("previous draft")
            with patch.object(proposal, "build_bid_pack", return_value=({}, "pack", folder)), \
                 patch.object(proposal, "attach_capability"), \
                 patch.object(proposal, "_call", side_effect=RuntimeError("both providers failed")):
                with self.assertRaises(RuntimeError):
                    proposal.draft_proposal("key")
            self.assertEqual(output.read_text(), "previous draft")

    def test_bid_check_records_actual_fallback_provider(self):
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            answer = {key: [] if spec["type"] == "array" else 70 if key == "score" else "Bid" if key == "verdict" else "detail"
                      for key, spec in proposal.BID_SCHEMA["properties"].items()}
            with patch.object(proposal, "build_bid_pack", return_value=({}, "pack", folder)), \
                 patch.object(proposal, "provider", return_value="claude-code"), \
                 patch.object(proposal, "codex_cli", return_value="codex.exe"), \
                 patch.object(proposal, "_call_provider", side_effect=[RuntimeError("runtime error"), json.dumps(answer)]):
                result = proposal.bid_check("key")
            self.assertEqual(result["ai"], "codex")
            self.assertEqual(json.loads((folder / "bid-check.json").read_text())["requested_ai"], "claude-code")


if __name__ == "__main__":
    unittest.main()
