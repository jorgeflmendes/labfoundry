# Captures and evidence

GNS3 captures packets at the infrastructure layer. LabFoundry therefore treats raw capture time and scenario time as separate domains.

## Evidence pipeline

```text
GNS3 link
   ↓
raw PCAP streams
   ↓
PCAP or multi-interface PCAPNG export
   ↓
realism audit
   ↓
published evidence
```

Raw captures are internal evidence. Exported captures are staged until timestamp handling and the
realism audit complete. The runner only publishes accepted evidence under `evidence/`; rejected
material remains under `internal/` for diagnosis.

Capture sources stop before tracked guest processes are terminated. Downloads marked
`refresh_at_end: true` are collected again before the audit, so TLS key logs include late sessions.
Truncated sources are retained unchanged and block export rather than silently losing their tail.
The PCAP parser has a tail-repair utility, but the evidence pipeline does not call it automatically.

## Why timestamps are mapped

Changing the guest operating-system clock does not guarantee that the capture header timestamps produced by the GNS3 compute will use that clock. Set `timestamp_policy: retime` to map records to the scenario clock, or `timestamp_policy: preserve` to retain the original capture timestamps for forensic delivery.

Use `format: pcapng` with named `endpoints` when one artefact should contain multiple capture
interfaces. For a whole switched subnet, use `segments: [office]`: the compiler captures every
attached link. Capturing only the router uplink misses same-subnet unicast traffic between other
hosts. PCAPNG section and interface metadata are declared in the scenario, not inferred from the
local runner.

Classic PCAP is the default and supports one endpoint. Multi-interface and segment captures require
PCAPNG. A compiled link can belong to only one capture declaration.

With `timestamp_policy: retime`, both formats apply the same piecewise mapping for `warp_gaps`.
`preserve` leaves packet timestamps and bytes unchanged. Retimed exports rewrite TCP timestamp
options and checksums when gap warping or a speed other than 1.0 is configured. Rewriting first scans
the complete capture to measure clock rates, then rewrites packets. This includes rates needed for
echoed timestamps before enough packets from the peer have appeared in the output pass. It measures
the sender's tick rate; export fails when a required rate cannot be established. Deduplication
matches observations across input interfaces and preserves repeated records within an interface.
The internal `capture-clock.json` stores the anchors and gap mappings after the raw captures
are finalized and before guest cleanup. If cleanup or export fails, those mappings remain
available for reprocessing the raw captures.

The realism audit checks for empty captures and observation points, virtual NIC OUIs, lab-identifying
payload text, duplicate DHCP hostnames, non-browser traffic claiming a browser user agent, DNS
answer-type mismatches, stale HTTP dates, high NXDOMAIN rates, TCP SYN bursts, low Ethernet source
diversity, and idle gaps beyond a configured limit. When `tshark` is available, it also flags a
high rate of out-of-order TCP packets or missing-segment indications. An optional baseline adds
volume, protocol and source-diversity comparisons. Accelerated retiming is flagged because guest
protocol timers do not follow mapped packet time.

If staged `.keys` files exist, including in subdirectories, the audit uses `tshark` to inspect
decrypted traffic for fatal TLS alerts, HTTP/HTTP2 errors, HTTP date skew and resources with a
`Last-Modified` date more than five minutes after capture time. It matches captured
ClientHello randoms to TLS secrets in both directions for streams with encrypted application data
or undecoded TLS records. When the same TCP segment is seen twice and one copy decodes as a TLS
handshake, the opaque copy does not trigger a missing-key error. Captures starting mid-session
are flagged separately. Matching secrets do not prove that every application
message was decoded. Missing `tshark`, an empty key log, or a failed decryption
check produces a warning rather than a successful inspection. Findings are written to
`internal/realism-audit.json`. Configure the publication threshold with
`evidence.realism_audit.fail_on`: `error` (the default) blocks on errors, `warning` blocks on any
finding, and `never` records findings without blocking publication. Published downloads, including
key logs, are staged until the capture passes this gate.

The core does not enable application key logging or normalize downloaded key files. Configure the
browser or TLS service to write NSS-format secrets and collect them with `guest.download` using
`publish: true`, a `.keys` destination, and `refresh_at_end: true` for growing logs. Only staged
`*.keys` files are discovered by the decryption audit; private downloads and files with other
extensions are not included automatically. Load the published keys in Wireshark's TLS preferences.
Key logs expose session contents and need the same handling as decrypted evidence.

`evidence.realism_audit.enabled: false` skips the audit; `fail_on: never` still writes its report.
Neither setting establishes that evidence meets a scenario's acceptance criteria. The audit does
not inspect arbitrary published downloads or verify every narrative event and required artifact.

## Run records and failures

`internal/scenario-plan.json` records compiled choices; `internal/ground-truth.json` records intended
events and nodes, rather than proving their execution. The journal records action starts, completions,
skips and failures. `manifest.json` hashes all files published under `evidence/` and includes private
controller/template metadata; distribute selected evidence rather than the whole run directory.

Runtime failure does not itself block publication: already-started captures can still pass finalization
and auditing. Such captures remain diagnostics from a failed run. Check `manifest.json`'s
`extra.status`, journal failures, scenario-specific receipts and required artifacts before delivery.
When capture finalization fails, the audit/publication path is not reached. Raw files and completed
staged exports may still be available, but their presence does not imply a complete capture set.

## Troubleshooting

- Empty endpoints often indicate a quiet link or incorrect capture placement. Router uplinks do
  not capture all lateral switched traffic; compare the compiled diagram with the intended flows.
- A truncated record or early stream EOF fails export. Keep the raw source for diagnosis; do not
  silently repair it and present the repaired file as a complete run.
- A TCP clock-rate error during retiming means the source lacks the observations needed for
  rewriting. Inspect the raw capture and required timestamp options before rerunning.
- Missing TLS secrets require checking key logging on both TLS legs and collecting the final
  files. A decryption warning is not proof of successful decoding.
- Unexpected capture dates require checking runner/compute clock alignment and the selected
  timestamp policy. Guest clock changes alone do not set GNS3 capture timestamps.

See [Architecture](../advanced/architecture.md) for the broader separation between scenario semantics and infrastructure execution.
