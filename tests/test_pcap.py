import struct
import subprocess
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from labfoundry.evidence.audit import audit_captures
from labfoundry.evidence.pcap import PcapNgSource, PcapNgWriter, PcapTimestampMapper


def _write_pcap(path: Path) -> None:
    # Little-endian, microsecond classic PCAP, Ethernet link type.
    global_header = b"\xd4\xc3\xb2\xa1" + struct.pack("<HHIIII", 2, 4, 0, 0, 65535, 1)
    payload1 = b"abc"
    payload2 = b"defgh"
    with path.open("wb") as handle:
        handle.write(global_header)
        handle.write(struct.pack("<IIII", 1000, 500_000, len(payload1), len(payload1)))
        handle.write(payload1)
        handle.write(struct.pack("<IIII", 1002, 0, len(payload2), len(payload2)))
        handle.write(payload2)


def test_retime_preserves_payload_and_scales_time(tmp_path: Path) -> None:
    source = tmp_path / "raw.pcap"
    output = tmp_path / "final.pcap"
    _write_pcap(source)
    scenario_start = datetime(2025, 1, 1, tzinfo=UTC)

    count = PcapTimestampMapper.retime(
        source,
        output,
        raw_anchor=Decimal("1000"),
        scenario_anchor=scenario_start,
        speed=10.0,
    )

    assert count == 2
    assert PcapTimestampMapper.first_timestamp(output) == (
        Decimal(str(scenario_start.timestamp())) + 5
    )
    data = output.read_bytes()
    assert b"abc" in data
    assert b"defgh" in data


def test_empty_pcap_can_be_retimed(tmp_path: Path) -> None:
    source = tmp_path / "empty.pcap"
    output = tmp_path / "empty-final.pcap"
    global_header = b"\xd4\xc3\xb2\xa1" + struct.pack("<HHIIII", 2, 4, 0, 0, 65535, 1)
    source.write_bytes(global_header)

    assert PcapTimestampMapper.first_timestamp_or_none(source) is None
    count = PcapTimestampMapper.retime(
        source,
        output,
        raw_anchor=Decimal(0),
        scenario_anchor=datetime(2025, 1, 1, tzinfo=UTC),
        speed=1.0,
    )

    assert count == 0
    assert output.read_bytes() == global_header


def test_copy_without_retime_preserves_capture_bytes(tmp_path: Path) -> None:
    source = tmp_path / "raw.pcap"
    output = tmp_path / "final.pcap"
    _write_pcap(source)
    output.write_bytes(b"previous-output")

    PcapTimestampMapper.copy_without_retime(source, output)

    assert output.read_bytes() == source.read_bytes()


def test_retime_is_atomic_on_malformed_packet(tmp_path: Path) -> None:
    from labfoundry.errors import PcapError

    source = tmp_path / "broken.pcap"
    output = tmp_path / "existing.pcap"
    global_header = b"\xd4\xc3\xb2\xa1" + struct.pack("<HHIIII", 2, 4, 0, 0, 65535, 1)
    source.write_bytes(global_header + struct.pack("<IIII", 1, 0, 10, 10) + b"short")
    output.write_bytes(b"keep-me")

    with pytest.raises(PcapError, match="truncated packet payload"):
        PcapTimestampMapper.retime(
            source,
            output,
            raw_anchor=Decimal(0),
            scenario_anchor=datetime(2025, 1, 1, tzinfo=UTC),
            speed=1.0,
        )

    assert output.read_bytes() == b"keep-me"


def test_invalid_fractional_timestamp_is_rejected(tmp_path: Path) -> None:
    from labfoundry.errors import PcapError

    source = tmp_path / "bad-fraction.pcap"
    global_header = b"\xd4\xc3\xb2\xa1" + struct.pack("<HHIIII", 2, 4, 0, 0, 65535, 1)
    source.write_bytes(global_header + struct.pack("<IIII", 1, 1_000_000, 0, 0))

    with pytest.raises(PcapError, match="fractional timestamp"):
        PcapTimestampMapper.first_timestamp(source)


