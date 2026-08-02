# Coding Style

## Python

- Style: PEP 8, 4-space indent, 100 char line limit
- Type hints required on all public functions and class methods
- Docstrings: Google-style for modules/classes; one-line for simple functions
- Error handling:
  - Sandbox errors → return `ExecutionResult` with error fields populated (never raise into Flask route)
  - Validation errors → raise `ValueError` / HTTP 400 in route layer
  - LLM errors → return synthetic `LLMTurnResult` with `status="error"` instead of propagating exceptions
  - Never let untrusted code execution errors crash the Flask process
- `# TODO(spec):` for deviations; `# TODO:` for unrelated future work
- All `__init__.py` files present in every package (even if empty)

## Module Boundaries

- `core/` has zero Flask imports — pure Python logic only
- `api/` imports `core/` but not vice versa
- `core/llm/` does not import `core/sandbox/` and vice versa
- Routes are thin: validate input → call core → serialize output

## Naming Conventions

- Classes: `PascalCase`
- Functions/variables: `snake_case`
- Constants: `UPPER_SNAKE`
- Template partials: `snake_case.html`
- JS modules: `camelCase.js`, exported functions/objects: `camelCase()`
- image_id / session_id / user_id: UUID4 hex strings (no dashes in filenames, keep dashes in JSON)

## File Headers

Each Python module begins with:
```python
"""Module description.

Longer explanation if needed.
"""
```

## Linting

- `ruff` for linting (if available); otherwise `flake8`
- No formatter enforced in MVP but code should be consistently formatted

## Adding a model (VRAM lifecycle rule)

**Rule:** anything that consumes VRAM loads only when an op actually asks for
it, and is unloaded once idle (`MODEL_IDLE_TIMEOUT_SECONDS`, default 300s).
This is enforced by `core/models/guard.py`, not by convention — an op module
that loads weights directly makes the app refuse to start.

There is exactly one sanctioned path:

**1. Declare the model** in `core/models/definitions.py`. The loader is stored,
not called — importing this module must never touch the GPU.

```python
@register_model(name="my_model", vram_mb=1200, doc="What it does.")
def _load_my_model(device):
    import torch
    return torch.hub.load("org/repo", "entry").to(device).eval()
```

**2. Declare the op** with `@register_model_op` in `core/pipeline/ops/models.py`.
The `model` argument is injected already loaded:

```python
@register_model_op(name="my_op", category="features", model="my_model",
                   inputs=[Port("image", PortType.IMAGE_ANY)],
                   outputs=[Port("features", PortType.FEATURE_MAP)])
def my_op(image, model, **params):
    ...   # inference only — never load, never .cuda(), never manage devices
```

`ModelManager` (in the sidecar) then handles everything else: lazy load on
first request, refcounting so a model in use is never yanked mid-inference,
idle unload, and LRU eviction under `MODEL_VRAM_BUDGET_MB`.

**What the guard rejects** anywhere under `core/pipeline/ops/`: `torch.hub.load`,
`torch.load`, `.from_pretrained`, `.load_state_dict`, `rembg.new_session`,
`onnxruntime.InferenceSession`, `timm.create_model`, `.cuda()`, `.to("cuda")`.
If you genuinely need something outside this shape, extend the sanctioned
package and the guard together — don't work around it.
