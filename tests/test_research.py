"""Research tools over real QMP/GDB sockets against a synthetic emulator peer.

All memory contents, addresses and snapshot text below are invented."""

import asyncio
import json
import struct
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import anyio

from xemu_mcp.protocols import GDB, QMP, ProtocolError, parse_stop_reply
from xemu_mcp.research import (
    CHUNK,
    PAGE,
    Capture,
    Matcher,
    SnapshotCatalog,
    parse_fields,
    parse_pattern,
    plan_sequence,
)
from xemu_mcp.service import Config, Emulator
from xemu_mcp.windows import controller_keys

REGISTERS = ('EAX', 'ECX', 'EDX', 'EBX', 'ESP', 'EBP', 'ESI', 'EDI',
             'EIP', 'EFL', 'CS', 'SS', 'DS', 'ES', 'FS', 'GS')


class ResearchPeer:
    """QMP + GDB RSP peer with unmapped pages, Z/z points, continue and ^C."""

    def __init__(self, size=3 * CHUNK):
        self.memory = bytearray(size)
        self.unmapped = set()
        self.running = True
        self.snapshots = {}
        self.inserted = set()
        self.log = []
        self.registers = dict.fromkeys(REGISTERS, 0) | {'ESP': 0x2000, 'EIP': 0x1000, 'EFL': 0x202}
        self.on_continue = None
        self.unsupported = set()
        self.ignore_interrupt = False
        self.ignore_qmp_stop = False
        self.continuing = None
        self.servers = []

    async def start(self):
        for handler in (self.qmp_peer, self.gdb_peer):
            self.servers.append(await asyncio.start_server(handler, '127.0.0.1', 0))
        return [server.sockets[0].getsockname()[1] for server in self.servers]

    async def close(self):
        for server in self.servers:
            server.close()
            await server.wait_closed()

    def readable(self, address, length):
        return (address + length <= len(self.memory)
                and not any(page in self.unmapped
                            for page in range(address // PAGE, (address + length - 1) // PAGE + 1)))

    async def send(self, writer, text, ack=True):
        encoded = text.encode()
        writer.write((b'+' if ack else b'') + b'$' + encoded + b'#' + f'{sum(encoded) & 255:02x}'.encode())
        await writer.drain()

    async def stop_reply(self, text):
        writer, self.continuing = self.continuing, None
        self.running = False
        await self.send(writer, text, ack=False)

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
                elif command == 'stop':
                    # Like QEMU, a VM stop while the debugger continues emits a stop reply.
                    if self.continuing and not self.ignore_qmp_stop:
                        await self.stop_reply('T02thread:01;')
                    self.running = False
                elif command == 'cont':
                    self.running = True
                elif command == 'human-monitor-command':
                    text = request['arguments']['command-line']
                    result = ''
                    if text.startswith('savevm '):
                        self.snapshots[text.split()[1]] = bytes(self.memory)
                    elif text.startswith('loadvm '):
                        self.memory[:] = self.snapshots[text.split()[1]]
                    elif text.startswith('delvm '):
                        self.snapshots.pop(text.split()[1], None)
                    elif text == 'info snapshots':
                        result = 'List of snapshots present on all disks:\r\nID        TAG               VM SIZE\r\n'
                        result += ''.join(f'--        {tag}        1 MiB\r\n' for tag in self.snapshots)
                writer.write(json.dumps({'return': result, 'id': request['id']}).encode() + b'\n')
                await writer.drain()
        finally:
            writer.close()

    async def gdb_peer(self, reader, writer):
        try:
            while True:
                char = await reader.readexactly(1)
                if char == b'\x03':
                    self.log.append('^C')
                    if self.continuing and not self.ignore_interrupt:
                        await self.stop_reply('T02thread:01;')
                    continue
                if char in (b'+', b'-'):
                    continue
                body = (await reader.readuntil(b'#'))[:-1]
                await reader.readexactly(2)
                command = body.decode()
                self.log.append(command)
                result = 'OK'
                if command[0] in 'mM':
                    head = command[1:].split(':')[0]
                    address, length = [int(v, 16) for v in head.split(',')]
                    if not self.readable(address, length):
                        result = 'E14'
                    elif command[0] == 'm':
                        result = bytes(self.memory[address:address + length]).hex()
                    else:
                        self.memory[address:address + length] = bytes.fromhex(command.split(':')[1])
                elif command[0] in 'Zz':
                    kind, address, length = [int(v, 16) for v in command[1:].split(',')]
                    if kind in self.unsupported:
                        result = ''
                    elif command[0] == 'Z':
                        self.inserted.add((kind, address, length))
                    else:
                        self.inserted.discard((kind, address, length))
                elif command == '?':
                    # QEMU removes all GDB points when a debugger (re)connects with '?'.
                    self.inserted.clear()
                    result = 'S05'
                elif command == 'g':
                    result = b''.join(self.registers[name].to_bytes(4, 'little') for name in REGISTERS).hex()
                    result += '00' * 40  # x87 state follows the general registers
                elif command == 'c':
                    writer.write(b'+')
                    await writer.drain()
                    self.running = True
                    self.continuing = writer
                    if self.on_continue:
                        asyncio.get_running_loop().call_later(.02, lambda: asyncio.ensure_future(self.fire()))
                    continue
                elif command == 'D':
                    self.inserted.clear()
                    self.running = True
                await self.send(writer, result)
                if command == 'D':
                    break
        except asyncio.IncompleteReadError:
            pass
        finally:
            writer.close()

    async def fire(self):
        kind, address = self.on_continue
        if not self.continuing:
            return
        if kind == 'exec' and (0, address, 1) in self.inserted:
            self.registers['EIP'] = address
            await self.stop_reply('T05thread:01;')
        elif kind == 'write' and any(k == 2 and a <= address < a + n for k, a, n in self.inserted):
            self.memory[address] ^= 0xff
            self.registers['EIP'] = 0x1006
            await self.stop_reply(f'T05thread:01;watch:{address:x};')


class ResearchIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.peer = ResearchPeer()
        qmp, gdb = await self.peer.start()
        self.tmp = tempfile.TemporaryDirectory()
        self.catalog = Path(self.tmp.name) / 'snapshots.json'
        self.emulator = Emulator(Config(qmp_port=qmp, gdb_port=gdb, snapshot_catalog=str(self.catalog)))
        self.emulator._require_owned = lambda: None
        self.emulator._assert_ports_owned = lambda: None
        self.emulator.gdb_timeout = .3
        self.emulator.qmp = QMP('127.0.0.1', qmp, timeout=1)
        await self.emulator.qmp.connect()

    async def asyncTearDown(self):
        await self.emulator.qmp.close()
        await self.peer.close()
        self.tmp.cleanup()

    def put(self, address, fmt, *values):
        struct.pack_into(fmt, self.peer.memory, address, *values)

    async def test_search_typed_values_patterns_chunk_boundaries_and_holes(self):
        self.put(0x100, '<I', 0xdeadbeef)
        self.put(0x2003, '<I', 0xdeadbeef)  # unaligned
        self.put(CHUNK - 2, '<I', 0xdeadbeef)  # straddles the 64 KiB chunk boundary
        self.put(0x8000, '<f', 99.75)
        self.put(0x8010, '<f', 100.25)
        self.peer.memory[CHUNK + 0x500:CHUNK + 0x504] = bytes.fromhex('8b4510c3')
        self.peer.unmapped.add(0x21)
        found = await self.emulator.search('0', len(self.peer.memory), 'u32', '0xdeadbeef', alignment=1)
        self.assertEqual([r['address'] for r in found['results']], ['0x100', '0x2003', hex(CHUNK - 2)])
        self.assertEqual(found['unreadable'], [{'start': '0x21000', 'end': '0x22000'}])
        aligned = await self.emulator.search('0', len(self.peer.memory), 'u32', 3735928559)
        self.assertEqual([r['address'] for r in aligned['results']], ['0x100'])
        floats = await self.emulator.search('0x7000', 0x2000, 'f32', 100, tolerance=.3)
        self.assertEqual([(r['address'], r['value']) for r in floats['results']],
                         [('0x8000', 99.75), ('0x8010', 100.25)])
        pattern = await self.emulator.search('0', len(self.peer.memory), 'pattern', '8b 45 ?? c3')
        self.assertEqual(pattern['results'], [{'address': hex(CHUNK + 0x500), 'value': '8b4510c3'}])
        capped = await self.emulator.search('0', 0x1000, 'u8', 0, max_results=3)
        self.assertEqual((capped['count'], capped['truncated']), (3, True))
        self.assertTrue(self.peer.running)

    async def test_search_validation_never_contacts_emulator(self):
        self.peer.log.clear()
        for arguments in (('0', 0, 'u32', 1), ('0', 65 * 1024 * 1024, 'u32', 1), ('0', 16, 'u64', 1),
                          ('0', 16, 'u8', 256), ('0', 16, 'f32', 'nan'), ('0', 16, 'pattern', '?? ??'),
                          ('0', 16, 'pattern', 'zz'), ('0xffffffff', 2, 'u8', 1)):
            with self.assertRaises((ValueError, TypeError)):
                await self.emulator.search(*arguments)
        with self.assertRaises(ValueError):
            await self.emulator.search('0', 16, 'u32', 1, tolerance=1)
        self.assertEqual(self.peer.log, [])

    async def test_capture_diff_and_narrow_like_a_cheat_finder(self):
        self.put(0x400, '<I', 100)  # invented health counter
        self.put(0x500, '<I', 7)
        capture = await self.emulator.capture('0x0', 0x3000, 'u32')
        handle = capture['handle']
        self.assertEqual(capture['candidates'], 0x3000 // 4)
        self.put(0x400, '<I', 90)
        self.put(0x500, '<I', 8)
        diff = await self.emulator.diff(handle)
        self.assertEqual(diff['changed'], 2)
        self.assertEqual([(r['address'], r['old'], r['new']) for r in diff['results']],
                         [('0x400', 100, 90), ('0x500', 7, 8)])
        narrowed = await self.emulator.narrow(handle, 'decreased')
        self.assertEqual((narrowed['candidates'], narrowed['results'][0]['address']), (1, '0x400'))
        self.put(0x400, '<I', 75)
        self.assertEqual((await self.emulator.narrow(handle, 'equals', 75))['candidates'], 1)
        self.assertEqual((await self.emulator.narrow(handle, 'unchanged'))['candidates'], 1)
        self.assertEqual((await self.emulator.narrow(handle, 'increased'))['candidates'], 0)
        listed = self.emulator.captures_list()['captures']
        self.assertEqual([(c['handle'], c['narrowed']) for c in listed], [(handle, 4)])
        self.emulator.capture_release(handle)
        with self.assertRaisesRegex(ValueError, 'Unknown capture'):
            await self.emulator.diff(handle)

    async def test_capture_excludes_unreadable_pages_and_limits_handles(self):
        self.peer.unmapped.add(1)
        capture = await self.emulator.capture('0', 3 * PAGE, 'u8')
        self.assertEqual(capture['candidates'], 2 * PAGE)
        self.assertEqual(capture['unreadable'], [{'start': '0x1000', 'end': '0x2000'}])
        self.peer.unmapped = {0, 1, 2}
        with self.assertRaisesRegex(RuntimeError, 'No readable memory'):
            await self.emulator.capture('0', 3 * PAGE)
        self.peer.unmapped = set()
        for _ in range(7):
            await self.emulator.capture('0', 16)
        with self.assertRaisesRegex(RuntimeError, 'At most 8'):
            await self.emulator.capture('0', 16)
        with self.assertRaises(ValueError):
            await self.emulator.narrow('mem-1', 'equals')

    async def test_struct_read_follows_pointers_with_depth_and_cycle_limits(self):
        # Invented actor list: 0x100 -> 0x200 -> 0x100 (cycle); owner -> unmapped page.
        self.put(0x100, '<IfffI', 0x200, 1.5, 2.5, -3.0, 0x29000)
        self.put(0x114, '<H', 77)
        self.peer.memory[0x118:0x120] = b'hero\0xyz'
        self.put(0x200, '<IfffI', 0x100, 4.0, 5.0, 6.0, 0)
        self.peer.unmapped.add(0x29)
        actor = ['next:ptr->Actor@0', 'pos:f32[3]@0x4', 'owner:ptr->Actor@0x10']
        result = await self.emulator.read_struct(
            '0x100', ['next:ptr->Actor@0', 'pos:f32[3]@4', 'raw:ptr@0x10', 'hp:u16@0x14',
                      'name:cstring[8]@0x18', 'tail:bytes[2]@0x1c'], {'Actor': actor}, max_depth=3)
        fields = result['fields']
        self.assertEqual(fields['pos'], [1.5, 2.5, -3.0])
        self.assertEqual((fields['raw'], fields['hp'], fields['name'], fields['tail']),
                         ('0x29000', 77, 'hero', '0078'))
        child = fields['next']
        self.assertEqual((child['address'], child['struct'], child['fields']['pos']), ('0x200', 'Actor', [4, 5, 6]))
        self.assertIsNone(child['fields']['owner'])
        self.assertEqual(child['fields']['next']['fields']['next'], {'address': '0x200', 'cycle': True})
        self.assertIn('error', child['fields']['next']['fields']['owner'])
        shallow = await self.emulator.read_struct('0x100', ['next:ptr->Actor@0'], {'Actor': actor}, 0)
        self.assertEqual(shallow['fields']['next'], {'address': '0x200', 'truncated': 'max_depth'})
        with self.assertRaisesRegex(RuntimeError, 'failed'):
            await self.emulator.read_struct('0x29000', ['hp:u16@0'])
        self.assertTrue(self.peer.running)

    async def test_exec_breakpoint_hit_reports_state_and_is_always_removed(self):
        self.put(0x2000, '<II', 0x1111, 0x2222)
        self.peer.memory[0x1234:0x1238] = b'\x90\x90\xc3\x00'
        self.emulator.break_set('0x1234')
        self.peer.on_continue = ('exec', 0x1234)
        result = await self.emulator.break_wait(timeout_s=2, stack_bytes=8, window_bytes=4)
        self.assertTrue(result['hit'])
        self.assertFalse(result['interrupted'])
        self.assertEqual(result['matched'], ['bp-1'])
        self.assertEqual(result['registers']['EIP'], '0x00001234')
        self.assertEqual(result['stack']['u32'], [0x1111, 0x2222])
        self.assertEqual(result['window'], {'address': '0x1234', 'hex': '9090c300'})
        self.assertEqual(self.peer.inserted, set())
        self.assertFalse(self.peer.running)  # leave='paused' is the default
        self.assertNotIn('D', self.peer.log)
        self.assertEqual(self.peer.log[:3], ['?', 'Z0,1234,1', 'c'])
        self.assertIn('z0,1234,1', self.peer.log)
        self.assertEqual(self.emulator.break_list()['points'][0]['hits'], 1)

    async def test_write_watchpoint_can_leave_guest_running(self):
        self.emulator.break_set('0x3004', 'write', 4)
        self.peer.on_continue = ('write', 0x3006)
        result = await self.emulator.break_wait(timeout_s=2, leave='running')
        self.assertTrue(result['hit'])
        self.assertEqual(result['stop']['watch'], {'type': 'watch', 'address': '0x3006'})
        self.assertEqual(result['window']['address'], '0x3000')
        self.assertIn('z2,3004,4', self.peer.log)
        self.assertEqual(self.peer.log[-1], 'D')
        self.assertTrue(self.peer.running)
        self.assertEqual(self.peer.inserted, set())

    async def test_timeout_interrupts_and_restores_requested_state(self):
        self.emulator.break_set('0x1234')
        result = await self.emulator.break_wait(timeout_s=.05)
        self.assertEqual((result['hit'], result['interrupted'], result['matched']), (False, True, []))
        self.assertIn('^C', self.peer.log)
        self.assertEqual(self.peer.inserted, set())
        self.assertFalse(self.peer.running)
        # When ^C is ignored, QMP stop produces the stop reply instead.
        self.peer.ignore_interrupt = True
        result = await self.emulator.break_wait(timeout_s=.05, leave='running')
        self.assertTrue(result['interrupted'])
        self.assertTrue(self.peer.running)
        self.assertEqual(self.peer.inserted, set())

    async def test_wedged_stub_is_disarmed_over_fresh_connection_and_guest_paused(self):
        self.emulator.break_set('0x1234')
        self.peer.ignore_interrupt = self.peer.ignore_qmp_stop = True
        with self.assertRaisesRegex(TimeoutError, 'no stop'):
            await self.emulator.break_wait(timeout_s=.05)
        self.assertEqual(self.peer.inserted, set())
        self.assertEqual(self.peer.log[-2:], ['?', 'z0,1234,1'])  # over a fresh connection
        self.assertFalse(self.peer.running)

    async def test_unsupported_point_type_fails_without_leaving_points(self):
        self.emulator.break_set('0x1234')
        self.emulator.break_set('0x3000', 'access', 4)
        self.peer.unsupported = {4}
        with self.assertRaisesRegex(ProtocolError, 'does not support Z4'):
            await self.emulator.break_wait(timeout_s=1)
        self.assertEqual(self.peer.inserted, set())
        self.assertNotIn('c', self.peer.log)
        self.assertFalse(self.peer.running)  # the requested final state applies to failures too

    async def test_break_wait_cancellation_mid_arm_still_disarms(self):
        # Hold back the Z0 acknowledgment so cancellation deterministically
        # lands after the stub processed the insert but before break_wait can
        # record it; the finally must still remove the point.
        self.emulator.break_set('0x1234')
        acked = asyncio.get_running_loop().create_future()
        release = asyncio.get_running_loop().create_future()
        real_send = self.peer.send
        held = False

        async def hold_first_ack(writer, text, ack=True):
            nonlocal held
            if not held and text == 'OK':
                held = True
                acked.set_result(None)
                await release
            return await real_send(writer, text, ack=ack)

        self.peer.send = hold_first_ack
        task = asyncio.create_task(self.emulator.break_wait(timeout_s=10))
        try:
            await asyncio.wait_for(acked, 5)
            task.cancel()
            release.set_result(None)
            with anyio.move_on_after(5):
                with self.assertRaises(asyncio.CancelledError):
                    await task
            self.assertEqual(self.peer.inserted, set())
            self.assertFalse(self.peer.running)
        finally:
            if not release.done():
                release.set_result(None)
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def test_break_wait_cancellation_still_disarms_and_restores(self):
        self.emulator.break_set('0x1234')
        self.peer.ignore_interrupt = self.peer.ignore_qmp_stop = True
        task = asyncio.create_task(self.emulator.break_wait(timeout_s=10))
        try:
            # Wait until the synthetic peer has processed the continue request
            # (generously), so cancellation always lands inside the stop wait
            # and never mid-arming; no fixed grace period is needed after it.
            for _ in range(500):
                if 'c' in self.peer.log:
                    break
                await asyncio.sleep(.01)
            else:
                self.fail('the synthetic peer never processed the continue request')
            task.cancel()
            with anyio.move_on_after(5):
                with self.assertRaises(asyncio.CancelledError):
                    await task
            self.assertTrue(task.cancelled())
            self.assertEqual(self.peer.inserted, set())
            self.assertFalse(self.peer.running)
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def test_snapshot_catalogue_records_lists_and_checks_emulator(self):
        await self.emulator.snapshot('save', 'invented-bridge', description='Before invented boss',
                                     zone='zone-2', notes='health at 0x400')
        await self.emulator.snapshot('save', 'invented-menu', zone='menu')
        stored = json.loads(self.catalog.read_text())['snapshots']
        self.assertEqual(stored['invented-bridge']['description'], 'Before invented boss')
        self.assertIn('created', stored['invented-bridge'])
        found = await self.emulator.state_list('boss')
        self.assertEqual([entry['tag'] for entry in found['snapshots']], ['invented-bridge'])
        self.assertEqual((await self.emulator.state_list(zone='MENU'))['count'], 1)
        loaded = await self.emulator.snapshot('load', 'invented-menu')
        self.assertIn('last_loaded', loaded['catalog'])
        self.emulator.state_annotate('invented-menu', notes='edited later')
        self.emulator.state_annotate('made-elsewhere', description='catalogued afterwards')
        await self.emulator.qmp.hmp('savevm uncatalogued-one')
        checked = await self.emulator.state_list(check_emulator=True)
        present = {entry['tag']: entry['present'] for entry in checked['snapshots']}
        self.assertEqual(present, {'invented-bridge': True, 'invented-menu': True, 'made-elsewhere': False})
        self.assertEqual(checked['uncatalogued'], ['uncatalogued-one'])
        await self.emulator.snapshot('delete', 'invented-bridge')
        self.assertNotIn('invented-bridge', json.loads(self.catalog.read_text())['snapshots'])
        with self.assertRaises(ValueError):
            await self.emulator.snapshot('save', 'x', description='d' * 501)

    async def test_input_sequence_dry_run_and_delivery_release_every_step(self):
        steps = [{'button': 'a', 'hold_ms': 1, 'wait_ms': 1},
                 {'buttons': ['lstick_left', 'x'], 'hold_ms': 2}]
        dry = await Emulator(Config()).input_sequence(steps, dry_run=True)
        self.assertEqual([step['sdl_scancodes'] for step in dry['steps']], [[4], [22, 27]])
        self.assertEqual((dry['total_ms'], dry['executed']), (4, 0))
        self.emulator.process = type('Process', (), {'pid': 123})()
        with patch('xemu_mcp.service.window_for_pid', return_value=42), \
                patch('xemu_mcp.service.post_key') as post:
            result = await self.emulator.input_sequence(steps)
            self.assertEqual(result['executed'], 2)
            self.assertEqual([c.args[1:3] for c in post.call_args_list],
                             [(4, True), (4, False), (22, True), (27, True), (27, False), (22, False)])
            post.reset_mock()
            with self.assertRaisesRegex(ValueError, 'Step 1'):
                await self.emulator.input_sequence([{'button': 'a'}, {'button': 'jump'}])
            post.assert_not_called()
            task = asyncio.create_task(self.emulator.input_sequence([{'button': 'b', 'hold_ms': 5000}]))
            await asyncio.sleep(.02)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertEqual([c.args[1:3] for c in post.call_args_list], [(5, True), (5, False)])


class ResearchHelperTests(unittest.TestCase):
    def test_pattern_wildcards_and_matcher_alignment(self):
        regex, size = parse_pattern('8B45??89')
        self.assertEqual(size, 4)
        self.assertTrue(regex.search(b'\x00\x8b\x45\xff\x89'))
        data = struct.pack('<IIf', 5, 5, 2.0) + b'\x05\x00\x00\x00'
        self.assertEqual([a for a, _ in Matcher('u32', 5).find(0x10, data)], [0x10, 0x14, 0x1c])
        self.assertEqual([a for a, _ in Matcher('u32', 5).find(0x11, data)], [])
        self.assertEqual([a for a, _ in Matcher('u32', 5, alignment=1).find(0x11, data)], [0x11, 0x15, 0x1d])
        self.assertEqual(list(Matcher('f32', 2.1, tolerance=.2).find(0, data)), [(8, 2.0)])
        self.assertEqual(list(Matcher('i16', -2).find(0, b'\xfe\xff')), [(0, -2)])

    def test_capture_narrowing_respects_holes_and_float_nan(self):
        old = bytearray(3 * PAGE)
        struct.pack_into('<f', old, 8, 1.0)
        capture = Capture('mem-x', 0, len(old), 'f32', 4, bytes(old), set())
        new = bytearray(old)
        struct.pack_into('<f', new, 8, float('nan'))
        struct.pack_into('<f', new, 12, 3.0)
        new[PAGE + 4] = 1
        self.assertEqual(list(capture.changed(new, {1})), [8, 12])
        self.assertEqual(list(capture.narrow(new, {1}, 'increased')), [12])
        self.assertEqual(capture.holes, {1})
        self.assertEqual(capture.candidate_count(), 1)

    def test_field_specs_reject_bad_input(self):
        fields = parse_fields(['hp:i32@0x10', 'name:cstring[4]@20', 'next:ptr->Node@0'], {'Node'})
        self.assertEqual([(f.name, f.kind, f.offset, f.size) for f in fields],
                         [('hp', 'i32', 16, 4), ('name', 'cstring', 20, 4), ('next', 'ptr', 0, 4)])
        for spec in (['hp:u64@0'], ['hp:u32'], ['hp:u32@0', 'hp:u8@4'], ['n:ptr->Missing@0'],
                     ['s:cstring@0'], ['big:u8[4096]@0'], ['x:u32@4094'], [], 'hp:u32@0'):
            with self.assertRaises(ValueError):
                parse_fields(spec, set())

    def test_sequence_plan_limits(self):
        resolve = controller_keys
        with self.assertRaises(ValueError):
            plan_sequence([], resolve)
        with self.assertRaises(ValueError):
            plan_sequence([{'button': 'a', 'buttons': ['b']}], resolve)
        with self.assertRaises(ValueError):
            plan_sequence([{'button': 'a', 'turbo': True}], resolve)
        with self.assertRaises(ValueError):
            plan_sequence([{'button': 'a', 'wait_ms': 60000}] * 6, resolve)
        with self.assertRaises(ValueError):
            plan_sequence([{'button': 'a', 'hold_ms': 0}], resolve)

    def test_stop_reply_parsing(self):
        self.assertEqual(parse_stop_reply('T05thread:p01.01;awatch:1000;'),
                         {'kind': 'T', 'signal': 5, 'thread': 'p01.01',
                          'watch': {'type': 'awatch', 'address': 0x1000}})
        self.assertEqual(parse_stop_reply('S02'), {'kind': 'S', 'signal': 2})
        with self.assertRaises(ProtocolError):
            parse_stop_reply('OK')

    def test_catalogue_must_be_absolute_json_outside_repository(self):
        repository = Path(__file__).resolve().parents[1]
        for path in ('relative.json', str(repository / 'snapshots.json'), str(Path(tempfile.gettempdir()) / 'x.txt')):
            with self.assertRaises(ValueError):
                SnapshotCatalog(path)
            with self.assertRaises(ValueError):
                Config(snapshot_catalog=path)


class GDBResumeTests(unittest.IsolatedAsyncioTestCase):
    async def test_interrupt_is_sent_after_timeout_and_reply_is_not_lost(self):
        seen = []

        async def peer(reader, writer):
            await reader.readuntil(b'#')
            await reader.readexactly(2)
            writer.write(b'+')
            await writer.drain()
            seen.append(await reader.readexactly(1))
            body = b'T02thread:01;'
            # Split the reply to prove a partially received packet is completed.
            writer.write(b'$T0')
            await writer.drain()
            await asyncio.sleep(.02)
            writer.write(body[2:] + b'#' + f'{sum(body) & 255:02x}'.encode())
            await writer.drain()
            await reader.read()
            writer.close()

        server = await asyncio.start_server(peer, '127.0.0.1', 0)
        gdb = GDB('127.0.0.1', server.sockets[0].getsockname()[1], timeout=1)
        await gdb.connect()
        reply, interrupted = await gdb.resume_until_stop(.05)
        self.assertEqual((reply, interrupted, seen), ('T02thread:01;', True, [b'\x03']))
        self.assertFalse(gdb.broken)
        await gdb.close()
        server.close()
        await server.wait_closed()


if __name__ == '__main__':
    unittest.main()
