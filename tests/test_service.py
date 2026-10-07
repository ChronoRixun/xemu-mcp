import asyncio
import json
import tempfile
import unittest
from PIL import Image
from pathlib import Path
from unittest.mock import AsyncMock, patch
import anyio

from xemu_mcp.service import Config, Emulator, decode, validate_range, parse_physical
from xemu_mcp.windows import controller_keys


class ValidationTests(unittest.TestCase):
    def test_decodes_little_endian_and_cstring(self):
        self.assertEqual(decode(b'\x01\x00\xff\x00', 'u16'), [1, 255])
        self.assertEqual(decode(b'\x00\x00\x80\x3f', 'f32'), [1.0])
        self.assertEqual(decode(b'hello\0tail', 'cstring'), 'hello')
        with self.assertRaises(ValueError):
            decode(b'\0', 'u32')
        with self.assertRaises(ValueError):
            decode(b'\0', 'unknown')

    def test_bounds_and_nonfinite_input(self):
        self.assertEqual(validate_range('0x1234', 16), 0x1234)
        for address, length in [('nope', 1), ('-1', 1), ('0xffffffff', 2), ('0', 0), ('0', 65537)]:
            with self.assertRaises(ValueError):
                validate_range(address, length)

    def test_physical_dump_parses_only_values_and_rejects_missing_bytes(self):
        text = '0000000000000100: 0x41 0x42\r\n0000000000000102: 0x43 0x44\r\n'
        self.assertEqual(parse_physical(text, 4), b'ABCD')
        with self.assertRaises(RuntimeError):
            parse_physical('Cannot access memory', 4)

    def test_controller_names_map_to_sdl_defaults_and_overrides(self):
        self.assertEqual(controller_keys(['a', 'start', 'lstick_up', 'rtrigger']), [4, 40, 8, 18])
        self.assertEqual(controller_keys(['a'], {'a': 5}), [5])
        with self.assertRaises(ValueError):
            controller_keys(['a;quit'])

    def test_config_rejects_remote_ports_and_write_enabled_is_explicit(self):
        self.assertFalse(Config().allow_write)
        with self.assertRaises(ValueError):
            Config(qmp_port=0)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'server.json'
            path.write_text('{"allow_write": "false"}')
            with self.assertRaises(ValueError):
                Config.load(path)


