import pytest
from pydantic import ValidationError

from labfoundry.backends.gns3.models import VirtualBoxTemplateCreate


def test_virtualbox_schema_rejects_unknown_adapter() -> None:
    with pytest.raises(ValidationError):
        VirtualBoxTemplateCreate(  # type: ignore[arg-type]
            name="bad",
            vmname="bad",
            adapter_type="not-a-vbox-adapter",
        )


def test_virtualbox_schema_supports_upstream_adapter_limit() -> None:
    model = VirtualBoxTemplateCreate(name="vm", vmname="vm", adapters=36)
    assert model.adapters == 36


def test_virtualbox_custom_adapter_is_typed_and_bounded() -> None:
    model = VirtualBoxTemplateCreate(
        name="vm",
        vmname="vm",
        adapters=2,
        custom_adapters=[
            {
                "adapter_number": 1,
                "port_name": "eth1",
                "mac_address": "02:00:00:00:00:01",
            }
        ],
    )
    assert model.custom_adapters[0].adapter_number == 1

    with pytest.raises(ValidationError):
        VirtualBoxTemplateCreate(
            name="vm",
            vmname="vm",
            adapters=1,
            custom_adapters=[{"adapter_number": 1}],
        )
