"""Owned-emulator lifecycle and composable operations."""

import asyncio
import base64
import json
import math
import os
import re
import socket
import struct
import subprocess
import time
import tomllib
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, fields
from pathlib import Path

import psutil
import anyio

from .protocols import GDB, QMP, ProtocolError
from .windows import capture_region, controller_keys, post_key, validate_windows_keys, window_for_pid


@dataclass
class Config:
    executable: str = 'xemu'
    qmp_port: int = 4444
    gdb_port: int = 1234
    allow_write: bool = False
    runtime_dir: str = str(Path.home() / '.xemu-mcp')
    symbols_file: str | None = None
    startup_timeout: float = 60
    allowed_game_roots: list[str] = field(default_factory=list)

    def __post_init__(self):
        for port in (self.qmp_port, self.gdb_port):
            if type(port) is not int or not 1 <= port <= 65535:
                raise ValueError('Ports must be integers between 1 and 65535')
        if self.qmp_port == self.gdb_port:
            raise ValueError('QMP and GDB ports must differ')
        if type(self.allow_write) is not bool:
            raise ValueError('allow_write must be a JSON boolean')
        if not math.isfinite(self.startup_timeout) or not 1 <= self.startup_timeout <= 300:
            raise ValueError('startup_timeout must be 1 to 300 seconds')
        for value in (self.executable, self.runtime_dir):
            if not isinstance(value, str) or not value:
                raise ValueError('Executable and runtime directory must be nonempty strings')
        if (not isinstance(self.allowed_game_roots, list)
                or any(not isinstance(root, str) or not Path(root).is_absolute()
                       for root in self.allowed_game_roots)):
            raise ValueError('allowed_game_roots must be a list of absolute paths')

    @classmethod
    def load(cls, path=None):
        if path is None:
            path = os.environ.get('XEMU_MCP_CONFIG')
        if not path:
            return cls()
        data = json.loads(Path(path).read_text(encoding='utf-8-sig'))
        unknown = set(data) - {field.name for field in fields(cls)}
        if unknown:
            raise ValueError(f'Unknown configuration keys: {sorted(unknown)}')
        return cls(**data)


def validate_range(address, length):
    try:
        value = int(address, 0) if isinstance(address, str) else address
    except ValueError as exc:
        raise ValueError('Address must be decimal or 0x-prefixed hexadecimal') from exc
    if type(value) is not int or type(length) is not int:
        raise ValueError('Address and length must be integers')
    if not 1 <= length <= 65536 or not 0 <= value < 2**32 or value + length > 2**32:
        raise ValueError('Read/write range must fit 32-bit memory and be 1 to 65536 bytes')
    return value


def decode(data, kind):
    if kind == 'cstring':
        return data.split(b'\0', 1)[0].decode('utf-8', errors='replace')
    formats = {'u8': 'B', 'u16': 'H', 'u32': 'I', 'f32': 'f'}
    if kind not in formats:
        raise ValueError('decode_as must be u8, u16, u32, f32 or cstring')
    fmt = '<' + formats[kind]
    size = struct.calcsize(fmt)
    if len(data) % size:
        raise ValueError(f'{kind} requires a length divisible by {size}')
    # JSON has no NaN/Infinity literals.
    return [v[0] if kind != 'f32' or math.isfinite(v[0]) else None
            for v in struct.iter_unpack(fmt, data)]


def parse_physical(text, length):
    values = []
    for line in text.splitlines():
        if re.match(r'^\s*(?:0x)?[0-9a-fA-F]+:', line):
            values.extend(re.findall(r'\b0x([0-9a-fA-F]{2})\b', line.split(':', 1)[1]))
    if len(values) != length:
        raise RuntimeError(f'Physical read failed/short ({len(values)}/{length} bytes): {text[:200]}')
    return bytes.fromhex(''.join(values))


