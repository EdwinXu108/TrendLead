"""Every module of the package parses, including modules no test imports."""
import ast
from pathlib import Path

PACKAGE = Path(__file__).resolve().parent.parent / 'leadtrend_v05'


def test_every_package_module_parses():
    failures = []
    for path in sorted(PACKAGE.rglob('*.py')):
        try:
            ast.parse(path.read_text(encoding='utf-8'), filename=str(path))
        except SyntaxError as exc:
            failures.append(f'{path.relative_to(PACKAGE)}:{exc.lineno}: {exc.msg}')
    assert failures == []
