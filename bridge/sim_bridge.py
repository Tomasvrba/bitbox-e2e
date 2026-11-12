#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["aiohttp>=3.9"]
# ///
"""Fake BitBoxBridge in front of a BitBox02 simulator.

Web wallets (Rabby via @bitboxswiss/bitbox-api `bitbox02ConnectBridge`) find the device with
GET http://127.0.0.1:8178/api/v1/devices and talk to it over ws://127.0.0.1:8178/api/v1/socket/<path>.
On the socket every message is one U2F-WS frame: cid u32 BE, cmd u8, len u16 BE, payload, no
packetisation. The simulator speaks U2F-HID: the same header, but split into 64-byte packets with
seq-numbered continuation headers. We re-frame in both directions, like the real bridge does
between the WS and the HID device.

One simulator TCP connection per WS connection. Like the real bridge (usb.rs `acquire_device`), a
new socket takes the device over: the previous socket and its simulator connection are closed.
Rabby relies on that, it reconnects for every keyring operation without closing the old socket.

  sim_bridge.py [--sim 127.0.0.1:15423] [--port 8178] [--log FILE]
"""

import argparse
import asyncio
import json
import struct
import sys
import time

from aiohttp import WSMsgType, web

PACKET = 64
INIT_HDR = 7
CONT_HDR = 5
DEVICE_PATH = "bitbox02-simulator"

log_file = sys.stderr
# The socket that currently owns the simulator: (ws, tcp writer).
STATE = {"current": None}


def log(msg):
    print(f"[bridge {time.strftime('%H:%M:%S')}] {msg}", file=log_file, flush=True)


def hid_packets(frame):
    """U2F-WS frame -> U2F-HID packets (header kept, payload split, zero padded)."""
    cid, cmd, n = struct.unpack(">IBH", frame[:INIT_HDR])
    payload = frame[INIT_HDR:INIT_HDR + n]
    if len(payload) != n:
        raise ValueError(f"ws frame says {n} payload bytes, has {len(payload)}")
    out = bytearray(frame[:INIT_HDR] + payload[:PACKET - INIT_HDR])
    out += bytes(-len(out) % PACKET)
    off, seq = PACKET - INIT_HDR, 0
    while off < n:
        chunk = payload[off:off + PACKET - CONT_HDR]
        pkt = struct.pack(">IB", cid, seq) + chunk
        out += pkt + bytes(PACKET - len(pkt))
        off, seq = off + len(chunk), seq + 1
    return bytes(out)


async def read_hid_message(reader):
    """Read U2F-HID packets until one message is complete; return it as a U2F-WS frame."""
    first = await reader.readexactly(PACKET)
    cid, cmd, n = struct.unpack(">IBH", first[:INIT_HDR])
    payload = bytearray(first[INIT_HDR:INIT_HDR + min(n, PACKET - INIT_HDR)])
    while len(payload) < n:
        pkt = await reader.readexactly(PACKET)
        payload += pkt[CONT_HDR:CONT_HDR + min(n - len(payload), PACKET - CONT_HDR)]
    return struct.pack(">IBH", cid, cmd, n) + bytes(payload)


async def connect_sim(host, port, attempts=50):
    """The simulator accepts a second client and drops it right away while the previous one is
    still registered (it notices a disconnect only on its next poll), so retry until a
    connection stays open."""
    for _ in range(attempts):
        try:
            reader, writer = await asyncio.open_connection(host, port)
        except OSError:
            await asyncio.sleep(0.2)
            continue
        try:
            data = await asyncio.wait_for(reader.read(1), 0.3)
        except asyncio.TimeoutError:
            return reader, writer
        writer.close()
        if data:
            raise RuntimeError("simulator sent data before any request")
        await asyncio.sleep(0.2)
    raise RuntimeError(f"simulator at {host}:{port} not accepting")


def cors(resp):
    # The real bridge whitelists origins; anything local is fine here.
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Private-Network"] = "true"
    return resp


async def devices(request):
    return cors(web.json_response({"devices": [{"path": DEVICE_PATH, "product": "BitBox02"}]}))


async def info(request):
    return cors(web.json_response({"version": "1.6.1-sim"}))


async def socket(request):
    host, port = request.app["sim"]
    if request.match_info["path"] != DEVICE_PATH:
        raise web.HTTPNotFound()
    ws = web.WebSocketResponse(max_msg_size=0)
    await ws.prepare(request)
    if prev := STATE["current"]:
        log("new socket takes over the device")
        prev[1].close()
        await prev[0].close()
    STATE["current"] = None
    try:
        reader, writer = await connect_sim(host, port)
    except RuntimeError as e:
        log(f"ws open failed: {e}")
        await ws.close()
        return ws
    log("ws open, simulator connected")
    STATE["current"] = (ws, writer)

    async def sim_to_ws():
        try:
            while True:
                await ws.send_bytes(await read_hid_message(reader))
        except (asyncio.IncompleteReadError, ConnectionError):
            log("simulator closed the connection")
            await ws.close()

    pump = asyncio.create_task(sim_to_ws())
    try:
        async for msg in ws:
            if msg.type == WSMsgType.BINARY:
                try:
                    packets = hid_packets(msg.data)
                except (ValueError, struct.error) as e:
                    # Like the real bridge: drop the message, keep the socket.
                    log(f"message ignored: {e}")
                    continue
                writer.write(packets)
                await writer.drain()
            elif msg.type == WSMsgType.ERROR:
                break
    finally:
        pump.cancel()
        writer.close()
        if STATE["current"] and STATE["current"][0] is ws:
            STATE["current"] = None
        log("ws closed, simulator disconnected")
    return ws


def main():
    global log_file
    p = argparse.ArgumentParser()
    p.add_argument("--sim", default="127.0.0.1:15423")
    p.add_argument("--port", type=int, default=8178)
    p.add_argument("--log")
    args = p.parse_args()
    if args.log:
        log_file = open(args.log, "a")
    host, port = args.sim.rsplit(":", 1)
    app = web.Application()
    app["sim"] = (host, int(port))
    app.router.add_get("/api/v1/devices", devices)
    app.router.add_get("/api/info", info)
    app.router.add_get("/api/v1/socket/{path}", socket)
    log(f"listening on 127.0.0.1:{args.port}, simulator {args.sim}")
    web.run_app(app, host="127.0.0.1", port=args.port, print=None, access_log=None)


if __name__ == "__main__":
    main()
