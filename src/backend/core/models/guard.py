"""Static guard enforcing the model-lifecycle rule.

The rule: anything that consumes VRAM must be declared with `@register_model`
and consumed via `@register_model_op`, so ModelManager owns its lifetime
(lazy load, refcounting, idle unload, VRAM budget).

Convention alone doesn't hold — someone adds `torch.hub.load(...)` inside an op
six months from now and the model is pinned in VRAM forever with nothing to
unload it. So this AST-scans op modules at registry load time and refuses to
start if it finds direct model loading outside the sanctioned package.

Deliberately static (AST) rather than runtime: it fires at import, before any
weights are fetched, and it points at the offending file and line.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

# Attribute calls that mean "I am loading model weights myself".
_BANNED_CALLS: dict[str, str] = {
    "hub.load": "torch.hub.load",
    "from_pretrained": "<...>.from_pretrained",
    "new_session": "rembg.new_session",
    "InferenceSession": "onnxruntime.InferenceSession",
    "create_model": "timm.create_model",
    "load_state_dict": "<...>.load_state_dict",
}

# Moving an object onto the GPU is equally a lifetime decision.
_BANNED_DEVICE_MOVES = {"cuda"}

_GUIDANCE = (
    "Models must be declared in core/models/definitions.py with @register_model "
    "and consumed with @register_model_op, which injects an already-loaded "
    "handle. That is what gives them lazy loading, refcounting, the idle "
    "unload, and the VRAM budget. See CODING_STYLE.md 'Adding a model'."
)


class ModelPolicyError(RuntimeError):
    """Raised at import time when an op module loads model weights directly."""


@dataclass
class Violation:
    file: str
    line: int
    what: str

    def __str__(self) -> str:
        return f"{self.file}:{self.line} — direct model loading via {self.what}"


def _attr_chain(node: ast.AST) -> str:
    """Render a dotted attribute chain, e.g. torch.hub.load -> 'torch.hub.load'."""
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return ".".join(reversed(parts))


def scan_source(source: str, filename: str) -> list[Violation]:
    """Return policy violations found in one module's source."""
    violations: list[Violation] = []
    try:
        tree = ast.parse(source, filename=filename)
    except SyntaxError as e:
        raise ModelPolicyError(f"{filename}: could not parse for model-policy check: {e}")

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue

        # torch.hub.load / x.from_pretrained / rembg.new_session / ...
        if isinstance(node.func, ast.Attribute):
            chain = _attr_chain(node.func)
            for suffix, label in _BANNED_CALLS.items():
                if chain == suffix or chain.endswith("." + suffix):
                    violations.append(Violation(filename, node.lineno, label))
                    break

            # .cuda()  /  .to("cuda")  /  .to(device="cuda")
            if node.func.attr == "cuda":
                violations.append(Violation(filename, node.lineno, ".cuda()"))
            elif node.func.attr == "to":
                for arg in list(node.args) + [kw.value for kw in node.keywords]:
                    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                        if arg.value.split(":")[0] in _BANNED_DEVICE_MOVES:
                            violations.append(
                                Violation(filename, node.lineno, f'.to("{arg.value}")'))

        # bare InferenceSession(...) / new_session(...) after a from-import
        elif isinstance(node.func, ast.Name) and node.func.id in _BANNED_CALLS:
            violations.append(Violation(filename, node.lineno, _BANNED_CALLS[node.func.id]))

    return violations


def scan_path(path: Path) -> list[Violation]:
    return scan_source(path.read_text(encoding="utf-8"), str(path))


def check_op_modules(ops_dir: Path | None = None) -> None:
    """Scan every op module; raise ModelPolicyError on the first offending file.

    Called from pipeline.registry._ensure_loaded() so it cannot be bypassed by
    importing the op modules through some other path.
    """
    if ops_dir is None:
        ops_dir = Path(__file__).resolve().parent.parent / "pipeline" / "ops"

    all_violations: list[Violation] = []
    for py in sorted(ops_dir.glob("*.py")):
        if py.name == "__init__.py":
            continue
        all_violations.extend(scan_path(py))

    if all_violations:
        listing = "\n  ".join(str(v) for v in all_violations)
        raise ModelPolicyError(
            f"Model-lifecycle policy violated in pipeline op module(s):\n  {listing}\n\n{_GUIDANCE}")
