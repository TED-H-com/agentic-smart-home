"""Loopback TCP forwarder for the HA browser, including WebSocket traffic."""
import asyncio

from .manager import IP


async def forward(reader, writer):
    upstream = None
    try:
        remote_reader, upstream = await asyncio.wait_for(asyncio.open_connection(IP, 80), 5)

        async def pump(source, target):
            try:
                while chunk := await source.read(65536):
                    target.write(chunk)
                    await target.drain()
            finally:
                target.close()

        await asyncio.gather(pump(reader, upstream), pump(remote_reader, writer))
    except (OSError, TimeoutError):
        pass
    finally:
        writer.close()
        if upstream:
            upstream.close()


async def main():
    server = await asyncio.start_server(forward, "127.0.0.1", 8123, limit=65536)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
