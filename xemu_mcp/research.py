"""Pure helpers for memory search, capture narrowing, struct specs, input plans and
the private snapshot catalogue. No emulator I/O happens in this module."""

import json
import math
import os
import re
import struct
import time
from array import array
from dataclasses import dataclass, field
from pathlib import Path

PAGE = 4096
CHUNK = 64 * 1024
SEARCH_LIMIT = 64 * 1024 * 1024
CAPTURE_LIMIT = 16 * 1024 * 1024
CAPTURE_HANDLES = 8
RESULT_LIMIT = 1000

# Little-endian scalar types shared by search, captures and struct fields.
SCALARS = {'u8': '<B', 'i8': '<b', 'u16': '<H', 'i16': '<h',
           'u32': '<I', 'i32': '<i', 'f32': '<f'}
CONDITIONS = ('changed', 'unchanged', 'increased', 'decreased', 'equals')
REPOSITORY = Path(__file__).resolve().parents[1]


def scalar_size(kind):
    if kind not in SCALARS:
        raise ValueError(f'value_type must be one of {", ".join(SCALARS)}')
    return struct.calcsize(SCALARS[kind])


def json_value(value):
    # JSON has no NaN/Infinity literals.
    return None if isinstance(value, float) and not math.isfinite(value) else value


def check_int(name, value, low, high):
    if type(value) is not int or not low <= value <= high:
        raise ValueError(f'{name} must be an integer from {low} to {high}')
    return value


def check_alignment(alignment, default):
    if alignment is None:
        return default
    if alignment not in (1, 2, 4):
        raise ValueError('alignment must be 1, 2 or 4')
    return alignment


def parse_number(kind, value):
    """Parse a typed search/equals operand from a JSON number or string."""
    if isinstance(value, bool) or value is None:
        raise ValueError(f'{kind} comparison needs a numeric value')
    if kind == 'f32':
        try:
            number = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError('f32 value must be a finite number') from exc
        if not math.isfinite(number):
            raise ValueError('f32 value must be a finite number')
        return number
    if isinstance(value, float):
        if not value.is_integer():
            raise ValueError(f'{kind} value must be an integer')
        value = int(value)
    if isinstance(value, str):
        try:
            value = int(value, 0)
        except ValueError as exc:
            raise ValueError(f'{kind} value must be decimal or 0x-prefixed hexadecimal') from exc
    try:
        struct.pack(SCALARS[kind], value)
    except struct.error as exc:
        raise ValueError(f'{value} does not fit {kind}') from exc
    return value


def parse_pattern(text):
    """Hex byte pattern with ?? wildcards, e.g. '8b 45 ?? 89' -> compiled regex."""
    if not isinstance(text, str):
        raise TypeError('pattern value must be a hex string such as "8b 45 ?? 89"')
    compact = re.sub(r'\s+', '', text)
    if not compact or len(compact) % 2 or len(compact) > 512:
        raise ValueError('pattern must be 1 to 256 hex byte pairs, ?? for any byte')
    parts = []
    for index in range(0, len(compact), 2):
        pair = compact[index:index + 2]
        if pair == '??':
            parts.append(b'.')
        elif re.fullmatch(r'[0-9a-fA-F]{2}', pair):
            parts.append(re.escape(bytes.fromhex(pair)))
        else:
            raise ValueError(f'Invalid pattern byte {pair!r}; use hex pairs or ??')
    if all(part == b'.' for part in parts):
        raise ValueError('pattern needs at least one fixed byte')
    return re.compile(b'(?=(' + b''.join(parts) + b'))', re.DOTALL), len(parts)


