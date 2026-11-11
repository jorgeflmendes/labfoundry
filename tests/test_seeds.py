from labfoundry.compiler.seeds import derive_seed


def test_namespaces_are_stable_and_independent() -> None:
    assert derive_seed(123, "ipam") == derive_seed(123, "ipam")
    assert derive_seed(123, "ipam") != derive_seed(123, "traffic")
    assert derive_seed(123, "ipam") != derive_seed(124, "ipam")
