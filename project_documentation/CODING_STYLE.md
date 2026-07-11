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
- image_id / window_id: UUID4 hex strings (no dashes in filenames, keep dashes in JSON)

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
