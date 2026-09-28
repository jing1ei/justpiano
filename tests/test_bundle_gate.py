"""Missing startup evidence must prevent a package from being published."""
import json
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch
from tools.check_bundle import check


class BundleGateTests(unittest.TestCase):
    def test_process_that_exits_without_evidence_fails(self):
        with patch('tools.check_bundle.subprocess.run', return_value=subprocess.CompletedProcess([], 0, '', '')):
            with self.assertRaisesRegex(RuntimeError, 'failed startup'):
                check('missing-app')

    def test_failed_native_ui_retains_diagnostic(self):
        def run(args, **kwargs):
            Path(args[-1]).write_text(json.dumps({'startup': 'failed', 'error': 'no panel'}))
            return subprocess.CompletedProcess(args, 0, '', 'native traceback')
        with patch('tools.check_bundle.subprocess.run', side_effect=run):
            with self.assertRaisesRegex(RuntimeError, 'native traceback'):
                check('app')

    def test_import_only_success_is_not_a_ui_pass(self):
        def run(args, **kwargs):
            Path(args[-1]).write_text(json.dumps({'startup': 'ok', 'frozen': True, 'checks': ['native-imports']}))
            return subprocess.CompletedProcess(args, 0, '', '')
        with patch('tools.check_bundle.subprocess.run', side_effect=run):
            with self.assertRaisesRegex(RuntimeError, 'Unexpected'):
                check('app')