def test_incomplete_final_record_is_discarded(tmp_path: Path) -> None:
    source = tmp_path / "capture.pcap"
    _write_pcap(source)
    complete = source.read_bytes()
    with source.open("ab") as handle:
        handle.write(struct.pack("<IIII", 1003, 0, 20, 20))
        handle.write(b"partial")

    assert PcapTimestampMapper.discard_incomplete_tail(source) is True
    assert source.read_bytes() == complete
    assert PcapTimestampMapper.discard_incomplete_tail(source) is False


def test_pcapng_export_merges_interfaces_and_preserves_timestamps(tmp_path: Path) -> None:
    left = tmp_path / "left.pcap"
    right = tmp_path / "right.pcap"
    output = tmp_path / "combined.pcapng"
    _write_pcap(left)
    _write_pcap(right)

    count = PcapNgWriter.write(
        output,
        sources=(
            PcapNgSource(left, "users", "user network"),
            PcapNgSource(right, "wan", "internet uplink"),
        ),
        metadata={"comment": "exercise capture", "application": "LabFoundry"},
        timestamp_policy="preserve",
    )

    records = list(PcapTimestampMapper.iter_records(output))
    assert count == 4
    assert [record.timestamp for record in records] == sorted(
        record.timestamp for record in records
    )
    assert b"users" in output.read_bytes()
    assert b"wan" in output.read_bytes()


def test_pcapng_export_deduplicates_packets_seen_at_two_observation_points(
    tmp_path: Path,
) -> None:
    left = tmp_path / "left.pcap"
    right = tmp_path / "right.pcap"
    output = tmp_path / "combined.pcapng"
    _write_pcap(left)
    _write_pcap(right)

    count = PcapNgWriter.write(
        output,
        sources=(PcapNgSource(left, "users"), PcapNgSource(right, "router")),
        metadata={},
        timestamp_policy="preserve",
        deduplicate_packets=True,
    )

    assert count == 2
    assert [record.payload for record in PcapTimestampMapper.iter_records(output)] == [
        b"abc",
        b"defgh",
    ]


def test_pcapng_export_preserves_repeated_source_records(tmp_path: Path) -> None:
    source = tmp_path / "source.pcap"
    output = tmp_path / "combined.pcapng"
    _write_pcap(source)
    with source.open("ab") as handle:
        handle.write(struct.pack("<IIII", 1002, 0, 5, 5))
        handle.write(b"defgh")
        handle.write(struct.pack("<IIII", 1002, 1000, 5, 5))
        handle.write(b"defgh")

    count = PcapNgWriter.write(
        output,
        sources=(PcapNgSource(source, "uplink"),),
        metadata={},
        timestamp_policy="preserve",
        deduplicate_packets=True,
    )

    assert count == 4

    mirrored = tmp_path / "mirror.pcap"
    mirrored.write_bytes(source.read_bytes())
    assert (
        PcapNgWriter.write(
            output,
            sources=(PcapNgSource(source, "uplink"), PcapNgSource(mirrored, "mirror")),
            metadata={},
            timestamp_policy="preserve",
            deduplicate_packets=True,
        )
        == 4
    )


def test_tcp_retiming_rejects_unknown_clock_rate() -> None:
    from labfoundry.errors import PcapError
    from labfoundry.evidence.pcap import _TcpTimestampRetimer

    retimer = _TcpTimestampRetimer(lambda stamp: stamp if stamp < 103 else stamp + 3600)
    assert retimer._value(1001, (Decimal(100), 1000), None, Decimal(101)) == 1001
    with pytest.raises(PcapError, match="measured clock rate"):
        retimer._value(1002, (Decimal(100), 1000), None, Decimal(104))


