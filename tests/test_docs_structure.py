from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"


def _markdown_nav_targets(value: Any) -> set[str]:
    targets: set[str] = set()
    if isinstance(value, list):
        for item in value:
            targets.update(_markdown_nav_targets(item))
    elif isinstance(value, dict):
        for item in value.values():
            targets.update(_markdown_nav_targets(item))
    elif isinstance(value, str) and value.endswith(".md"):
        targets.add(value)
    return targets


def test_mkdocs_navigation_references_every_markdown_page() -> None:
    config = yaml.safe_load((ROOT / "mkdocs.yml").read_text(encoding="utf-8"))
    nav_targets = _markdown_nav_targets(config["nav"])
    markdown_files = {path.relative_to(DOCS).as_posix() for path in DOCS.rglob("*.md")}

    assert nav_targets == markdown_files
    for target in nav_targets:
        assert (DOCS / target).is_file()


def test_mkdocstrings_directives_reference_real_modules() -> None:
    directives: set[str] = set()
    for page in (DOCS / "reference" / "api").glob("*.md"):
        for line in page.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped.startswith("::: labfoundry."):
                directives.add(stripped.removeprefix("::: ").strip())

    assert directives
    for module in directives:
        candidate = ROOT / "src" / Path(*module.split("."))
        assert candidate.with_suffix(".py").is_file() or (candidate / "__init__.py").is_file()


def test_public_python_api_has_docstrings() -> None:
    import ast

    package_root = ROOT / "src" / "labfoundry"
    missing: list[str] = []
    for path in package_root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                if node.name.startswith("_"):
                    continue
                if ast.get_docstring(node) is None:
                    missing.append(f"{path.relative_to(ROOT)}:{node.lineno}:{node.name}")
                if isinstance(node, ast.ClassDef):
                    for member in node.body:
                        if not isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef)):
                            continue
                        if member.name.startswith("_"):
                            continue
                        if ast.get_docstring(member) is None:
                            missing.append(
                                f"{path.relative_to(ROOT)}:{member.lineno}:"
                                f"{node.name}.{member.name}"
                            )

    assert not missing, "public API symbols without docstrings:\n" + "\n".join(missing)


def test_every_public_module_is_in_api_reference() -> None:
    import ast

    package_root = ROOT / "src" / "labfoundry"
    documented: set[str] = set()
    for page in DOCS.rglob("*.md"):
        for line in page.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped.startswith("::: labfoundry."):
                documented.add(stripped.removeprefix("::: ").strip())

    missing: list[str] = []
    for path in package_root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        public = False
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                public |= not node.name.startswith("_")
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                public |= not node.target.id.startswith("_")
            elif isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and not target.id.startswith("_"):
                        public = True
        if not public:
            continue
        module = ".".join(path.relative_to(ROOT / "src").with_suffix("").parts)
        if module not in documented:
            missing.append(module)

    assert not missing, "public modules missing from API reference:\n" + "\n".join(missing)


def test_cli_reference_lists_every_registered_command() -> None:
    from labfoundry.cli.main import app, gns3_app

    page = (DOCS / "reference" / "cli.md").read_text(encoding="utf-8")
    root_commands = {command.name for command in app.registered_commands if command.name}
    gns3_commands = {command.name for command in gns3_app.registered_commands if command.name}

    for command in sorted(root_commands):
        assert f"## `labfoundry {command}`" in page
    for command in sorted(gns3_commands):
        assert f"## `labfoundry gns3 {command}`" in page


def test_dsl_reference_mentions_every_public_schema_field() -> None:
    from labfoundry.schema import scenario as scenario_schema

    page = (DOCS / "reference" / "scenario-dsl.md").read_text(encoding="utf-8")
    missing: list[str] = []
    for name, model in vars(scenario_schema).items():
        if not isinstance(model, type):
            continue
        fields = getattr(model, "model_fields", None)
        if fields is None or model.__module__ != scenario_schema.__name__:
            continue
        for field_name, field in fields.items():
            public_name = field.alias or field_name
            if f"`{public_name}`" not in page:
                missing.append(f"{name}.{public_name}")

    assert not missing, "schema fields missing from DSL reference:\n" + "\n".join(missing)


def test_markdown_relative_links_resolve() -> None:
    import re

    broken: list[str] = []
    pattern = re.compile(r"\[[^\]]+\]\(([^)]+)\)")
    for page in DOCS.rglob("*.md"):
        for target in pattern.findall(page.read_text(encoding="utf-8")):
            if target.startswith(("http://", "https://", "#", "mailto:")):
                continue
            path_part = target.split("#", 1)[0]
            if not path_part:
                continue
            if not (page.parent / path_part).resolve().exists():
                broken.append(f"{page.relative_to(ROOT)} -> {target}")

    assert not broken, "broken relative documentation links:\n" + "\n".join(broken)


def test_guest_download_example_is_valid_yaml() -> None:
    import re

    page = (DOCS / "guide" / "guest-execution.md").read_text(encoding="utf-8")
    blocks = re.findall(r"```yaml\n(.*?)\n```", page, re.DOTALL)
    block = next(item for item in blocks if "source: /tmp/browser.keys" in item)

    example = yaml.safe_load(block)
    assert example[0]["parameters"] == {
        "source": "/tmp/browser.keys",
        "destination": "browser.keys",
        "publish": True,
        "refresh_at_end": True,
    }


def test_real_world_egress_example_keeps_nodes_in_network() -> None:
    import re

    from labfoundry.schema.scenario import NetworkSpec

    page = (DOCS / "guide" / "real-world-egress.md").read_text(encoding="utf-8")
    block = re.search(r"```yaml\n(.*?)\n```", page, re.DOTALL)
    assert block is not None
    network = yaml.safe_load(block.group(1))["network"]

    NetworkSpec.model_validate(network)
    assert "client" in network["nodes"]


def test_ci_validates_distribution_artifacts() -> None:
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")

    assert "python -m twine check dist/*" in workflow
    assert "check-wheel-contents dist/*.whl" in workflow
    assert "pip-audit ." in workflow


def test_release_workflow_uses_trusted_publishing() -> None:
    workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")

    assert "id-token: write" in workflow
    assert "pypa/gh-action-pypi-publish@release/v1" in workflow
    assert "Check tag matches package version" in workflow


def test_release_requires_ci_checks_before_building() -> None:
    ci = yaml.load(
        (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8"), Loader=yaml.BaseLoader
    )
    release = yaml.safe_load((ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8"))

    assert "workflow_call" in ci["on"]
    jobs = release["jobs"]
    assert jobs["checks"]["uses"] == "./.github/workflows/ci.yml"
    assert jobs["build"]["needs"] == "checks"
    assert jobs["publish"]["needs"] == "build"


def test_installation_docs_cover_release_and_development_installs() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    installation = (DOCS / "getting-started" / "installation.md").read_text(encoding="utf-8")

    for page in (readme, installation):
        assert "python -m pip install labfoundry" in page
        assert "python -m pip install -e" in page
