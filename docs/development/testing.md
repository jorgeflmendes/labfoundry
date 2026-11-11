# Testing

Run the portable checks before every change:

```bash
python -m pytest
python -m ruff check .
python -m ruff format --check .
python -m mypy src
python -m mkdocs build --strict
```

The package CI job extracts the source distribution and runs the portable suite from that tree.
Keep test fixtures, documentation and workflow files in `MANIFEST.in` when tests depend on them.

## Real integration tests

The opt-in tests in `tests/integration/` exercise installed GNS3, VirtualBox and Incus.

```bash
export LABFOUNDRY_INTEGRATION=1
export LABFOUNDRY_GNS3_URL=http://127.0.0.1:3080
export LABFOUNDRY_GNS3_COMPUTE=local
pytest -m integration
```

Set `LABFOUNDRY_GNS3_USER` and `LABFOUNDRY_GNS3_PASSWORD` if the controller uses Basic authentication. To include the VirtualBox lifecycle test, set `LABFOUNDRY_TEST_VBOX_TEMPLATE` to an available template name.

The OpenSSH integration test uses Docker to run the test server and requires the system
`ssh`, `scp`, `ssh-keygen`, and `ssh-keyscan` clients:

```bash
export LABFOUNDRY_GUEST_INTEGRATION=1
pytest tests/integration/test_guest_transports_real.py
```

The Incus acceptance test requires a prepared [scenario environment](../guide/incus.md) and a
local GNS3 compute. Set `LABFOUNDRY_TEST_INCUS_INVENTORY` to the private guest inventory path
and `LABFOUNDRY_TEST_INCUS_IMAGE` to a preloaded container image alias or fingerprint.
`LABFOUNDRY_TEST_INCUS_PROFILE` selects the guest profile and defaults to `incus`.
The image must include Python 3 for the HTTP workload. Run
`pytest tests/integration/test_incus_real.py` to check container identities, private files,
command cancellation, shared historical time, Ethernet traffic, snapshots and capture continuity.

The integration suite creates uniquely named temporary resources and removes them during cleanup. It covers controller capability checks, project and topology lifecycle, PCAP capture/download, project notifications, VirtualBox nodes, and the OpenSSH guest transport.

For an environment change, also exercise the target Linux and Windows guests: Guest Additions operations, isolated NAT egress, snapshot lifecycle, large capture export, and failure rollback. Keep any site-specific credentials and host paths outside the repository.