class Matcher:
    """Find aligned typed values or wildcard byte patterns in memory chunks."""

    def __init__(self, value_type, value, tolerance=0.0, alignment=None):
        if not isinstance(tolerance, (int, float)) or isinstance(tolerance, bool) \
                or not math.isfinite(tolerance) or tolerance < 0:
            raise ValueError('tolerance must be a finite number >= 0')
        self.value_type = value_type
        self.tolerance = float(tolerance)
        if value_type == 'pattern':
            self.regex, self.size = parse_pattern(value)
            self.alignment = check_alignment(alignment, 1)
            self.target = None
            return
        self.size = scalar_size(value_type)
        self.alignment = check_alignment(alignment, self.size)
        self.target = parse_number(value_type, value)
        if value_type != 'f32' and self.tolerance:
            raise ValueError('tolerance applies only to f32 searches')
        if value_type == 'f32':
            self.regex = None
        else:
            packed = struct.pack(SCALARS[value_type], self.target)
            self.regex = re.compile(b'(?=(' + re.escape(packed) + b'))', re.DOTALL)

    def find(self, address, data):
        """Yield (absolute address, value) for matches in data, in address order."""
        if self.regex is not None:
            for match in self.regex.finditer(data):
                if (address + match.start()) % self.alignment == 0:
                    found = match.group(1)
                    value = found.hex() if self.value_type == 'pattern' \
                        else struct.unpack(SCALARS[self.value_type], found)[0]
                    yield address + match.start(), value
            return
        hits = []
        for shift in range(4):
            if (address + shift) % self.alignment:
                continue
            usable = (len(data) - shift) // 4 * 4
            if usable <= 0:
                continue
            for index, (value,) in enumerate(struct.iter_unpack('<f', data[shift:shift + usable])):
                if math.isfinite(value) and abs(value - self.target) <= self.tolerance:
                    hits.append((address + shift + 4 * index, value))
        yield from sorted(hits)


def merge_holes(pages, limit=64):
    """Summarise unreadable page numbers as at most `limit` hex ranges."""
    ranges = []
    for page in sorted(pages):
        if ranges and ranges[-1][1] == page * PAGE:
            ranges[-1][1] += PAGE
        else:
            ranges.append([page * PAGE, (page + 1) * PAGE])
    return [{'start': hex(start), 'end': hex(end)} for start, end in ranges[:limit]], len(ranges) > limit


