"""LabFoundry command-line interface."""

from __future__ import annotations

import asyncio
import os
from datetime import date
from pathlib import Path
from typing import Annotated

import typer
import yaml
from rich.console import Console
from rich.table import Table

from labfoundry.backends.gns3.backend import GNS3Backend
from labfoundry.backends.gns3.client import GNS3Client
from labfoundry.backends.gns3.incus import IncusEnvironment
from labfoundry.backends.gns3.models import VirtualBoxTemplateCreate
from labfoundry.compiler.compiler import ScenarioCompiler
from labfoundry.compiler.diagram import NetworkView, render_network_svg
from labfoundry.compiler.flow import FlowLayout, FlowMode, narrative_labels, render_flow_svg
from labfoundry.compiler.plan import ScenarioPlan
from labfoundry.errors import BackendError, GuestConfigurationError, LabFoundryError
from labfoundry.evidence.capture import CaptureManager
from labfoundry.groundtruth.manifest import write_internal_metadata
from labfoundry.guest.bootstrap import (
    LinuxBootstrapSpec,
    generate_credentials,
    generate_password,
    load_bootstrap_spec,
    write_linux_seed,
    write_linux_seed_from_spec,
    write_windows_bundle,
    write_windows_bundle_from_spec,
)
from labfoundry.guest.config import GuestInventory, load_guest_inventory
from labfoundry.guest.manager import GuestManager
from labfoundry.runtime.orchestrator import ScenarioRunner
from labfoundry.schema.loader import load_scenario

app = typer.Typer(no_args_is_help=True, help="Deterministic procedural cyber-range compiler.")
gns3_app = typer.Typer(no_args_is_help=True, help="GNS3 Controller API operations.")
guest_app = typer.Typer(no_args_is_help=True, help="Guest transport configuration.")
app.add_typer(gns3_app, name="gns3")
app.add_typer(guest_app, name="guest")
console = Console()
_DEFAULT_GUEST_INVENTORY = Path.home() / ".labfoundry" / "guest_inventory.yaml"
_DEFAULT_LINUX_BOOTSTRAP = Path.home() / ".labfoundry" / "bootstrap" / "linux"
_DEFAULT_WINDOWS_BOOTSTRAP = Path.home() / ".labfoundry" / "bootstrap" / "windows"


def _compile(path: Path, seed: int | None = None) -> ScenarioPlan:
    spec = load_scenario(path)
    return ScenarioCompiler().compile(spec, seed=seed)


def _client(
    url: str,
    username: str | None,
    password: str | None,
    verify_tls: bool,
) -> GNS3Client:
    return GNS3Client(
        url,
        username=username,
        password=password,
        verify_tls=verify_tls,
    )


def _reserve_run_dir(run_root: Path, scenario_id: str, root_seed: int) -> Path:
    """Reserve a new run directory without overwriting an earlier attempt."""
    parent = run_root / scenario_id
    for attempt in range(1, 10_000):
        suffix = "" if attempt == 1 else f"-{attempt}"
        candidate = parent / f"{root_seed}{suffix}"
        try:
            candidate.mkdir(parents=True)
        except FileExistsError:
            continue
        return candidate
    raise RuntimeError(f"could not reserve a run directory under {parent}")


