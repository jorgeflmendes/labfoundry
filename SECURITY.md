# Security model

LabFoundry is intended for controlled teaching/research cyber ranges. It can deliberately execute
arbitrary commands in lab guests and can optionally provide real Internet egress, so **scenario and
guest-inventory files are trusted operator input, not a sandbox for untrusted code**.

## Default boundaries

- No public Internet route is created unless `network.egress` is explicitly declared.
- NAT templates are rejected in `network.nodes`; NAT must use `network.egress`.
- GNS3 Cloud templates are rejected unless `network.allow_cloud_nodes: true` is set explicitly.
- GNS3 Controller access belongs to the control plane and should not traverse student data-plane
  capture points.
- VirtualBox Guest Additions is preferred for full-VM out-of-band control when available.
- SSH management paths are operator-defined; keep them outside student capture points if evidence
  isolation matters.

## Secrets

- Guest passwords/keys do not belong in scenario YAML, compiled plans, ground truth or evidence.
- VirtualBox passwords may be sourced from environment variables or separate files. LabFoundry passes
  them to VBoxManage via a temporary password file and deletes it after use. POSIX permissions are
  mode 0600; Windows uses the temporary directory's ACLs. Persistent secret files need operator-managed
  permissions. The inventory loader does not check file permissions.
- SSH profiles are key-based; private-key paths live in the separate guest inventory.
- GNS3 Basic authentication does not encrypt HTTP. Use HTTPS or a protected VPN/management network
  when the controller is remote.

## File safety

- `guest.upload` confines resolved source paths to the scenario directory, the selected `--asset-dir`,
  or the two bundled `@helpers/` files. Sources must resolve to regular files. Symlinks resolving
  within an allowed root are accepted after resolution; path checks do not protect against concurrent
  filesystem changes.
- `guest.download` confines resolved destinations to `internal/downloads/`, or to
  `internal/staged-evidence/` when `publish: true` is set. Published downloads move to `evidence/`
  after the capture audit accepts them.
- run directories are not silently reused when non-empty.
- streamed PCAP writes use temporary files and atomic replacement.

## Real-world side effects

A command executed inside a NAT-connected guest can contact real external systems. Use only accounts,
domains, recipients and APIs authorized for the laboratory. Do not use LabFoundry to generate spam,
probe unrelated systems or execute untrusted workloads on infrastructure containing sensitive data.

Instructor-only material includes `internal/ground-truth.json`, raw captures and deployment metadata.
The root `manifest.json` also contains deployment identifiers and template metadata. Journals record
error text supplied by callers and do not redact it. Scenario commands, their output and exported
TLS key logs may contain sensitive material even though guest inventory secrets are kept separate.
Review evidence bundles before distribution when a scenario intentionally interacts with public
services.
