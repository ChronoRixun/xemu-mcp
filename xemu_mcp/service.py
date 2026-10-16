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
from contextlib import aclosing, asynccontextmanager
from dataclasses import dataclass, field, fields
from pathlib import Path

import psutil
import anyio

from . import research
from .protocols import GDB, QMP, GDBErrorReply, ProtocolError, parse_stop_reply
from .research import (CAPTURE_HANDLES, CAPTURE_LIMIT, CHUNK, PAGE, RESULT_LIMIT, SEARCH_LIMIT,
                       Capture, Matcher, SnapshotCatalog, check_alignment, check_int, json_value)
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
    protected_processes: list[str] = field(default_factory=list)
    audiodev: str | None = None
    snapshot_catalog: str | None = None

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
        if (self.audiodev is not None
                and (not isinstance(self.audiodev, str)
                     or not re.fullmatch(r'[A-Za-z0-9_,=.:-]{1,128}', self.audiodev))):
            raise ValueError('audiodev must be a single -audiodev option value such as "sdl,id=capture0"')
        if (not isinstance(self.allowed_game_roots, list)
                or any(not isinstance(root, str) or not Path(root).is_absolute()
                       for root in self.allowed_game_roots)):
            raise ValueError('allowed_game_roots must be a list of absolute paths')
        # Only a missing key or an empty list falls back to the environment;
        # an explicit falsy non-list (null, "") is invalid and must raise.
        if isinstance(self.protected_processes, list) and not self.protected_processes:
            listed = os.environ.get('XEMU_MCP_PROTECTED_PROCESSES')
            if listed:
                self.protected_processes = [name.strip() for name in re.split(r'[,;]', listed)
                                            if name.strip()]
        if (not isinstance(self.protected_processes, list)
                or any(not isinstance(name, str) or not name.strip()
                       or '/' in name or '\\' in name
                       for name in self.protected_processes)):
            raise ValueError('protected_processes must be a list of process names without path separators')
        self.protected_processes = [name.strip() for name in self.protected_processes]
        if self.snapshot_catalog is not None:
            if not isinstance(self.snapshot_catalog, str):
                raise ValueError('snapshot_catalog must be an absolute .json path')
            SnapshotCatalog(self.snapshot_catalog)

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


