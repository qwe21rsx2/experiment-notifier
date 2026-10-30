import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from experiment_notifier import optuna_cli as mod


class CliTests(unittest.TestCase):
    def test_config_lookup(self):
        config = {"optuna": {"storage": "sqlite:///x.db", "study_name": "s"}}
        a = type("Args", (), {"storage": None, "study_name": None, "results": None})()
        self.assertEqual(mod.resolve_options(a, config)[:2], ("sqlite:///x.db", "s"))

    def test_redaction(self):
        text = mod.format_config({"lr": 1e-5, "password": "SECRET", "nested": {"api_key": "SECRET2"}})
        self.assertNotIn("SECRET", text)
        self.assertIn("lr", text)

    def test_results_json_freshness(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "results.json"
            start = time.time()
            p.write_text(json.dumps({"best_value": 0.72, "best_params": {"lr": 0.01}}))
            result = mod.load_json_summary(p, start)
            self.assertEqual(result["best_value"], 0.72)
            os.utime(p, (start - 100, start - 100))
            with self.assertRaises(RuntimeError):
                mod.load_json_summary(p, start)

    def test_storage(self):
        try:
            import optuna
        except ImportError:
            self.skipTest("Optuna is optional")
        with tempfile.TemporaryDirectory() as td:
            storage = "sqlite:///" + str(Path(td) / "study.db")
            study = optuna.create_study(storage=storage, study_name="mockstudy", direction="maximize")
            study.optimize(lambda t: t.suggest_float("lr", 0.01, 0.1), n_trials=2)
            result = mod.load_storage_summary(storage, "mockstudy")
            self.assertEqual(result["trials_total"], 2)
            self.assertEqual(result["counts"]["COMPLETE"], 2)
            self.assertIn("lr", result["best_params"])

    def test_process_code_and_notification(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = Path(td) / "config.json"
            cfg.write_text('{"name":"test"}')
            data = Path(td) / "results.json"
            code = ("import json,sys; "
                    "json.dump({'best_value': 0.7, 'best_params': {'mc': 16}}, open(sys.argv[1], 'w')); "
                    "sys.exit(3)")
            sent = []
            with mock.patch.object(mod, "send", side_effect=lambda _web, title, body: sent.append((title, body))):
                with mock.patch.object(sys, "argv", ["notify-run-optuna", "--results", str(data), "--config", str(cfg), "--", sys.executable, "-c", code, str(data)]):
                    ret = mod.main()
            self.assertEqual(ret, 3)
            self.assertEqual(len(sent), 2)
            self.assertIn("Failed", sent[-1][0])
            self.assertIn("0.7", sent[-1][1])
            self.assertIn("mc", sent[-1][1])

    def test_no_study_does_not_prevent_training(self):
        sent = []
        with mock.patch.object(mod, "send", side_effect=lambda _web, title, body: sent.append((title, body))):
            with mock.patch.object(sys, "argv", ["notify-run-optuna", "--", sys.executable, "-c", "print('ok')"]):
                ret = mod.main()
        self.assertEqual(ret, 0)
        self.assertIn("No readable Optuna results", sent[-1][1])


if __name__ == "__main__":
    unittest.main()
