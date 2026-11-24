"""Contract tests for bridge/sim_bridge.py U2F-WS <-> U2F-HID packet framing."""

import asyncio
import importlib.util
import struct
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "sim_bridge",
    Path(__file__).resolve().parent.parent / "bridge" / "sim_bridge.py",
)
sim_bridge = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(sim_bridge)


def _make_frame(cid: int, cmd: int, payload: bytes) -> bytes:
    return struct.pack(">IBH", cid, cmd, len(payload)) + payload


def _roundtrip(frame: bytes) -> tuple[bytes, bytes]:
    packets = sim_bridge.hid_packets(frame)

    async def read() -> bytes:
        reader = asyncio.StreamReader()
        reader.feed_data(packets)
        reader.feed_eof()
        return await sim_bridge.read_hid_message(reader)

    return packets, asyncio.run(read())


def test_single_packet_payload_round_trips_with_zero_padding():
    frame = _make_frame(0x11223344, 0x83, b"hello bitbox")
    packets, rebuilt = _roundtrip(frame)
    assert len(packets) == sim_bridge.PACKET
    assert packets[: len(frame)] == frame
    assert packets[len(frame) :] == b"\x00" * (sim_bridge.PACKET - len(frame))
    assert rebuilt == frame


def test_exact_57_byte_payload_fits_in_one_packet_without_continuation():
    payload = bytes(range(57))
    frame = _make_frame(0xCAFEBABE, 0x81, payload)
    packets, rebuilt = _roundtrip(frame)
    assert len(packets) == sim_bridge.PACKET
    assert packets == frame
    assert rebuilt == frame


def test_multi_packet_payload_continuation_sequence_numbers_and_padding():
    cid, cmd = 0x01020304, 0xBF
    # 57 bytes in init packet + 59 + 59 + 35 bytes across 3 continuation packets = 210 bytes.
    payload = bytes(i % 251 for i in range(210))
    frame = _make_frame(cid, cmd, payload)
    packets, rebuilt = _roundtrip(frame)

    assert len(packets) == 4 * sim_bridge.PACKET
    for seq in range(3):
        pkt = packets[(seq + 1) * sim_bridge.PACKET : (seq + 2) * sim_bridge.PACKET]
        pkt_cid, pkt_seq = struct.unpack(">IB", pkt[: sim_bridge.CONT_HDR])
        assert pkt_cid == cid
        assert pkt_seq == seq

    last_pkt = packets[3 * sim_bridge.PACKET : 4 * sim_bridge.PACKET]
    assert last_pkt[sim_bridge.CONT_HDR : sim_bridge.CONT_HDR + 35] == payload[175:210]
    assert last_pkt[sim_bridge.CONT_HDR + 35 :] == b"\x00" * (sim_bridge.PACKET - sim_bridge.CONT_HDR - 35)
    assert rebuilt == frame


def test_header_length_exceeding_payload_raises_value_error():
    truncated = struct.pack(">IBH", 0x12345678, 0x83, 20) + b"x" * 10
    with pytest.raises(ValueError, match="20"):
        sim_bridge.hid_packets(truncated)
