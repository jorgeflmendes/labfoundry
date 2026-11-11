from __future__ import annotations

from pathlib import Path
from xml.etree import ElementTree

from typer.testing import CliRunner

from labfoundry.cli.main import _reserve_run_dir, app

runner = CliRunner()


def test_validate_command_compiles_without_gns3(scenario_path: Path) -> None:
    result = runner.invoke(app, ["validate", str(scenario_path)])

    assert result.exit_code == 0, result.output
    assert "valid" in result.output
    assert "scenario=test-network" in result.output


def test_compile_command_writes_plan(tmp_path: Path, scenario_path: Path) -> None:
    target = tmp_path / "plan.json"

    result = runner.invoke(app, ["compile", str(scenario_path), "-o", str(target)])

    assert result.exit_code == 0, result.output
    assert target.is_file()
    assert '"schema_version": "0.2"' in target.read_text(encoding="utf-8")


def test_inspect_command_renders_nodes_and_timeline(scenario_path: Path) -> None:
    result = runner.invoke(app, ["inspect", str(scenario_path)])

    assert result.exit_code == 0, result.output
    assert "test-network" in result.output
    assert "Timeline" in result.output
    assert "probe" in result.output


def test_diagram_command_writes_compiled_topology(tmp_path: Path, scenario_path: Path) -> None:
    target = tmp_path / "topology.svg"

    result = runner.invoke(app, ["diagram", str(scenario_path), "-o", str(target)])

    assert result.exit_code == 0, result.output
    image = ElementTree.fromstring(target.read_text(encoding="utf-8"))
    assert image.tag == "{http://www.w3.org/2000/svg}svg"
    labels = " ".join(element.text or "" for element in image.iter())
    assert "test-network" in labels
    assert "10.10.10.0/24" in labels
    assert "gateway" in labels
    assert "Ethernet switch" in labels


def test_run_command_validates_scenario_presence() -> None:
    result = runner.invoke(app, ["run"])

    assert result.exit_code == 2, result.output
    assert "scenario must be specified" in result.output


def test_diagram_command_filters_capture_coverage(tmp_path: Path, scenario_path: Path) -> None:
    target = tmp_path / "students.svg"
    result = runner.invoke(
        app,
        [
            "diagram",
            str(scenario_path),
            "--view",
            "captured",
            "--capture",
            "users-perimeter",
            "--exclude-segment",
            "servers",
            "-o",
            str(target),
        ],
    )
    assert result.exit_code == 0, result.output
    svg = target.read_text(encoding="utf-8")
    assert "10.10.10.1/24" in svg
    assert "10.20.20.1/24" not in svg
    assert "198.51.100.1/24" not in svg
    result = runner.invoke(
        app, ["diagram", str(scenario_path), "--segment", "missing", "-o", str(target)]
    )
    assert result.exit_code == 2
    assert "unknown segment" in result.output


def test_run_command_validates_missing_file() -> None:
    result = runner.invoke(app, ["run", "--scenario", "nonexistent_scenario.yaml"])

    assert result.exit_code == 2, result.output
    assert "scenario file does not exist" in result.output


def test_run_directories_do_not_reuse_prior_attempts(tmp_path: Path) -> None:
    first = _reserve_run_dir(tmp_path, "scenario", 123)
    (first / "internal").mkdir()

    second = _reserve_run_dir(tmp_path, "scenario", 123)

    assert first.name == "123"
    assert second.name == "123-2"
