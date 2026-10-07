"""MCP SDK entry point. Stdout is reserved for the stdio protocol."""

from contextlib import asynccontextmanager
from typing import Any
import anyio

from mcp.server.fastmcp import FastMCP

from .service import Config, Emulator


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
    async def xemu_save_state(tag: str) -> dict[str, Any]:
        """Save a qcow2-backed snapshot. Tag: 1..64 ASCII letters/digits/underscore/hyphen.

        Requires a running guest (paused saves can stall xemu on resume).
        Use a private copy of the HDD; this writes a snapshot into it.
        """
        return await emulator.snapshot('save', tag)

    @server.tool()
    async def xemu_load_state(tag: str) -> dict[str, Any]:
        """Load a named qcow2 snapshot; guest execution and memory rewind to it."""
        return await emulator.snapshot('load', tag)

    @server.tool()
    async def xemu_delete_state(tag: str) -> dict[str, Any]:
        """Delete a named snapshot from the owned emulator's qcow2 HDD."""
        return await emulator.snapshot('delete', tag)

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
    async def xemu_symbols(query: str = '') -> dict[str, Any]:
        """Filter symbols from an optional private JSON file outside the repository."""
        return emulator.symbols(query)

    return server
