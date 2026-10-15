import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch
import anyio

from xemu_mcp.service import Config, Emulator


class LifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancelled_stop_still_quits_and_waits_for_owned_process(self):
        emulator = Emulator(Config())
        live = True

        def wait(timeout):
            nonlocal live
            live = False
            return 0

        emulator.process = Mock(pid=123)
        emulator.process.poll.side_effect = lambda: None if live else 0
        emulator.process.wait.side_effect = wait
        emulator._require_owned = lambda: None
        emulator.qmp = AsyncMock()

        async def delayed_quit(*args):
            await anyio.sleep(.04)
            return {}

        emulator.qmp.execute.side_effect = delayed_quit
        with anyio.move_on_after(.01) as scope:
            await emulator.stop()
        self.assertTrue(scope.cancel_called)
        self.assertFalse(live)
        emulator.process.wait.assert_called_once()

    async def test_reused_pid_is_rejected_before_any_qmp_command(self):
        emulator = Emulator(Config())
        emulator.process = Mock(pid=123)
        emulator.process.poll.return_value = None
        emulator.process_started = 10.0
        emulator.executable = Path('xemu.exe').resolve()
        emulator.qmp = AsyncMock()
        identity = Mock()
        identity.create_time.return_value = 11.0
        with patch('xemu_mcp.service.psutil.Process', return_value=identity):
            with self.assertRaisesRegex(RuntimeError, 'identity changed'):
                await emulator.status()
        emulator.qmp.execute.assert_not_awaited()

    async def test_keyboard_binding_is_required_without_spawning(self):
        emulator = Emulator(Config())
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / 'invented.toml'
            config.write_text('[general]\nshow_welcome = false\n')
            with patch.object(emulator, '_check_games'), patch('xemu_mcp.service.subprocess.Popen') as launch:
                with self.assertRaisesRegex(ValueError, 'Keyboard bound'):
                    await emulator.start(str(config))
            launch.assert_not_called()

    async def test_existing_emulator_blocks_start_without_attaching(self):
        emulator = Emulator(Config())
        foreign = Mock(pid=987, info={'name': 'xemu.exe', 'exe': 'invented/xemu.exe'})
        with patch('xemu_mcp.service.psutil.process_iter', return_value=[foreign]):
            with self.assertRaisesRegex(RuntimeError, 'Another xemu'):
                await emulator.start('unused.toml')

    def test_only_explicit_test_build_roots_may_coexist(self):
        with tempfile.TemporaryDirectory() as tmp:
            allowed = Path(tmp) / 'test-builds'
            forbidden = Path(tmp) / 'play-build'
            emulator = Emulator(Config(allowed_game_roots=[str(allowed)],
                                       protected_processes=['ExampleGame.exe']))
            for location, permitted in ((allowed, True), (forbidden, False),
                                        (Path(str(allowed) + '-lookalike'), False)):
                game = Mock(pid=987, info={'name': 'ExampleGame.exe',
                                          'exe': str(location / 'ExampleGame.exe')})
                with patch('xemu_mcp.service.psutil.process_iter', return_value=[game]):
                    if permitted:
                        emulator._check_games()
                    else:
                        with self.assertRaisesRegex(RuntimeError, 'Protected'):
                            emulator._check_games()

    def test_protected_processes_default_empty_and_env_var_is_honored(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(Config().protected_processes, [])
        env = {'XEMU_MCP_PROTECTED_PROCESSES': 'ExampleGame.exe; other.exe,, '}
        with patch.dict(os.environ, env, clear=True):
            self.assertEqual(Config().protected_processes, ['ExampleGame.exe', 'other.exe'])
        # An explicit config list wins over the environment variable.
        with patch.dict(os.environ, env, clear=True):
            self.assertEqual(Config(protected_processes=['Configured.exe']).protected_processes,
                             ['Configured.exe'])
        # Matching is case-insensitive against the running process name.
        emulator = Emulator(Config(protected_processes=['ExampleGame.exe']))
        game = Mock(pid=987, info={'name': 'EXAMPLEGAME.EXE', 'exe': 'invented/ExampleGame.exe'})
        with patch('xemu_mcp.service.psutil.process_iter', return_value=[game]):
            with self.assertRaisesRegex(RuntimeError, 'Protected'):
                emulator._check_games()


if __name__ == '__main__':
    unittest.main()
