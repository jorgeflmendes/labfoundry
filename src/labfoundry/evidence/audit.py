"""Evidence realism checks for packet captures produced by LabFoundry."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from datetime import UTC
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Literal

from labfoundry.evidence.pcap import PcapRecord, PcapTimestampMapper

AuditSeverity = Literal["warning", "error"]
_VIRTUAL_OUIS = {
    b"\x08\x00\x27": "VirtualBox",
    b"\x00\x05\x69": "VMware",
    b"\x00\x0c\x29": "VMware",
    b"\x00\x50\x56": "VMware",
    b"\x00\x15\x5d": "Hyper-V",
    b"\x52\x54\x00": "QEMU/KVM",
}
_LAB_MARKER_PATTERN = re.compile(
    rb"(?<![a-z0-9])(?:labfoundry|rangeforge|gns3|virtualbox|vbox)(?![a-z0-9])"
)


@dataclass(frozen=True, slots=True)
class AuditFinding:
    """One realism issue detected in packet-capture evidence."""

    severity: AuditSeverity
    code: str
    message: str


@dataclass(frozen=True, slots=True)
class RealismAuditResult:
    """Machine-readable evidence realism result written before publication."""

    findings: tuple[AuditFinding, ...]
    packets: int
    source_macs: int
    protocols: tuple[str, ...]
    dns_responses: int
    dns_nxdomain_responses: int
    max_tcp_syns_per_second: int
    baseline: Path | None = None

    def should_fail(self, threshold: Literal["never", "error", "warning"]) -> bool:
        """Return whether findings meet the scenario's delivery-blocking threshold."""
        if threshold == "never":
            return False
        if threshold == "warning":
            return bool(self.findings)
        return any(item.severity == "error" for item in self.findings)

    def write(self, path: Path) -> None:
        """Atomically write a stable JSON audit report."""
        payload = {
            "findings": [asdict(item) for item in self.findings],
            "packets": self.packets,
            "source_macs": self.source_macs,
            "protocols": list(self.protocols),
            "dns_responses": self.dns_responses,
            "dns_nxdomain_responses": self.dns_nxdomain_responses,
            "max_tcp_syns_per_second": self.max_tcp_syns_per_second,
            "baseline": None if self.baseline is None else str(self.baseline),
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=path.parent,
                prefix=f".{path.name}.",
                suffix=".tmp",
                delete=False,
            ) as handle:
                temporary_path = Path(handle.name)
                json.dump(payload, handle, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            assert temporary_path is not None
            temporary_path.replace(path)
        except Exception:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
            raise


@dataclass(frozen=True, slots=True)
class _CaptureMetrics:
    packets: int
    max_idle_gap_seconds: float
    source_macs: frozenset[bytes]
    protocols: frozenset[str]
    virtual_ouis: frozenset[str]
    lab_markers: frozenset[str]
    duplicate_dhcp_hostnames: frozenset[str]
    browser_impersonation: bool
    dns_answer_mismatches: int
    stale_http_dates: int
    dns_responses: int
    dns_nxdomain_responses: int
    max_tcp_syns_per_second: int


def audit_captures(
    paths: Sequence[Path],
    *,
    baseline: Path | None = None,
    clock_speed: float,
    retimed: bool,
    warped: bool = False,
    max_idle_gap_seconds: float | None = None,
    keylog_files: Sequence[Path] = (),
) -> RealismAuditResult:
    """Inspect exported capture inputs and compare their observable profile to a baseline."""
    metrics = _metrics(paths)
    findings: list[AuditFinding] = []
    if metrics.packets == 0:
        findings.append(AuditFinding("error", "empty-capture", "capture contains no packets"))
    for path in paths:
        if PcapTimestampMapper.first_timestamp_or_none(path) is None:
            findings.append(
                AuditFinding("warning", "empty-observation-point", f"{path.name} has no packets")
            )
    if clock_speed != 1.0 and retimed:
        findings.append(
            AuditFinding(
                "warning",
                "accelerated-protocol-time",
                "retimed packet timestamps cannot accelerate guest protocol timers",
            )
        )
    if metrics.virtual_ouis:
        findings.append(
            AuditFinding(
                "warning",
                "virtual-mac-oui",
                "virtual NIC OUIs observed: " + ", ".join(sorted(metrics.virtual_ouis)),
            )
        )
    if metrics.lab_markers:
        findings.append(
            AuditFinding(
                "error",
                "lab-identifier",
                "lab-specific strings observed in packet payloads: "
                + ", ".join(sorted(metrics.lab_markers)),
            )
        )
    if metrics.duplicate_dhcp_hostnames:
        findings.append(
            AuditFinding(
                "error",
                "duplicate-dhcp-hostname",
                "DHCP host names were reused by different clients: "
                + ", ".join(sorted(metrics.duplicate_dhcp_hostnames)),
            )
        )
    if metrics.browser_impersonation:
        findings.append(
            AuditFinding(
                "error",
                "browser-impersonation",
                "browser user agent was emitted by a non-browser HTTP header profile",
            )
        )
    if metrics.dns_answer_mismatches:
        findings.append(
            AuditFinding(
                "error",
                "dns-answer-type-mismatch",
                f"{metrics.dns_answer_mismatches} DNS responses returned a record type "
                "incompatible with the question",
            )
        )
    if metrics.stale_http_dates:
        findings.append(
            AuditFinding(
                "warning",
                "http-date-skew",
                f"{metrics.stale_http_dates} HTTP responses differ from capture time "
                "by over 5 minutes",
            )
        )
    if metrics.dns_responses >= 20 and metrics.dns_nxdomain_responses * 4 >= metrics.dns_responses:
        findings.append(
            AuditFinding(
                "warning",
                "dns-nxdomain-rate",
                f"{metrics.dns_nxdomain_responses} of {metrics.dns_responses} DNS responses "
                "returned NXDOMAIN",
            )
        )
    if metrics.max_tcp_syns_per_second > 100:
        findings.append(
            AuditFinding(
                "warning",
                "tcp-syn-burst",
                f"capture has {metrics.max_tcp_syns_per_second} initial TCP SYN packets "
                "in one second",
            )
        )
    if metrics.packets >= 10 and len(metrics.source_macs) < 2:
        findings.append(
            AuditFinding(
                "warning",
                "low-source-diversity",
                "capture has fewer than two observed Ethernet source addresses",
            )
        )
    if max_idle_gap_seconds is not None and metrics.max_idle_gap_seconds > max_idle_gap_seconds:
        findings.append(
            AuditFinding(
                "error",
                "idle-gap-exceeded",
                f"capture has a {metrics.max_idle_gap_seconds:.1f}s idle gap "
                f"(limit {max_idle_gap_seconds:.1f}s)",
            )
        )
    if baseline is not None:
        baseline_metrics = _metrics((baseline,))
        _compare_baseline(metrics, baseline_metrics, findings)
    findings.extend(_tcp_anomaly_findings(paths, metrics.packets))
    if keylog_files:
        findings.extend(_decrypted_findings(paths, keylog_files))
    return RealismAuditResult(
        findings=tuple(findings),
        packets=metrics.packets,
        source_macs=len(metrics.source_macs),
        protocols=tuple(sorted(metrics.protocols)),
        dns_responses=metrics.dns_responses,
        dns_nxdomain_responses=metrics.dns_nxdomain_responses,
        max_tcp_syns_per_second=metrics.max_tcp_syns_per_second,
        baseline=baseline,
    )


def _tshark_path() -> str | None:
    tshark = shutil.which("tshark")
    if tshark is None and os.name == "nt":
        candidate = (
            Path(os.environ.get("PROGRAMFILES", "C:/Program Files")) / "Wireshark/tshark.exe"
        )
        if candidate.is_file():
            tshark = str(candidate)
    return tshark


def _tcp_anomaly_findings(paths: Sequence[Path], packets: int) -> list[AuditFinding]:
    tshark = _tshark_path()
    if tshark is None:
        return []
    findings: list[AuditFinding] = []
    for path in paths:
        try:
            result = subprocess.run(
                [
                    tshark,
                    "-r",
                    str(path),
                    "-Y",
                    "tcp.analysis.out_of_order || tcp.analysis.lost_segment",
                    "-T",
                    "fields",
                    "-e",
                    "frame.number",
                ],
                check=True,
                capture_output=True,
                text=True,
                timeout=120,
            )
        except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
            continue
        count = len(result.stdout.splitlines())
        if count >= 50 and count * 100 >= packets:
            findings.append(
                AuditFinding(
                    "warning",
                    "tcp-sequence-anomalies",
                    f"{path.name} has {count} TCP out-of-order or missing-segment indications",
                )
            )
    return findings


def _decrypted_findings(paths: Sequence[Path], keylog_files: Sequence[Path]) -> list[AuditFinding]:
    findings: list[AuditFinding] = []
    usable_keylogs: list[Path] = []
    for source in keylog_files:
        if not source.is_file() or source.stat().st_size == 0:
            findings.append(
                AuditFinding("warning", "empty-tls-keylog", f"{source.name} has no TLS keys")
            )
        else:
            usable_keylogs.append(source)

    secrets: dict[str, set[str]] = defaultdict(set)
    tshark = _tshark_path()
    if tshark is None:
        if usable_keylogs:
            findings.append(
                AuditFinding(
                    "warning",
                    "decryption-audit-unavailable",
                    "TLS keys exist but tshark is unavailable",
                )
            )
        return findings

    with tempfile.TemporaryDirectory(prefix="labfoundry-audit-") as directory:
        keys = Path(directory) / "tls.keys"
        with keys.open("wb") as output:
            for source in usable_keylogs:
                content = source.read_bytes()
                output.write(content.rstrip(b"\n") + b"\n")
                for line in content.decode("ascii", errors="replace").splitlines():
                    parts = line.split()
                    if (
                        len(parts) == 3
                        and re.fullmatch(r"[0-9a-fA-F]{64}", parts[1])
                        and re.fullmatch(r"[0-9a-fA-F]+", parts[2])
                        and len(parts[2]) in ((96,) if parts[0] == "CLIENT_RANDOM" else (64, 96))
                    ):
                        secrets[parts[1].lower()].add(parts[0])
        for path in paths:
            try:
                result = subprocess.run(
                    [
                        tshark,
                        "-r",
                        str(path),
                        "-o",
                        f"tls.keylog_file:{keys}",
                        "-Y",
                        "tls.alert_message.level==2 || http.response.code>=400 "
                        "|| http2.headers.status>=400 || tls.handshake.type==1 "
                        "|| tls.app_data || tls.record.opaque_type==23 "
                        "|| http.date || http2.headers.date "
                        "|| http.last_modified || http2.headers.last_modified",
                        "-E",
                        "aggregator=|",
                        "-T",
                        "fields",
                        "-e",
                        "frame.number",
                        "-e",
                        "tls.alert_message.level",
                        "-e",
                        "http.response.code",
                        "-e",
                        "http2.headers.status",
                        "-e",
                        "ip.src",
                        "-e",
                        "ip.dst",
                        "-e",
                        "tls.alert_message.desc",
                        "-e",
                        "frame.time_epoch",
                        "-e",
                        "http.date",
                        "-e",
                        "http2.headers.date",
                        "-e",
                        "tcp.stream",
                        "-e",
                        "tls.handshake.type",
                        "-e",
                        "tls.handshake.random",
                        "-e",
                        "tls.record.content_type",
                        "-e",
                        "tls.record.opaque_type",
                        "-e",
                        "http.response.line",
                        "-e",
                        "http2.headers.age",
                        "-e",
                        "tls.app_data",
                        "-e",
                        "http.last_modified",
                        "-e",
                        "http2.headers.last_modified",
                        "-e",
                        "ipv6.src",
                        "-e",
                        "tcp.srcport",
                        "-e",
                        "tcp.seq_raw",
                        "-e",
                        "tcp.payload",
                    ],
                    check=True,
                    capture_output=True,
                    text=True,
                    timeout=120,
                )
            except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
                findings.append(
                    AuditFinding(
                        "warning", "decryption-audit-failed", f"{path.name}: {type(exc).__name__}"
                    )
                )
                continue
            hellos: dict[str, set[str]] = defaultdict(set)
            encrypted_streams: set[str] = set()
            decoded_handshakes: set[tuple[str, ...]] = set()
            opaque_packets: set[tuple[str, ...]] = set()
            for line in result.stdout.splitlines():
                fields = line.split("\t")
                frame = fields[0] if fields else "?"
                if len(fields) > 1 and "2" in fields[1].split("|"):
                    source_address = fields[4] if len(fields) > 4 else "?"
                    destination = fields[5] if len(fields) > 5 else "?"
                    description = fields[6] if len(fields) > 6 else "unknown"
                    findings.append(
                        AuditFinding(
                            "error",
                            "tls-fatal-alert",
                            f"{path.name} frame {frame} {source_address} -> {destination} "
                            f"has a fatal TLS alert ({description})",
                        )
                    )
                for value in (code for field in fields[2:4] for code in field.split("|")):
                    if value.isdecimal() and int(value) >= 400:
                        code = int(value)
                        findings.append(
                            AuditFinding(
                                "error" if code >= 500 else "warning",
                                "http-server-error" if code >= 500 else "http-client-error",
                                f"{path.name} frame {frame} returned HTTP {code}",
                            )
                        )
                if len(fields) >= 14:
                    for value in (
                        date for field in fields[18:20] for date in field.split("|") if date
                    ):
                        try:
                            modified = parsedate_to_datetime(value)
                            if modified.utcoffset() is None:
                                raise ValueError("Last-Modified has no timezone")
                            future = modified.timestamp() - float(fields[7])
                        except (ValueError, TypeError, OverflowError):
                            findings.append(
                                AuditFinding(
                                    "warning",
                                    "invalid-last-modified",
                                    f"{path.name} frame {frame} has an invalid Last-Modified date",
                                )
                            )
                            continue
                        if future > 300:
                            findings.append(
                                AuditFinding(
                                    "error",
                                    "future-http-resource",
                                    f"{path.name} frame {frame} Last-Modified is "
                                    f"{future:.1f}s after capture time",
                                )
                            )
                    stream = fields[10]
                    if "1" in fields[11].split("|"):
                        hellos[stream].update(fields[12].lower().replace(":", "").split("|"))
                    # TLS 1.3 also wraps encrypted handshakes in opaque type 23.
                    # After decryption, content_type identifies the inner record.
                    opaque = len(fields) > 14 and "23" in fields[14].split("|")
                    inner_types = fields[13].split("|")
                    packet = (
                        (stream, fields[4] or fields[20], *fields[21:24])
                        if len(fields) >= 24 and all(fields[21:24])
                        else None
                    )
                    if "23" in inner_types:
                        encrypted_streams.add(stream)
                    elif (len(fields) > 17 and fields[17]) or (opaque and not fields[13]):
                        if packet is None:
                            encrypted_streams.add(stream)
                        else:
                            opaque_packets.add(packet)
                    elif opaque and packet is not None and "22" in inner_types:
                        decoded_handshakes.add(packet)
                    dates = [date for field in fields[8:10] for date in field.split("|") if date]
                    ages = []
                    if len(fields) >= 17:
                        ages.extend(re.findall(r"(?:^|\|)Age:\s*(\d+)", fields[15], re.I))
                        ages.extend(age for age in fields[16].split("|") if age)
                    if ages and len(dates) != 1:
                        findings.append(
                            AuditFinding(
                                "warning",
                                "http-cache-date-ambiguous",
                                f"{path.name} frame {frame} combines cached response dates",
                            )
                        )
                        continue
                    for value in dates:
                        if not value:
                            continue
                        try:
                            date = parsedate_to_datetime(value)
                            if date.utcoffset() is None:
                                raise ValueError("HTTP date has no timezone")
                            age = int(ages[0]) if ages else 0
                            skew = abs(date.timestamp() + age - float(fields[7]))
                        except (ValueError, TypeError, OverflowError):
                            findings.append(
                                AuditFinding(
                                    "warning",
                                    "invalid-http-date",
                                    f"{path.name} frame {frame} has an invalid HTTP date",
                                )
                            )
                            continue
                        if skew > 300:
                            findings.append(
                                AuditFinding(
                                    "error",
                                    "decrypted-http-date-skew",
                                    f"{path.name} frame {frame} HTTP date differs by {skew:.1f}s",
                                )
                            )
            encrypted_streams.update(packet[0] for packet in opaque_packets - decoded_handshakes)
            for stream in sorted(encrypted_streams):
                randoms = hellos.get(stream)
                if not randoms:
                    findings.append(
                        AuditFinding(
                            "warning",
                            "tls-handshake-missing",
                            f"{path.name} TCP stream {stream} has TLS data without ClientHello",
                        )
                    )
                    continue
                for random in randoms:
                    labels = secrets.get(random, set())
                    if (
                        "CLIENT_RANDOM" not in labels
                        and not {
                            "CLIENT_TRAFFIC_SECRET_0",
                            "SERVER_TRAFFIC_SECRET_0",
                            "CLIENT_HANDSHAKE_TRAFFIC_SECRET",
                            "SERVER_HANDSHAKE_TRAFFIC_SECRET",
                        }
                        <= labels
                    ):
                        findings.append(
                            AuditFinding(
                                "error",
                                "tls-key-coverage-missing",
                                f"{path.name} TCP stream {stream} lacks matching TLS secrets "
                                "in both directions",
                            )
                        )
    return findings


def _metrics(paths: Sequence[Path]) -> _CaptureMetrics:
    packets = 0
    max_idle_gap_seconds = 0.0
    source_macs: set[bytes] = set()
    protocols: set[str] = set()
    virtual_ouis: set[str] = set()
    lab_markers: set[str] = set()
    dhcp_clients: dict[str, set[bytes]] = defaultdict(set)
    browser_impersonation = False
    dns_answer_mismatches = 0
    stale_http_dates = 0
    dns_responses = 0
    dns_nxdomain_responses = 0
    tcp_syns_by_second: dict[int, int] = defaultdict(int)
    for path in paths:
        previous_timestamp = None
        for record in PcapTimestampMapper.iter_records(path):
            packets += 1
            if previous_timestamp is not None:
                max_idle_gap_seconds = max(
                    max_idle_gap_seconds,
                    float(record.timestamp - previous_timestamp),
                )
            previous_timestamp = record.timestamp
            (
                packet_browser_impersonation,
                packet_date_skew,
                packet_dns_mismatch,
                packet_dns_rcode,
                packet_tcp_syn,
            ) = _record_metrics(
                record,
                source_macs=source_macs,
                protocols=protocols,
                virtual_ouis=virtual_ouis,
                lab_markers=lab_markers,
                dhcp_clients=dhcp_clients,
            )
            browser_impersonation |= packet_browser_impersonation
            dns_answer_mismatches += int(packet_dns_mismatch)
            stale_http_dates += int(packet_date_skew)
            if packet_dns_rcode is not None:
                dns_responses += 1
                dns_nxdomain_responses += int(packet_dns_rcode == 3)
            if packet_tcp_syn:
                tcp_syns_by_second[int(record.timestamp)] += 1
    return _CaptureMetrics(
        packets=packets,
        max_idle_gap_seconds=max_idle_gap_seconds,
        source_macs=frozenset(source_macs),
        protocols=frozenset(protocols),
        virtual_ouis=frozenset(virtual_ouis),
        lab_markers=frozenset(lab_markers),
        duplicate_dhcp_hostnames=frozenset(
            hostname for hostname, clients in dhcp_clients.items() if len(clients) > 1
        ),
        browser_impersonation=browser_impersonation,
        dns_answer_mismatches=dns_answer_mismatches,
        stale_http_dates=stale_http_dates,
        dns_responses=dns_responses,
        dns_nxdomain_responses=dns_nxdomain_responses,
        max_tcp_syns_per_second=max(tcp_syns_by_second.values(), default=0),
    )


def _record_metrics(
    record: PcapRecord,
    *,
    source_macs: set[bytes],
    protocols: set[str],
    virtual_ouis: set[str],
    lab_markers: set[str],
    dhcp_clients: dict[str, set[bytes]],
) -> tuple[bool, bool, bool, int | None, bool]:
    payload = record.payload
    if record.network == 1 and len(payload) >= 14:
        source = payload[6:12]
        source_macs.add(source)
        for address in (payload[:6], source):
            vendor = _VIRTUAL_OUIS.get(address[:3])
            if vendor is not None:
                virtual_ouis.add(vendor)
        ether_type = int.from_bytes(payload[12:14], "big")
        protocols.add({0x0800: "ipv4", 0x0806: "arp", 0x86DD: "ipv6"}.get(ether_type, "other"))
    for text in re.finditer(rb"[\x20-\x7e]{8,}", payload):
        for match in _LAB_MARKER_PATTERN.finditer(text.group().lower()):
            lab_markers.add(match.group().decode("ascii"))
    transport = _ipv4_transport(payload) if record.network == 1 else None
    if transport is None:
        return False, False, False, None, False
    protocol, source_port, destination_port, application = transport
    dns_rcode = _dns_response_rcode(application) if protocol == 17 and source_port == 53 else None
    dns_answer_mismatch = (
        protocol == 17 and source_port == 53 and _dns_answer_type_mismatch(application)
    )
    if protocol == 17 and {source_port, destination_port} == {67, 68}:
        parsed = _dhcp_hostname(application)
        if parsed is not None:
            hostname, client = parsed
            dhcp_clients[hostname].add(client)
    if protocol != 6:
        return False, False, dns_answer_mismatch, dns_rcode, False
    lowered = application.lower()
    browser_impersonation = b"user-agent: mozilla/" in lowered and (
        b"accept-encoding: identity" in lowered or b"python-urllib" in lowered
    )
    stale_date = _http_date_is_stale(application, float(record.timestamp))
    return (
        browser_impersonation,
        stale_date,
        dns_answer_mismatch,
        dns_rcode,
        _is_initial_tcp_syn(payload),
    )


def _dns_response_rcode(payload: bytes) -> int | None:
    if len(payload) < 12:
        return None
    flags = int.from_bytes(payload[2:4], "big")
    return flags & 0xF if flags & 0x8000 else None


def _is_initial_tcp_syn(frame: bytes) -> bool:
    if len(frame) < 54 or frame[12:14] != b"\x08\x00":
        return False
    ip_offset = 14
    ip_header_length = (frame[ip_offset] & 0x0F) * 4
    tcp_offset = ip_offset + ip_header_length
    if ip_header_length < 20 or len(frame) < tcp_offset + 14 or frame[ip_offset + 9] != 6:
        return False
    flags = frame[tcp_offset + 13]
    return bool(flags & 0x02) and not bool(flags & 0x15)


def _dns_answer_type_mismatch(payload: bytes) -> bool:
    if len(payload) < 12:
        return False
    flags = int.from_bytes(payload[2:4], "big")
    questions = int.from_bytes(payload[4:6], "big")
    answers = int.from_bytes(payload[6:8], "big")
    if not flags & 0x8000 or questions != 1 or answers == 0:
        return False
    offset = _dns_skip_name(payload, 12)
    if offset is None or offset + 4 > len(payload):
        return False
    question_type = int.from_bytes(payload[offset : offset + 2], "big")
    offset += 4
    for _ in range(answers):
        offset = _dns_skip_name(payload, offset)
        if offset is None or offset + 10 > len(payload):
            return False
        answer_type = int.from_bytes(payload[offset : offset + 2], "big")
        length = int.from_bytes(payload[offset + 8 : offset + 10], "big")
        offset += 10
        if offset + length > len(payload):
            return False
        if question_type != 255 and answer_type not in {question_type, 5}:
            return True
        offset += length
    return False


def _dns_skip_name(payload: bytes, offset: int) -> int | None:
    while offset < len(payload):
        length = payload[offset]
        if length & 0xC0 == 0xC0:
            return offset + 2 if offset + 2 <= len(payload) else None
        offset += 1
        if length == 0:
            return offset
        if length & 0xC0 or offset + length > len(payload):
            return None
        offset += length
    return None


def _ipv4_transport(frame: bytes) -> tuple[int, int, int, bytes] | None:
    if len(frame) < 34 or frame[12:14] != b"\x08\x00":
        return None
    ip_offset = 14
    header_length = (frame[ip_offset] & 0x0F) * 4
    if header_length < 20 or len(frame) < ip_offset + header_length + 4:
        return None
    protocol = frame[ip_offset + 9]
    transport_offset = ip_offset + header_length
    source_port = int.from_bytes(frame[transport_offset : transport_offset + 2], "big")
    destination_port = int.from_bytes(frame[transport_offset + 2 : transport_offset + 4], "big")
    if protocol == 17:
        application_offset = transport_offset + 8
    elif protocol == 6:
        if len(frame) < transport_offset + 13:
            return None
        application_offset = transport_offset + ((frame[transport_offset + 12] >> 4) * 4)
    else:
        return protocol, source_port, destination_port, b""
    if application_offset > len(frame):
        return None
    return protocol, source_port, destination_port, frame[application_offset:]


def _dhcp_hostname(payload: bytes) -> tuple[str, bytes] | None:
    if len(payload) < 240 or payload[236:240] != b"\x63\x82\x53\x63":
        return None
    hardware_length = min(payload[2], 16)
    client = payload[28 : 28 + hardware_length]
    offset = 240
    while offset < len(payload):
        option = payload[offset]
        offset += 1
        if option == 255:
            break
        if option == 0:
            continue
        if offset >= len(payload):
            break
        length = payload[offset]
        offset += 1
        value = payload[offset : offset + length]
        offset += length
        if option == 12 and value:
            return value.decode("ascii", "replace").lower(), client
    return None


def _http_date_is_stale(payload: bytes, capture_timestamp: float) -> bool:
    if not payload.startswith(b"HTTP/1."):
        return False
    for line in payload.split(b"\r\n"):
        if not line.lower().startswith(b"date:"):
            continue
        try:
            value = parsedate_to_datetime(line.split(b":", 1)[1].strip().decode("ascii"))
        except (TypeError, ValueError, UnicodeDecodeError):
            return False
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return abs(value.timestamp() - capture_timestamp) > 300
    return False


def _compare_baseline(
    observed: _CaptureMetrics,
    baseline: _CaptureMetrics,
    findings: list[AuditFinding],
) -> None:
    if baseline.packets == 0:
        findings.append(
            AuditFinding("warning", "empty-baseline", "configured baseline contains no packets")
        )
        return
    if len(observed.source_macs) < len(baseline.source_macs):
        findings.append(
            AuditFinding(
                "warning",
                "baseline-source-diversity",
                "capture has fewer Ethernet sources than the configured baseline",
            )
        )
    if observed.packets < baseline.packets / 4:
        findings.append(
            AuditFinding(
                "warning",
                "baseline-volume-low",
                f"capture has {observed.packets} packets versus {baseline.packets} in baseline",
            )
        )
    elif observed.packets > baseline.packets * 4:
        findings.append(
            AuditFinding(
                "warning",
                "baseline-volume-high",
                f"capture has {observed.packets} packets versus {baseline.packets} in baseline",
            )
        )
    missing_protocols = baseline.protocols - observed.protocols
    if missing_protocols:
        findings.append(
            AuditFinding(
                "warning",
                "baseline-protocol-gap",
                "capture lacks baseline protocols: " + ", ".join(sorted(missing_protocols)),
            )
        )