@dataclass
class Capture:
    """A server-side memory capture with typed cheat-finder candidates."""
    handle: str
    address: int
    length: int
    value_type: str
    alignment: int
    data: bytes
    holes: set
    created: float = field(default_factory=time.time)
    candidates: object = None  # None means every aligned, readable position.
    narrowed: int = 0

    @property
    def size(self):
        return scalar_size(self.value_type)

    def positions(self, start=0, end=None):
        end = self.length - self.size + 1 if end is None else min(end, self.length - self.size + 1)
        start = max(0, start)
        first = start + (-(self.address + start)) % self.alignment
        return range(first, max(first, end), self.alignment)

    def readable(self, offset, holes):
        first = (self.address + offset) // PAGE
        last = (self.address + offset + self.size - 1) // PAGE
        return all(page not in self.holes and page not in holes for page in range(first, last + 1))

    def value(self, data, offset):
        return struct.unpack_from(SCALARS[self.value_type], data, offset)[0]

    def summary(self):
        holes, more = merge_holes(self.holes, 8)
        return {'handle': self.handle, 'address': hex(self.address), 'length': self.length,
                'value_type': self.value_type, 'alignment': self.alignment,
                'candidates': self.candidate_count(), 'narrowed': self.narrowed,
                'unreadable': holes, 'unreadable_truncated': more,
                'created': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(self.created))}

    def candidate_count(self):
        if self.candidates is not None:
            return len(self.candidates)
        unreadable = set()
        for page in self.holes:
            start = page * PAGE - self.address
            unreadable.update(self.positions(start - self.size + 1, start + PAGE))
        return len(self.positions()) - len(unreadable)

    def _blocks(self, new, new_holes, want_changed):
        """Yield offsets whose bytes changed (or did not), skipping equal blocks quickly."""
        size = self.size
        for start in range(0, self.length, CHUNK):
            end = min(self.length, start + CHUNK)
            tail = min(self.length, end + size - 1)
            pages = range((self.address + start) // PAGE, (self.address + tail - 1) // PAGE + 1)
            clean = not any(page in self.holes or page in new_holes for page in pages)
            if clean and self.data[start:tail] == new[start:tail]:
                if not want_changed:
                    yield from self.positions(start, end)
                continue
            for offset in self.positions(start, end):
                if not self.readable(offset, new_holes):
                    continue
                if (self.data[offset:offset + size] != new[offset:offset + size]) == want_changed:
                    yield offset

    def changed(self, new, new_holes):
        return self._blocks(new, new_holes, True)

    def narrow(self, new, new_holes, condition, operand=None, tolerance=0.0):
        if condition not in CONDITIONS:
            raise ValueError(f'condition must be one of {", ".join(CONDITIONS)}')
        if condition == 'equals':
            operand = parse_number(self.value_type, operand)
        if self.candidates is not None:
            source = (offset for offset in self.candidates if self.readable(offset, new_holes))
        elif condition == 'unchanged':
            source = self._blocks(new, new_holes, False)
        elif condition == 'equals':
            source = (offset for offset in self.positions() if self.readable(offset, new_holes))
        else:
            source = self.changed(new, new_holes)
        kept = array('I')
        for offset in source:
            old, current = self.value(self.data, offset), self.value(new, offset)
            if condition == 'changed':
                keep = self.data[offset:offset + self.size] != new[offset:offset + self.size]
            elif condition == 'unchanged':
                keep = self.data[offset:offset + self.size] == new[offset:offset + self.size]
            elif condition == 'equals':
                keep = (math.isfinite(current) and abs(current - operand) <= tolerance
                        if self.value_type == 'f32' else current == operand)
            else:
                comparable = all(map(math.isfinite, (old, current))) if self.value_type == 'f32' else True
                keep = comparable and (current > old if condition == 'increased' else current < old)
            if keep:
                kept.append(offset)
        self.candidates = kept
        self.data, self.holes = bytes(new), set(new_holes)
        self.narrowed += 1
        return kept


# Struct field specs: "name:type@offset", type is a scalar, scalar[N],
# cstring[N], bytes[N], ptr, or ptr->StructName.
FIELD = re.compile(r'\s*([A-Za-z_][A-Za-z0-9_]{0,63})\s*:\s*([^@]+?)\s*@\s*(0x[0-9a-fA-F]+|\d+)\s*')
STRUCT_SPAN = 4096


@dataclass
class Field:
    name: str
    kind: str
    offset: int
    count: int = 1
    target: str | None = None

    @property
    def size(self):
        if self.kind in ('cstring', 'bytes'):
            return self.count
        if self.kind == 'ptr':
            return 4
        return scalar_size(self.kind) * self.count


def parse_fields(spec, names=()):
    if not isinstance(spec, list) or not 1 <= len(spec) <= 64:
        raise ValueError('A struct spec is a list of 1 to 64 "name:type@offset" strings')
    fields, seen = [], set()
    for text in spec:
        match = FIELD.fullmatch(text) if isinstance(text, str) else None
        if not match:
            raise ValueError(f'Invalid field {text!r}; expected name:type@offset')
        name, kind, offset = match[1], match[2].replace(' ', ''), int(match[3], 0)
        if name in seen:
            raise ValueError(f'Duplicate field name {name!r}')
        seen.add(name)
        if pointer := re.fullmatch(r'ptr(?:->([A-Za-z_][A-Za-z0-9_]{0,63}))?', kind):
            if pointer[1] is not None and pointer[1] not in names:
                raise ValueError(f'Field {name!r} points to undefined struct {pointer[1]!r}')
            item = Field(name, 'ptr', offset, target=pointer[1])
        elif array := re.fullmatch(r'(u8|i8|u16|i16|u32|i32|f32|cstring|bytes)(?:\[(\d{1,4})\])?', kind):
            count = int(array[2]) if array[2] else 1
            limit = 1024 if array[1] in ('cstring', 'bytes') else 256
            if not 1 <= count <= limit or (array[1] in ('cstring', 'bytes') and not array[2]):
                raise ValueError(f'Field {name!r}: {array[1]} needs a count from 1 to {limit}')
            item = Field(name, array[1], offset, count)
        else:
            raise ValueError(f'Field {name!r}: unknown type {kind!r}')
        if item.offset + item.size > STRUCT_SPAN:
            raise ValueError(f'Field {name!r} ends beyond the {STRUCT_SPAN}-byte struct span limit')
        fields.append(item)
    return fields


def decode_field(item, data):
    raw = data[item.offset:item.offset + item.size]
    if item.kind == 'cstring':
        return raw.split(b'\0', 1)[0].decode('utf-8', errors='replace')
    if item.kind == 'bytes':
        return raw.hex()
    if item.kind == 'ptr':
        return int.from_bytes(raw, 'little')
    values = [json_value(v[0]) for v in struct.iter_unpack(SCALARS[item.kind], raw)]
    return values if item.count > 1 else values[0]


def span(fields):
    return max(item.offset + item.size for item in fields)


# Scripted controller input steps.
SEQUENCE_STEPS = 200
SEQUENCE_TOTAL_MS = 300_000


def plan_sequence(steps, resolve):
    """Validate every step before any input; resolve(buttons) -> scancodes."""
    if not isinstance(steps, list) or not 1 <= len(steps) <= SEQUENCE_STEPS:
        raise ValueError(f'steps must be a list of 1 to {SEQUENCE_STEPS} step objects')
    plan, total = [], 0
    for index, step in enumerate(steps):
        if not isinstance(step, dict) or set(step) - {'buttons', 'button', 'hold_ms', 'wait_ms'}:
            raise ValueError(f'Step {index}: use only buttons/button, hold_ms and wait_ms')
        if ('buttons' in step) == ('button' in step):
            raise ValueError(f'Step {index}: give exactly one of buttons (list) or button (name)')
        buttons = step['buttons'] if 'buttons' in step else [step['button']]
        if not isinstance(buttons, list) or not all(isinstance(b, str) for b in buttons):
            raise ValueError(f'Step {index}: buttons must be a list of names')
        hold = step.get('hold_ms', 100)
        wait = step.get('wait_ms', 0)
        try:
            check_int('hold_ms', hold, 1, 10000)
            check_int('wait_ms', wait, 0, 60000)
            keys = resolve(buttons)
        except ValueError as exc:
            raise ValueError(f'Step {index}: {exc}') from exc
        total += hold + wait
        plan.append({'buttons': buttons, 'sdl_scancodes': keys, 'hold_ms': hold, 'wait_ms': wait})
    if total > SEQUENCE_TOTAL_MS:
        raise ValueError(f'Sequence lasts {total} ms; the limit is {SEQUENCE_TOTAL_MS} ms')
    return plan, total


# Private snapshot catalogue (JSON sidecar outside the repository).
CATALOG_TEXT = {'description': 500, 'zone': 100, 'notes': 4000}


def utc_now():
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())