def test_tcp_echo_before_capture_does_not_look_like_a_future_wrap() -> None:
    from labfoundry.evidence.pcap import _TcpTimestampRetimer

    retimer = _TcpTimestampRetimer(lambda stamp: stamp if stamp < 103 else stamp + 3600)
    assert retimer._value(900, (Decimal(100), 1000), Decimal(1000), Decimal(104)) == 900
    assert retimer._value(50, (Decimal(100), 0xFFFFFFCE), Decimal(1000), Decimal(101)) == 50


def test_realism_audit_reports_virtual_identifiers(tmp_path: Path) -> None:
    source = tmp_path / "audit.pcap"
    payload = b"\xaa\xbb\xcc\xdd\xee\xff\x08\x00\x27\x11\x22\x33\x08\x00LabFoundry"
    source.write_bytes(
        b"\xd4\xc3\xb2\xa1"
        + struct.pack("<HHIIII", 2, 4, 0, 0, 65535, 1)
        + struct.pack("<IIII", 1, 0, len(payload), len(payload))
        + payload
    )

    result = audit_captures([source], clock_speed=10.0, retimed=True)

    assert {item.code for item in result.findings} >= {
        "virtual-mac-oui",
        "lab-identifier",
    }


@pytest.mark.parametrize("content", [b"AZbgNs3LTtqjvbOXpIL", b"\xe9VBOX\xd7\x00"])
def test_realism_audit_ignores_identifiers_inside_encoded_data(tmp_path: Path, content) -> None:
    source = tmp_path / "encoded.pcap"
    payload = b"\xaa\xbb\xcc\xdd\xee\xff\x00\x11\x22\x33\x44\x55\x08\x00" + content
    source.write_bytes(
        b"\xd4\xc3\xb2\xa1"
        + struct.pack("<HHIIII", 2, 4, 0, 0, 65535, 1)
        + struct.pack("<IIII", 1, 0, len(payload), len(payload))
        + payload
    )

    result = audit_captures([source], clock_speed=1.0, retimed=False)

    assert "lab-identifier" not in {item.code for item in result.findings}


def test_realism_audit_accepts_warped_time_after_packet_retiming(tmp_path: Path) -> None:
    source = tmp_path / "warped.pcap"
    payload = b"\xaa\xbb\xcc\xdd\xee\xff\x00\x11\x22\x33\x44\x55\x08\x00"
    source.write_bytes(
        b"\xd4\xc3\xb2\xa1"
        + struct.pack("<HHIIII", 2, 4, 0, 0, 65535, 1)
        + struct.pack("<IIII", 1, 0, len(payload), len(payload))
        + payload
    )

    result = audit_captures([source], clock_speed=1.0, retimed=True, warped=True)

    assert "scaled-timestamps" not in {item.code for item in result.findings}


def test_realism_audit_checks_observed_idle_gap(tmp_path: Path) -> None:
    source = tmp_path / "idle.pcap"
    _write_pcap(source)

    result = audit_captures([source], clock_speed=1.0, retimed=False, max_idle_gap_seconds=1.4)

    assert "idle-gap-exceeded" in {item.code for item in result.findings}
    assert result.should_fail("error")


def test_realism_audit_rejects_dns_answer_type_mismatch(tmp_path: Path) -> None:
    source = tmp_path / "dns-mismatch.pcap"
    question = b"\x07example\x03com\x00" + struct.pack("!HH", 28, 1)
    dns = (
        b"\x12\x34\x81\x80\x00\x01\x00\x01\x00\x00\x00\x00"
        + question
        + b"\xc0\x0c"
        + struct.pack("!HHIH", 1, 1, 60, 4)
        + b"\xc0\x00\x02\x01"
    )
    udp = struct.pack("!HHHH", 53, 53000, 8 + len(dns), 0) + dns
    frame = (
        b"\xaa\xbb\xcc\xdd\xee\xff\x00\x11\x22\x33\x44\x55\x08\x00"
        + b"\x45\x00"
        + struct.pack("!H", 20 + len(udp))
        + b"\x00\x00\x40\x00\x40\x11\x00\x00"
        + b"\xc0\x00\x02\x35\xc0\x00\x02\x64"
        + udp
    )
    source.write_bytes(
        b"\xd4\xc3\xb2\xa1"
        + struct.pack("<HHIIII", 2, 4, 0, 0, 65535, 1)
        + struct.pack("<IIII", 1, 0, len(frame), len(frame))
        + frame
    )

    result = audit_captures([source], clock_speed=1.0, retimed=False)

    assert "dns-answer-type-mismatch" in {item.code for item in result.findings}


