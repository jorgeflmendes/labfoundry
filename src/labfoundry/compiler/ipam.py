"""Deterministic IPv4 address allocation."""

from __future__ import annotations

from ipaddress import IPv4Address, IPv4Interface, IPv4Network

from labfoundry.compiler.seeds import derive_seed
from labfoundry.errors import CompilationError


class DeterministicIPAM:
    """Allocate stable addresses without materializing an entire subnet."""

    def __init__(self, root_seed: int) -> None:
        self._root_seed = root_seed
        self._used: dict[IPv4Network, set[IPv4Address]] = {}

    def reserve(self, network: IPv4Network, interface: IPv4Interface, *, owner: str) -> None:
        """Reserve an explicit address and reject duplicate or out-of-network assignments."""
        if interface.network != network:
            raise CompilationError(
                f"Static address {interface} for {owner} does not belong to segment {network}"
            )
        self._reserve_address(network, interface.ip, owner=owner)

    def allocate(self, network: IPv4Network, *, owner: str, namespace: str) -> IPv4Interface:
        """Allocate the next deterministic usable address for a stable logical key."""
        usable = self._usable_count(network)
        if usable <= 0:
            raise CompilationError(f"Segment {network} has no usable host addresses")

        seed = derive_seed(self._root_seed, f"ipam:{namespace}")
        start = seed % usable
        for probe in range(usable):
            index = (start + probe) % usable
            address = self._host_at(network, index)
            if address not in self._used.setdefault(network, set()):
                self._reserve_address(network, address, owner=owner)
                return IPv4Interface(f"{address}/{network.prefixlen}")
        raise CompilationError(f"No free addresses remain in {network} for {owner}")

    def _reserve_address(self, network: IPv4Network, address: IPv4Address, *, owner: str) -> None:
        if address == network.network_address or (
            network.prefixlen <= 30 and address == network.broadcast_address
        ):
            raise CompilationError(f"Address {address} for {owner} is not a usable host address")
        used = self._used.setdefault(network, set())
        if address in used:
            raise CompilationError(f"Address {address} is allocated more than once in {network}")
        used.add(address)

    @staticmethod
    def _usable_count(network: IPv4Network) -> int:
        if network.prefixlen <= 30:
            return max(0, network.num_addresses - 2)
        return network.num_addresses

    @staticmethod
    def _host_at(network: IPv4Network, index: int) -> IPv4Address:
        offset = index + 1 if network.prefixlen <= 30 else index
        return IPv4Address(int(network.network_address) + offset)
