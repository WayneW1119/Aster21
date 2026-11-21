"""The shipped entry must match its maintained sources and licensed dependency."""
import importlib.util
import json
from pathlib import Path
import unittest


class WebBundleTests(unittest.TestCase):
    def test_shipped_bundle_is_current_and_license_is_included(self):
        root=Path(__file__).resolve().parents[1]
        spec=importlib.util.spec_from_file_location('relay_web_build',root/'tools/build_web.py')
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        self.assertEqual((module.WEB/'app.js').read_text(encoding="utf-8"),module.bundle())
        self.assertEqual(json.loads((module.WEB/'markdown-it.provenance.json').read_text(encoding="utf-8"))['license'],'MIT')
        self.assertIn('Permission is hereby granted, free of charge',(module.WEB/'markdown-it.LICENSE.txt').read_text(encoding="utf-8"))
