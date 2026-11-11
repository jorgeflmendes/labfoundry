from pathlib import Path

import pytest

from labfoundry.backends.gns3.backend import DeploymentState
from labfoundry.compiler.compiler import ScenarioCompiler
from labfoundry.guest.config import (
    GuestInventory,
    SSHGuestProfile,
    VirtualBoxGuestProfile,
)
from labfoundry.guest.factory import DefaultGuestExecutorFactory
from labfoundry.guest.ssh import SSHGuestExecutor
from labfoundry.guest.virtualbox import VirtualBoxGuestExecutor
from labfoundry.schema.loader import load_scenario


@pytest.mark.parametrize("transport", ["virtualbox", "ssh"])
def test_factory_binds_builtin_transports(
    tmp_path: Path, scenario_path: Path, monkeypatch, transport: str
) -> None:
    monkeypatch.setenv("LF_FACTORY_PASSWORD", "secret")
    identity = tmp_path / "id_ed25519"
    identity.write_text("fixture", encoding="utf-8")
    profiles = {
        "virtualbox": VirtualBoxGuestProfile(
            os="linux", username="operator", password_env="LF_FACTORY_PASSWORD"
        ),
        "ssh": SSHGuestProfile(username="root", host="{node}.test", identity_file=identity),
    }
    profile = profiles[transport]
    factory = DefaultGuestExecutorFactory(
        GuestInventory(profiles={"guest": profile}), base_dir=tmp_path
    )
    node = ScenarioCompiler().compile(load_scenario(scenario_path)).nodes[0]
    deployment = DeploymentState(
        project_id="project-id",
        project_name="project",
        gns3_version="2.2.61",
        guest_runtime_ids={node.name: "runtime-vm"},
        template_metadata={node.name: {"template_type": transport}},
    )

    factory.validate(node, profile)
    executor = factory.create(node, profile, deployment)

    if transport == "virtualbox":
        assert isinstance(executor, VirtualBoxGuestExecutor)
        assert executor.vm_name == "runtime-vm"
        assert executor.password == "secret"
    else:
        assert isinstance(executor, SSHGuestExecutor)
        assert executor.host == f"{node.name}.test"
