import asyncio
import json
import unittest

from xemu_mcp.protocols import GDB, QMP, ProtocolError


class ProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.servers = []
        self.commands = []

    async def asyncTearDown(self):
        for server in self.servers:
            server.close()
            await server.wait_closed()

    async def serve(self, handler):
        server = await asyncio.start_server(handler, "127.0.0.1", 0)
        self.servers.append(server)
        return server.sockets[0].getsockname()[1]

    async def qmp_peer(self, reader, writer):
        writer.write(b'{"QMP":{"version":{}}}\r\n')
        await writer.drain()
        while line := await reader.readline():
            request = json.loads(line)
            self.commands.append(request)
            writer.write(b'{"event":"STOP"}\r\n')
            if request["execute"] == "bad":
                response = {"error": {"class": "CommandNotFound", "desc": "missing"}}
            else:
                response = {"return": {"running": True}}
            response["id"] = request["id"]
            writer.write(json.dumps(response).encode() + b'\r\n')
            await writer.drain()
        writer.close()

    async def test_qmp_negotiates_skips_events_serializes_and_surfaces_errors(self):
        qmp = QMP("127.0.0.1", await self.serve(self.qmp_peer), timeout=1)
        await qmp.connect()
        result = await asyncio.gather(qmp.execute("query-status"), qmp.execute("query-status"))
        self.assertEqual(result, [{"running": True}] * 2)
        self.assertEqual(self.commands[0]["execute"], "qmp_capabilities")
        self.assertEqual(len({c["id"] for c in self.commands}), 3)
        with self.assertRaisesRegex(ProtocolError, "CommandNotFound"):
            await qmp.execute("bad")
        await qmp.close()

    async def test_qmp_timeout_invalidates_connection(self):
        async def peer(reader, writer):
            writer.write(b'{"QMP":{}}\n')
            await writer.drain()
            await reader.read()
            writer.close()

        qmp = QMP("127.0.0.1", await self.serve(peer), timeout=.05)
        with self.assertRaises(TimeoutError):
            await qmp.connect()
        self.assertFalse(qmp.connected)

    async def test_gdb_checksums_ack_memory_error_and_detach(self):
        async def peer(reader, writer):
            try:
                while True:
                    char = await reader.readexactly(1)
                    if char in (b'+', b'-'):
                        continue
                    self.assertEqual(char, b'$')
                    payload = (await reader.readuntil(b'#'))[:-1]
                    checksum = await reader.readexactly(2)
                    self.assertEqual(int(checksum, 16), sum(payload) & 255)
                    self.commands.append(payload.decode())
                    reply = b'E14' if payload == b'mbad,1' else b'41424344'
                    if payload == b'D':
                        reply = b'OK'
                    writer.write(b'+' + b'$' + reply + b'#' + f'{sum(reply)&255:02x}'.encode())
                    await writer.drain()
                    if payload == b'D':
                        break
            finally:
                writer.close()

        gdb = GDB("127.0.0.1", await self.serve(peer), timeout=1)
        await gdb.connect()
        self.assertEqual(await gdb.read(0x100, 4), b'ABCD')
        with self.assertRaisesRegex(ProtocolError, "E14"):
            await gdb.read(0xbad, 1)
        await gdb.detach()
        self.assertEqual(self.commands, ['m100,4', 'mbad,1', 'D'])

    async def test_gdb_bad_checksum_is_retransmitted_and_rle_decoded(self):
        async def peer(reader, writer):
            await reader.readuntil(b'#')
            await reader.readexactly(2)
            writer.write(b'+$0*!#00')
            await writer.drain()
            self.assertEqual(await reader.readexactly(1), b'-')
            body = b'0*!'  # one zero plus four repeats => five zeroes
            writer.write(b'$' + body + b'#' + f'{sum(body)&255:02x}'.encode())
            await writer.drain()
            self.assertEqual(await reader.readexactly(1), b'+')
            writer.close()

        gdb = GDB("127.0.0.1", await self.serve(peer), timeout=1)
        await gdb.connect()
        self.assertEqual(await gdb.request('test'), '00000')
        await gdb.close()


if __name__ == '__main__':
    unittest.main()
