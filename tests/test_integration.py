"""Service operations over real sockets against synthetic emulator peers."""

import asyncio
import json
import re
import unittest

from xemu_mcp.protocols import QMP
from xemu_mcp.service import Config, Emulator


class FakeEmulator:
    def __init__(self):
        self.memory = bytearray(range(256)) * 8
        self.running = True
        self.snapshots = {}
        self.servers = []

    async def start(self):
        for handler in (self.qmp_peer, self.gdb_peer):
            self.servers.append(await asyncio.start_server(handler, '127.0.0.1', 0))
        return [server.sockets[0].getsockname()[1] for server in self.servers]

    async def close(self):
        for server in self.servers:
            server.close()
            await server.wait_closed()

    async def qmp_peer(self, reader, writer):
        writer.write(b'{"QMP":{"version":{}}}\n')
        await writer.drain()
        try:
            while line := await reader.readline():
                request = json.loads(line)
                command = request['execute']
                result = {}
                if command == 'query-status':
                    result = {'running': self.running, 'status': 'running' if self.running else 'paused'}
                elif command in ('stop', 'cont'):
                    self.running = command == 'cont'
                    writer.write(b'{"event":"RESUME"}\n')
                elif command == 'human-monitor-command':
                    text = request['arguments']['command-line']
                    result = ''
                    if text.startswith('savevm '):
                        self.snapshots[text.split()[1]] = bytes(self.memory)
                    elif text.startswith('loadvm '):
                        tag = text.split()[1]
                        if tag in self.snapshots:
                            self.memory[:] = self.snapshots[tag]
                        else:
                            result = 'Error: snapshot not found'
                    elif text.startswith('delvm '):
                        self.snapshots.pop(text.split()[1], None)
                    elif text.startswith('xp '):
                        match = re.fullmatch(r'xp /(\d+)xb 0x([0-9a-f]+)', text)
                        length, address = int(match[1]), int(match[2], 16)
                        result = f'{address:016x}: ' + ' '.join(f'0x{v:02x}' for v in self.memory[address:address+length])
                    elif text == 'info registers':
                        result = 'EAX=00000001 EIP=00001000 ESP=00000100 EFL=00000202\r\n'
                writer.write(json.dumps({'return': result, 'id': request['id']}).encode() + b'\n')
                await writer.drain()
        finally:
            writer.close()

    async def gdb_peer(self, reader, writer):
        try:
            while True:
                char = await reader.readexactly(1)
                if char in (b'+', b'-'):
                    continue
                body = (await reader.readuntil(b'#'))[:-1]
                checksum = await reader.readexactly(2)
                if int(checksum, 16) != sum(body) & 255:
                    writer.write(b'-')
                    await writer.drain()
                    continue
                command = body.decode()
                result = 'OK'
                if command.startswith('m'):
                    address, length = [int(v, 16) for v in command[1:].split(',')]
                    result = bytes(self.memory[address:address + length]).hex()
                elif command.startswith('M'):
                    head, payload = command[1:].split(':')
                    address, length = [int(v, 16) for v in head.split(',')]
                    self.memory[address:address + length] = bytes.fromhex(payload)
                elif command == 'D':
                    # A detach resumes the fake guest, just as real debugger
                    # detach may do. Service must restore explicit pause state.
                    self.running = True
                encoded = result.encode()
                writer.write(b'+$' + encoded + b'#' + f'{sum(encoded)&255:02x}'.encode())
                await writer.drain()
                if command == 'D':
                    break
        except asyncio.IncompleteReadError:
            pass
        finally:
            writer.close()


class IntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.peer = FakeEmulator()
        qmp, gdb = await self.peer.start()
        self.emulator = Emulator(Config(qmp_port=qmp, gdb_port=gdb, allow_write=True))
        # This peer is deliberately not a native process. Ownership is tested
        # separately; all operation traffic uses real QMP/GDB socket framing.
        self.emulator._require_owned = lambda: None
        self.emulator.qmp = QMP('127.0.0.1', qmp, timeout=1)
        await self.emulator.qmp.connect()

    async def asyncTearDown(self):
        await self.emulator.qmp.close()
        await self.peer.close()

    async def test_multichunk_virtual_and_physical_reads(self):
        virtual = await self.emulator.read('0x100', 800, 'u8')
        expected = list(bytes(range(256)) * 4)[:800]
        self.assertEqual(virtual['values'], expected)
        physical = await self.emulator.read_physical('0x100', 16, 'u8')
        self.assertEqual(physical['values'], list(range(16)))
        self.assertTrue(self.peer.running)
        regs = await self.emulator.registers()
        self.assertEqual(regs['registers']['EIP'], '0x00001000')

    async def test_snapshot_restores_changed_memory_without_resuming_paused_guest(self):
        before = await self.emulator.read('0x100', 16)
        await self.emulator.snapshot('save', 'invented-slot')
        self.peer.running = False
        await self.emulator.write('0x100', 'ff' * 16)
        changed = await self.emulator.read('0x100', 16)
        self.assertNotEqual(changed['hex'], before['hex'])
        await self.emulator.snapshot('load', 'invented-slot')
        restored = await self.emulator.read('0x100', 16)
        self.assertEqual(restored['hex'], before['hex'])
        self.assertFalse(self.peer.running)
        await self.emulator.snapshot('delete', 'invented-slot')
        with self.assertRaisesRegex(RuntimeError, 'snapshot not found'):
            await self.emulator.snapshot('load', 'invented-slot')


if __name__ == '__main__':
    unittest.main()