class SnapshotCatalog:
    def __init__(self, path):
        candidate = Path(path).expanduser()
        if not candidate.is_absolute() or candidate.suffix.lower() != '.json':
            raise ValueError('snapshot_catalog must be an absolute .json path')
        self.path = candidate.resolve()
        if self.path == REPOSITORY or REPOSITORY in self.path.parents:
            raise ValueError('snapshot_catalog must be outside the xemu-mcp repository')

    def load(self):
        if not self.path.exists():
            return {}
        data = json.loads(self.path.read_text(encoding='utf-8-sig'))
        if not isinstance(data, dict) or not isinstance(data.get('snapshots', {}), dict):
            raise TypeError(f'Snapshot catalogue {self.path} is not a {{"snapshots": {{...}}}} object')
        return data.get('snapshots', {})

    def _store(self, entries):
        if not self.path.parent.is_dir():
            raise ValueError('Snapshot catalogue parent directory must already exist')
        temporary = self.path.with_name(f'.{self.path.name}.{os.getpid()}.tmp')
        temporary.write_text(json.dumps({'version': 1, 'snapshots': entries}, indent=2, sort_keys=True),
                             encoding='utf-8')
        temporary.replace(self.path)

    @staticmethod
    def check_text(values):
        for key, value in values.items():
            if value is not None and (not isinstance(value, str) or len(value) > CATALOG_TEXT[key]):
                raise ValueError(f'{key} must be a string of at most {CATALOG_TEXT[key]} characters')

    def record(self, tag, saved=False, **values):
        self.check_text(values)
        entries = self.load()
        now = utc_now()
        entry = entries.get(tag) or {'tag': tag, 'created': now}
        if saved:
            entry['created'] = now
        entry.update({key: value for key, value in values.items() if value is not None})
        entry['updated'] = now
        entries[tag] = entry
        self._store(entries)
        return entry

    def touch(self, tag, key):
        entries = self.load()
        if tag in entries:
            entries[tag][key] = utc_now()
            self._store(entries)
            return entries[tag]
        return None

    def remove(self, tag):
        entries = self.load()
        removed = entries.pop(tag, None)
        if removed is not None:
            self._store(entries)
        return removed

    def search(self, query='', zone=None):
        query = (query or '').lower()
        found = []
        for entry in self.load().values():
            if zone is not None and (entry.get('zone') or '').lower() != zone.lower():
                continue
            text = ' '.join(str(entry.get(key) or '') for key in ('tag', 'description', 'zone', 'notes'))
            if query in text.lower():
                found.append(entry)
        return sorted(found, key=lambda entry: entry.get('created', ''))
