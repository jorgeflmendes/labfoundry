from pathlib import Path
from typing import Any

import pytest
import yaml


@pytest.fixture
def scenario_data() -> dict[str, Any]:
    return {
        "scenario": {"id": "test-network", "seed": 834927},
        "clock": {"start": "2025-04-17T08:00:00+01:00", "duration": "2h"},
        "network": {
            "segments": {
                "users": {"subnet": "10.10.10.0/24"},
                "servers": {"subnet": "10.20.20.0/24"},
                "simulated_internet": {"subnet": "198.51.100.0/24"},
            },
            "nodes": {
                "gateway": {
                    "template": "test-gateway",
                    "template_type": "virtualbox",
                    "guest_profile": "linux-vbox",
                    "ipv4_forwarding": True,
                    "interfaces": {
                        "users": {"segment": "users", "ip": "10.10.10.1/24", "adapter": 0},
                        "servers": {"segment": "servers", "ip": "10.20.20.1/24", "adapter": 1},
                        "wan": {
                            "segment": "simulated_internet",
                            "ip": "198.51.100.1/24",
                            "adapter": 2,
                        },
                    },
                },
                "ws": {
                    "template": "test-client",
                    "count": 3,
                    "interfaces": {"eth0": {"segment": "users"}},
                },
                "dns": {
                    "template": "test-dns",
                    "interfaces": {"eth0": {"segment": "servers"}},
                },
                "external": {
                    "template": "test-external",
                    "interfaces": {"eth0": {"segment": "simulated_internet"}},
                },
            },
        },
        "traffic": {
            "profiles": {
                "office-user": {
                    "actions": [
                        {
                            "action": "guest.exec",
                            "to": "dns",
                            "interval": {"distribution": "uniform", "min": "10s", "max": "60s"},
                            "parameters": {"script": "true"},
                        },
                        {
                            "action": "guest.exec",
                            "to": "external",
                            "interval": {"distribution": "exponential", "mean": "45s"},
                            "parameters": {"script": "true"},
                        },
                    ]
                }
            },
            "assignments": [{"profile": "office-user", "nodes": "ws"}],
        },
        "events": [
            {
                "id": "probe",
                "at": "09:17:32",
                "select": {"from": "ws", "count": 1, "as": "selected"},
                "action": "guest.exec",
                "from": "selected",
                "to": "dns",
                "parameters": {"script": "true"},
            },
            {
                "id": "connect",
                "after": {"event": "probe", "delay": "2s"},
                "action": "guest.exec",
                "from": "selected",
                "to": "external",
                "parameters": {"script": "true"},
            },
            {
                "id": "transfer",
                "after": "8s",
                "action": "guest.exec",
                "from": "selected",
                "to": "external",
                "parameters": {"script": "true"},
            },
        ],
        "captures": [
            {"name": "users-perimeter", "endpoint": {"node": "gateway", "interface": "users"}},
            {"name": "wan-perimeter", "endpoint": {"node": "gateway", "interface": "wan"}},
        ],
    }


@pytest.fixture
def scenario_path(tmp_path: Path, scenario_data: dict[str, Any]) -> Path:
    path = tmp_path / "scenario.yaml"
    path.write_text(yaml.safe_dump(scenario_data, sort_keys=False), encoding="utf-8")
    return path