class ServiceTests(unittest.IsolatedAsyncioTestCase):
    def setup_emulator(self, running=True, allow_write=False):
        emulator = Emulator(Config(allow_write=allow_write))
        emulator.qmp = AsyncMock()
        emulator.qmp.execute.return_value = {'running': running, 'status': 'running' if running else 'paused'}
        emulator._require_owned = lambda: None
        return emulator

    async def test_read_preserves_running_and_paused_states(self):
        for running in (True, False):
            emulator = self.setup_emulator(running)
            gdb = AsyncMock()
            gdb.read.return_value = b'\x01\x00\x02\x00'
            with patch('xemu_mcp.service.GDB', return_value=gdb):
                result = await emulator.read('0x100', 4, 'u16')
            self.assertEqual(result['values'], [1, 2])
            if running:
                gdb.detach.assert_awaited_once()
            else:
                gdb.detach.assert_not_awaited()
                gdb.close.assert_awaited_once()
            commands = [c.args[0] for c in emulator.qmp.execute.await_args_list]
            self.assertEqual(commands[-1], 'cont' if running else 'stop')

    async def test_anyio_request_cancellation_still_cleans_up_and_restores(self):
        emulator = self.setup_emulator()
        gdb = AsyncMock()
        detached = False

        async def blocked_read(*args):
            await anyio.sleep(30)

        async def detach():
            nonlocal detached
            await anyio.sleep(.005)
            detached = True

        gdb.read.side_effect = blocked_read
        gdb.detach.side_effect = detach
        with patch('xemu_mcp.service.GDB', return_value=gdb):
            with anyio.move_on_after(.02) as scope:
                await emulator.read('0x100', 4)
        self.assertTrue(scope.cancel_called)
        self.assertTrue(detached)
        self.assertEqual(emulator.qmp.execute.await_args_list[-1].args[0], 'cont')

    async def test_memory_error_detaches_and_restores_state(self):
        emulator = self.setup_emulator()
        gdb = AsyncMock()
        gdb.read.side_effect = RuntimeError('unmapped')
        with patch('xemu_mcp.service.GDB', return_value=gdb):
            with self.assertRaisesRegex(RuntimeError, 'unmapped'):
                await emulator.read('0x100', 4)
        gdb.detach.assert_awaited_once()
        self.assertEqual(emulator.qmp.execute.await_args_list[-1].args[0], 'cont')

    async def test_write_disabled_never_contacts_emulator(self):
        emulator = self.setup_emulator()
        with self.assertRaisesRegex(PermissionError, 'disabled'):
            await emulator.write('0x100', '0102')
        emulator.qmp.execute.assert_not_awaited()

    async def test_enabled_write_reads_back_and_restores_state(self):
        emulator = self.setup_emulator(allow_write=True)
        gdb = AsyncMock()
        gdb.read.return_value = b'\x01\x02'
        with patch('xemu_mcp.service.GDB', return_value=gdb):
            self.assertTrue((await emulator.write('0x100', '0102'))['verified'])
        gdb.write.assert_awaited_once_with(0x100, b'\x01\x02')

    async def test_snapshot_rejects_injection_and_hmp_reports_failure(self):
        emulator = self.setup_emulator()
        with self.assertRaises(ValueError):
            await emulator.snapshot('save', 'slot\nquit')
        emulator.qmp.hmp.return_value = 'Error: no block device supports snapshots'
        with self.assertRaisesRegex(RuntimeError, 'Error'):
            await emulator.snapshot('save', 'test-slot')

    async def test_paused_snapshot_save_is_rejected_before_hmp(self):
        emulator = self.setup_emulator(running=False)
        with self.assertRaisesRegex(RuntimeError, 'running guest'):
            await emulator.snapshot('save', 'invented-slot')
        emulator.qmp.hmp.assert_not_awaited()

    async def test_press_releases_keys_on_cancellation(self):
        emulator = self.setup_emulator()
        emulator.keymap = {}
        with patch('xemu_mcp.service.window_for_pid', return_value=42), patch('xemu_mcp.service.post_key') as post:
            emulator.process = type('Process', (), {'pid': 123})()
            task = asyncio.create_task(emulator.press(['a'], 1000))
            await asyncio.sleep(.01)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual([c.args for c in post.call_args_list], [(42, 4, True, 123), (42, 4, False, 123)])

    async def test_unowned_actions_fail(self):
        emulator = Emulator(Config())
        for call in (emulator.status(), emulator.stop(), emulator.read('0x100', 4), emulator.press(['a'])):
            with self.assertRaisesRegex(RuntimeError, 'owned'):
                await call

    async def test_screenshot_uses_fork_and_propagates_real_errors(self):
        emulator = self.setup_emulator()
        emulator.commands = {'xemu-screenshot'}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'frame.png'
            def create(*args, **kwargs):
                target = Path(args[1]['filename'])
                Image.new('RGB', (8, 6), (12, 34, 56)).save(target)
                return {}
            emulator.qmp.execute.side_effect = create
            result = await emulator.screenshot(str(path))
            self.assertEqual(result['method'], 'qmp')
            self.assertEqual((result['width'], result['height']), (8, 6))
            emulator.qmp.execute.side_effect = RuntimeError('render failed')
            with self.assertRaisesRegex(RuntimeError, 'render failed'):
                await emulator.screenshot(str(path))

    async def test_screenshot_cannot_reuse_an_old_file_when_peer_does_not_write(self):
        emulator = self.setup_emulator()
        emulator.commands = {'xemu-screenshot'}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'frame.png'
            Image.new('RGB', (8, 6)).save(path)
            original = path.read_bytes()
            emulator.qmp.execute.return_value = {}
            with self.assertRaisesRegex(RuntimeError, 'writing'):
                await emulator.screenshot(str(path))
            self.assertEqual(path.read_bytes(), original)


if __name__ == '__main__':
    unittest.main()