def _write_virtualbox_inventory(
    template: Path,
    output: Path,
    password_file: Path,
    *,
    username: str,
    password: str,
    guest_os: str | None = None,
) -> None:
    """Create an inventory that keeps the VirtualBox password in a sibling file."""
    try:
        raw = yaml.safe_load(template.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        message = f"cannot read guest inventory template {template}: {exc}"
        raise GuestConfigurationError(message) from exc
    if not isinstance(raw, dict) or not isinstance(raw.get("profiles"), dict):
        raise GuestConfigurationError("guest inventory template must contain a profiles mapping")
    if output.exists():
        raise GuestConfigurationError(f"guest inventory already exists: {output}")
    if password_file.exists():
        raise GuestConfigurationError(f"guest password file already exists: {password_file}")

    profiles = raw["profiles"]
    configured = 0
    for profile in profiles.values():
        if not isinstance(profile, dict) or profile.get("transport") != "virtualbox":
            continue
        if guest_os is not None and profile.get("os") != guest_os:
            continue
        profile["username"] = username
        profile.pop("password_env", None)
        profile["password_file"] = os.path.relpath(password_file, start=output.parent)
        configured += 1
    if configured == 0:
        qualifier = f" {guest_os}" if guest_os is not None else ""
        raise GuestConfigurationError(
            f"guest inventory template has no{qualifier} VirtualBox profiles"
        )

    output.parent.mkdir(parents=True, exist_ok=True)
    password_file.parent.mkdir(parents=True, exist_ok=True)
    password_created = False
    try:
        descriptor = os.open(password_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        password_created = True
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(password)
            handle.write("\n")
        with output.open("x", encoding="utf-8") as handle:
            yaml.safe_dump(raw, handle, sort_keys=False)
    except OSError as exc:
        if password_created:
            password_file.unlink(missing_ok=True)
        raise GuestConfigurationError(f"cannot write guest configuration: {exc}") from exc


def _write_generated_credentials(
    directory: Path, credentials: dict[str, str], *, exclude: frozenset[str] = frozenset()
) -> None:
    """Write generated guest passwords to separate local files."""
    directory.mkdir(parents=True, exist_ok=True)
    try:
        for username, password in credentials.items():
            if username in exclude:
                continue
            path = directory / f"{username}.password"
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(password)
                handle.write("\n")
    except OSError as exc:
        raise GuestConfigurationError(f"cannot write generated guest credentials: {exc}") from exc


@guest_app.command("configure")
def configure_virtualbox_guests(
    template: Annotated[Path, typer.Argument(exists=True, dir_okay=False, readable=True)],
    output: Annotated[
        Path,
        typer.Option("--output", "-o", help="Inventory location outside the repository."),
    ] = _DEFAULT_GUEST_INVENTORY,
    password_file: Annotated[
        Path | None,
        typer.Option(help="Password-file location; defaults beside the inventory."),
    ] = None,
    username: Annotated[str | None, typer.Option(help="VirtualBox guest username.")] = None,
) -> None:
    """Create a local VirtualBox inventory and prompt once for the guest password."""
    resolved_username = username or typer.prompt("VirtualBox guest username", default="rangeforge")
    password = typer.prompt("VirtualBox guest password", hide_input=True, confirmation_prompt=True)
    resolved_password_file = password_file or output.with_suffix(".password")
    try:
        _write_virtualbox_inventory(
            template,
            output,
            resolved_password_file,
            username=resolved_username,
            password=password,
        )
    except GuestConfigurationError as exc:
        console.print(f"[red]guest configuration failed[/red] {exc}")
        raise typer.Exit(2) from exc
    console.print(f"[green]wrote[/green] guest inventory={output}")


@guest_app.command("bootstrap-linux")
def bootstrap_linux_guest(
    template: Annotated[Path, typer.Argument(exists=True, dir_okay=False, readable=True)],
    output: Annotated[
        Path,
        typer.Option("--output", "-o", help="Local bootstrap directory outside the repository."),
    ] = _DEFAULT_LINUX_BOOTSTRAP,
    username: Annotated[
        str, typer.Option(help="Dedicated Linux guest-control account.")
    ] = "labfoundry",
    hostname: Annotated[str, typer.Option(help="Hostname assigned at first boot.")] = "labfoundry",
) -> None:
    """Create a cloud-init seed and local inventory for a new Linux VirtualBox template."""
    if output.exists():
        console.print(f"[red]guest bootstrap failed[/red] output already exists: {output}")
        raise typer.Exit(2)
    password = generate_password()
    try:
        _write_virtualbox_inventory(
            template,
            output / "guest_inventory.yaml",
            output / "guest.password",
            username=username,
            password=password,
            guest_os="linux",
        )
        seed = write_linux_seed(
            output / "seed", username=username, password=password, hostname=hostname
        )
    except GuestConfigurationError as exc:
        console.print(f"[red]guest bootstrap failed[/red] {exc}")
        raise typer.Exit(2) from exc
    console.print(f"[green]wrote[/green] guest inventory={output / 'guest_inventory.yaml'}")
    console.print(f"[green]attach once[/green] {seed} to the powered-off cloud-init template")


@guest_app.command("bootstrap")
def bootstrap_guest(
    specification: Annotated[
        Path, typer.Argument(exists=True, dir_okay=False, readable=True, help="Bootstrap YAML.")
    ],
    template: Annotated[
        Path,
        typer.Argument(
            exists=True, dir_okay=False, readable=True, help="Guest inventory template."
        ),
    ],
    output: Annotated[
        Path | None,
        typer.Option("--output", "-o", help="Local bootstrap directory outside the repository."),
    ] = None,
) -> None:
    """Create a guest-image bundle from a declarative bootstrap specification."""
    destination = output or Path.home() / ".labfoundry" / "bootstrap" / specification.stem
    if destination.exists():
        console.print(f"[red]guest bootstrap failed[/red] output already exists: {destination}")
        raise typer.Exit(2)
    try:
        spec = load_bootstrap_spec(specification)
        credentials = generate_credentials(spec)
        control_path = destination / "credentials" / f"{spec.control_user.name}.password"
        _write_virtualbox_inventory(
            template,
            destination / "guest_inventory.yaml",
            control_path,
            username=spec.control_user.name,
            password=credentials[spec.control_user.name],
            guest_os=spec.os,
        )
        _write_generated_credentials(
            destination / "credentials",
            credentials,
            exclude=frozenset({spec.control_user.name}),
        )
        if isinstance(spec, LinuxBootstrapSpec):
            media = write_linux_seed_from_spec(
                destination / "seed", spec=spec, credentials=credentials
            )
        else:
            media = write_windows_bundle_from_spec(
                destination / "unattend", spec=spec, credentials=credentials
            )
    except (GuestConfigurationError, OSError, yaml.YAMLError) as exc:
        console.print(f"[red]guest bootstrap failed[/red] {exc}")
        raise typer.Exit(2) from exc
    console.print(f"[green]wrote[/green] guest inventory={destination / 'guest_inventory.yaml'}")
    console.print(f"[green]generated[/green] credentials={destination / 'credentials'}")
    console.print(f"[green]attach during first boot[/green] {media}")


@guest_app.command("bootstrap-windows")
def bootstrap_windows_guest(
    template: Annotated[Path, typer.Argument(exists=True, dir_okay=False, readable=True)],
    output: Annotated[
        Path,
        typer.Option("--output", "-o", help="Local bootstrap directory outside the repository."),
    ] = _DEFAULT_WINDOWS_BOOTSTRAP,
    username: Annotated[
        str, typer.Option(help="Dedicated Windows guest-control account.")
    ] = "labfoundry",
) -> None:
    """Create unattended-install and inventory assets for a new Windows VirtualBox template."""
    if output.exists():
        console.print(f"[red]guest bootstrap failed[/red] output already exists: {output}")
        raise typer.Exit(2)
    password = generate_password()
    try:
        _write_virtualbox_inventory(
            template,
            output / "guest_inventory.yaml",
            output / "guest.password",
            username=username,
            password=password,
            guest_os="windows",
        )
        unattended = write_windows_bundle(output / "unattend", username=username, password=password)
    except GuestConfigurationError as exc:
        console.print(f"[red]guest bootstrap failed[/red] {exc}")
        raise typer.Exit(2) from exc
    console.print(f"[green]wrote[/green] guest inventory={output / 'guest_inventory.yaml'}")
    console.print(f"[green]attach during installation[/green] {unattended}")


@app.command("validate")
def validate_scenario(
    scenario: Annotated[Path, typer.Argument(exists=True, dir_okay=False, readable=True)],
    seed: Annotated[int | None, typer.Option(help="Override the DSL seed.")] = None,
) -> None:
    """Validate and fully compile a scenario without contacting GNS3."""
    try:
        plan = _compile(scenario, seed)
    except LabFoundryError as exc:
        console.print(f"[red]invalid[/red] {exc}")
        raise typer.Exit(2) from exc
    console.print(
        f"[green]valid[/green] scenario={plan.scenario_id} seed={plan.root_seed} "
        f"nodes={len(plan.nodes)} links={len(plan.links)} events={len(plan.events)}"
    )


@app.command("compile")
def compile_scenario(
    scenario: Annotated[Path, typer.Argument(exists=True, dir_okay=False, readable=True)],
    output: Annotated[Path | None, typer.Option("--output", "-o")] = None,
    seed: Annotated[int | None, typer.Option(help="Override the DSL seed.")] = None,
) -> None:
    """Compile YAML into the frozen JSON execution plan."""
    try:
        plan = _compile(scenario, seed)
    except LabFoundryError as exc:
        console.print(f"[red]compile failed[/red] {exc}")
        raise typer.Exit(2) from exc
    target = output or scenario.with_suffix(".plan.json")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(plan.model_dump_json(indent=2, by_alias=True), encoding="utf-8")
    console.print(f"[green]wrote[/green] {target}")


@app.command("inspect")
def inspect_scenario(
    scenario: Annotated[Path, typer.Argument(exists=True, dir_okay=False, readable=True)],
    seed: Annotated[int | None, typer.Option(help="Override the DSL seed.")] = None,
) -> None:
    """Show resolved nodes, addresses, aliases and event times."""
    try:
        plan = _compile(scenario, seed)
    except LabFoundryError as exc:
        console.print(f"[red]inspect failed[/red] {exc}")
        raise typer.Exit(2) from exc

    table = Table(title=f"{plan.scenario_id} / seed {plan.root_seed}")
    table.add_column("Node")
    table.add_column("Template")
    table.add_column("Interfaces")
    for node in plan.nodes:
        interfaces = ", ".join(
            f"{item.name}={item.address} [{item.segment}]" for item in node.interfaces
        )
        table.add_row(node.name, node.template, interfaces)
    console.print(table)

    event_table = Table(title="Timeline")
    event_table.add_column("Time")
    event_table.add_column("ID")
    event_table.add_column("Action")
    event_table.add_column("From")
    event_table.add_column("To")
    for event in plan.events:
        event_table.add_row(
            event.timestamp.isoformat(), event.id, event.action, event.from_ or "", event.to or ""
        )
    console.print(event_table)


@app.command("diagram")
def diagram_scenario(
    scenario: Annotated[Path, typer.Argument(exists=True, dir_okay=False, readable=True)],
    output: Annotated[Path | None, typer.Option("--output", "-o")] = None,
    seed: Annotated[int | None, typer.Option(help="Override the DSL seed.")] = None,
    view: Annotated[
        NetworkView, typer.Option(help="Full topology or declared capture coverage.")
    ] = NetworkView.FULL,
    capture: Annotated[
        list[str] | None, typer.Option(help="Select a capture by name (repeatable).")
    ] = None,
    segment: Annotated[
        list[str] | None, typer.Option(help="Include a segment (repeatable).")
    ] = None,
    exclude_segment: Annotated[
        list[str] | None, typer.Option(help="Hide a segment (repeatable).")
    ] = None,
    node: Annotated[
        list[str] | None, typer.Option(help="Include a concrete node (repeatable).")
    ] = None,
    exclude_node: Annotated[
        list[str] | None, typer.Option(help="Hide a concrete node (repeatable).")
    ] = None,
) -> None:
    """Draw the compiled network topology and capture points as SVG."""
    try:
        plan = _compile(scenario, seed)
        image = render_network_svg(
            plan,
            view=view,
            captures=tuple(capture or ()),
            segments=tuple(segment or ()),
            exclude_segments=tuple(exclude_segment or ()),
            nodes=tuple(node or ()),
            exclude_nodes=tuple(exclude_node or ()),
        )
    except (LabFoundryError, ValueError) as exc:
        console.print(f"[red]diagram failed[/red] {exc}")
        raise typer.Exit(2) from exc
    suffix = ".captured.svg" if view == NetworkView.CAPTURED or capture else ".svg"
    target = output or scenario.with_suffix(suffix)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(image, encoding="utf-8")
    console.print(f"[green]wrote[/green] {target}")


@app.command("flow")
def flow_scenario(
    scenario: Annotated[Path, typer.Argument(exists=True, dir_okay=False, readable=True)],
    output: Annotated[Path | None, typer.Option("--output", "-o")] = None,
    mode: Annotated[
        FlowMode, typer.Option(help="Narrative comments or full action parameters.")
    ] = FlowMode.NARRATIVE,
    layout: Annotated[
        FlowLayout, typer.Option(help="Compact column or node lanes.")
    ] = FlowLayout.COMPACT,
    seed: Annotated[int | None, typer.Option(help="Override the DSL seed.")] = None,
    day: Annotated[str | None, typer.Option(help="Only events on this date (YYYY-MM-DD).")] = None,
    background: Annotated[bool | None, typer.Option("--background/--no-background")] = None,
    provision: Annotated[bool | None, typer.Option("--provision/--no-provision")] = None,
) -> None:
    """Draw scheduled actions as a chronological SVG in a column or node lanes."""
    try:
        selected_day = date.fromisoformat(day) if day else None
        plan = _compile(scenario, seed)
        image = render_flow_svg(
            plan,
            mode=mode,
            layout=layout,
            labels=narrative_labels(scenario.read_text(encoding="utf-8")),
            include_background=background if background is not None else mode == FlowMode.COMPLETE,
            include_provision=provision if provision is not None else mode == FlowMode.COMPLETE,
            day=selected_day,
        )
    except (LabFoundryError, ValueError) as exc:
        console.print(f"[red]flow failed[/red] {exc}")
        raise typer.Exit(2) from exc
    target = output or scenario.with_suffix(f".flow.{mode.value}.svg")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(image, encoding="utf-8")
    console.print(f"[green]wrote[/green] {target}")


@app.command("run")
def run_scenario(
    scenario: Annotated[
        Path | None,
        typer.Argument(
            help="Scenario YAML specification.",
        ),
    ] = None,
    scenario_opt: Annotated[
        Path | None,
        typer.Option(
            "--scenario",
            "-s",
            help="Scenario YAML specification.",
        ),
    ] = None,
    guest_config: Annotated[
        Path | None,
        typer.Option("--guest-config", exists=True, dir_okay=False, readable=True),
    ] = None,
    asset_dir: Annotated[
        Path | None,
        typer.Option("--asset-dir", exists=True, file_okay=False),
    ] = None,
    run_root: Annotated[
        Path,
        typer.Option(
            "--run-root",
            "--out",
            help="Directory where run artifacts are stored.",
        ),
    ] = Path("runs"),
    seed: Annotated[int | None, typer.Option(help="Override the DSL seed.")] = None,
    url: Annotated[str, typer.Option(envvar="LABFOUNDRY_GNS3_URL")] = "http://127.0.0.1:3080",
    username: Annotated[str | None, typer.Option(envvar="LABFOUNDRY_GNS3_USER")] = None,
    password: Annotated[
        str | None,
        typer.Option(envvar="LABFOUNDRY_GNS3_PASSWORD", hide_input=True),
    ] = None,
    compute: Annotated[str, typer.Option(envvar="LABFOUNDRY_GNS3_COMPUTE")] = "local",
    nat_compute: Annotated[
        str | None,
        typer.Option(
            envvar="LABFOUNDRY_GNS3_NAT_COMPUTE",
            help="Optional compute for the built-in NAT node.",
        ),
    ] = None,
    verify_tls: Annotated[bool, typer.Option("--verify-tls/--no-verify-tls")] = True,
    keep_project: Annotated[bool, typer.Option("--keep-project/--destroy-project")] = False,
    keep_on_failure: Annotated[
        bool, typer.Option("--keep-on-failure/--cleanup-on-failure")
    ] = False,
) -> None:
    """Run a scenario end-to-end, including guest actions, captures and cleanup."""
    target_scenario = scenario_opt if scenario_opt is not None else scenario
    if target_scenario is None:
        console.print(
            "[red]preflight failed[/red] scenario must be specified as an argument or "
            "via --scenario"
        )
        raise typer.Exit(2)
    if not target_scenario.exists():
        console.print(
            f"[red]preflight failed[/red] scenario file does not exist: {target_scenario}"
        )
        raise typer.Exit(2)

    resolved_guest_config = guest_config
    if resolved_guest_config is None and _DEFAULT_GUEST_INVENTORY.exists():
        resolved_guest_config = _DEFAULT_GUEST_INVENTORY

    try:
        plan = _compile(target_scenario, seed)
        inventory = (
            load_guest_inventory(resolved_guest_config)
            if resolved_guest_config is not None
            else GuestInventory(profiles={})
        )
    except LabFoundryError as exc:
        console.print(f"[red]preflight failed[/red] {exc}")
        raise typer.Exit(2) from exc

    run_dir = _reserve_run_dir(run_root, plan.scenario_id, plan.root_seed)
    inventory_base = (
        resolved_guest_config.resolve().parent
        if resolved_guest_config is not None
        else target_scenario.resolve().parent
    )

    async def run() -> None:
        async with _client(url, username, password, verify_tls) as client:
            environment = IncusEnvironment.for_plan(plan, inventory, base_dir=inventory_base)
            backend = GNS3Backend(
                client, compute_id=compute, nat_compute_id=nat_compute, environment=environment
            )
            guests = GuestManager(plan, inventory, base_dir=inventory_base)
            runner = ScenarioRunner(
                backend=backend,
                captures=CaptureManager(client, source_root=target_scenario.resolve().parent),
                guests=guests,
                source_root=target_scenario.resolve().parent,
                asset_root=asset_dir,
                run_dir=run_dir,
                destroy_project=not keep_project,
                keep_project_on_failure=keep_on_failure,
            )
            result = await runner.run(plan)
            console.print(
                f"[green]completed[/green] scenario={plan.scenario_id} "
                f"captures={len(result.captures)} run={result.run_dir}"
            )

    try:
        asyncio.run(run())
    except (LabFoundryError, ValueError) as exc:
        console.print(f"[red]run failed[/red] {exc}")
        raise typer.Exit(2) from exc


@gns3_app.command("check")
def gns3_check(
    url: Annotated[str, typer.Option(envvar="LABFOUNDRY_GNS3_URL")] = "http://127.0.0.1:3080",
    username: Annotated[str | None, typer.Option(envvar="LABFOUNDRY_GNS3_USER")] = None,
    password: Annotated[
        str | None,
        typer.Option(envvar="LABFOUNDRY_GNS3_PASSWORD", hide_input=True),
    ] = None,
    compute: Annotated[str, typer.Option(envvar="LABFOUNDRY_GNS3_COMPUTE")] = "local",
    verify_tls: Annotated[bool, typer.Option("--verify-tls/--no-verify-tls")] = True,
) -> None:
    """Verify supported GNS3 2.2 server connectivity and compute capabilities."""

    async def run() -> None:
        async with _client(url, username, password, verify_tls) as client:
            version = await client.assert_supported_2_2()
            compute_info = await client.get_compute(compute)
            templates = await client.list_templates()
            console.print(f"GNS3 Server: [green]{version.version}[/green]")
            console.print(
                f"Compute: {compute_info.compute_id} connected={compute_info.connected} "
                f"capabilities={compute_info.capabilities}"
            )
            virtualbox = [item for item in templates if item.template_type == "virtualbox"]
            console.print(f"VirtualBox templates: {len(virtualbox)}")

    try:
        asyncio.run(run())
    except (LabFoundryError, ValueError) as exc:
        console.print(f"[red]GNS3 check failed[/red] {exc}")
        raise typer.Exit(2) from exc


@gns3_app.command("create-vbox-template")
def create_vbox_template(
    name: Annotated[str, typer.Option(prompt=True)],
    vmname: Annotated[
        str,
        typer.Option(prompt=True, help="Exact VM name registered in VirtualBox."),
    ],
    url: Annotated[str, typer.Option(envvar="LABFOUNDRY_GNS3_URL")] = "http://127.0.0.1:3080",
    username: Annotated[str | None, typer.Option(envvar="LABFOUNDRY_GNS3_USER")] = None,
    password: Annotated[
        str | None,
        typer.Option(envvar="LABFOUNDRY_GNS3_PASSWORD", hide_input=True),
    ] = None,
    compute: Annotated[str, typer.Option(envvar="LABFOUNDRY_GNS3_COMPUTE")] = "local",
    linked_clone: Annotated[bool, typer.Option("--linked-clone/--no-linked-clone")] = True,
    headless: Annotated[bool, typer.Option("--headless/--no-headless")] = True,
    adapters: Annotated[int, typer.Option(min=0, max=36)] = 2,
    ram: Annotated[int, typer.Option(min=0, max=65535)] = 256,
    verify_tls: Annotated[bool, typer.Option("--verify-tls/--no-verify-tls")] = True,
) -> None:
    """Create an official GNS3 VirtualBox template referencing an existing VBox VM."""

    async def run() -> None:
        payload = VirtualBoxTemplateCreate(
            name=name,
            vmname=vmname,
            compute_id=compute,
            linked_clone=linked_clone,
            headless=headless,
            adapters=adapters,
            ram=ram,
        )
        async with _client(url, username, password, verify_tls) as client:
            await client.assert_supported_2_2()
            created = await client.create_virtualbox_template(payload)
            console.print(
                f"[green]created[/green] template={created.name!r} id={created.template_id} "
                f"type={created.template_type}"
            )

    try:
        asyncio.run(run())
    except (LabFoundryError, ValueError) as exc:
        console.print(f"[red]template creation failed[/red] {exc}")
        raise typer.Exit(2) from exc


@gns3_app.command("deploy")
def gns3_deploy(
    scenario: Annotated[Path, typer.Argument(exists=True, dir_okay=False, readable=True)],
    run_root: Annotated[Path, typer.Option()] = Path("runs"),
    seed: Annotated[int | None, typer.Option(help="Override the DSL seed.")] = None,
    url: Annotated[str, typer.Option(envvar="LABFOUNDRY_GNS3_URL")] = "http://127.0.0.1:3080",
    username: Annotated[str | None, typer.Option(envvar="LABFOUNDRY_GNS3_USER")] = None,
    password: Annotated[
        str | None,
        typer.Option(envvar="LABFOUNDRY_GNS3_PASSWORD", hide_input=True),
    ] = None,
    compute: Annotated[str, typer.Option(envvar="LABFOUNDRY_GNS3_COMPUTE")] = "local",
    verify_tls: Annotated[bool, typer.Option("--verify-tls/--no-verify-tls")] = True,
    start: Annotated[bool, typer.Option("--start/--no-start")] = False,
) -> None:
    """Compile and deploy topology to GNS3. Guest OS configuration remains separate."""
    try:
        plan = _compile(scenario, seed)
    except LabFoundryError as exc:
        console.print(f"[red]compile failed[/red] {exc}")
        raise typer.Exit(2) from exc

    run_dir = _reserve_run_dir(run_root, plan.scenario_id, plan.root_seed)

    async def run() -> None:
        async with _client(url, username, password, verify_tls) as client:
            backend = GNS3Backend(client, compute_id=compute)
            deployment = await backend.deploy(plan)
            try:
                if start:
                    await backend.start(deployment)
                write_internal_metadata(run_dir, plan=plan, deployment=deployment)
                (run_dir / "internal" / "deployment.json").write_text(
                    deployment.model_dump_json(indent=2), encoding="utf-8"
                )
            except Exception as exc:
                try:
                    await backend.destroy(deployment)
                except Exception as cleanup_exc:
                    exc.add_note(f"deployment cleanup also failed: {cleanup_exc}")
                if isinstance(exc, LabFoundryError):
                    raise
                raise BackendError(f"failed to finalize deployment metadata: {exc}") from exc
            console.print(
                f"[green]deployed[/green] project={deployment.project_name} "
                f"id={deployment.project_id} run={run_dir}"
            )

    try:
        asyncio.run(run())
    except (LabFoundryError, ValueError) as exc:
        console.print(f"[red]deployment failed[/red] {exc}")
        raise typer.Exit(2) from exc


if __name__ == "__main__":
    app()