def test_realism_audit_flags_dns_failures_and_syn_bursts(tmp_path: Path) -> None:
    source = tmp_path / "noisy.pcap"
    dns = b"\x12\x34\x81\x83\x00\x01\x00\x00\x00\x00\x00\x00"
    udp = struct.pack("!HHHH", 53, 53000, 8 + len(dns), 0) + dns
    udp_frame = (
        b"\xaa\xbb\xcc\xdd\xee\xff\x00\x11\x22\x33\x44\x55\x08\x00"
        + b"\x45\x00"
        + struct.pack("!H", 20 + len(udp))
        + b"\x00\x00\x40\x00\x40\x11\x00\x00"
        + b"\xc0\x00\x02\x35\xc0\x00\x02\x64"
        + udp
    )
    tcp_frame = (
        b"\xaa\xbb\xcc\xdd\xee\xff\x00\x11\x22\x33\x44\x55\x08\x00"
        + b"\x45\x00\x00\x28\x00\x00\x40\x00\x40\x06\x00\x00"
        + b"\xc0\x00\x02\x35\xc0\x00\x02\x64"
        + b"\x9c\x40\x00\x50"
        + b"\x00" * 8
        + b"\x50\x02\x00\x00\x00\x00\x00\x00"
    )
    header = b"\xd4\xc3\xb2\xa1" + struct.pack("<HHIIII", 2, 4, 0, 0, 65535, 1)
    with source.open("wb") as handle:
        handle.write(header)
        for payload in [udp_frame] * 20 + [tcp_frame] * 101:
            handle.write(struct.pack("<IIII", 1, 0, len(payload), len(payload)))
            handle.write(payload)

    result = audit_captures([source], clock_speed=1.0, retimed=False)

    assert result.dns_responses == 20
    assert result.dns_nxdomain_responses == 20
    assert result.max_tcp_syns_per_second == 101
    assert {item.code for item in result.findings} >= {
        "dns-nxdomain-rate",
        "tcp-syn-burst",
    }


def test_realism_audit_checks_destination_mac_oui(tmp_path: Path) -> None:
    source = tmp_path / "destination-oui.pcap"
    payload = b"\x08\x00\x27\x11\x22\x33\xaa\xbb\xcc\xdd\xee\xff\x08\x00"
    source.write_bytes(
        b"\xd4\xc3\xb2\xa1"
        + struct.pack("<HHIIII", 2, 4, 0, 0, 65535, 1)
        + struct.pack("<IIII", 1, 0, len(payload), len(payload))
        + payload
    )

    result = audit_captures([source], clock_speed=1.0, retimed=False)

    assert "virtual-mac-oui" in {item.code for item in result.findings}


def test_realism_audit_compares_protocols_with_baseline(tmp_path: Path) -> None:
    observed = tmp_path / "observed.pcap"
    baseline = tmp_path / "baseline.pcap"
    arp = b"\xaa\xbb\xcc\xdd\xee\xff\x00\x11\x22\x33\x44\x55\x08\x06"
    ipv4 = b"\xaa\xbb\xcc\xdd\xee\xff\x00\x11\x22\x33\x44\x55\x08\x00"
    header = b"\xd4\xc3\xb2\xa1" + struct.pack("<HHIIII", 2, 4, 0, 0, 65535, 1)
    observed.write_bytes(header + struct.pack("<IIII", 1, 0, len(ipv4), len(ipv4)) + ipv4)
    baseline.write_bytes(header + struct.pack("<IIII", 1, 0, len(arp), len(arp)) + arp)

    result = audit_captures([observed], baseline=baseline, clock_speed=1.0, retimed=False)

    assert "baseline-protocol-gap" in {item.code for item in result.findings}


