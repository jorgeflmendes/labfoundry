# Releasing

Releases are built from version tags and published to PyPI through GitHub's trusted publishing.
The workflow does not use an API token.

## One-time setup

Create the GitHub repository and a protected `pypi` environment. In PyPI, add a pending trusted
publisher for the repository with workflow name `release.yml` and environment name `pypi`.

## Release checklist

1. Confirm `main` is clean and CI is green.
2. Update the version in `pyproject.toml` and `src/labfoundry/__init__.py`.
3. Review user-visible changes and update the affected documentation.
4. Run the test suite, static checks, strict documentation build, and applicable opt-in integration tests.
5. Build and inspect the exact artefacts:

    ```bash
    python -m build
    python -m twine check dist/*
    check-wheel-contents dist/*.whl
    pip-audit .
    ```

6. Commit the release changes, create an annotated tag matching the package version, and push it:

    ```bash
    git tag -a v0.2.0 -m "LabFoundry 0.2.0"
    git push origin main v0.2.0
    ```

The release workflow runs the reusable CI workflow on the tagged commit before building release
artifacts. Tests, coverage, lint, formatting, typing, documentation and package checks must pass.
It rejects a tag whose value does not match `project.version`, rebuilds the wheel and source
distribution, checks both, and publishes through PyPI OIDC. Protect the `pypi` environment if
publication should require approval.
