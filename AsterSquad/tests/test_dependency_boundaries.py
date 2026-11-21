"""Keep the declared dependency direction explicit, including packaged scripts."""
import ast
from pathlib import Path
import unittest
import squad

class DependencyBoundaryTests(unittest.TestCase):
    def test_runtime_code_has_no_forbidden_imports(self):
        forbidden = ('agent_harness',)
        root = Path(squad.__file__).parent
        for path in root.rglob('*.py'):
            with self.subTest(path=str(path.relative_to(root))):
                for node in ast.walk(ast.parse(path.read_text(encoding='utf-8'))):
                    names = ([node.module] if isinstance(node, ast.ImportFrom) and node.module
                             else [a.name for a in node.names] if isinstance(node, ast.Import) else [])
                    self.assertFalse({n.split('.')[0] for n in names} & set(forbidden))
