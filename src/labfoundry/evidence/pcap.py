"""Packet-capture inspection, timestamp mapping, and PCAPNG export."""

from __future__ import annotations

import hashlib
import heapq
import os
import shutil
import struct
import tempfile
from collections import deque
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from decimal import ROUND_HALF_EVEN, Decimal
from pathlib import Path
from typing import BinaryIO, Protocol

from labfoundry.errors import PcapError

_PCAP_MAGIC = {
    b"\xd4\xc3\xb2\xa1": ("<", 1_000_000),
    b"\xa1\xb2\xc3\xd4": (">", 1_000_000),
    b"\x4d\x3c\xb2\xa1": ("<", 1_000_000_000),
    b"\xa1\xb2\x3c\x4d": (">", 1_000_000_000),
}
_PCAPNG_SECTION_HEADER = 0x0A0D0D0A
_PCAPNG_BYTE_ORDER_MAGIC = 0x1A2B3C4D
_PCAPNG_INTERFACE_DESCRIPTION = 0x00000001
_PCAPNG_ENHANCED_PACKET = 0x00000006
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


class _TcpTimestampRetimer:
    def __init__(self, map_time: Callable[[Decimal], Decimal]) -> None:
        self.map_time = map_time
        self.origins: dict[tuple[bytes, int, bytes, int], tuple[Decimal, int]] = {}
        self.rates: dict[tuple[bytes, int, bytes, int], Decimal] = {}
        self.syn_sequences: dict[tuple[bytes, int, bytes, int], int] = {}
        self.measured_rates: dict[
            tuple[tuple[bytes, int, bytes, int], tuple[Decimal, int]], Decimal
        ] = {}

    def measure(self, records: Iterator[PcapRecord]) -> None:
        mapping = self.map_time
        self.map_time = lambda value: value
        try:
            for record in records:
                self.rewrite(record)
        finally:
            self.map_time = mapping
            self.origins.clear()
            self.rates.clear()
            self.syn_sequences.clear()

    def rewrite(self, record: PcapRecord) -> PcapRecord:
        if record.network != 1 or record.captured_length != record.original_length:
            return record
        frame = bytearray(record.payload)
        if len(frame) < 14 or frame[12:14] != b"\x08\x00":
            return record
        ip = 14
        if len(frame) < ip + 20 or frame[ip] >> 4 != 4 or frame[ip + 9] != 6:
            return record
        ip_length = (frame[ip] & 15) * 4
        total_length = int.from_bytes(frame[ip + 2 : ip + 4], "big")
        if (
            ip_length < 20
            or len(frame) < ip + total_length
            or frame[ip + 6] & 0x20
            or frame[ip + 6] & 0x1F
            or frame[ip + 7]
        ):
            return record
        tcp = ip + ip_length
        if total_length < ip_length + 20:
            return record
        tcp_length = (frame[tcp + 12] >> 4) * 4
        if tcp_length < 20 or total_length < ip_length + tcp_length:
            return record
        option = tcp + 20
        option_end = tcp + tcp_length
        while option < option_end:
            kind = frame[option]
            if kind == 0:
                break
            if kind == 1:
                option += 1
                continue
            if option + 2 > option_end:
                return record
            length = frame[option + 1]
            if length < 2 or option + length > option_end:
                return record
            if kind == 8 and length == 10:
                break
            option += length
        else:
            return record
        if option >= option_end or frame[option] != 8:
            return record

        source = bytes(frame[ip + 12 : ip + 16])
        destination = bytes(frame[ip + 16 : ip + 20])
        source_port = int.from_bytes(frame[tcp : tcp + 2], "big")
        destination_port = int.from_bytes(frame[tcp + 2 : tcp + 4], "big")
        flow = (source, source_port, destination, destination_port)
        peer = (destination, destination_port, source, source_port)
        if frame[tcp + 13] & 2 and not frame[tcp + 13] & 16:
            sequence = int.from_bytes(frame[tcp + 4 : tcp + 8], "big")
            if self.syn_sequences.get(flow) != sequence:
                self.origins.pop(flow, None)
                self.origins.pop(peer, None)
                self.rates.pop(flow, None)
                self.rates.pop(peer, None)
                self.syn_sequences[flow] = sequence
        timestamp = int.from_bytes(frame[option + 2 : option + 6], "big")
        echo = int.from_bytes(frame[option + 6 : option + 10], "big")
        origin = self.origins.setdefault(flow, (record.timestamp, timestamp))
        measured = self.measured_rates.get((flow, origin))
        if measured is not None:
            self.rates[flow] = measured
        elapsed = record.timestamp - origin[0]
        if flow not in self.rates and elapsed >= Decimal("0.1"):
            ticks = (timestamp - origin[1]) & 0xFFFFFFFF
            observed = Decimal(ticks) / elapsed
            candidates = [
                Decimal(rate)
                for rate in (1, 10, 100, 250, 1000)
                if abs(Decimal(ticks) - elapsed * rate) <= max(Decimal(2), elapsed * rate / 20)
            ]
            if len(candidates) == 1 and observed > 0:
                self.rates[flow] = candidates[0]
                self.measured_rates[(flow, origin)] = candidates[0]
        mapped_timestamp = self._value(timestamp, origin, self.rates.get(flow), record.timestamp)
        peer_origin = self.origins.get(peer)
        peer_rate = self.rates.get(peer)
        if peer_rate is None and peer_origin is not None:
            peer_rate = self.measured_rates.get((peer, peer_origin))
        mapped_echo = (
            self._value(echo, peer_origin, peer_rate, record.timestamp)
            if echo and peer_origin
            else echo
        )
        if mapped_timestamp == timestamp and mapped_echo == echo:
            return record
        frame[option + 2 : option + 6] = mapped_timestamp.to_bytes(4, "big")
        frame[option + 6 : option + 10] = mapped_echo.to_bytes(4, "big")
        frame[tcp + 16 : tcp + 18] = b"\x00\x00"
        segment = bytes(frame[tcp : ip + total_length])
        pseudo_header = source + destination + b"\x00\x06" + len(segment).to_bytes(2, "big")
        frame[tcp + 16 : tcp + 18] = self._checksum(pseudo_header + segment).to_bytes(2, "big")
        return replace(record, payload=bytes(frame))

    def _value(
        self,
        value: int,
        origin: tuple[Decimal, int],
        rate: Decimal | None,
        observed_at: Decimal,
    ) -> int:
        raw_time, first_value = origin
        if value == first_value:
            return value
        if rate is None:
            if self.map_time(observed_at) - self.map_time(raw_time) != observed_at - raw_time:
                raise PcapError("cannot retime TCP timestamps without a measured clock rate")
            return value
        ticks = (value - first_value) & 0xFFFFFFFF
        # Echoes can refer to packets sent before capture began.
        if ticks >= 0x80000000:
            ticks -= 0x100000000
        estimated_time = raw_time + Decimal(ticks) / rate
        elapsed_ticks = (self.map_time(estimated_time) - self.map_time(raw_time)) * rate
        mapped = first_value + int(elapsed_ticks.to_integral_value(rounding=ROUND_HALF_EVEN))
        return mapped & 0xFFFFFFFF

    @staticmethod
    def _checksum(data: bytes) -> int:
        if len(data) & 1:
            data += b"\x00"
        total = sum(int(value) for value in struct.unpack(f"!{len(data) // 2}H", data))
        while total >> 16:
            total = (total & 0xFFFF) + (total >> 16)
        return (~total) & 0xFFFF


