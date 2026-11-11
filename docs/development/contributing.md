# Contributing

## Local checks

Install the development dependencies and run the checks before submitting a change:

```bash
python -m pip install -e '.[dev]'
python -m pytest
python -m ruff check .
python -m ruff format --check .
python -m mypy src
python -m mkdocs build --strict
```

## Documentation

The documentation site uses:

- **MkDocs** as the documentation site generator;
- **Material for MkDocs** as the theme and navigation layer;
- **mkdocstrings** as the autodoc integration;
- **mkdocstrings-python** to collect Python signatures, types and docstrings with Griffe.

## Build locally

```bash
python -m pip install -e '.[docs]'
mkdocs build --strict
```

Run a live preview while editing:

```bash
mkdocs serve
```

## Source layout

```text
mkdocs.yml
docs/
  index.md
  getting-started/
  guide/
  advanced/
  reference/
    api/
  development/
src/labfoundry/
```

`mkdocstrings` uses `paths: [src]`, so it reads the package directly from the source layout.

## Documentation contract

Public Python APIs should have:

1. full type annotations;
2. a concise docstring that explains behavior rather than restating the name;
3. `Args`, `Returns` and `Raises` sections where they add information;
4. no secrets, environment-specific paths or unstable implementation detail in documentation.

New public modules must be added to `docs/reference/api/` and to the MkDocs navigation. `mkdocs build --strict` is a CI gate, so broken references and documentation warnings should fail pull requests.

The test suite also enforces documentation coverage structurally:

- every public Python module must appear in a `mkdocstrings` directive;
- every public function/class/method must have a docstring;
- every registered Typer command must have a CLI reference section;
- every public Pydantic DSL field must be named in the human DSL reference;
- every Markdown page must be present in `mkdocs.yml` navigation;
- relative Markdown links must resolve;
- every `mkdocstrings` module directive must point at a real source module.

These checks are intentionally independent of the generated site so documentation drift is caught even before MkDocs renders the pages.
