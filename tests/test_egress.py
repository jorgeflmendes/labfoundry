from __future__ import annotations

import pytest
from pydantic import ValidationError

from labfoundry.compiler.compiler import ScenarioCompiler
from labfoundry.schema.scenario import NetworkSpec, ScenarioSpec


@pytest.mark.parametrize("egress", [None, {"segment": "uplink"}])
def test_nat_templates_cannot_bypass_egress_configuration(egress) -> None:
    with pytest.raises(
        ValidationError, match=r"NAT nodes must be declared through network\.egress"
    ):
        NetworkSpec.model_validate(
            {
                "segments": {"uplink": {"subnet": "192.168.122.0/24"}},
                "nodes": {"edge": {"template": "NAT", "template_type": "nat"}},
                "egress": egress,
            }
        )


def test_nat_egress_reserves_switch_port_and_dhcp_is_not_ipam_allocated() -> None:
    spec = ScenarioSpec.model_validate(
        {
            "version": "0.2",
            "scenario": {"id": "egress", "seed": 1},
            "clock": {"start": "2025-01-01T00:00:00+00:00", "duration": "1m"},
            "network": {
                "segments": {"uplink": {"subnet": "192.168.122.0/24"}},
                "egress": {"mode": "nat", "segment": "uplink"},
                "nodes": {
                    "router": {
                        "template": "router",
                        "interfaces": {"wan": {"segment": "uplink", "ip": "dhcp", "adapter": 0}},
                    }
                },
            },
        }
    )
    plan = ScenarioCompiler().compile(spec)

    assert plan.egress is not None
    assert plan.egress.segment == "uplink"
    assert plan.egress.switch_port == 1
    assert plan.nodes[0].interfaces[0].address == "dhcp"
    assert plan.segments[0].port_count >= 2


def test_nat_egress_is_explicit_and_cloud_nodes_are_denied_by_default() -> None:
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="allow_cloud_nodes"):
        ScenarioSpec.model_validate(
            {
                "version": "0.2",
                "scenario": {"id": "cloud", "seed": 1},
                "clock": {"start": "2025-01-01T00:00:00+00:00"},
                "network": {
                    "segments": {"wan": {"subnet": "192.0.2.0/24"}},
                    "nodes": {
                        "cloud": {
                            "template": "host-cloud",
                            "template_type": "cloud",
                            "interfaces": {"eth0": {"segment": "wan", "ip": "auto"}},
                        }
                    },
                },
            }
        )
