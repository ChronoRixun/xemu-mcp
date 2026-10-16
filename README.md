# xemu MCP

A Python MCP server over stdio for comparing an Xbox game with its port. It starts
and owns one local xemu process, uses QMP for machine control and GDB RSP for
virtual memory, and quits that process on normal server shutdown. It never attaches
to an existing emulator. All debugging ports bind to `127.0.0.1`.

## Setup

Requires Python 3.11+, xemu, and your own legally obtained console boot files and
game. Windows is required for keyboard-controller delivery and the region-capture
screenshot fallback. QMP/GDB control works with stock xemu; the lossless
`xemu-screenshot` QMP command used by `xemu_screenshot` is provided only by the
fork at https://github.com/ChronoRixun/xemu — with stock xemu the tool falls back
to the Windows region capture described below.

```powershell
python -m venv .venv
.venv/Scripts/python -m pip install -e .
.venv/Scripts/python -m unittest discover -s tests -v
```

Keep all boot files, disc images, EEPROM, HDD, saves, symbols, logs, memory dumps
and screenshots **outside this repository**. Copy your HDD and EEPROM into a
private runtime directory before testing; snapshots write into the qcow2 HDD.
Use an absolute-path xemu TOML config pointing to those private copies. Bind the
Keyboard controller to port 1 (other ports may be empty):

```toml
[general]
show_welcome = false

[general.updates]
check = false

[input]
auto_bind = false

[input.bindings]
port1 = "keyboard"

[display.window]
fullscreen_on_startup = false
startup_size = "1280x720"

[display.ui]
show_menubar = false
```

These settings keep unattended controls on the guest. xemu's UI suppresses guest
controller reports while ImGui navigation is active. In the live test, the
automatic update dialog swallowed otherwise-correct SDL A presses; disabling
update checks fixed it. Close any other emulator dialogs before testing.
The server reads the supplied config and does not rewrite these settings.

Create a private JSON server config, for example `C:/private/xemu/server.json`:

```json
{
  "executable": "C:/emulators/xemu/xemu.exe",
  "runtime_dir": "C:/private/xemu/runtime",
  "qmp_port": 4444,
  "gdb_port": 1234,
  "allow_write": false,
  "startup_timeout": 60,
  "allowed_game_roots": [],
  "protected_processes": []
}
```

Optional `symbols_file` points to a private JSON object mapping names to addresses.
Optional `audiodev` is a single `-audiodev` option value, for example
`"sdl,id=capture0"`, appended to the emulator command line so that audio capture
has a named backend to attach to; `"none,id=capture0"` captures without playing
sound on the host. Optional `snapshot_catalog` is an absolute `.json` path outside
this repository (its directory must exist) where snapshot tags, descriptions,
zones and notes are recorded; see *Snapshot catalogue*. No symbols or console/game files are distributed here. Unknown
config keys are errors; `allow_write` must be a JSON boolean. Optional
`protected_processes` is a list of process names (for example `["ExampleGame.exe"]`)
that must not run while the emulator runs: while any listed process is running
outside `allowed_game_roots`, `xemu_start` refuses to start. The default is empty.
The same list can be supplied without editing the config file via the
`XEMU_MCP_PROTECTED_PROCESSES` environment variable (comma- or semicolon-separated
process names); a nonempty config list takes precedence. These are the
complete config keys.

Add this to your MCP client's configuration after replacing the example paths:

```json
{
  "mcpServers": {
    "xemu": {
      "command": "C:/path/to/xemu-mcp/.venv/Scripts/python.exe",
      "args": ["-m", "xemu_mcp"],
      "env": {"XEMU_MCP_CONFIG": "C:/private/xemu/server.json"}
    }
  }
}
```

The package must be installed into that interpreter (`pip install -e .`); it then
works from any client working directory. This project does not register itself in
any client configuration. Stdout is only MCP; diagnostics go to stderr.

## Tools