def test_realism_audit_reports_empty_observation_point(tmp_path: Path) -> None:
    observed = tmp_path / "observed.pcap"
    empty = tmp_path / "empty.pcap"
    _write_pcap(observed)
    empty.write_bytes(b"\xd4\xc3\xb2\xa1" + struct.pack("<HHIIII", 2, 4, 0, 0, 65535, 1))

    result = audit_captures([observed, empty], clock_speed=1.0, retimed=False)

    assert "empty-observation-point" in {item.code for item in result.findings}


def test_realism_audit_warns_about_accelerated_protocol_timers(tmp_path: Path) -> None:
    source = tmp_path / "source.pcap"
    _write_pcap(source)

    result = audit_captures([source], clock_speed=10.0, retimed=True)

    assert "accelerated-protocol-time" in {item.code for item in result.findings}


def test_realism_audit_checks_baseline_packet_volume(tmp_path: Path) -> None:
    observed = tmp_path / "observed.pcap"
    baseline = tmp_path / "baseline.pcap"
    _write_pcap(observed)
    _write_pcap(baseline)
    packet = baseline.read_bytes()[24:]
    with baseline.open("ab") as handle:
        handle.write(packet * 8)

    result = audit_captures([observed], baseline=baseline, clock_speed=1.0, retimed=False)

    assert "baseline-volume-low" in {item.code for item in result.findings}


def test_realism_audit_inspects_decrypted_http_and_tls(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "source.pcap"
    keys = tmp_path / "source.keys"
    _write_pcap(source)
    keys.write_text("CLIENT_RANDOM abc def\n", encoding="ascii")
    monkeypatch.setattr("labfoundry.evidence.audit.shutil.which", lambda _: "tshark")
    monkeypatch.setattr(
        "labfoundry.evidence.audit.subprocess.run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0], 0, "1\t1|2\t\t\n2\t\t503\t\n3\t\t\t200|404|502\n", ""
        ),
    )

    result = audit_captures([source], clock_speed=1.0, retimed=False, keylog_files=[keys])

    assert {item.code for item in result.findings} >= {
        "tls-fatal-alert",
        "http-server-error",
        "http-client-error",
    }
    assert result.should_fail("error")
    http_findings = [item.message for item in result.findings if item.code.startswith("http-")]
    assert len(http_findings) == 3
    assert not any("HTTP 200" in message for message in http_findings)


