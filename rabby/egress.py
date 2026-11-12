"""TCP <-> unix socket forwarder, to reach a host-side service from inside isolated.sh's netns.

A filesystem unix socket is visible in every netns, loopback TCP is not. Recording the Rabby API
fixture needs the internet, the simulator must stay in the netns, so the recording proxy runs on
the host and the browser in the netns reaches it through this:

  host:   egress.py unix-to-tcp /tmp/x.sock 127.0.0.1:18080
  netns:  egress.py tcp-to-unix 127.0.0.1:18080 /tmp/x.sock
"""

import asyncio
import sys


async def pipe(reader, writer):
    try:
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
    except ConnectionError:
        pass
    finally:
        writer.close()


def handler(connect):
    async def handle(reader, writer):
        try:
            r2, w2 = await connect()
        except OSError:
            writer.close()
            return
        await asyncio.gather(pipe(reader, w2), pipe(r2, writer))
    return handle


async def main(mode, src, dst):
    if mode == "unix-to-tcp":
        host, port = dst.rsplit(":", 1)
        server = await asyncio.start_unix_server(handler(lambda: asyncio.open_connection(host, int(port))), src)
    elif mode == "tcp-to-unix":
        host, port = src.rsplit(":", 1)
        server = await asyncio.start_server(handler(lambda: asyncio.open_unix_connection(dst)), host, int(port))
    else:
        raise SystemExit(f"unknown mode {mode}")
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main(*sys.argv[1:4]))
