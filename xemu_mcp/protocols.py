"""Bounded QMP and GDB Remote Serial Protocol clients (no SDK dependency)."""

import asyncio
import json


class ProtocolError(RuntimeError):
    """The peer rejected a command or sent invalid protocol data."""


class QMP:
    def __init__(self, host, port, timeout=10):
        self.host, self.port, self.timeout = host, port, timeout
        self.reader = self.writer = None
        self.lock = asyncio.Lock()
        self.sequence = 0

    @property
    def connected(self):
        return self.writer is not None and not self.writer.is_closing()

    async def connect(self):
        try:
            async with asyncio.timeout(self.timeout):
                self.reader, self.writer = await asyncio.open_connection(
                    self.host, self.port, limit=4 * 1024 * 1024)
                greeting = await self._message()
                if 'QMP' not in greeting:
                    raise ProtocolError('Missing QMP greeting')
                await self.execute('qmp_capabilities')
        except BaseException:
            await self.close()
            raise

    async def _message(self):
        line = await self.reader.readline()
        if not line:
            raise ProtocolError('QMP disconnected')
        try:
            return json.loads(line)
        except (ValueError, UnicodeError) as exc:
            raise ProtocolError('Invalid QMP JSON') from exc

    async def execute(self, command, arguments=None, timeout=None):
        async with self.lock:
            if not self.connected:
                raise ProtocolError('QMP is not connected')
            try:
                async with asyncio.timeout(timeout or self.timeout):
                    self.sequence += 1
                    request = {'execute': command, 'id': self.sequence}
                    if arguments is not None:
                        request['arguments'] = arguments
                    self.writer.write(json.dumps(request).encode() + b'\n')
                    await self.writer.drain()
                    while True:
                        response = await self._message()
                        if 'event' in response:
                            continue
                        if response.get('id') != self.sequence:
                            raise ProtocolError('Unexpected QMP response ID')
                        if 'error' in response:
                            error = response['error']
                            raise ProtocolError(f"{error.get('class')}: {error.get('desc')}")
                        if 'return' not in response:
                            raise ProtocolError('Missing QMP result')
                        return response['return']
            except TimeoutError as exc:
                await self.close()
                raise TimeoutError(f'QMP {command} timed out after {timeout or self.timeout}s; '
                                   'connection closed') from exc
            except (OSError, asyncio.CancelledError):
                await self.close()
                raise

    async def hmp(self, command, timeout=None):
        return await self.execute('human-monitor-command',
                                  {'command-line': command}, timeout=timeout)

    async def close(self):
        if self.writer:
            self.writer.close()
            try:
                await self.writer.wait_closed()
            except OSError:
                pass
        self.reader = self.writer = None


class GDB:
    def __init__(self, host, port, timeout=10):
        self.host, self.port, self.timeout = host, port, timeout
        self.reader = self.writer = None

    async def connect(self):
        self.reader, self.writer = await asyncio.wait_for(
            asyncio.open_connection(self.host, self.port), self.timeout)

    async def request(self, payload):
        body = payload.encode('ascii')
        packet = b'$' + body + b'#' + f'{sum(body) & 255:02x}'.encode()
        async with asyncio.timeout(self.timeout):
            self.writer.write(packet)
            await self.writer.drain()
            failures = 0
            while True:
                char = await self.reader.readexactly(1)
                if char == b'-':
                    failures += 1
                    if failures > 3:
                        raise ProtocolError('GDB rejected packet checksum')
                    self.writer.write(packet)
                    await self.writer.drain()
                    continue
                if char == b'+':
                    continue
                if char != b'$':
                    raise ProtocolError('Invalid GDB packet start')
                encoded = bytearray()
                while (char := await self.reader.readexactly(1)) != b'#':
                    encoded.extend(char)
                    if len(encoded) > 128 * 1024:
                        raise ProtocolError('GDB packet exceeds limit')
                checksum = await self.reader.readexactly(2)
                try:
                    valid = int(checksum, 16) == sum(encoded) & 255
                except ValueError:
                    valid = False
                self.writer.write(b'+' if valid else b'-')
                await self.writer.drain()
                if not valid:
                    failures += 1
                    if failures > 3:
                        raise ProtocolError('Invalid GDB reply checksum')
                    continue
                decoded = self._decode(encoded)
                if decoded.startswith('E'):
                    raise ProtocolError(f'GDB error {decoded}')
                # Console output can precede the actual command result.
                if decoded.startswith('O') and decoded != 'OK':
                    continue
                return decoded

    @staticmethod
    def _decode(encoded):
        decoded = bytearray()
        index = 0
        while index < len(encoded):
            char = encoded[index]
            if char in (ord('}'), ord('*')):
                index += 1
                if index >= len(encoded):
                    raise ProtocolError('Truncated GDB escape/RLE')
                if char == ord('}'):
                    decoded.append(encoded[index] ^ 0x20)
                else:
                    count = encoded[index] - 29
                    if not decoded or count < 0:
                        raise ProtocolError('Invalid GDB RLE')
                    decoded.extend([decoded[-1]] * count)
            else:
                decoded.append(char)
            if len(decoded) > 128 * 1024:
                raise ProtocolError('Decoded GDB packet exceeds limit')
            index += 1
        return decoded.decode('ascii')

    async def read(self, address, length):
        result = bytearray()
        while len(result) < length:
            count = min(512, length - len(result))
            reply = await self.request(f'm{address + len(result):x},{count:x}')
            try:
                chunk = bytes.fromhex(reply)
            except ValueError as exc:
                raise ProtocolError('Invalid GDB memory hex') from exc
            if len(chunk) != count:
                raise ProtocolError('Short GDB memory read')
            result.extend(chunk)
        return bytes(result)

    async def write(self, address, data):
        for offset in range(0, len(data), 512):
            chunk = data[offset:offset + 512]
            reply = await self.request(f'M{address + offset:x},{len(chunk):x}:{chunk.hex()}')
            if reply != 'OK':
                raise ProtocolError('GDB memory write was not acknowledged')

    async def detach(self):
        try:
            if await self.request('D') != 'OK':
                raise ProtocolError('GDB detach was not acknowledged')
        finally:
            await self.close()

    async def close(self):
        if self.writer:
            self.writer.close()
            try:
                await self.writer.wait_closed()
            except OSError:
                pass
        self.reader = self.writer = None
