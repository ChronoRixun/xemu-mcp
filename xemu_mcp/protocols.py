"""Bounded QMP and GDB Remote Serial Protocol clients (no SDK dependency)."""

import asyncio
import json


class ProtocolError(RuntimeError):
    """The peer rejected a command or sent invalid protocol data."""


class GDBErrorReply(ProtocolError):
    """The GDB stub answered E<nn>; the connection itself remains usable."""


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


# Leading general registers of QEMU's i386 GDB 'g' packet, in order.
I386_REGISTERS = ('EAX', 'ECX', 'EDX', 'EBX', 'ESP', 'EBP', 'ESI', 'EDI',
                  'EIP', 'EFL', 'CS', 'SS', 'DS', 'ES', 'FS', 'GS')


def parse_stop_reply(reply):
    """Decode an RSP stop reply (S/T/W/X) into signal, watch hit and fields."""
    kind = reply[:1]
    if kind not in ('S', 'T', 'W', 'X') or len(reply) < 3:
        raise ProtocolError(f'Unexpected GDB stop reply {reply[:40]!r}')
    try:
        result = {'kind': kind, 'signal': int(reply[1:3], 16)}
    except ValueError as exc:
        raise ProtocolError('Invalid GDB stop signal') from exc
    fields = {}
    for part in reply[3:].split(';'):
        if ':' in part:
            key, value = part.split(':', 1)
            fields[key] = value
    for key in ('watch', 'rwatch', 'awatch'):
        if key in fields:
            try:
                result['watch'] = {'type': key, 'address': int(fields[key], 16)}
            except ValueError as exc:
                raise ProtocolError('Invalid GDB watch address') from exc
    if 'thread' in fields:
        result['thread'] = fields['thread']
    return result


class GDB:
    def __init__(self, host, port, timeout=10):
        self.host, self.port, self.timeout = host, port, timeout
        self.reader = self.writer = None
        self.broken = False

    async def connect(self):
        self.reader, self.writer = await asyncio.wait_for(
            asyncio.open_connection(self.host, self.port, limit=256 * 1024), self.timeout)

    async def request(self, payload):
        async with asyncio.timeout(self.timeout):
            return await self._exchange(payload)

    async def _exchange(self, payload):
        body = payload.encode('ascii')
        packet = b'$' + body + b'#' + f'{sum(body) & 255:02x}'.encode()
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
            # '#' is always escaped inside RSP payloads, so one bulk read suffices.
            try:
                encoded = (await self.reader.readuntil(b'#'))[:-1]
            except asyncio.LimitOverrunError as exc:
                raise ProtocolError('GDB packet exceeds limit') from exc
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
                raise GDBErrorReply(f'GDB error {decoded}')
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

    async def set_point(self, kind, address, length):
        await self._point('Z', kind, address, length)

    async def clear_point(self, kind, address, length):
        await self._point('z', kind, address, length)

    async def _point(self, verb, kind, address, length):
        reply = await self.request(f'{verb}{kind},{address:x},{length:x}')
        # An empty reply is the RSP spelling of "unsupported packet".
        if reply == '':
            raise ProtocolError(f'GDB stub does not support {verb}{kind} breakpoints/watchpoints')
        if reply != 'OK':
            raise ProtocolError(f'GDB {verb}{kind} was not acknowledged: {reply[:40]}')

    async def registers(self):
        reply = await self.request('g')
        try:
            data = bytes.fromhex(reply[:len(I386_REGISTERS) * 8])
        except ValueError as exc:
            raise ProtocolError('Invalid GDB register hex') from exc
        if len(data) != len(I386_REGISTERS) * 4:
            raise ProtocolError('Short GDB register packet')
        return {name: int.from_bytes(data[4 * index:4 * index + 4], 'little')
                for index, name in enumerate(I386_REGISTERS)}

    async def resume_until_stop(self, timeout, interrupt=None):
        """Continue, wait for a stop reply; after timeout send ^C (then interrupt()).

        Returns (stop reply, interrupted). The pending reply is awaited by one
        task, never cancelled mid-packet unless every fallback fails.
        """
        task = asyncio.ensure_future(self._exchange('c'))
        try:
            if (await asyncio.wait({task}, timeout=timeout))[0]:
                return task.result(), False
            self.writer.write(b'\x03')
            await self.writer.drain()
            if (await asyncio.wait({task}, timeout=self.timeout))[0]:
                return task.result(), True
            if interrupt is not None:
                await interrupt()
                if (await asyncio.wait({task}, timeout=self.timeout))[0]:
                    return task.result(), True
            self.broken = True
            raise TimeoutError('GDB stub reported no stop after interrupt')
        except BaseException:
            if not task.done():
                self.broken = True
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            raise

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