def test_realism_audit_reports_empty_keylog_without_tshark(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "source.pcap"
    keys = tmp_path / "source.keys"
    _write_pcap(source)
    keys.write_text("", encoding="ascii")
    monkeypatch.setattr("labfoundry.evidence.audit._tshark_path", lambda: None)

    result = audit_captures([source], clock_speed=1.0, retimed=False, keylog_files=[keys])

    assert "empty-tls-keylog" in {item.code for item in result.findings}


def test_realism_audit_flags_excessive_tcp_sequence_anomalies(tmp_path: Path, monkeypatch) -> None:
    from labfoundry.evidence.audit import _tcp_anomaly_findings

    capture = tmp_path / "trace.pcapng"
    monkeypatch.setattr("labfoundry.evidence.audit.shutil.which", lambda _: "tshark")
    monkeypatch.setattr(
        "labfoundry.evidence.audit.subprocess.run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0, "1\n" * 60, ""),
    )

    findings = _tcp_anomaly_findings([capture], 5000)

    assert [item.code for item in findings] == ["tcp-sequence-anomalies"]
    assert not _tcp_anomaly_findings([capture], 7000)


@pytest.mark.parametrize(
    "labels,matching,covered",
    [
        (["CLIENT_RANDOM"], True, True),
        (["CLIENT_RANDOM"], False, False),
        (["CLIENT_TRAFFIC_SECRET_0"], True, False),
        (
            [
                "CLIENT_TRAFFIC_SECRET_0",
                "SERVER_TRAFFIC_SECRET_0",
                "CLIENT_HANDSHAKE_TRAFFIC_SECRET",
                "SERVER_HANDSHAKE_TRAFFIC_SECRET",
            ],
            True,
            True,
        ),
        ([], True, False),
    ],
)
def test_tls_key_coverage_and_decrypted_dates(tmp_path, monkeypatch, labels, matching, covered):
    from labfoundry.evidence.audit import _decrypted_findings

    random = "ab" * 32
    keys = tmp_path / "capture.keys"
    keys.write_text(
        "".join(f"{label} {random if matching else 'cd' * 32} {'ef' * 48}\n" for label in labels)
    )
    fields = [
        "1",
        "",
        "",
        "",
        "10.0.0.1",
        "10.0.0.2",
        "",
        str(datetime(2025, 9, 5, tzinfo=UTC).timestamp()),
        "",
        "Fri, 05 Sep 2025 00:00:00 GMT|Fri, 05 Sep 2025 01:00:00 GMT",
        "5",
        "1",
        random,
        "22|23",
        "23",
    ]
    missing_hello = fields.copy()
    missing_hello[8:13] = ["", "", "6", "", ""]
    monkeypatch.setattr("labfoundry.evidence.audit.shutil.which", lambda _: "tshark")
    monkeypatch.setattr(
        "labfoundry.evidence.audit.subprocess.run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0], 0, "\t".join(fields) + "\n" + "\t".join(missing_hello), ""
        ),
    )

    findings = _decrypted_findings([tmp_path / "capture.pcapng"], [keys])
    codes = [item.code for item in findings]
    assert ("tls-key-coverage-missing" not in codes) == covered
    assert codes.count("decrypted-http-date-skew") == 1
    assert "tls-handshake-missing" in codes


@pytest.mark.parametrize("undecoded", [False, True])
@pytest.mark.parametrize("duplicate_payload", [None, "abcd", "dcba"])
def test_tls_encrypted_handshake_is_not_application_data(
    tmp_path, monkeypatch, undecoded, duplicate_payload
):
    from labfoundry.evidence.audit import _decrypted_findings

    random = "ab" * 32
    keys = tmp_path / "capture.keys"
    keys.write_text(
        "".join(
            f"{label} {random} {'ef' * 48}\n"
            for label in ("CLIENT_HANDSHAKE_TRAFFIC_SECRET", "SERVER_HANDSHAKE_TRAFFIC_SECRET")
        )
    )
    fields = [
        "1",
        "",
        "",
        "",
        "10.0.0.1",
        "10.0.0.2",
        "",
        "1",
        "",
        "",
        "5",
        "1|2|8",
        random,
        "22|20|22",
        "23",
        "",
        "",
        "ab" if undecoded else "",
    ]
    fields.extend(["", "", "", "443", "1234", "abcd"])
    lines = ["\t".join(fields)]
    if duplicate_payload is not None:
        duplicate = fields.copy()
        duplicate[11:14] = ["", "", ""]
        duplicate[17] = "ab"
        duplicate[23] = duplicate_payload
        lines.append("\t".join(duplicate))
    monkeypatch.setattr("labfoundry.evidence.audit.shutil.which", lambda _: "tshark")
    monkeypatch.setattr(
        "labfoundry.evidence.audit.subprocess.run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0, "\n".join(lines), ""),
    )
    codes = {item.code for item in _decrypted_findings([tmp_path / "capture.pcapng"], [keys])}
    assert ("tls-key-coverage-missing" in codes) == (undecoded or duplicate_payload == "dcba")


