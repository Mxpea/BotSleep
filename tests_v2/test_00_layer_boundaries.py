"""AST architectural guard: enforce strict L0->L4 single-direction imports and zero legacy coupling inside v2/."""

from __future__ import annotations

import ast
import unittest
from pathlib import Path
from typing import Dict, Set, Tuple

REPO_ROOT = Path(__file__).resolve().parent.parent
V2_ROOT = REPO_ROOT / "v2"

# Numeric rank: a subpackage may ONLY import v2 subpackages with rank <= its own rank
# (plus allowed same-layer peers: tasks -> primitives).
LAYER_RANK: Dict[str, int] = {
    "domain": 0,
    "kernel": 1,
    "gateway": 2,
    "bt": 3,
    "primitives": 4,
    "tasks": 5,
    "planner": 6,
    "observability": 6,
}

FORBIDDEN_LEGACY_TOPLEVELS: Set[str] = {"agent", "core", "skills", "debug"}


def _subpackage_of(file_path: Path) -> str:
    rel = file_path.relative_to(V2_ROOT)
    if len(rel.parts) == 1:
        return "domain" if rel.stem == "domain" else "root"
    return rel.parts[0]


def _extract_imports(file_path: Path, pkg_parts: Tuple[str, ...]) -> Set[str]:
    tree = ast.parse(file_path.read_text(encoding="utf-8"), filename=str(file_path))
    imported: Set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level > 0:
                base = list(pkg_parts[: max(0, len(pkg_parts) - node.level + 1)])
                if node.module:
                    base.extend(node.module.split("."))
                if base:
                    imported.add(".".join(base))
            elif node.module:
                imported.add(node.module)
    return imported


class TestV2LayerBoundaries(unittest.TestCase):
    def test_v2_single_direction_imports_and_no_legacy_coupling(self) -> None:
        self.assertTrue(V2_ROOT.is_dir(), "v2/ root directory must exist")
        violations = []
        for py_file in sorted(V2_ROOT.rglob("*.py")):
            src_layer = _subpackage_of(py_file)
            if src_layer == "root":
                continue
            src_rank = LAYER_RANK.get(src_layer)
            self.assertIsNotNone(src_rank, f"Unregistered v2 subpackage '{src_layer}' in {py_file}")
            rel_parts = py_file.relative_to(REPO_ROOT).with_suffix("").parts
            pkg_parts = rel_parts[:-1] if py_file.name == "__init__.py" else rel_parts[:-1]
            for target in sorted(_extract_imports(py_file, pkg_parts)):
                top = target.split(".")[0]
                if top in FORBIDDEN_LEGACY_TOPLEVELS:
                    violations.append(f"{py_file.relative_to(REPO_ROOT)} imports legacy '{target}'")
                    continue
                if top != "v2":
                    continue
                parts = target.split(".")
                if len(parts) < 2:
                    continue
                dst_layer = parts[1]
                dst_rank = LAYER_RANK.get(dst_layer)
                if dst_rank is None:
                    violations.append(f"{py_file.relative_to(REPO_ROOT)} imports unknown v2 module '{target}'")
                elif dst_rank > src_rank:
                    violations.append(
                        f"{py_file.relative_to(REPO_ROOT)} (L:{src_layer}={src_rank}) "
                        f"illegally imports higher layer '{target}' (L:{dst_layer}={dst_rank})"
                    )
        self.assertEqual(violations, [], "Layer boundary violations:\n" + "\n".join(violations))


if __name__ == "__main__":
    unittest.main()
