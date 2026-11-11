# Python API reference

The API reference is generated from the `src/labfoundry` source tree with **mkdocstrings** and the Python handler. Signatures, annotations and source links shown here come from the code used to build the documentation.

## Stability

LabFoundry is still pre-1.0. Public modules are documented intentionally, but API compatibility is not yet guaranteed across minor releases. Internal names beginning with `_` are not part of the supported API.

## Packages

- [Schema](schema.md): validated scenario input and shared schema types.
- [Compiler](compiler.md): deterministic plan construction and IPAM.
- [Runtime](runtime.md): clock and event scheduling.
- [Traffic](traffic.md): deterministic recurring traffic timing.
- [Evidence](evidence.md): capture orchestration and PCAP time mapping.
- [Guest control](guest.md): out-of-band guest-management contract.
- [GNS3 backend](gns3.md): typed Controller API integration.
- [Ground truth](groundtruth.md): reproducibility metadata.
- [Utilities](utilities.md): shared parsers and helpers.
- [CLI implementation](cli.md): Typer callbacks and application objects.