@pytest.mark.parametrize("http2", [False, True])
def test_decrypted_date_audit_accounts_for_cache_age(tmp_path, monkeypatch, http2):
    from labfoundry.evidence.audit import _decrypted_findings

    keys = tmp_path / "capture.keys"
    keys.write_text("# key log\n")
    fields = [""] * 17
    fields[0] = "1"
    fields[7] = str(datetime(2025, 9, 5, 1, tzinfo=UTC).timestamp())
    fields[9 if http2 else 8] = "Fri, 05 Sep 2025 00:00:00 GMT"
    fields[16 if http2 else 15] = "3600" if http2 else "Age: 3600\\r\\n"
    monkeypatch.setattr("labfoundry.evidence.audit.shutil.which", lambda _: "tshark")
    monkeypatch.setattr(
        "labfoundry.evidence.audit.subprocess.run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0, "\t".join(fields), ""),
    )

    assert not _decrypted_findings([tmp_path / "capture.pcapng"], [keys])


@pytest.mark.parametrize("http2", [False, True])
@pytest.mark.parametrize("year,expected", [(2024, False), (2025, False), (2026, True)])
def test_decrypted_audit_rejects_future_resources(tmp_path, monkeypatch, http2, year, expected):
    from labfoundry.evidence.audit import _decrypted_findings

    keys = tmp_path / "capture.keys"
    keys.write_text("# key log\n")
    fields = [""] * 20
    fields[0] = "1"
    fields[7] = str(datetime(2025, 9, 5, tzinfo=UTC).timestamp())
    fields[19 if http2 else 18] = f"01 Jan {year} 00:00:00 GMT"
    monkeypatch.setattr("labfoundry.evidence.audit.shutil.which", lambda _: "tshark")
    monkeypatch.setattr(
        "labfoundry.evidence.audit.subprocess.run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0, "\t".join(fields), ""),
    )

    findings = _decrypted_findings([tmp_path / "capture.pcapng"], [keys])
    assert bool(findings) == expected
    if expected:
        assert findings[0].code == "future-http-resource"
        assert findings[0].severity == "error"


def test_pcapng_export_retimed_piecewise_segments(tmp_path: Path) -> None:
    source = tmp_path / "raw.pcap"
    output = tmp_path / "retimed_segments.pcapng"

    # Write 3 packets: two at t=1000.0, 1000.1, one after a gap at t=1003.0
    global_header = b"\xd4\xc3\xb2\xa1" + struct.pack("<HHIIII", 2, 4, 0, 0, 65535, 1)
    p1 = b"pkt1"
    p2 = b"pkt2"
    p3 = b"pkt3"
    with source.open("wb") as handle:
        handle.write(global_header)
        handle.write(struct.pack("<IIII", 1000, 0, len(p1), len(p1)))
        handle.write(p1)
        handle.write(struct.pack("<IIII", 1000, 100_000, len(p2), len(p2)))
        handle.write(p2)
        handle.write(struct.pack("<IIII", 1003, 0, len(p3), len(p3)))
        handle.write(p3)

    t0_scenario = datetime(2025, 9, 5, 8, 55, 0, tzinfo=UTC)
    t1_scenario = datetime(2025, 9, 5, 14, 55, 0, tzinfo=UTC)

    segments = (
        (Decimal("1000"), t0_scenario, 1.0),
        (Decimal("1003"), t1_scenario, 1.0),
    )

    count = PcapNgWriter.write(
        output,
        sources=(PcapNgSource(source, "eth0", "capture interface"),),
        metadata={"comment": "piecewise time warp test"},
        timestamp_policy="retime",
        segments=segments,
    )

    assert count == 3
    records = list(PcapTimestampMapper.iter_records(output))
    assert len(records) == 3

    # First packet: mapped to t0_scenario
    assert records[0].timestamp == Decimal(str(t0_scenario.timestamp()))
    # Second packet: 100ms delta preserved exactly at 1.0x physical rate
    assert records[1].timestamp == Decimal(str(t0_scenario.timestamp())) + Decimal("0.1")
    # Third packet: mapped to t1_scenario (6 hours later)
    assert records[2].timestamp == Decimal(str(t1_scenario.timestamp()))

    classic = tmp_path / "retimed.pcap"
    PcapTimestampMapper.retime(
        source,
        classic,
        raw_anchor=Decimal("1000"),
        scenario_anchor=t0_scenario,
        speed=1.0,
        segments=segments,
    )
    assert list(PcapTimestampMapper.iter_records(classic)) == records