class Emulator:
    def __init__(self, config):
        self.config = config
        self.process = None
        self.process_started = None
        self.executable = None
        self.qmp = QMP('127.0.0.1', config.qmp_port)
        self.commands = set()
        self.keymap = {}
        self.lock = asyncio.Lock()

    def _require_owned(self):
        if self.process is None or self.process.poll() is not None:
            raise RuntimeError('No live xemu process owned by this server; use xemu_start')
        try:
            process = psutil.Process(self.process.pid)
            if (process.create_time() != self.process_started
                    or Path(process.exe()).resolve() != self.executable):
                raise RuntimeError('Owned xemu process identity changed')
        except psutil.NoSuchProcess as exc:
            raise RuntimeError('Owned xemu process exited') from exc
        return process

    def _assert_ports_owned(self):
        process = self._require_owned()
        listeners = {c.laddr.port for c in process.net_connections(kind='tcp')
                     if c.status == psutil.CONN_LISTEN and c.laddr.ip == '127.0.0.1'}
        if not {self.config.qmp_port, self.config.gdb_port} <= listeners:
            raise RuntimeError('QMP/GDB listeners do not belong to the owned xemu process')

    def _check_games(self):
        for process in psutil.process_iter(['name', 'exe']):
            name = (process.info['name'] or '').lower()
            if name in ('xemu', 'xemu.exe'):
                raise RuntimeError(f'Another xemu is already running (PID {process.pid}); refusing to attach or start')
            if name == 'xmen2.exe':
                exe = (process.info['exe'] or '').replace('\\', '/').lower()
                allowed = [root.replace('\\', '/').lower().rstrip('/') + '/'
                           for root in self.config.allowed_game_roots]
                if not exe or not any(exe.startswith(root) for root in allowed):
                    raise RuntimeError(f'Protected or unidentified game is running (PID {process.pid}); refusing to start')

    async def start(self, config_path, disc_path=None):
        async with self.lock:
            if self.process and self.process.poll() is None:
                raise RuntimeError('This server already owns a running xemu')
            self._check_games()
            path = Path(config_path).expanduser().resolve(strict=True)
            settings = tomllib.loads(path.read_text(encoding='utf-8-sig'))
            self.keymap = settings.get('input', {}).get('keyboard_controller_scancode_map', {})
            keyboard_port = settings.get('input', {}).get('bindings', {}).get('port1')
            if keyboard_port != 'keyboard':
                raise ValueError('Controller input requires Keyboard bound to port 1 in the xemu config')
            for port in (self.config.qmp_port, self.config.gdb_port):
                with socket.socket() as sock:
                    sock.bind(('127.0.0.1', port))
            import shutil
            executable = shutil.which(self.config.executable)
            if not executable:
                raise FileNotFoundError(f'xemu executable not found: {self.config.executable}')
            self.executable = Path(executable).resolve()
            if self.executable.stem.lower() != 'xemu':
                raise ValueError('Configured executable must be named xemu or xemu.exe')
            runtime = Path(self.config.runtime_dir).expanduser().resolve()
            runtime.mkdir(parents=True, exist_ok=True)
            args = [str(self.executable), '-config_path', str(path), '-qmp',
                    f'tcp:127.0.0.1:{self.config.qmp_port},server=on,wait=off',
                    '-gdb', f'tcp:127.0.0.1:{self.config.gdb_port}']
            if disc_path:
                disc = Path(disc_path).expanduser().resolve(strict=True)
                args.extend(['-dvd_path', str(disc)])
            log = runtime / 'xemu-process.log'
            environment = os.environ.copy()
            startupinfo = None
            if os.name == 'nt':
                # Targeted WM_KEY messages require SDL's message-loop backend,
                # rather than raw hardware keyboard input.
                environment['SDL_WINDOWS_RAW_KEYBOARD'] = '0'
                # MCP stdio hosts are normally launched without a window.
                # Explicitly show this GUI child rather than inherit hidden
                # startup presentation from its launcher.
                startupinfo = subprocess.STARTUPINFO()
                startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                startupinfo.wShowWindow = 1  # SW_SHOWNORMAL
            with log.open('ab') as stream:
                self.process = subprocess.Popen(args, cwd=runtime, stdin=subprocess.DEVNULL,
                                                stdout=stream, stderr=stream, startupinfo=startupinfo,
                                                env=environment)
            self.process_started = psutil.Process(self.process.pid).create_time()
            deadline = time.monotonic() + self.config.startup_timeout
            try:
                while True:
                    self._require_owned()
                    try:
                        self._assert_ports_owned()
                        await self.qmp.connect()
                        break
                    except (OSError, RuntimeError, TimeoutError):
                        await self.qmp.close()
                        if time.monotonic() >= deadline:
                            raise TimeoutError(f'QMP/GDB startup timed out; see {log}')
                        await asyncio.sleep(.2)
                self.commands = {entry['name'] for entry in await self.qmp.execute('query-commands')}
                # Negotiate a real GDB request, not just a TCP connection, while
                # preserving whether QMP says the guest was running.
                async with self._memory_session() as gdb:
                    await gdb.request('?')
                return await self._status()
            except BaseException:
                with anyio.CancelScope(shield=True):
                    await self._terminate()
                raise

    async def _status(self):
        self._require_owned()
        result = await self.qmp.execute('query-status')
        return {'pid': self.process.pid, 'owned': True, 'qmp': result,
                'qmp_port': self.config.qmp_port, 'gdb_port': self.config.gdb_port,
                'writes_enabled': self.config.allow_write,
                'screenshot_qmp': 'xemu-screenshot' in self.commands,
                'input_backend': 'windows-targeted-keyboard', 'keyboard_scancodes': self.keymap}

    async def status(self):
        async with self.lock:
            return await self._status()

    async def _terminate(self):
        if self.process and self.process.poll() is None:
            self._require_owned()
            self.process.terminate()
            try:
                await asyncio.to_thread(self.process.wait, 5)
            except subprocess.TimeoutExpired:
                self._require_owned()
                self.process.kill()
                await asyncio.to_thread(self.process.wait, 5)
        await self.qmp.close()

    async def stop(self):
        async with self.lock:
            with anyio.CancelScope(shield=True):
                self._require_owned()
                pid = self.process.pid
                try:
                    await self.qmp.execute('quit')
                    await asyncio.to_thread(self.process.wait, 5)
                except (OSError, ProtocolError, TimeoutError, subprocess.TimeoutExpired):
                    await self._terminate()
                finally:
                    await self.qmp.close()
                return {'pid': pid, 'stopped': self.process.poll() is not None}

    async def cleanup(self):
        if self.process and self.process.poll() is None:
            await self.stop()
        else:
            await self.qmp.close()

    async def pause(self, resume=False):
        async with self.lock:
            self._require_owned()
            await self.qmp.execute('cont' if resume else 'stop')
            return await self._status()

    @asynccontextmanager
    async def _memory_session(self):
        self._require_owned()
        running = None
        gdb = GDB('127.0.0.1', self.config.gdb_port)
        try:
            # Do not leave an uncertain stop command in flight when an SDK
            # cancellation scope expires. QMP requests have bounded timeouts.
            with anyio.CancelScope(shield=True):
                running = (await self.qmp.execute('query-status'))['running']
                await self.qmp.execute('stop')
            await gdb.connect()
            yield gdb
        finally:
            with anyio.CancelScope(shield=True):
                try:
                    if gdb.writer is not None:
                        if running:
                            await gdb.detach()
                        else:
                            # xemu's GDB D packet resumes execution before OK.
                            # TCP close emits CLOSED, which its stub ignores:
                            # a paused caller therefore never briefly executes.
                            await gdb.close()
                finally:
                    if running is not None:
                        if not self.qmp.connected:
                            self._assert_ports_owned()
                            await self.qmp.connect()
                        await self.qmp.execute('cont' if running else 'stop')

    async def read(self, address, length, decode_as=None):
        value = validate_range(address, length)
        if decode_as:
            decode(bytes(length), decode_as)
        async with self.lock:
            async with self._memory_session() as gdb:
                data = await gdb.read(value, length)
        result = {'address': hex(value), 'length': length, 'hex': data.hex()}
        if decode_as:
            result['values'] = decode(data, decode_as)
        return result

    async def write(self, address, hex_data):
        if not self.config.allow_write:
            raise PermissionError('Memory writes are disabled; set allow_write=true in the private server config')
        if len(hex_data) > 2 * 65536:
            raise ValueError('Write payload is too large')
        data = bytes.fromhex(hex_data)
        value = validate_range(address, len(data))
        async with self.lock:
            async with self._memory_session() as gdb:
                await gdb.write(value, data)
                actual = await gdb.read(value, len(data))
                if actual != data:
                    raise ProtocolError('Write read-back mismatch; guest memory may have been partially changed')
        return {'address': hex(value), 'length': len(data), 'verified': True}

    async def read_physical(self, address, length, decode_as=None):
        value = validate_range(address, length)
        if decode_as:
            decode(bytes(length), decode_as)
        async with self.lock:
            self._require_owned()
            text = await self.qmp.hmp(f'xp /{length}xb 0x{value:x}')
        data = parse_physical(text, length)
        result = {'address': hex(value), 'length': length, 'hex': data.hex()}
        if decode_as:
            result['values'] = decode(data, decode_as)
        return result

    async def registers(self):
        async with self.lock:
            self._require_owned()
            text = await self.qmp.hmp('info registers')
        pairs = re.findall(r'\b([A-Z][A-Z0-9]{1,7})=([0-9a-fA-F]+)\b', text)
        return {'registers': {name: '0x' + value for name, value in pairs}, 'raw': text}

    async def snapshot(self, action, tag):
        if action not in ('save', 'load', 'delete') or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', tag):
            raise ValueError('Snapshot tag must be 1 to 64 ASCII letters, digits, underscore or hyphen')
        async with self.lock:
            self._require_owned()
            if action == 'save' and not (await self.qmp.execute('query-status'))['running']:
                raise RuntimeError('Snapshot save requires a running guest; use xemu_resume first. '
                                   'Paused savevm can stall xemu when it is resumed.')
            command = {'save': 'savevm', 'load': 'loadvm', 'delete': 'delvm'}[action]
            output = await self.qmp.hmp(f'{command} {tag}', timeout=120)
            # Successful savevm/loadvm/delvm return empty text. HMP errors arrive
            # as a QMP success string, not a QMP error object.
            if output.strip():
                raise RuntimeError(f'Snapshot {action} failed: {output.strip()}')
        return {'tag': tag, 'action': action, 'ok': True}

    async def press(self, buttons, hold_ms=100):
        if type(hold_ms) is not int or not 1 <= hold_ms <= 10000:
            raise ValueError('hold_ms must be an integer from 1 to 10000')
        keys = controller_keys(buttons, self.keymap)
        validate_windows_keys(keys)
        async with self.lock:
            self._require_owned()
            hwnd = window_for_pid(self.process.pid)
            pressed = []
            try:
                for scan in keys:
                    post_key(hwnd, scan, True, self.process.pid)
                    pressed.append(scan)
                await asyncio.sleep(hold_ms / 1000)
            finally:
                for scan in reversed(pressed):
                    post_key(hwnd, scan, False, self.process.pid)
        return {'buttons': buttons, 'hold_ms': hold_ms, 'sdl_scancodes': keys,
                'backend': 'windows-targeted-keyboard', 'analog': False}

    async def screenshot(self, filename, include_base64=False, bring_to_front=False):
        path = Path(filename).expanduser()
        if not path.is_absolute() or path.suffix.lower() != '.png':
            raise ValueError('Screenshot filename must be an absolute .png path')
        path = path.resolve()
        if not path.parent.is_dir():
            raise ValueError('Screenshot parent directory must already exist')
        async with self.lock:
            self._require_owned()
            # Capture to a fresh sibling so a stale existing destination can
            # never be mistaken for the current frame after a failed write.
            temporary = path.with_name(f'.xemu-frame-{uuid.uuid4().hex}.png')
            try:
                if 'xemu-screenshot' in self.commands:
                    await self.qmp.execute('xemu-screenshot', {'filename': str(temporary)}, timeout=30)
                    result = {'method': 'qmp', 'needs_visible_window': False}
                else:
                    result = capture_region(self.process.pid, str(temporary), bring_to_front)
                    result['method'] = 'window-region'
                if not temporary.is_file() or temporary.stat().st_size == 0:
                    raise RuntimeError('Screenshot command returned without writing a nonempty file')
                from PIL import Image
                with Image.open(temporary) as frame:
                    if frame.format != 'PNG':
                        raise RuntimeError('Screenshot is not a PNG')
                    result.update(width=frame.width, height=frame.height)
                    frame.verify()
                temporary.replace(path)
            finally:
                temporary.unlink(missing_ok=True)
            result.update(filename=str(path), bytes=path.stat().st_size)
            if include_base64:
                if path.stat().st_size > 16 * 1024 * 1024:
                    raise ValueError('Screenshot too large to return inline (16 MiB limit)')
                result['mime_type'] = 'image/png'
                result['base64'] = base64.b64encode(path.read_bytes()).decode('ascii')
            return result

    def symbols(self, query=''):
        if not self.config.symbols_file:
            return {'symbols': {}, 'note': 'No private symbols_file configured'}
        data = json.loads(Path(self.config.symbols_file).expanduser().read_text(encoding='utf-8-sig'))
        return {'symbols': {name: address for name, address in data.items() if query.lower() in name.lower()}}