| Tool | Arguments / result |
| --- | --- |
| `xemu_start` | `config_path`, optional `disc_path`; start process, wait for both protocols, return PID/status |
| `xemu_stop` | Quit only the owned process; fall back to terminating that PID if QMP fails |
| `xemu_status` | Owned PID, QMP run state, ports, writes flag, screenshot capability, input backend |
| `xemu_pause`, `xemu_resume` | Stop / continue guest execution |
| `xemu_press` | `buttons` list, `hold_ms` (1–10000); simultaneous digital controller inputs, always release |
| `xemu_read` | `address` string, `length` (1–65536), optional `decode_as`; virtual memory hex + decode |
| `xemu_read_physical` | Same arguments; physical memory through HMP `xp` |
| `xemu_write` | `address`, `hex_data`; disabled by default; virtual write with read-back verification |
| `xemu_registers` | Parsed register hex values plus full HMP text |
| `xemu_save_state` | `tag`, optional `description`, `zone`, `notes`; qcow2 snapshot using HMP savevm, recorded in the catalogue |
| `xemu_load_state`, `xemu_delete_state` | `tag`; HMP loadvm/delvm; catalogue `last_loaded` / entry removal |
| `xemu_state_list` | Optional `query` substring, `zone`, `check_emulator`; search the private snapshot catalogue |
| `xemu_state_annotate` | `tag`, optional `description`, `zone`, `notes`; edit a catalogue entry only |
| `xemu_input_sequence` | `steps` list of `{buttons or button, hold_ms, wait_ms}`, optional `dry_run`; scripted controller input |
| `xemu_mem_search` | `address`, `length` (1–64 MiB), `value_type`, `value`, optional `tolerance`, `alignment`, `max_results` (1–1000) |
| `xemu_mem_capture` | `address`, `length` (1–16 MiB), optional `value_type` (default `u32`), `alignment`; returns a handle |
| `xemu_mem_diff` | `handle`, optional `max_results`, `rebase`; changed typed positions with old/new values |
| `xemu_mem_narrow` | `handle`, `condition`, optional `value`, `tolerance`, `max_results`; cheat-finder candidate filter |
| `xemu_mem_captures`, `xemu_mem_release` | List handles / release `handle` |
| `xemu_read_struct` | `address`, `fields` (`name:type@offset` list), optional `structs`, `max_depth` (0–4) |
| `xemu_break_set` | `address`, optional `kind` (`exec`/`write`/`read`/`access`), `length` (1/2/4/8); register a point |
| `xemu_break_clear`, `xemu_break_list` | Optional `point_id` (all when omitted) / list points with hit counts |
| `xemu_break_wait` | Optional `timeout_s` (0.05–120), `leave` (`paused`/`running`), `stack_bytes` (0–1024), `window_bytes` (0–4096) |
| `xemu_screenshot` | Absolute PNG `filename`; optional `include_base64`, `bring_to_front` |
| `xemu_symbols` | Optional case-insensitive name substring `query` in private symbols file |
| `xemu_audio_devices` | List running QEMU audiodevs and their ids |
| `xemu_audio_capture_start` | Absolute `.wav` `filename`; optional `audiodev`, `frequency` (4000–192000), `bits` (8/16), `channels` (1/2) |
| `xemu_audio_capture_stop` | `capture_index` (0–9); stop capture and finalize the WAV header |

Addresses accept decimal or `0x` hexadecimal and must fit 32-bit memory. Decodes
are little-endian `u8`, `u16`, `u32`, `f32`, or bounded UTF-8 `cstring`. Numeric
lengths must be aligned to the selected type; nonfinite floats become JSON `null`.
Virtual reads/writes pause the machine and explicitly restore its previous run
state. Running callers detach GDB; paused callers close its socket without a
detach packet, because detach resumes execution in xemu. SDK cancellation shields
the bounded cleanup. Physical reads/registers
do not halt it: pause explicitly to compare multiple stable samples.