@pytest.mark.parametrize("rate", [100, 250, 1000])
@pytest.mark.parametrize("syn_retries", [False, True])
def test_warped_pcapng_retimes_tcp_timestamps_and_checksum(
    tmp_path: Path, rate: int, syn_retries: bool
) -> None:
    source = tmp_path / "tcp.pcap"
    output = tmp_path / "tcp.pcapng"
    client = bytes((192, 0, 2, 10))
    server = bytes((192, 0, 2, 20))

    def checksum(data: bytes) -> int:
        if len(data) % 2:
            data += b"\0"
        total = sum(struct.unpack(f"!{len(data) // 2}H", data))
        while total >> 16:
            total = (total & 0xFFFF) + (total >> 16)
        return (~total) & 0xFFFF

    def packet(
        src: bytes, dst: bytes, sport: int, dport: int, flags: int, tsval: int, tsecr: int
    ) -> bytes:
        options = b"\x01\x01\x08\x0a" + struct.pack("!II", tsval, tsecr)
        tcp = bytearray(
            struct.pack("!HHIIBBHHH", sport, dport, 1, 1, 8 << 4, flags, 64240, 0, 0) + options
        )
        pseudo = src + dst + b"\0\x06" + struct.pack("!H", len(tcp))
        tcp[16:18] = struct.pack("!H", checksum(pseudo + tcp))
        ip = b"\x45\0" + struct.pack("!H", 20 + len(tcp)) + b"\0\0\x40\0\x40\x06\0\0" + src + dst
        return b"\xaa" * 6 + b"\xbb" * 6 + b"\x08\0" + ip + tcp

    frames = (
        (1000, 0, packet(client, server, 40000, 443, 2, 1000, 0)),
        (1000, 10_000, packet(server, client, 443, 40000, 18, 2000, 1000)),
        # The echoed value predates the gap, but its server packet was not captured.
        (1003, 0, packet(client, server, 40000, 443, 16, 1000 + rate * 3, 2000 + rate)),
        (1003, 10_000, packet(server, client, 443, 40000, 16, 2000 + rate * 3, 1000 + rate * 3)),
    )
    if syn_retries:
        frames = tuple(
            (second, 0, packet(client, server, 40000, 443, 2, 1000 + rate * (second - 1000), 0))
            for second in (1000, 1001, 1003, 1004)
        )
    with source.open("wb") as handle:
        handle.write(b"\xd4\xc3\xb2\xa1" + struct.pack("<HHIIII", 2, 4, 0, 0, 65535, 1))
        for second, fraction, frame in frames:
            handle.write(struct.pack("<IIII", second, fraction, len(frame), len(frame)))
            handle.write(frame)

    start = datetime(2025, 9, 5, tzinfo=UTC)
    segments = (
        (Decimal("1000"), start, 1.0),
        (Decimal("1003"), datetime(2025, 9, 5, 1, tzinfo=UTC), 1.0),
    )
    PcapNgWriter.write(
        output,
        sources=(PcapNgSource(source, "office"),),
        metadata={},
        timestamp_policy="retime",
        segments=segments,
        rewrite_tcp_timestamps=True,
    )

    records = list(PcapTimestampMapper.iter_records(output))
    values = [struct.unpack("!II", record.payload[58:66]) for record in records]
    if syn_retries:
        assert values == [(1000 + rate * second, 0) for second in (0, 1, 3600, 3601)]
    else:
        assert values[0] == (1000, 0)
        assert values[1] == (2000, 1000)
        assert values[2] == (1000 + 3600 * rate, 2000 + rate)
        assert values[3] == (2000 + 3600 * rate, 1000 + 3600 * rate)
    for record in records:
        frame = record.payload
        src, dst = frame[26:30], frame[30:34]
        tcp = frame[34:66]
        pseudo = src + dst + b"\0\x06" + struct.pack("!H", len(tcp))
        assert checksum(pseudo + tcp) == 0
