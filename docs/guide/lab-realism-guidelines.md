# Lab realism guidelines

These are authoring and acceptance guidelines, not guarantees enforced by the framework. The aim
is traffic consistent with the network and story without depending on third parties during a run.
Controlled DNS, addressing, certificates and content remain inside the isolated lab topology.

## Core rules

1. Generate traffic through real actions. A browser opens the page, an FTP client transfers the file, an IRC client maintains its session and a mail service delivers the message. Packet injection is reserved for cases that cannot be produced by an ordinary client or service.
2. Preserve the narrative rather than defects in a reference capture. The source material defines the events, people, order and evidence that matter. A protocol signature may differ when the replacement is more plausible and does not change what the student must discover.
3. Use real protocol implementations. A controlled service still follows the protocol state machine, negotiation, errors, retries, keep-alive and shutdown behaviour. Similar-looking payloads are not sufficient.
4. Keep one timeline. Guest clocks, packet timestamps, HTTP headers, certificate validity, file and account metadata, logs and visible page content must agree with the scenario date. Installed software and bundled material must not post-date the scenario.
5. Do not make the network unnaturally clean. Plausible DNS failures, blocked resources, redirects, rejected authentication and retries belong in normal traffic. They should come from client and service behaviour, not unexplained random faults.
6. Keep simulation inside the lab. Public-looking names and addresses may be routed internally, but manipulated traffic must never reach the corresponding real service. A lab CA must not claim to be a real public CA.

## External services and web content

Workstations use the enterprise proxy for external-looking traffic unless the scenario states a specific reason for a direct connection.

Choose content in this order:

1. a genuine archived version from no later than the scenario date;
2. a current capture reviewed and stripped of later dates, events and identifiers;
3. a controlled copy based on the genuine structure and resources;
4. a purpose-built service when no usable counterpart exists.

A controlled copy must preserve the observable application behaviour needed by the exercise: navigation, paths, methods, redirects, status codes, headers, MIME types, secondary resources, caching, cookies, persistent connections and response cadence. The guest creates a fresh session against the controlled service. Recorded TCP or TLS packets are not replayed.

Current content is inspected before it enters a scenario bundle. Later dates, news, versions, copyright years and identifiers are replaced deterministically where they contradict the story. The final resources are stored with the scenario so later runs do not depend on Internet availability.

An unknown route must not silently return a generic HTTP 200 page. It should return the plausible result for that service: 404, a proxy block, DNS failure or another explicit response. Known acceptable failures are recorded so that an incomplete run is not mistaken for normal behaviour.

## DNS, addressing and TLS

The enterprise DNS server resolves internal hosts, controlled public-looking services and names used by background activity. Unapproved queries do not escape to the Internet.

Public-looking addresses exist only inside the isolated topology. DNS records, routes and proxy mappings must agree. A service represented as external also needs a plausible network path, RTT, jitter, throughput and server response time.

Certificates must match the hostname, scenario date and role:

- subject and SAN match the names used by clients;
- chain, extensions, algorithms and validity are plausible for the simulated date;
- internal services may use the company CA;
- an external-looking controlled service uses a separate lab-controlled CA with a plausible name that does not imitate a real public issuer;
- the proxy inspection CA remains distinct from service issuer CAs.

TLS secrets are exported only when the exercise requires them. Their source may be managed-endpoint
key logging, browser instrumentation or authorised TLS inspection at the proxy. The scenario must
produce NSS-format keys and declare their collection; the core copies files without normalizing
them. Publish a `.keys` file for the decryption audit and record its source and scope privately.

## Background traffic and time

Background traffic comes from several employees and hosts, not only the protagonist. It may include DNS, NTP, mDNS, browsing, short sessions, approved update checks, third-party page resources and ordinary internal communication.

A seed makes a run reproducible without making it periodic. Generators vary intervals, destinations, duration, size and activity mix. Background traffic is unrelated to the story and does not fill every gap with the same pattern.

When a scenario uses time warping, long narrative gaps may contain unimportant activity. It is distributed over narrative time before packet retiming so the trace remains continuous without making the run last for the full story duration.

Reading, typing and decision delays belong in the scenario. Network and application delays belong in the route or service. A local service standing in for a remote one receives realistic RTT, jitter, bandwidth and response timing; equal sleeps before every request are not a substitute.

## Scenario actions and artefacts

