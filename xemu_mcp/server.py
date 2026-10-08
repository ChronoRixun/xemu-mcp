"""MCP SDK entry point. Stdout is reserved for the stdio protocol."""

from contextlib import asynccontextmanager
from typing import Any
import anyio

from mcp.server.fastmcp import FastMCP

from .service import Config, Emulator

# Typed search operands: JSON integers stay exact; strings allow 0x hex or patterns.
Operand = str | int | float


def create_server(config=None):
    emulator = Emulator(config or Config.load())

    @asynccontextmanager
    async def lifespan(server):
        try:
            yield emulator
        finally:
            with anyio.CancelScope(shield=True):
                await emulator.cleanup()

    server = FastMCP('xemu', lifespan=lifespan)

    # SDK tool exceptions become isError=true responses, keeping failures
    # distinguishable from successful structured JSON results.
    @server.tool()
    async def xemu_start(config_path: str, disc_path: str | None = None) -> dict[str, Any]:
        """Start an owned xemu with a private TOML config; wait for local QMP/GDB.

        Refuses existing xemu or protected game processes. Optional disc_path
        overrides the DVD for this launch. Booting to the game menu takes longer
        than opening the ports; observe with screenshot/status afterwards.
        """
        return await emulator.start(config_path, disc_path)

    @server.tool()
    async def xemu_stop() -> dict[str, Any]:
        """Quit only the process started by this server; never attach to or stop another xemu."""
        return await emulator.stop()

    @server.tool()
    async def xemu_status() -> dict[str, Any]:
        """Report owned PID, guest run state, ports, input backend, and screenshot support."""
        return await emulator.status()

    @server.tool()
    async def xemu_pause() -> dict[str, Any]:
        """Pause the owned guest through QMP."""
        return await emulator.pause()

    @server.tool()
    async def xemu_resume() -> dict[str, Any]:
        """Resume the owned guest through QMP."""
        return await emulator.pause(resume=True)

    @server.tool()
    async def xemu_press(buttons: list[str], hold_ms: int = 100) -> dict[str, Any]:
        """Hold controller names simultaneously, then release, including on cancellation.

        Windows targeted keyboard input; Keyboard must be bound to port 1.
        Names: a,b,x,y,start,back,white,black,lstick_btn,rstick_btn,guide,
        dpad_up/down/left/right,lstick_up/down/left/right,
        rstick_up/down/left/right,ltrigger,rtrigger. Stick/trigger values are
        digital full deflection; hold_ms is 1..10000. No foreground key injection.
        """
        return await emulator.press(buttons, hold_ms)

    @server.tool()
    async def xemu_read(address: str, length: int, decode_as: str | None = None) -> dict[str, Any]:
        """Read 1..65536 virtual bytes through GDB; hex and optional little-endian decode.

        address is decimal or 0x-prefixed hex; decode_as: u8/u16/u32/f32/cstring.
        Pauses briefly, detaches and restores the previous run state. Nonfinite
        f32 values decode as null; strings are bounded by length, UTF-8 replacement.
        """
        return await emulator.read(address, length, decode_as)

    @server.tool()
    async def xemu_read_physical(address: str, length: int, decode_as: str | None = None) -> dict[str, Any]:
        """Read 1..65536 physical bytes using QMP HMP xp; optional little-endian decode.

        Physical addresses are distinct from virtual addresses. This does not
        halt the guest; pause explicitly if a stable multi-operation sample is needed.
        """
        return await emulator.read_physical(address, length, decode_as)

    @server.tool()
    async def xemu_write(address: str, hex_data: str) -> dict[str, Any]:
        """Write virtual memory only if allow_write=true in private server config.

        Payload is hex, 1..65536 bytes. Pauses, verifies read-back and restores
        run state. Failure can leave a partial write; there is no rollback.
        """
        return await emulator.write(address, hex_data)

    @server.tool()
    async def xemu_registers() -> dict[str, Any]:
        """Read CPU registers through QMP/HMP; return parsed hex values and raw text."""
        return await emulator.registers()

    @server.tool()
    async def xemu_save_state(tag: str, description: str | None = None, zone: str | None = None,
                              notes: str | None = None) -> dict[str, Any]:
        """Save a qcow2-backed snapshot. Tag: 1..64 ASCII letters/digits/underscore/hyphen.

        Requires a running guest (paused saves can stall xemu on resume).
        Use a private copy of the HDD; this writes a snapshot into it. With a
        private snapshot_catalog configured, description (<=500), zone (<=100)
        and notes (<=4000) are recorded with the creation time.
        """
        return await emulator.snapshot('save', tag, description, zone, notes)

    @server.tool()
    async def xemu_load_state(tag: str) -> dict[str, Any]:
        """Load a named qcow2 snapshot; guest execution and memory rewind to it.

        Records last_loaded in the private snapshot catalogue when configured.
        """
        return await emulator.snapshot('load', tag)

    @server.tool()
    async def xemu_delete_state(tag: str) -> dict[str, Any]:
        """Delete a named snapshot from the owned emulator's qcow2 HDD (and its catalogue entry)."""
        return await emulator.snapshot('delete', tag)

    @server.tool()
    async def xemu_state_list(query: str = '', zone: str | None = None,
                              check_emulator: bool = False) -> dict[str, Any]:
        """Search the private snapshot catalogue (tag/description/zone/notes substring).

        zone filters case-insensitively by exact zone. check_emulator=true also
        runs HMP 'info snapshots' on the owned emulator and marks each entry
        present/absent, listing uncatalogued tags. Needs snapshot_catalog config.
        """
        return await emulator.state_list(query, zone, check_emulator)

    @server.tool()
    async def xemu_state_annotate(tag: str, description: str | None = None, zone: str | None = None,
                                  notes: str | None = None) -> dict[str, Any]:
        """Create or edit a catalogue entry without touching the emulator; omitted fields are kept."""
        return emulator.state_annotate(tag, description, zone, notes)

    @server.tool()
    async def xemu_screenshot(filename: str, include_base64: bool = False,
                              bring_to_front: bool = False) -> dict[str, Any]:
        """Save PNG at an absolute path, optionally return base64 (max 16 MiB).

        Prefer detected xemu-screenshot QMP command. Otherwise capture the visible
        Windows client region (includes UI, not guest resolution). Fallback needs
        an unobscured window; bring_to_front may briefly change its z-order.
        """
        return await emulator.screenshot(filename, include_base64, bring_to_front)

    @server.tool()
    async def xemu_audio_devices() -> dict[str, Any]:
        """List running QEMU audiodevs (ids needed by xemu_audio_capture_start)."""
        return await emulator.audio_devices()

    @server.tool()
    async def xemu_audio_capture_start(filename: str, audiodev: str | None = None,
                                       frequency: int = 44100, bits: int = 16,
                                       channels: int = 2) -> dict[str, Any]:
        """Start capturing emulated audio to a .wav via HMP wavcapture over QMP.

        filename: absolute .wav path without whitespace; its directory must exist.
        audiodev: QEMU audiodev id; optional when exactly one is running (list
        with xemu_audio_devices). If none is running, set "audiodev" in the
        private server config (e.g. "sdl,id=capture0") and restart. Capture taps
        the audio mixer, so record only while the guest scene of interest runs;
        stop with xemu_audio_capture_stop before reading the file.
        """
        return await emulator.audio_capture_start(filename, audiodev, frequency, bits, channels)

    @server.tool()
    async def xemu_audio_capture_stop(capture_index: int = 0) -> dict[str, Any]:
        """Stop capture 0..9 via HMP stopcapture; finalizes the .wav header."""
        return await emulator.audio_capture_stop(capture_index)

    @server.tool()
    async def xemu_input_sequence(steps: list[dict[str, Any]], dry_run: bool = False) -> dict[str, Any]:
        """Run scripted controller steps through the same targeted delivery as xemu_press.

        steps: 1..200 objects {"buttons": [names] or "button": name, "hold_ms":
        1..10000 (default 100), "wait_ms": 0..60000 after release (default 0)};
        total <= 300000 ms. Every step is validated before any input; keys are
        always released, including on cancellation. dry_run=true only validates
        and returns the resolved plan (no emulator or window needed).
        """
        return await emulator.input_sequence(steps, dry_run)

    @server.tool()
    async def xemu_mem_search(address: str, length: int, value_type: str, value: Operand,
                              tolerance: float = 0.0, alignment: int | None = None,
                              max_results: int = 100) -> dict[str, Any]:
        """Search 1..64 MiB of virtual memory for a typed value or wildcard byte pattern.

        value_type: u8/i8/u16/i16/u32/i32/f32 or pattern. Integers accept decimal
        or 0x hex; f32 matches within tolerance; pattern is hex pairs with ??
        wildcards ("8b 45 ?? 89"). alignment 1/2/4 (default: type size, 1 for
        patterns). Reads in 64 KiB chunks while paused, skips unreadable 4 KiB
        pages (reported) and stops after max_results (1..1000).
        """
        return await emulator.search(address, length, value_type, value, tolerance, alignment, max_results)

    @server.tool()
    async def xemu_mem_capture(address: str, length: int, value_type: str = 'u32',
                               alignment: int | None = None) -> dict[str, Any]:
        """Capture 1..16 MiB of virtual memory server-side; returns a handle (max 8 kept).

        value_type (u8/i8/u16/i16/u32/i32/f32) and alignment define the typed
        positions used by xemu_mem_diff and xemu_mem_narrow. Unreadable pages
        are recorded and excluded. Nothing is written to disk.
        """
        return await emulator.capture(address, length, value_type, alignment)

    @server.tool()
    async def xemu_mem_diff(handle: str, max_results: int = 100, rebase: bool = False) -> dict[str, Any]:
        """Re-read a capture and report typed positions whose bytes changed (old/new values).

        rebase=true replaces the stored baseline with the new read. Candidate
        narrowing is not affected by a diff.
        """
        return await emulator.diff(handle, max_results, rebase)

    @server.tool()
    async def xemu_mem_narrow(handle: str, condition: str, value: Operand | None = None,
                              tolerance: float = 0.0, max_results: int = 100) -> dict[str, Any]:
        """Cheat-finder step: re-read a capture, keep candidates matching condition.

        condition: changed, unchanged, increased, decreased (vs the previous
        read) or equals (value; f32 within tolerance). The first narrow starts
        from every aligned readable position; each read becomes the next baseline.
        """
        return await emulator.narrow(handle, condition, value, tolerance, max_results)

    @server.tool()
    async def xemu_mem_captures() -> dict[str, Any]:
        """List server-side capture handles with region, type and candidate counts."""
        return emulator.captures_list()

    @server.tool()
    async def xemu_mem_release(handle: str) -> dict[str, Any]:
        """Forget a capture handle and its stored bytes."""
        return emulator.capture_release(handle)

    @server.tool()
    async def xemu_read_struct(address: str, fields: list[str], structs: dict[str, list[str]] | None = None,
                               max_depth: int = 1) -> dict[str, Any]:
        """Decode a guest struct from "name:type@offset" field specs in one paused read.

        Types: u8/i8/u16/i16/u32/i32/f32 with optional [N] array, cstring[N],
        bytes[N], ptr (hex) or ptr->Name to follow into structs[Name]. Struct
        span <= 4096 bytes; max_depth 0..4 pointer hops, 64 structs and 64 KiB
        total; null pointers are null, cycles and unreadable targets are reported.
        """
        return await emulator.read_struct(address, fields, structs, max_depth)

    @server.tool()
    async def xemu_break_set(address: str, kind: str = 'exec', length: int = 1) -> dict[str, Any]:
        """Register a breakpoint (exec, GDB Z0) or watchpoint (write Z2, read Z3, access Z4).

        Watchpoint length 1/2/4/8, address aligned to it; at most 16 points.
        Points are stored server-side and inserted only while xemu_break_wait runs.
        """
        return emulator.break_set(address, kind, length)

    @server.tool()
    async def xemu_break_clear(point_id: str | None = None) -> dict[str, Any]:
        """Remove one registered point by id, or all points when point_id is omitted."""
        return emulator.break_clear(point_id)

    @server.tool()
    async def xemu_break_list() -> dict[str, Any]:
        """List registered breakpoints/watchpoints and their hit counts."""
        return emulator.break_list()

    @server.tool()
    async def xemu_break_wait(timeout_s: float = 10, leave: str = 'paused', stack_bytes: int = 64,
                              window_bytes: int = 32) -> dict[str, Any]:
        """Arm all points, continue the guest until a hit or timeout_s (0.05..120), then disarm.

        On timeout the guest is interrupted (GDB ^C, then QMP stop). Returns
        stop reason, matched point ids, registers, stack_bytes (0..1024) at ESP
        and window_bytes (0..4096) at the watch address or EIP. Points are always
        removed; leave='paused' (default) or 'running' sets the final state.
        """
        return await emulator.break_wait(timeout_s, leave, stack_bytes, window_bytes)

    @server.tool()
    async def xemu_symbols(query: str = '') -> dict[str, Any]:
        """Filter symbols from an optional private JSON file outside the repository."""
        return emulator.symbols(query)

    return server