Write failures may leave partial changes; there is no rollback. Use snapshots
before experimental writes. Snapshot tags allow only 1–64 ASCII letters, digits,
hyphens and underscores. Monitor command injection and arbitrary HMP commands are
not exposed. HMP snapshot errors are detected even when QMP reports success.
Save while the guest is running: paused HMP saves stalled on subsequent resume in
both tested builds. `xemu_save_state` rejects paused callers with an explicit
error; it does not briefly resume them or silently change their state.

`xemu_start` waits for port/protocol readiness, **not** the game's menu. A running
guest, readable header and visible menu are separate observations. It refuses
any existing xemu process or occupied port, and refuses to start while any process
listed in `protected_processes` (or `XEMU_MCP_PROTECTED_PROCESSES`) is running,
unless it runs from an explicitly allowed test-build root. Processes you list
there are never signalled or closed by the server — listing one only blocks
`xemu_start`. An explicit private
`allowed_game_roots` list may permit other agents' isolated test builds to coexist;
never list your game or play-build directories. It does not stop other games. Abruptly killing the MCP
server can bypass normal shutdown; identify the owned PID before manual cleanup.

## Input and screenshots

Controller names: `a`, `b`, `x`, `y`, `start`, `back`, `white`, `black`,
`lstick_btn`, `rstick_btn`, `guide`, `dpad_up/down/left/right`,
`lstick_up/down/left/right`, `rstick_up/down/left/right`, `ltrigger`, `rtrigger`.
Use each slash-separated spelling as a separate name, e.g. `lstick_left`.
Sticks/triggers are full digital deflections, not fractional analog values.