class _BinaryWriter(Protocol):
    def write(self, data: bytes) -> int: ...


@dataclass(frozen=True, slots=True)
class PcapHeader:
    """Classic-PCAP link and timestamp metadata."""

    endian: str
    timestamp_scale: int
    snaplen: int
    network: int


@dataclass(frozen=True, slots=True)
class PcapRecord:
    """One packet record read from a PCAP or PCAPNG file."""

    timestamp: Decimal
    captured_length: int
    original_length: int
    payload: bytes
    network: int


@dataclass(frozen=True, slots=True)
class PcapNgSource:
    """One classic-PCAP input represented as an interface in a PCAPNG export."""

    path: Path
    name: str
    description: str | None = None


class PcapTimestampMapper:
    """Read PCAP/PCAPNG records and retime classic PCAP, optionally rewriting TCP timestamps."""

    @staticmethod
    def first_timestamp(path: Path) -> Decimal:
        """Return the first packet timestamp or raise when the capture has no packets."""
        timestamp = PcapTimestampMapper.first_timestamp_or_none(path)
        if timestamp is None:
            raise PcapError(f"PCAP {path} contains no packets")
        return timestamp

    @staticmethod
    def first_timestamp_or_none(path: Path) -> Decimal | None:
        """Return the first packet timestamp, or ``None`` for an empty capture."""
        record = next(PcapTimestampMapper.iter_records(path), None)
        return None if record is None else record.timestamp

    @staticmethod
    def classic_header(path: Path) -> PcapHeader:
        """Return the header for a classic-PCAP source used by the GNS3 exporter."""
        with path.open("rb") as handle:
            return PcapTimestampMapper._classic_header(handle, path)

    @staticmethod
    def iter_records(path: Path) -> Iterator[PcapRecord]:
        """Yield records from classic PCAP or PCAPNG without loading the capture into memory."""
        with path.open("rb") as handle:
            magic = handle.read(4)
            handle.seek(0)
            if magic in _PCAP_MAGIC:
                yield from PcapTimestampMapper._iter_classic_records(handle, path)
                return
            if magic == struct.pack("<I", _PCAPNG_SECTION_HEADER):
                yield from PcapTimestampMapper._iter_pcapng_records(handle, path)
                return
        raise PcapError(f"unsupported PCAP magic number in {path}")

    @staticmethod
    def retime(
        source: Path,
        destination: Path,
        *,
        raw_anchor: Decimal,
        scenario_anchor: datetime,
        speed: float,
        rewrite_tcp_timestamps: bool = False,
        segments: Sequence[tuple[Decimal, datetime, float]] = (),
    ) -> int:
        """Atomically map classic-PCAP timestamps to scenario time and return the packet count.

        Packet bytes stay unchanged unless rewrite_tcp_timestamps is enabled. That option
        measures TCP clock rates before rewriting timestamp options and TCP checksums.
        """
        if speed <= 0:
            raise ValueError("speed must be positive")
        destination.parent.mkdir(parents=True, exist_ok=True)
        packet_count = 0
        temporary_path: Path | None = None
        try:
            with (
                source.open("rb") as src,
                tempfile.NamedTemporaryFile(
                    mode="wb",
                    dir=destination.parent,
                    prefix=f".{destination.name}.",
                    suffix=".tmp",
                    delete=False,
                ) as dst,
            ):
                temporary_path = Path(dst.name)
                header_bytes = src.read(24)
                header = PcapTimestampMapper._parse_classic_header(header_bytes, source)
                dst.write(header_bytes)
                retimer = (
                    _TcpTimestampRetimer(
                        lambda value: PcapNgWriter._retimed_timestamp(
                            value,
                            raw_anchor=raw_anchor,
                            scenario_anchor=scenario_anchor,
                            speed=speed,
                            segments=segments,
                        )
                    )
                    if rewrite_tcp_timestamps
                    else None
                )
                if retimer is not None:
                    retimer.measure(PcapTimestampMapper.iter_records(source))
                while True:
                    record_header = src.read(16)
                    if not record_header:
                        break
                    if len(record_header) != 16:
                        raise PcapError(f"PCAP {source} has a truncated packet header")
                    ts_sec, ts_frac, incl_len, orig_len = struct.unpack(
                        f"{header.endian}IIII", record_header
                    )
                    PcapTimestampMapper._validate_record(
                        source,
                        ts_frac=ts_frac,
                        scale=header.timestamp_scale,
                        incl_len=incl_len,
                        snaplen=header.snaplen,
                    )
                    payload = src.read(incl_len)
                    if len(payload) != incl_len:
                        raise PcapError(f"PCAP {source} has a truncated packet payload")
                    raw_ticks = Decimal(ts_sec * header.timestamp_scale + ts_frac)
                    if retimer is not None:
                        payload = retimer.rewrite(
                            PcapRecord(
                                timestamp=raw_ticks / header.timestamp_scale,
                                captured_length=incl_len,
                                original_length=orig_len,
                                payload=payload,
                                network=header.network,
                            )
                        ).payload
                    mapped_ticks = (
                        PcapNgWriter._retimed_timestamp(
                            raw_ticks / header.timestamp_scale,
                            raw_anchor=raw_anchor,
                            scenario_anchor=scenario_anchor,
                            speed=speed,
                            segments=segments,
                        )
                        * header.timestamp_scale
                    )
                    mapped_int = int(mapped_ticks.to_integral_value(rounding=ROUND_HALF_EVEN))
                    if mapped_int < 0:
                        raise PcapError("retimed timestamp would be before Unix epoch")
                    mapped_sec, mapped_frac = divmod(mapped_int, header.timestamp_scale)
                    if mapped_sec > 0xFFFFFFFF:
                        raise PcapError(
                            "retimed timestamp exceeds classic PCAP 32-bit seconds range"
                        )
                    dst.write(
                        struct.pack(
                            f"{header.endian}IIII", mapped_sec, mapped_frac, incl_len, orig_len
                        )
                    )
                    dst.write(payload)
                    packet_count += 1
                dst.flush()
                os.fsync(dst.fileno())
            assert temporary_path is not None
            temporary_path.replace(destination)
        except Exception:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
            raise
        return packet_count

    @staticmethod
    def copy_without_retime(source: Path, destination: Path) -> None:
        """Copy a capture atomically while preserving packet timestamps and bytes."""
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary_path: Path | None = None
        try:
            with (
                source.open("rb") as src,
                tempfile.NamedTemporaryFile(
                    mode="wb",
                    dir=destination.parent,
                    prefix=f".{destination.name}.",
                    suffix=".tmp",
                    delete=False,
                ) as dst,
            ):
                temporary_path = Path(dst.name)
                shutil.copyfileobj(src, dst)
                dst.flush()
                os.fsync(dst.fileno())
            assert temporary_path is not None
            temporary_path.replace(destination)
        except Exception:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
            raise

    @staticmethod
    def discard_incomplete_tail(path: Path) -> bool:
        """Discard an incomplete final classic-PCAP record left by capture shutdown."""
        with path.open("r+b") as handle:
            header = PcapTimestampMapper._classic_header(handle, path)
            complete_size = 24
            while True:
                record_header = handle.read(16)
                if not record_header:
                    return False
                if len(record_header) != 16:
                    handle.truncate(complete_size)
                    handle.flush()
                    os.fsync(handle.fileno())
                    return True
                _sec, fraction, length, _original = struct.unpack(
                    f"{header.endian}IIII", record_header
                )
                PcapTimestampMapper._validate_record(
                    path,
                    ts_frac=fraction,
                    scale=header.timestamp_scale,
                    incl_len=length,
                    snaplen=header.snaplen,
                )
                if len(handle.read(length)) != length:
                    handle.truncate(complete_size)
                    handle.flush()
                    os.fsync(handle.fileno())
                    return True
                complete_size += 16 + length

    @staticmethod
    def _classic_header(handle: BinaryIO, path: Path) -> PcapHeader:
        return PcapTimestampMapper._parse_classic_header(handle.read(24), path)

    @staticmethod
    def _parse_classic_header(value: bytes, path: Path) -> PcapHeader:
        if len(value) != 24:
            raise PcapError("not a complete classic PCAP global header")
        try:
            endian, scale = _PCAP_MAGIC[value[:4]]
        except KeyError as exc:
            if value[:4] == struct.pack("<I", _PCAPNG_SECTION_HEADER):
                raise PcapError("classic PCAP operation does not accept PCAPNG") from exc
            raise PcapError("unsupported PCAP magic number") from exc
        major, minor, _zone, _sigfigs, snaplen, network = struct.unpack(
            f"{endian}HHiIII", value[4:]
        )
        if (major, minor) != (2, 4):
            raise PcapError(f"unsupported classic PCAP version {major}.{minor}")
        if snaplen <= 0:
            raise PcapError("classic PCAP snaplen must be greater than zero")
        return PcapHeader(endian=endian, timestamp_scale=scale, snaplen=snaplen, network=network)

    @staticmethod
    def _iter_classic_records(handle: BinaryIO, path: Path) -> Iterator[PcapRecord]:
        header = PcapTimestampMapper._classic_header(handle, path)
        while True:
            record_header = handle.read(16)
            if not record_header:
                return
            if len(record_header) != 16:
                raise PcapError(f"PCAP {path} has a truncated packet header")
            seconds, fraction, length, original_length = struct.unpack(
                f"{header.endian}IIII", record_header
            )
            PcapTimestampMapper._validate_record(
                path,
                ts_frac=fraction,
                scale=header.timestamp_scale,
                incl_len=length,
                snaplen=header.snaplen,
            )
            payload = handle.read(length)
            if len(payload) != length:
                raise PcapError(f"PCAP {path} has a truncated packet payload")
            yield PcapRecord(
                timestamp=Decimal(seconds) + Decimal(fraction) / Decimal(header.timestamp_scale),
                captured_length=length,
                original_length=original_length,
                payload=payload,
                network=header.network,
            )

    @staticmethod
    def _iter_pcapng_records(handle: BinaryIO, path: Path) -> Iterator[PcapRecord]:
        block_type = handle.read(4)
        length_bytes = handle.read(4)
        byte_order_magic = handle.read(4)
        if block_type != struct.pack("<I", _PCAPNG_SECTION_HEADER) or len(byte_order_magic) != 4:
            raise PcapError(f"PCAPNG {path} lacks a section header")
        if byte_order_magic == struct.pack("<I", _PCAPNG_BYTE_ORDER_MAGIC):
            endian = "<"
        elif byte_order_magic == struct.pack(">I", _PCAPNG_BYTE_ORDER_MAGIC):
            endian = ">"
        else:
            raise PcapError(f"PCAPNG {path} has an invalid byte-order magic")
        section_length = struct.unpack(f"{endian}I", length_bytes)[0]
        if section_length < 28:
            raise PcapError(f"PCAPNG {path} has an invalid section-header length")
        remainder = byte_order_magic + handle.read(section_length - 12)
        PcapTimestampMapper._validate_pcapng_block(path, remainder, section_length, endian)
        interfaces: list[tuple[int, int]] = []
        while True:
            type_bytes = handle.read(4)
            if not type_bytes:
                return
            length_bytes = handle.read(4)
            if len(length_bytes) != 4:
                raise PcapError(f"PCAPNG {path} has a truncated block header")
            kind = struct.unpack(f"{endian}I", type_bytes)[0]
            block_length = struct.unpack(f"{endian}I", length_bytes)[0]
            if block_length < 12:
                raise PcapError(f"PCAPNG {path} has an invalid block length")
            remainder = handle.read(block_length - 8)
            PcapTimestampMapper._validate_pcapng_block(path, remainder, block_length, endian)
            body = remainder[:-4]
            if kind == _PCAPNG_INTERFACE_DESCRIPTION:
                if len(body) < 8:
                    raise PcapError(f"PCAPNG {path} has a truncated interface description")
                network, _reserved, _snaplen = struct.unpack(f"{endian}HHI", body[:8])
                interfaces.append((network, PcapTimestampMapper._timestamp_scale(body[8:], endian)))
            elif kind == _PCAPNG_ENHANCED_PACKET:
                if len(body) < 20:
                    raise PcapError(f"PCAPNG {path} has a truncated enhanced packet")
                interface_id, high, low, length, original_length = struct.unpack(
                    f"{endian}IIIII", body[:20]
                )
                try:
                    network, scale = interfaces[interface_id]
                except IndexError as exc:
                    raise PcapError(
                        f"PCAPNG {path} packet references unknown interface {interface_id}"
                    ) from exc
                padded_length = PcapNgWriter.padded_length(length)
                if len(body) < 20 + padded_length:
                    raise PcapError(f"PCAPNG {path} has a truncated packet payload")
                yield PcapRecord(
                    timestamp=Decimal((high << 32) | low) / Decimal(scale),
                    captured_length=length,
                    original_length=original_length,
                    payload=body[20 : 20 + length],
                    network=network,
                )

    @staticmethod
    def _timestamp_scale(options: bytes, endian: str) -> int:
        for code, value in PcapTimestampMapper._pcapng_options(options, endian):
            if code == 9 and len(value) == 1:
                resolution = int.from_bytes(value, "little")
                return int(2 ** (resolution & 0x7F)) if resolution & 0x80 else int(10**resolution)
        return 1_000_000

    @staticmethod
    def _pcapng_options(value: bytes, endian: str) -> Iterator[tuple[int, bytes]]:
        offset = 0
        while offset + 4 <= len(value):
            code, length = struct.unpack(f"{endian}HH", value[offset : offset + 4])
            offset += 4
            if code == 0:
                return
            padded_length = PcapNgWriter.padded_length(length)
            if offset + padded_length > len(value):
                raise PcapError("PCAPNG has a truncated option")
            yield code, value[offset : offset + length]
            offset += padded_length

    @staticmethod
    def _validate_pcapng_block(path: Path, remainder: bytes, length: int, endian: str) -> None:
        if len(remainder) != length - 8:
            raise PcapError(f"PCAPNG {path} has a truncated block")
        if struct.unpack(f"{endian}I", remainder[-4:])[0] != length:
            raise PcapError(f"PCAPNG {path} has mismatched block lengths")

    @staticmethod
    def _validate_record(
        path: Path, *, ts_frac: int, scale: int, incl_len: int, snaplen: int
    ) -> None:
        if ts_frac >= scale:
            raise PcapError(f"PCAP {path} has an invalid fractional timestamp")
        if incl_len > snaplen:
            raise PcapError(f"PCAP {path} record length {incl_len} exceeds snaplen {snaplen}")

    @staticmethod
    def _datetime_ticks(value: datetime, scale: int) -> Decimal:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("scenario_anchor must be timezone-aware")
        delta = value.astimezone(UTC) - _EPOCH
        total_microseconds = (delta.days * 86_400 + delta.seconds) * 1_000_000 + delta.microseconds
        return Decimal(total_microseconds) * Decimal(scale) / Decimal(1_000_000)


