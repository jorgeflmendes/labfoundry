# Installation

LabFoundry requires Python 3.12 or newer. GNS3 and a configured emulator are required for deployment;
validation and compilation work without a hypervisor.

## Source install

Clone the repository and install it in a virtual environment:

```bash
git clone https://github.com/jorgeflmendes/LabFoundry.git
cd LabFoundry
python -m venv .venv
source .venv/bin/activate  # PowerShell: .venv\Scripts\Activate.ps1
python -m pip install -U pip
python -m pip install -e .
labfoundry --help
```

To test a wheel built locally:

```bash
python -m pip install build
python -m build
python -m pip install dist/labfoundry-0.2.0-py3-none-any.whl
labfoundry --help
```

Continue with the [quickstart](quickstart.md) to prepare and validate your own scenario.

## Published releases

When a release is available on PyPI, install it without cloning the repository:

```bash
python -m pip install labfoundry
```

## Development environment

```bash
python -m venv .venv
source .venv/bin/activate  # PowerShell: .venv\Scripts\Activate.ps1
python -m pip install -U pip
python -m pip install -e '.[dev]'
```

Verify the installation:

```bash
labfoundry --help
python -m pytest
```

## Documentation environment only

If you only need to build the documentation:

```bash
python -m pip install -e '.[docs]'
mkdocs build --strict
```

For local live preview:

```bash
mkdocs serve
```

The documentation configuration points `mkdocstrings` directly at `src/`, so API collection does not depend on importing LabFoundry from an installed wheel.

## Runtime dependencies

Execution requires GNS3 Server `>=2.2.61,<2.3.0`, the templates named in the scenario, and the
host tools for its guest transport. VirtualBox needs `VBoxManage` on the runner host and working
Guest Additions in each guest. SSH needs the system `ssh` and `scp` clients and a key accepted
by the Linux guest.
These executables are not installed by the Python package.

Incus mode also needs a prepared Linux VirtualBox base snapshot, a host-only network and local
container images. [Incus environments](../guide/incus.md) lists the VM and inventory requirements.

Linux networking uses `ip`, `hostnamectl` for VM/SSH guests, and non-interactive sudo for non-root
control accounts. DHCP defaults to `dhclient`; guest profiles can select another client.
Windows networking and clock changes require an administrative control account.
Application tools, services and Python libraries used by a scenario belong in its images or
pre-capture provision actions. Bootstrap bundles help prepare new images; they do not install
or register a VM automatically. See [VirtualBox](../guide/virtualbox.md).

`tshark` is optional for the core audit's TCP analysis and required for its TLS decryption checks.
Unavailable TCP analysis is skipped; unavailable TLS inspection with usable key logs records a
warning. The Python package does not install Wireshark or `tshark`.
Validation, compilation and SVG generation need none of these
runtime tools. The `dev` extra includes the documentation dependencies.
