"""spec/vectors.json is the cross-language contract consumed by the TypeScript SDK tests."""

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_shared_vectors_match_the_python_implementation():
    spec = importlib.util.spec_from_file_location("export_vectors", ROOT / "scripts" / "export_vectors.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    current = (ROOT / "spec" / "vectors.json").read_text(encoding="utf-8")
    assert module.render() == current, "Run: python scripts/export_vectors.py"