class PcapNgWriter:
    """Write a streaming PCAPNG export from one or more classic-PCAP captures."""

    @staticmethod
    def write(
        destination: Path,
        *,
        sources: Sequence[PcapNgSource],
        metadata: Mapping[str, str | None],
        timestamp_policy: str,
        raw_anchor: Decimal | None = None,
        scenario_anchor: datetime | None = None,
        speed: float = 1.0,
        segments: Sequence[tuple[Decimal, datetime, float]] = (),
        rewrite_tcp_timestamps: bool = False,
        deduplicate_packets: bool = False,
    ) -> int:
        """Export sources as a timestamp-ordered PCAPNG capture without buffering packets."""
        if not sources:
            raise PcapError("PCAPNG export requires at least one source")
        if timestamp_policy not in {"retime", "preserve"}:
            raise ValueError(f"unsupported timestamp policy {timestamp_policy!r}")
        if (
            timestamp_policy == "retime"
            and not segments
            and (raw_anchor is None or scenario_anchor is None)
        ):
            raise ValueError("retimed PCAPNG export requires raw and scenario anchors")
        if speed <= 0:
            raise ValueError("speed must be positive")
        headers = [PcapTimestampMapper.classic_header(source.path) for source in sources]
        scale = max(header.timestamp_scale for header in headers)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary_path: Path | None = None
        packet_count = 0
        retimer = (
            _TcpTimestampRetimer(
                lambda value: PcapNgWriter._retimed_timestamp(
                    value,
                    raw_anchor=raw_anchor if raw_anchor is not None else Decimal(0),
                    scenario_anchor=(
                        scenario_anchor
                        if scenario_anchor is not None
                        else datetime.fromtimestamp(0, tz=UTC)
                    ),
                    speed=speed,
                    segments=segments,
                )
            )
            if rewrite_tcp_timestamps and timestamp_policy == "retime"
            else None
        )
        if retimer is not None:
            retimer.measure(
                record
                for _index, record in PcapNgWriter._merge_records(
                    sources, deduplicate_packets=deduplicate_packets
                )
            )
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb",
                dir=destination.parent,
                prefix=f".{destination.name}.",
                suffix=".tmp",
                delete=False,
            ) as handle:
                temporary_path = Path(handle.name)
                PcapNgWriter._write_section_header(handle.file, metadata)
                for source, header in zip(sources, headers, strict=True):
                    PcapNgWriter._write_interface_description(
                        handle.file,
                        link_type=header.network,
                        snaplen=header.snaplen,
                        name=source.name,
                        description=source.description,
                        scale=scale,
                    )
                for index, record in PcapNgWriter._merge_records(
                    sources, deduplicate_packets=deduplicate_packets
                ):
                    timestamp = record.timestamp
                    if timestamp_policy == "retime":
                        if not segments:
                            assert raw_anchor is not None and scenario_anchor is not None
                        timestamp = PcapNgWriter._retimed_timestamp(
                            timestamp,
                            raw_anchor=raw_anchor if raw_anchor is not None else Decimal(0),
                            scenario_anchor=(
                                scenario_anchor
                                if scenario_anchor is not None
                                else datetime.fromtimestamp(0, tz=UTC)
                            ),
                            speed=speed,
                            segments=segments,
                        )
                        if retimer is not None:
                            record = retimer.rewrite(record)
                    PcapNgWriter._write_enhanced_packet(
                        handle.file,
                        source_index=index,
                        timestamp=timestamp,
                        scale=scale,
                        record=record,
                    )
                    packet_count += 1
                handle.flush()
                os.fsync(handle.fileno())
            assert temporary_path is not None
            temporary_path.replace(destination)
        except Exception:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
            raise
        return packet_count

    @staticmethod
    def padded_length(length: int) -> int:
        """Return a PCAPNG four-byte padded payload length."""
        return (length + 3) & ~3

    @staticmethod
    def _merge_records(
        sources: Sequence[PcapNgSource], *, deduplicate_packets: bool = False
    ) -> Iterator[tuple[int, PcapRecord]]:
        iterators = [iter(PcapTimestampMapper.iter_records(source.path)) for source in sources]
        heap: list[tuple[Decimal, int, int, PcapRecord]] = []
        sequence = 0
        recent: dict[bytes, deque[tuple[Decimal, set[int]]]] = {}
        for index, iterator in enumerate(iterators):
            record = next(iterator, None)
            if record is not None:
                heapq.heappush(heap, (record.timestamp, sequence, index, record))
                sequence += 1
        while heap:
            _timestamp, _order, index, record = heapq.heappop(heap)
            if not deduplicate_packets:
                yield index, record
            else:
                signature = hashlib.blake2s(record.payload, digest_size=16).digest()
                observations = recent.setdefault(signature, deque())
                while observations and record.timestamp - observations[0][0] > Decimal("0.01"):
                    observations.popleft()
                match = next((seen for _time, seen in observations if index not in seen), None)
                if match is None:
                    observations.append((record.timestamp, {index}))
                    yield index, record
                else:
                    match.add(index)
            next_record = next(iterators[index], None)
            if next_record is not None:
                heapq.heappush(heap, (next_record.timestamp, sequence, index, next_record))
                sequence += 1

    @staticmethod
    def _retimed_timestamp(
        timestamp: Decimal,
        *,
        raw_anchor: Decimal,
        scenario_anchor: datetime,
        speed: float,
        segments: Sequence[tuple[Decimal, datetime, float]] = (),
    ) -> Decimal:
        if segments:
            # Find the active segment matching this packet's raw timestamp
            active_segment = segments[0]
            for seg in segments:
                if timestamp >= seg[0]:
                    active_segment = seg
                else:
                    break
            seg_raw_start, seg_scenario_start, seg_speed = active_segment
            return Decimal(str(seg_scenario_start.timestamp())) + (
                timestamp - seg_raw_start
            ) * Decimal(str(seg_speed))

        return Decimal(str(scenario_anchor.timestamp())) + (timestamp - raw_anchor) * Decimal(
            str(speed)
        )

    @staticmethod
    def _write_section_header(handle: _BinaryWriter, metadata: Mapping[str, str | None]) -> None:
        options = PcapNgWriter._options(
            (
                (1, metadata.get("comment")),
                (2, metadata.get("hardware")),
                (3, metadata.get("os")),
                (4, metadata.get("application")),
            )
        )
        PcapNgWriter._write_block(
            handle,
            _PCAPNG_SECTION_HEADER,
            struct.pack("<IHHq", _PCAPNG_BYTE_ORDER_MAGIC, 1, 0, -1) + options,
        )

    @staticmethod
    def _write_interface_description(
        handle: _BinaryWriter,
        *,
        link_type: int,
        snaplen: int,
        name: str,
        description: str | None,
        scale: int,
    ) -> None:
        resolution = 9 if scale == 1_000_000_000 else 6
        options = PcapNgWriter._options(((2, name), (3, description), (9, bytes((resolution,)))))
        PcapNgWriter._write_block(
            handle,
            _PCAPNG_INTERFACE_DESCRIPTION,
            struct.pack("<HHI", link_type, 0, snaplen) + options,
        )

    @staticmethod
    def _write_enhanced_packet(
        handle: _BinaryWriter,
        *,
        source_index: int,
        timestamp: Decimal,
        scale: int,
        record: PcapRecord,
    ) -> None:
        ticks = int((timestamp * Decimal(scale)).to_integral_value(rounding=ROUND_HALF_EVEN))
        if ticks < 0 or ticks > 0xFFFFFFFFFFFFFFFF:
            raise PcapError("PCAPNG timestamp is outside the supported range")
        payload = (
            struct.pack(
                "<IIIII",
                source_index,
                ticks >> 32,
                ticks & 0xFFFFFFFF,
                record.captured_length,
                record.original_length,
            )
            + record.payload
        )
        PcapNgWriter._write_block(handle, _PCAPNG_ENHANCED_PACKET, payload)

    @staticmethod
    def _write_block(handle: _BinaryWriter, block_type: int, payload: bytes) -> None:
        padded = payload + b"\x00" * (PcapNgWriter.padded_length(len(payload)) - len(payload))
        length = len(padded) + 12
        handle.write(struct.pack("<II", block_type, length))
        handle.write(padded)
        handle.write(struct.pack("<I", length))

    @staticmethod
    def _options(values: Sequence[tuple[int, str | bytes | None]]) -> bytes:
        result = bytearray()
        for code, value in values:
            if value is None:
                continue
            encoded = value if isinstance(value, bytes) else value.encode("utf-8")
            result.extend(struct.pack("<HH", code, len(encoded)))
            result.extend(encoded)
            result.extend(b"\x00" * (PcapNgWriter.padded_length(len(encoded)) - len(encoded)))
        result.extend(struct.pack("<HH", 0, 0))
        return bytes(result)
