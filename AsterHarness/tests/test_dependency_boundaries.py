"""Keep the declared dependency direction explicit, including packaged scripts."""
import ast
from pathlib import Path
import unittest
import agent_harness
import aster

class DependencyBoundaryTests(unittest.TestCase):
    def test_runtime_code_has_no_forbidden_imports(self):
        forbidden = ('relay', 'squad', 'aster_web', 'aster_images')
        roots = [Path(agent_harness.__file__).parent, Path(aster.__file__).parent]
        for path in (p for root in roots for p in root.rglob('*.py')):
            with self.subTest(path=str(path)):
                for node in ast.walk(ast.parse(path.read_text(encoding='utf-8'))):
                    names = ([node.module] if isinstance(node, ast.ImportFrom) and node.module
                             else [a.name for a in node.names] if isinstance(node, ast.Import) else [])
                    self.assertFalse({n.split('.')[0] for n in names} & set(forbidden))

    def test_distribution_does_not_require_downstream_projects(self):
        import re, tomllib
        config = tomllib.loads((Path(__file__).parents[1]/'pyproject.toml').read_text())
        names = {re.match(r'[A-Za-z0-9_.-]+', requirement)[0].lower().replace('_', '-')
                 for requirement in config['project']['dependencies']}
        self.assertFalse(names & set(('aster-relay', 'aster-squad', 'aster-web', 'aster-images')))