The YAML declares what happens, when, who participates and which result is required. Scripts perform the concrete action and return a verifiable receipt. They should not hide narrative decisions that belong in the scenario.

Synthetic artefacts are acceptable when their format, content and metadata fit the story. Names, authors, internal timestamps, application versions, sizes and expected hashes must agree. Downloads and attachments are checked byte-for-byte before a later event depends on them.

Long-lived sessions include normal maintenance behaviour. IRC may negotiate capabilities and produce `MODE`, `NOTICE`, `PING` and `PONG`; FTP uses the active or passive mode of the selected client; browsers load page resources and retain connections instead of issuing one artificial request.

When the story describes a person using a site, drive a real browser inside the guest. Direct HTTP scripts remain appropriate for infrastructure and non-interactive clients, but do not replace browser behaviour in user-facing events.

## Capture lifecycle

Capture starts before the first event and stops when scheduled runtime work ends. Without
`clock.duration`, there is no separate drain period after the last event. A final event must wait
for any response or receipt that belongs in the capture; the runner checks that capture streams
close correctly.

Capture placement covers workstation and server links, lateral office traffic, scans and attacks, both proxy sides when required, DNS and the controlled external segment. Traffic between two switched hosts does not necessarily cross the router or proxy, so capture coverage follows the topology rather than assuming a single observation point.

Treat an interrupted run or one without its required receipt as incomplete. The runner may publish
diagnostic captures after a failure if finalization and audit succeed; it does not enforce a
scenario's final receipt. Check run status and scenario-specific assertions before delivery.
Project deletion is the default; retention flags keep a stopped project for inspection.

`labfoundry diagram` renders the compiled scenario before GNS3 starts. It shows node types with
distinct symbols, links, segments, routers, services and capture points. Compare it with the
deployed project when checking runtime changes or capture placement.

## Acceptance checks

The framework validates declared capture links, capture completeness and configured action results.
Its realism audit checks selected packet, timing, DNS, HTTP and TLS anomalies and optional baseline
differences. It does not verify all required artifacts, event receipts, repetitive cadence, or
successful cleanup of every guest process. Those require scenario-specific checks and review.

Scenario checks cover expected flows, ports, paths, methods, protocol markers, participants, addresses, event order, dependencies, download integrity, action duration and the background activity expected from active hosts.

Automated checks do not replace manual review. Each PCAP is followed through its own timeline and compared with the narrative. The complete set is then reviewed for contradictions across traces.

## Common fixes

- If a live page has disappeared, try an archive first. If it is incomplete, capture the current site once and remove only facts that contradict the date. Build a controlled copy only when neither source is usable.
- If a session is too short, check that the real application ran, secondary resources exist, caching and persistent connections work, and the scenario includes reading or interaction time. Random padding does not fix an artificial action.
- If a local service is too fast, model both path and application delay: RTT, jitter, time to first byte, progressive delivery and bandwidth limits. Vary them by destination and request.
- If DNS is sparse, make clients use internal DNS and load genuine page resources, including permitted third-party origins. Add ordinary activity from several hosts rather than meaningless random names.
- If traffic is too clean, retain natural failures such as a missing favicon, blocked tracker, rejected login, cache revalidation or unresolved name. A failure that prevents the narrative event remains a run error.
- If warped gaps are visible, generate seeded but irregular activity across several hosts and distribute it over narrative time before retiming. Audit density by time window, not just first and last packet.
- If lateral traffic is absent, capture the links or segments that actually carry it. A router or proxy cannot observe traffic that remains on a switched LAN.
- If capture stops early, make the final action wait for the required responses and receipts. A
  detached process or packet still in flight is not kept alive by a post-event drain. Exceptions
  and timeouts mark the run incomplete.
- If later dates appear, inspect content, headers, certificates, cookies, version strings, archives, documents and guest metadata. Fix the source and regenerate; changing packet timestamps alone leaves visible contradictions.

## Ownership

LabFoundry supplies multi-segment capture, clock handling and retiming, seeded background-event
scheduling, command-result conditions, auditing and cleanup. Scenarios supply services, certificate
issuance, delay queues and application receipts through their scripts and configuration.

Each scenario remains responsible for its topology, narrative, identities, content, authorised deviations, actions, protocol parameters and required indicators. Auditing can find contradictions, but it cannot infer a story omitted from the YAML.

Evidence is final only after a complete run finishes without errors, the realism audit passes, scenario-specific verification passes and manual PCAP review finds no material contradiction.