# GDB RSP Z/z point types for software breakpoints and write/read/access watchpoints.
POINT_KINDS = {'exec': 0, 'write': 2, 'read': 3, 'access': 4}


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
        self.gdb_timeout = 10
        self.captures = {}
        self.capture_serial = 0
        self.points = {}
        self.point_serial = 0
        self.catalog = SnapshotCatalog(config.snapshot_catalog) if config.snapshot_catalog else None

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
        protected = {name.lower() for name in self.config.protected_processes}
        for process in psutil.process_iter(['name', 'exe']):
            name = (process.info['name'] or '').lower()
            if name in ('xemu', 'xemu.exe'):
                raise RuntimeError(f'Another xemu is already running (PID {process.pid}); refusing to attach or start')
            if name in protected:
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
            if self.config.audiodev:
                # wavcapture/stopcapture need a named audiodev; an emulator
                # started on the legacy default audio path may expose none.
                args.extend(['-audiodev', self.config.audiodev])
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
    async def _memory_session(self, leave=None):
        # leave=None restores the caller's run state; True/False choose it.
        self._require_owned()
        running = None
        gdb = GDB('127.0.0.1', self.config.gdb_port, self.gdb_timeout)
        try:
            # Do not leave an uncertain stop command in flight when an SDK
            # cancellation scope expires. QMP requests have bounded timeouts.
            with anyio.CancelScope(shield=True):
                running = (await self.qmp.execute('query-status'))['running']
                if leave is not None:
                    running = leave
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

    async def snapshot(self, action, tag, description=None, zone=None, notes=None):
        if action not in ('save', 'load', 'delete') or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', tag):
            raise ValueError('Snapshot tag must be 1 to 64 ASCII letters, digits, underscore or hyphen')
        SnapshotCatalog.check_text({'description': description, 'zone': zone, 'notes': notes})
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
        result = {'tag': tag, 'action': action, 'ok': True}
        if self.catalog is None:
            return result
        # The snapshot itself succeeded; a catalogue problem is reported, not raised.
        try:
            if action == 'save':
                result['catalog'] = self.catalog.record(tag, saved=True, description=description,
                                                        zone=zone, notes=notes)
            elif action == 'load':
                result['catalog'] = self.catalog.touch(tag, 'last_loaded')
            else:
                result['catalog'] = self.catalog.remove(tag)
        except (OSError, TypeError, ValueError) as exc:
            result['catalog_error'] = str(exc)
        return result

    def _require_catalog(self):
        if self.catalog is None:
            raise RuntimeError('No snapshot_catalog configured; set an absolute .json path outside '
                               'the repository in the private server config')
        return self.catalog

    async def state_list(self, query='', zone=None, check_emulator=False):
        catalog = self._require_catalog()
        if not isinstance(query, str) or (zone is not None and not isinstance(zone, str)):
            raise ValueError('query and zone must be strings')
        entries = catalog.search(query, zone)
        result = {'catalog': str(catalog.path), 'count': len(entries), 'snapshots': entries}
        if check_emulator:
            async with self.lock:
                self._require_owned()
                text = await self.qmp.hmp('info snapshots')
            tags = self._snapshot_tags(text)
            for entry in entries:
                entry['present'] = entry['tag'] in tags
            result['uncatalogued'] = sorted(tags - set(catalog.load()))
        return result

    @staticmethod
    def _snapshot_tags(text):
        # HMP 'info snapshots': a header line starting "ID TAG", then one row per snapshot.
        tags, rows = set(), False
        for line in text.splitlines():
            columns = line.split()
            if columns[:2] == ['ID', 'TAG']:
                rows = True
            elif rows and len(columns) >= 2:
                tags.add(columns[1])
        return tags

    def state_annotate(self, tag, description=None, zone=None, notes=None):
        if not isinstance(tag, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', tag):
            raise ValueError('Snapshot tag must be 1 to 64 ASCII letters, digits, underscore or hyphen')
        return {'snapshot': self._require_catalog().record(tag, description=description,
                                                           zone=zone, notes=notes)}

    async def audio_devices(self):
        async with self.lock:
            self._require_owned()
            return {'audiodevs': await self.qmp.execute('query-audiodevs')}

    async def _resolve_audiodev(self, requested):
        devices = await self.qmp.execute('query-audiodevs')
        ids = [device['id'] for device in devices if device.get('id')]
        if requested is not None:
            if requested not in ids:
                raise ValueError(f"audiodev '{requested}' is not running; available: {ids or 'none'}")
            return requested
        if len(ids) == 1:
            return ids[0]
        if not ids:
            raise RuntimeError('The emulator exposes no named audiodev; set "audiodev" in the private '
                               'server config (for example "sdl,id=capture0") and restart')
        raise ValueError(f'Several audiodevs are running {ids}; pass audiodev explicitly')

    async def audio_capture_start(self, filename, audiodev=None, frequency=44100, bits=16, channels=2):
        path = Path(filename).expanduser()
        if not path.is_absolute() or path.suffix.lower() != '.wav':
            raise ValueError('Capture filename must be an absolute .wav path')
        path = path.resolve()
        if not path.parent.is_dir():
            raise ValueError('Capture parent directory must already exist')
        if any(char.isspace() for char in str(path)):
            raise ValueError('Capture path must not contain whitespace (HMP command line)')
        if type(frequency) is not int or not 4000 <= frequency <= 192000:
            raise ValueError('frequency must be an integer from 4000 to 192000 Hz')
        if bits not in (8, 16):
            raise ValueError('bits must be 8 or 16')
        if channels not in (1, 2):
            raise ValueError('channels must be 1 or 2')
        if audiodev is not None and not re.fullmatch(r'[A-Za-z0-9_.-]{1,64}', audiodev):
            raise ValueError('audiodev must be 1 to 64 ASCII letters, digits, dot, underscore or hyphen')
        async with self.lock:
            self._require_owned()
            device = await self._resolve_audiodev(audiodev)
            output = await self.qmp.hmp(f'wavcapture {path} {device} {frequency} {bits} {channels}')
            self._check_capture_output('start', output)
        return {'filename': str(path), 'audiodev': device, 'frequency': frequency,
                'bits': bits, 'channels': channels, 'capturing': True}

    async def audio_capture_stop(self, capture_index=0):
        if type(capture_index) is not int or not 0 <= capture_index <= 9:
            raise ValueError('capture_index must be an integer from 0 to 9')
        async with self.lock:
            self._require_owned()
            output = await self.qmp.hmp(f'stopcapture {capture_index}')
            self._check_capture_output('stop', output)
        return {'capture_index': capture_index, 'capturing': False}

    @staticmethod
    def _check_capture_output(action, output):
        # Successful wavcapture/stopcapture return empty text; HMP errors arrive
        # as a QMP success string. Deprecation notices go to the monitor as
        # warnings on some builds and are not failures.
        problems = [line.strip() for line in output.splitlines()
                    if line.strip() and 'deprecat' not in line.lower()]
        if problems:
            raise RuntimeError(f'Audio capture {action} failed: {problems[0]}')

    async def press(self, buttons, hold_ms=100):
        if type(hold_ms) is not int or not 1 <= hold_ms <= 10000:
            raise ValueError('hold_ms must be an integer from 1 to 10000')
        keys = self._resolve_keys(buttons)
        async with self.lock:
            self._require_owned()
            hwnd = window_for_pid(self.process.pid)
            await self._deliver(hwnd, keys, hold_ms)
        return {'buttons': buttons, 'hold_ms': hold_ms, 'sdl_scancodes': keys,
                'backend': 'windows-targeted-keyboard', 'analog': False}

    def _resolve_keys(self, buttons):
        keys = controller_keys(buttons, self.keymap)
        validate_windows_keys(keys)
        return keys

    async def _deliver(self, hwnd, keys, hold_ms):
        pressed = []
        try:
            for scan in keys:
                post_key(hwnd, scan, True, self.process.pid)
                pressed.append(scan)
            await asyncio.sleep(hold_ms / 1000)
        finally:
            for scan in reversed(pressed):
                post_key(hwnd, scan, False, self.process.pid)

    async def input_sequence(self, steps, dry_run=False):
        """Run validated (buttons, hold_ms, wait_ms) steps; every press is released."""
        plan, total = research.plan_sequence(steps, self._resolve_keys)
        result = {'steps': plan, 'total_ms': total, 'dry_run': bool(dry_run),
                  'backend': 'windows-targeted-keyboard', 'analog': False, 'executed': 0}
        if dry_run:
            # Validation and scancode resolution only: no ownership or window needed.
            return result
        async with self.lock:
            self._require_owned()
            hwnd = window_for_pid(self.process.pid)
            started = time.monotonic()
            for index, step in enumerate(plan):
                await self._deliver(hwnd, step['sdl_scancodes'], step['hold_ms'])
                result['executed'] = index + 1
                if step['wait_ms']:
                    await asyncio.sleep(step['wait_ms'] / 1000)
            result['elapsed_ms'] = round((time.monotonic() - started) * 1000)
        return result

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

    # Bounded memory research: search, captures/narrowing, typed structs.

    @staticmethod
    async def _iter_readable(gdb, address, length, holes):
        """Yield (address, bytes) readable pieces; add unreadable 4 KiB pages to holes."""
        offset = 0
        while offset < length:
            start = address + offset
            end = min(address + length, (start // CHUNK + 1) * CHUNK)
            try:
                data = await gdb.read(start, end - start)
            except GDBErrorReply:
                data = None
            if data is not None:
                yield start, data
            else:
                page = start
                while page < end:
                    stop = min(end, (page // PAGE + 1) * PAGE)
                    try:
                        data = await gdb.read(page, stop - page)
                    except GDBErrorReply:
                        holes.add(page // PAGE)
                    else:
                        yield page, data
                    page = stop
            offset = end - address

    async def _read_sparse(self, gdb, address, length):
        data = bytearray(length)
        holes = set()
        async with aclosing(self._iter_readable(gdb, address, length, holes)) as pieces:
            async for start, chunk in pieces:
                data[start - address:start - address + len(chunk)] = chunk
        pages = range(address // PAGE, (address + length - 1) // PAGE + 1)
        if all(page in holes for page in pages):
            raise RuntimeError(f'No readable memory in {hex(address)}..{hex(address + length)}')
        return data, holes

    async def search(self, address, length, value_type, value, tolerance=0.0, alignment=None,
                     max_results=100):
        start = self._validate_region(address, length, SEARCH_LIMIT)
        check_int('max_results', max_results, 1, RESULT_LIMIT)
        matcher = Matcher(value_type, value, tolerance, alignment)
        results, truncated = [], False
        holes = set()
        async with self.lock, self._memory_session() as gdb, \
                aclosing(self._iter_readable(gdb, start, length, holes)) as pieces:
            carry_address, carry = None, b''
            async for piece, data in pieces:
                if carry_address is not None and carry_address + len(carry) == piece:
                    piece, data = carry_address, carry + data
                for found, found_value in matcher.find(piece, data):
                    if len(results) == max_results:
                        truncated = True
                        break
                    results.append({'address': hex(found), 'value': json_value(found_value)})
                if truncated:
                    break
                keep = min(len(data), matcher.size - 1)
                carry_address, carry = piece + len(data) - keep, data[len(data) - keep:]
        unreadable, more = research.merge_holes(holes)
        return {'address': hex(start), 'length': length, 'value_type': value_type,
                'alignment': matcher.alignment, 'count': len(results), 'truncated': truncated,
                'results': results, 'unreadable': unreadable, 'unreadable_truncated': more}

    @staticmethod
    def _validate_region(address, length, limit):
        try:
            value = int(address, 0) if isinstance(address, str) else address
        except ValueError as exc:
            raise ValueError('Address must be decimal or 0x-prefixed hexadecimal') from exc
        if type(value) is not int or type(length) is not int:
            raise ValueError('Address and length must be integers')
        if not 1 <= length <= limit or not 0 <= value < 2**32 or value + length > 2**32:
            raise ValueError(f'Range must fit 32-bit memory and be 1 to {limit} bytes')
        return value

    async def capture(self, address, length, value_type='u32', alignment=None):
        start = self._validate_region(address, length, CAPTURE_LIMIT)
        size = research.scalar_size(value_type)
        alignment = check_alignment(alignment, size)
        if len(self.captures) >= CAPTURE_HANDLES:
            raise RuntimeError(f'At most {CAPTURE_HANDLES} captures are kept; release one with xemu_mem_release')
        async with self.lock, self._memory_session() as gdb:
            data, holes = await self._read_sparse(gdb, start, length)
        self.capture_serial += 1
        handle = f'mem-{self.capture_serial}'
        self.captures[handle] = Capture(handle, start, length, value_type, alignment, bytes(data), holes)
        return self.captures[handle].summary()

    def _capture(self, handle):
        if handle not in self.captures:
            raise ValueError(f'Unknown capture handle {handle!r}; list them with xemu_mem_captures')
        return self.captures[handle]

    async def _recapture(self, capture):
        async with self.lock, self._memory_session() as gdb:
            return await self._read_sparse(gdb, capture.address, capture.length)

    async def diff(self, handle, max_results=100, rebase=False):
        capture = self._capture(handle)
        check_int('max_results', max_results, 1, RESULT_LIMIT)
        new, holes = await self._recapture(capture)
        results, count = [], 0
        for offset in capture.changed(new, holes):
            count += 1
            if len(results) < max_results:
                results.append({'offset': hex(offset), 'address': hex(capture.address + offset),
                                'old': json_value(capture.value(capture.data, offset)),
                                'new': json_value(capture.value(new, offset))})
        if rebase:
            capture.data, capture.holes = bytes(new), set(holes)
        return {'handle': handle, 'value_type': capture.value_type, 'changed': count,
                'truncated': count > len(results), 'rebased': bool(rebase), 'results': results}

    async def narrow(self, handle, condition, value=None, tolerance=0.0, max_results=100):
        capture = self._capture(handle)
        check_int('max_results', max_results, 1, RESULT_LIMIT)
        if not isinstance(tolerance, (int, float)) or isinstance(tolerance, bool) \
                or not math.isfinite(tolerance) or tolerance < 0:
            raise ValueError('tolerance must be a finite number >= 0')
        if tolerance and (capture.value_type != 'f32' or condition != 'equals'):
            raise ValueError('tolerance applies only to f32 equals')
        if condition == 'equals':
            research.parse_number(capture.value_type, value)
        elif condition not in research.CONDITIONS:
            raise ValueError(f'condition must be one of {", ".join(research.CONDITIONS)}')
        previous = capture.data
        new, holes = await self._recapture(capture)
        kept = capture.narrow(new, holes, condition, value, tolerance)
        results = [{'address': hex(capture.address + offset),
                    'previous': json_value(capture.value(previous, offset)),
                    'value': json_value(capture.value(new, offset))} for offset in kept[:max_results]]
        return {'handle': handle, 'condition': condition, 'candidates': len(kept),
                'truncated': len(kept) > len(results), 'narrowed': capture.narrowed, 'results': results}

    def captures_list(self):
        return {'captures': [capture.summary() for capture in self.captures.values()],
                'limit': CAPTURE_HANDLES}

    def capture_release(self, handle):
        self._capture(handle)
        del self.captures[handle]
        return {'handle': handle, 'released': True}

    async def read_struct(self, address, fields, structs=None, max_depth=1):
        root = validate_range(address, 1)
        check_int('max_depth', max_depth, 0, 4)
        structs = structs or {}
        if not isinstance(structs, dict) or len(structs) > 16:
            raise ValueError('structs must map up to 16 names to field spec lists')
        names = set(structs)
        specs = {name: research.parse_fields(spec, names) for name, spec in structs.items()}
        root_spec = research.parse_fields(fields, names)
        budget = {'nodes': 64, 'bytes': 64 * 1024}

        async def decode_at(gdb, where, spec, depth, path):
            length = research.span(spec)
            if budget['nodes'] <= 0 or budget['bytes'] < length:
                return {'address': hex(where), 'truncated': 'read budget exhausted'}
            budget['nodes'] -= 1
            budget['bytes'] -= length
            try:
                validate_range(where, length)
                data = await gdb.read(where, length)
            except (GDBErrorReply, ValueError) as exc:
                return {'address': hex(where), 'error': str(exc)}
            decoded = {}
            for item in spec:
                value = research.decode_field(item, data)
                if item.kind != 'ptr':
                    decoded[item.name] = value
                elif item.target is None or value == 0:
                    decoded[item.name] = hex(value) if item.target is None else None
                elif (value, item.target) in path:
                    decoded[item.name] = {'address': hex(value), 'cycle': True}
                elif depth >= max_depth:
                    decoded[item.name] = {'address': hex(value), 'truncated': 'max_depth'}
                else:
                    decoded[item.name] = await decode_at(gdb, value, specs[item.target], depth + 1,
                                                         path | {(value, item.target)})
                    decoded[item.name]['struct'] = item.target
            return {'address': hex(where), 'fields': decoded}

        async with self.lock, self._memory_session() as gdb:
            result = await decode_at(gdb, root, root_spec, 0, frozenset({(root, None)}))
        if 'error' in result:
            raise RuntimeError(f'Struct read at {hex(root)} failed: {result["error"]}')
        result['max_depth'] = max_depth
        return result

    # Breakpoints/watchpoints are kept server-side and armed only while
    # xemu_break_wait runs; they are never left inserted between calls.

    def break_set(self, address, kind='exec', length=1):
        value = validate_range(address, 1)
        if kind not in POINT_KINDS:
            raise ValueError(f'kind must be one of {", ".join(POINT_KINDS)}')
        if kind == 'exec':
            if length != 1:
                raise ValueError('exec breakpoints use length 1')
        elif length not in (1, 2, 4, 8) or value % length:
            raise ValueError('watchpoint length must be 1, 2, 4 or 8 and the address aligned to it')
        if len(self.points) >= 16:
            raise RuntimeError('At most 16 breakpoints/watchpoints may be set')
        for point in self.points.values():
            if (point['kind'], point['address'], point['length']) == (kind, hex(value), length):
                raise ValueError(f'Identical point already set as {point["id"]}')
        self.point_serial += 1
        point = {'id': f'bp-{self.point_serial}', 'kind': kind, 'address': hex(value),
                 'length': length, 'z': POINT_KINDS[kind], 'hits': 0}
        self.points[point['id']] = point
        return {'point': point, 'armed': False,
                'note': 'Points are inserted only while xemu_break_wait runs'}

    def break_clear(self, point_id=None):
        if point_id is None:
            cleared = sorted(self.points)
            self.points.clear()
        else:
            if point_id not in self.points:
                raise ValueError(f'Unknown point {point_id!r}')
            del self.points[point_id]
            cleared = [point_id]
        return {'cleared': cleared, 'remaining': len(self.points)}

    def break_list(self):
        return {'points': list(self.points.values())}

    async def _disarm(self, gdb, armed):
        """Remove inserted points; on a broken stream reconnect and clear them."""
        if not armed:
            return
        if not gdb.broken and gdb.writer is not None:
            try:
                for point in armed:
                    await gdb.clear_point(point['z'], int(point['address'], 16), point['length'])
                return
            except (OSError, ProtocolError, TimeoutError):
                pass
        # QEMU's stub accepts one client: drop the broken socket, then use a
        # fresh connection. Its '?' handler also removes every GDB point.
        await gdb.close()
        fresh = GDB('127.0.0.1', self.config.gdb_port, self.gdb_timeout)
        try:
            await fresh.connect()
            await fresh.request('?')
            for point in armed:
                try:
                    await fresh.clear_point(point['z'], int(point['address'], 16), point['length'])
                except ProtocolError:
                    pass
        finally:
            await fresh.close()

    async def _window(self, gdb, address, length):
        try:
            validate_range(address, length)
            data = await gdb.read(address, length)
        except (GDBErrorReply, ValueError) as exc:
            return {'address': hex(address), 'error': str(exc)}
        return {'address': hex(address), 'hex': data.hex()}

    async def break_wait(self, timeout_s=10, leave='paused', stack_bytes=64, window_bytes=32):
        if (not isinstance(timeout_s, (int, float)) or isinstance(timeout_s, bool)
                or not math.isfinite(timeout_s) or not 0.05 <= timeout_s <= 120):
            raise ValueError('timeout_s must be 0.05 to 120 seconds')
        if leave not in ('paused', 'running'):
            raise ValueError("leave must be 'paused' or 'running'")
        check_int('stack_bytes', stack_bytes, 0, 1024)
        check_int('window_bytes', window_bytes, 0, 4096)
        if stack_bytes % 4:
            raise ValueError('stack_bytes must be a multiple of 4')
        if not self.points:
            raise ValueError('No breakpoints/watchpoints set; use xemu_break_set first')
        points = list(self.points.values())
        async with self.lock, self._memory_session(leave=leave == 'running') as gdb:
            armed = []
            try:
                # QEMU clears stale GDB points on '?'; also confirms the stub.
                await gdb.request('?')
                for point in points:
                    # Record before the insert exchange: a cancellation that
                    # lands after the stub processed Z but before the request
                    # returned must still find the point here and disarm it.
                    armed.append(point)
                    await gdb.set_point(point['z'], int(point['address'], 16), point['length'])
                reply, interrupted = await gdb.resume_until_stop(
                    timeout_s, interrupt=lambda: self.qmp.execute('stop'))
            finally:
                with anyio.CancelScope(shield=True):
                    await self._disarm(gdb, armed)
            stop = parse_stop_reply(reply)
            if stop['kind'] in ('W', 'X'):
                raise RuntimeError(f'GDB stub reported target exit: {reply[:40]}')
            registers = await gdb.registers()
            matched = self._match_points(points, stop, registers['EIP'])
            result = {'hit': bool(matched) or (not interrupted and stop['signal'] == 5),
                      'interrupted': interrupted, 'matched': [p['id'] for p in matched],
                      'stop': {key: value for key, value in stop.items() if key != 'watch'},
                      'registers': {name: f'0x{value:08x}' for name, value in registers.items()},
                      'leave': leave}
            if 'watch' in stop:
                result['stop']['watch'] = {'type': stop['watch']['type'],
                                           'address': hex(stop['watch']['address'])}
            if stack_bytes:
                stack = await self._window(gdb, registers['ESP'], stack_bytes)
                if 'hex' in stack:
                    stack['u32'] = [value for (value,) in struct.iter_unpack('<I', bytes.fromhex(stack['hex']))]
                result['stack'] = stack
            if window_bytes:
                focus = stop['watch']['address'] & ~0xF if 'watch' in stop else registers['EIP']
                result['window'] = await self._window(gdb, focus, window_bytes)
            for point in matched:
                point['hits'] += 1
        return result

    @staticmethod
    def _match_points(points, stop, eip):
        if 'watch' in stop:
            address = stop['watch']['address']
            return [point for point in points if point['kind'] != 'exec'
                    and int(point['address'], 16) <= address < int(point['address'], 16) + point['length']]
        return [point for point in points if point['kind'] == 'exec' and int(point['address'], 16) == eip]

    def symbols(self, query=''):
        if not self.config.symbols_file:
            return {'symbols': {}, 'note': 'No private symbols_file configured'}
        data = json.loads(Path(self.config.symbols_file).expanduser().read_text(encoding='utf-8-sig'))
        return {'symbols': {name: address for name, address in data.items() if query.lower() in name.lower()}}