Defaults follow [xemu's controller documentation](https://xemu.app/docs/controller/)
and the [v0.8.136 settings schema](https://github.com/xemu-project/xemu/blob/v0.8.136/config_spec.yml).
Custom `[input.keyboard_controller_scancode_map]` SDL-scancode overrides are read
from the supplied xemu config. Common letters, digits, punctuation, function,
navigation and modifier keys have Windows translations; unsupported remaps fail
before pressing anything. No configuration or registry controller bindings are
changed by this server.

xemu's controller state comes from SDL's host keyboard state, rather than the
emulated keyboard events of QMP `sendkey` / `input-send-event`
([input source](https://github.com/xemu-project/xemu/blob/v0.8.136/ui/xemu-input.c)).
Input therefore uses key-down/up window messages targeted to the owned xemu SDL
window, with bounded synchronous delivery and SDL's virtual-key path; it does not
inject global keys into whatever application has focus. The child process uses
`SDL_WINDOWS_RAW_KEYBOARD=0`. Verify a menu selection
changes before relying on input for evidence. A successful delivery result alone
does not prove the game consumed the input.

Screenshot capability is detected using QMP `query-commands`. The
`xemu-screenshot` QMP command is added by the xemu fork at
https://github.com/ChronoRixun/xemu; stock xemu does not provide it. If the
running build provides the `xemu-screenshot` command, it is used and is passed
a `filename` argument;
errors from it propagate rather than silently falling back. Otherwise Windows
captures the visible client region, including xemu overlays and scaling. This is
**not guest-resolution capture**. The window must be restored and unobscured;
visibility samples reject an obviously covered region, but partial occlusion
between samples remains possible. `bring_to_front=true` briefly raises only the
owned window without changing keyboard focus, then restores its topmost flag.
The optional base64 return is limited to 16 MiB. Screenshots are saved locally;
they are not added to Git. A fresh PNG is validated before atomically replacing
the destination, so failed captures cannot return a stale existing image.
Windows may refuse raising a window from a hidden stdio host. If that happens,
make the owned window visible before calling the fallback. The QMP screenshot
command avoids this desktop dependency.

## Audio capture

`xemu_audio_capture_start` / `xemu_audio_capture_stop` drive QEMU's HMP
`wavcapture` / `stopcapture` through QMP `human-monitor-command`, writing a WAV
file on the host while the guest runs. Capture taps the emulated audio mixer:
it records what the game actually outputs, including looped sounds, without
depending on host speakers, desktop focus or WASAPI loopback.

`wavcapture` requires a named audiodev. Query `xemu_audio_devices` first; when
exactly one audiodev runs, its id is selected automatically. Emulators started
on the legacy default audio path may expose none — in that case set `"audiodev"`
in the private server config (for example `"sdl,id=capture0"`, or
`"none,id=capture0"` for silent capture) and restart the emulator. Both commands
are deprecated upstream since v10.2 but present in the tested builds; monitor
deprecation warnings are tolerated, other non-empty monitor output is an error.
The WAV header is finalized only when capture stops: stop before reading the
file, and before stopping the emulator. Capture paths must be absolute, end in
`.wav`, contain no whitespace (HMP command line) and have an existing parent
directory. Keep captures outside this repository; recorded game audio is game
content.

## Memory research

All research reads use the same GDB session discipline as `xemu_read`: the guest
is paused for the whole operation (so one search, capture or struct read is a
consistent sample) and its previous run state is restored afterwards. Typed
values are little-endian `u8`, `i8`, `u16`, `i16`, `u32`, `i32` or `f32`.

`xemu_mem_search` scans up to 64 MiB in 64 KiB GDB reads (512-byte packets).
Integers accept decimal or `0x` strings or JSON numbers and must fit the type;
`f32` matches `abs(value - target) <= tolerance` (finite values only). A
`pattern` value is hex pairs with `??` wildcards, e.g. `"8b 45 ?? 89"` (1–256
bytes, at least one fixed). `alignment` is 1, 2 or 4 relative to absolute
addresses (default: the type size; 1 for patterns). Matches may straddle chunk
boundaries. A chunk the stub refuses is retried page by page; unreadable 4 KiB
pages are skipped and listed in `unreadable`. Scanning stops after `max_results`
and sets `truncated`. Expect large ranges to take tens of seconds while the
guest is paused.

`xemu_mem_capture` stores up to 16 MiB server-side (at most 8 handles, in
memory only, never on disk) with a type and alignment. `xemu_mem_diff` re-reads
and lists positions whose bytes changed with old/new typed values (`rebase`
makes the new read the baseline). `xemu_mem_narrow` is a cheat-finder step:
`changed`, `unchanged`, `increased`, `decreased` (against the previous read) or
`equals` (`value`, with `tolerance` for `f32`). The first narrow considers every
aligned readable position; each narrow keeps the surviving candidates and makes
its read the next baseline. Typical flow: capture, take damage in game, narrow
`decreased`, wait, narrow `unchanged`, then `equals` the displayed number.

`xemu_read_struct` decodes fields `name:type@offset`, where type is a scalar,
`scalar[N]` (N ≤ 256), `cstring[N]` / `bytes[N]` (N ≤ 1024), `ptr` (hex) or
`ptr->Name` to follow into `structs[Name]`. Each struct spans at most 4096 bytes;
following stops at `max_depth` hops, 64 structs or 64 KiB in total. Null pointers
decode as `null`; cycles, depth cut-offs and unreadable targets are reported in
place. Example: `fields=["hp:f32@0x40", "pos:f32[3]@0x10", "next:ptr->Actor@0x4"]`,
`structs={"Actor": ["hp:f32@0x40", "next:ptr->Actor@0x4"]}`.

## Breakpoints and watchpoints

`xemu_break_set` registers points server-side: `exec` (GDB `Z0`), `write`
(`Z2`), `read` (`Z3`) or `access` (`Z4`); watchpoints are 1, 2, 4 or 8 bytes,
aligned. Nothing is inserted into the guest at that time. `xemu_break_wait`
pauses, sends `?` (QEMU's stub clears stale debugger points on it), inserts every
point, continues and waits up to `timeout_s`. A hit returns the stop reply,
matched point ids, the 16 general registers from the GDB `g` packet, a
`stack_bytes` window at ESP (hex and u32 words) and a `window_bytes` window at
the watch address (aligned down to 16) or EIP. On timeout it sends GDB `^C`,
then QMP `stop` if the stub stays silent, and reports `interrupted`.

Points are **always removed** before the call returns: by `z` packets, or — if
the GDB stream broke — over a fresh connection (`?` plus `z`). The final state is
always the requested `leave`: `paused` (default, closes GDB without the resuming
`D`) or `running` (detaches). This applies to errors and cancellation too, so a
guest is never left halted in a debugger stop with nobody attached. x86
watchpoints report after the accessing instruction, so EIP is normally the next
instruction. The MCP client's own request timeout must exceed `timeout_s`.

## Scripted input

`xemu_input_sequence` runs up to 200 steps (≤ 300 s in total) through the same
targeted delivery as `xemu_press`. Each step is `{"buttons": [...]}` or
`{"button": "a"}` with `hold_ms` (1–10000, default 100) and `wait_ms` after
release (0–60000, default 0). Every step is validated and resolved to scancodes
before any key is sent; keys are released after each hold, including on
cancellation, and the server lock is held for the whole sequence. `dry_run=true`
returns the resolved plan without an emulator or window (Linux-friendly). As with
`xemu_press`, delivery does not prove the game consumed the input.

## Snapshot catalogue

With `snapshot_catalog` configured, successful `xemu_save_state` calls record
`{tag, description, zone, notes, created, updated}` in that private JSON file
(written atomically); `xemu_load_state` adds `last_loaded` and
`xemu_delete_state` removes the entry. A catalogue failure after a successful
HMP command is returned as `catalog_error` rather than hiding the snapshot
result. `xemu_state_list` searches tag/description/zone/notes and filters by
zone; `check_emulator=true` compares against HMP `info snapshots` (`present`
per entry plus `uncatalogued` tags). `xemu_state_annotate` edits metadata
without contacting the emulator. The catalogue must be outside this repository;
descriptions are your notes, so keep game text out of the repo as usual.

## Real-emulator smoke test

This is opt-in: it launches xemu, sends real controller input, saves/loads a
snapshot, captures private evidence, and closes its process. Set the waits for
your game and visually inspect `before.png` / `after.png`; it does not infer game
progress from screenshots or a generic header alone.

```powershell
.venv/Scripts/python scripts/smoke.py `
  --server-config C:/private/xemu/server.json `
  --config C:/private/xemu/xemu.toml `
  --output-dir C:/private/xemu/evidence `
  --boot-wait 75 --press a --assert-read-match
```

Optional `--skip-with-start` sends Start before the baseline capture. The default
read is 16 bytes at the usual virtual XBE header location. `--address` / `--length`
select another range. The smoke script records memory/register samples, saves
while running, presses a button, loads the snapshot and records the restored
state. Samples are not atomic with the snapshot: register equality is not claimed,
and mutable ranges can legitimately differ. `--assert-read-match` is suitable for
a known stable range such as the header. Inspect the menu/scene before, after and
restored images as well. It uses a timestamped tag and removes its snapshot
unless `--keep-snapshot` is supplied. `smoke.json` contains the numeric evidence;
it remains outside the repo and is written even on failure. Menu progression must
also be checked visually.

## Verification

```powershell
.venv/Scripts/python tests/unit/run.py
.venv/Scripts/python tools/check_no_game_content.py --all
.venv/Scripts/python tools/check_no_game_content.py --self-test
```

Tests use synthetic socket peers and invented bytes; no console assets required.
They cover QMP negotiation/event handling/timeouts, GDB framing/checksums/RLE,
memory errors/state restoration, write guards/read-back, snapshot validation,
controller release on cancellation, and a real MCP stdio subprocess. The
research tests use a fake QMP/GDB peer with unmapped pages, `Z`/`z` points,
continue, `^C` and a wedged-stub mode to cover search, capture/narrow, struct
decoding, breakpoint arming/removal and final run state, the catalogue and
input sequences. The content
validator checks the Git index against a source-file allowlist and rejects
binary/runtime files and private machine paths. It is a cheap guard, not proof
of provenance; review the staged diff before pushing.

## License

[Apache License 2.0](LICENSE). Copyright 2026 ChronoRixun.
