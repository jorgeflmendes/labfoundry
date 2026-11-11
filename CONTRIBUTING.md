# Contributing

Core rules:

1. No direct GNS3 HTTP calls outside `backends/gns3`.
2. Do not add backend-specific UUIDs to the DSL.
3. All procedural randomness must use `derive_seed(root_seed, namespace)`.
4. Scenario compilation must remain side-effect free.
5. New GNS3 routes require a mock contract test and an upstream documentation/source reference in `docs/advanced/gns3-api-contract.md`.
6. Guest operating-system configuration does not belong in the GNS3 adapter.
7. Raw evidence and exported evidence are separate objects.
8. Every public Python API must have type annotations and a useful docstring.
9. New public modules must be represented in `docs/reference/api/`.
10. Documentation changes must pass `mkdocs build --strict`.

Before opening a change:

```bash
python -m ruff check .
python -m ruff format --check .
python -m mypy src
python -m pytest --cov=labfoundry --cov-branch --cov-report=term-missing --cov-fail-under=70
python -m build
python -m mkdocs build --strict
```

Install contributor tooling with:

```bash
python -m pip install -e '.[dev]'
```

The `dev` extra includes the documentation stack. For documentation-only work, `.[docs]` is sufficient.

## Real GNS3 integration tests

Unit/contract tests must remain runnable without a hypervisor. Tests that create real GNS3 projects
belong under `tests/integration/` and are opt-in:

```bash
LABFOUNDRY_INTEGRATION=1 pytest -m integration
```

Set `LABFOUNDRY_TEST_VBOX_TEMPLATE` to include the real VirtualBox lifecycle smoke test. Do not make
normal CI depend on a developer's local GNS3 or VirtualBox installation.

## Coverage policy

Normal unit/contract coverage is measured with branch coverage and must stay at or above 70%.
Integration tests remain opt-in and are not required to satisfy the portable coverage gate. New core
features should add focused tests rather than relying on the global percentage alone.
