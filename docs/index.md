# LabFoundry

**LabFoundry** compiles deterministic cyber-range scenarios, runs guest activity through GNS3 and
exports packet captures with private execution metadata.

The framework keeps scenario semantics separate from infrastructure execution. A scenario describes topology, addressing, traffic, events, captures and time; the GNS3 backend materializes that plan without exposing controller details to scenario authors.

## Start here

<div class="grid cards" markdown>

-   **Install LabFoundry**

    ---

    Create a development environment and install the framework.

    [:octicons-arrow-right-24: Installation](getting-started/installation.md)

-   **Build a first scenario**

    ---

    Validate, inspect and compile a deterministic YAML scenario.

    [:octicons-arrow-right-24: Quickstart](getting-started/quickstart.md)

-   **Scenario DSL**

    ---

    Read the language reference for networks, traffic, events and captures.

    [:octicons-arrow-right-24: DSL reference](reference/scenario-dsl.md)

-   **Python API**

    ---

    Browse signatures, types and docstrings generated directly from the source tree.

    [:octicons-arrow-right-24: API reference](reference/api/index.md)

</div>

## Design goals

LabFoundry is built around a small set of constraints:

- deterministic compilation from a scenario and seed;
- strict validation before infrastructure changes;
- GNS3 Controller API integration behind a typed backend;
- VirtualBox for full virtual machines;
- generic guest execution kept separate from application-specific behavior;
- explicit, opt-in GNS3 NAT egress for real-world connectivity;
- raw evidence kept separate from exported evidence;
- reproducible manifests and private ground truth.

## Documentation model

The documentation is split into five layers:

1. **Getting started** for installation and first use.
2. **Guide** for scenario authoring and operating LabFoundry.
3. **Advanced** for architecture and controller details.
4. **Reference** for the DSL, CLI and Python API.
5. **Development** for maintainers and contributors.

The Python API reference is rendered from the code with `mkdocstrings`, so signatures and type annotations stay tied to the implementation.
