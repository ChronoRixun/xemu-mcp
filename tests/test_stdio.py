import asyncio
import os
import sys
import unittest

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


class StdioTests(unittest.IsolatedAsyncioTestCase):
    async def test_initialize_list_call_and_errors_are_real_mcp(self):
        # Exercise the actual server subprocess; no emulator or desktop needed.
        params = StdioServerParameters(command=sys.executable,
                                      args=['-m', 'xemu_mcp'],
                                      env={**os.environ, 'XEMU_MCP_CONFIG': ''})
        async with stdio_client(params) as (reader, writer):
            async with ClientSession(reader, writer) as session:
                await session.initialize()
                tools = (await session.list_tools()).tools
                names = {tool.name for tool in tools}
                self.assertEqual(names, {
                    'xemu_start', 'xemu_stop', 'xemu_status', 'xemu_pause', 'xemu_resume',
                    'xemu_press', 'xemu_read', 'xemu_read_physical', 'xemu_write',
                    'xemu_registers', 'xemu_save_state', 'xemu_load_state',
                    'xemu_delete_state', 'xemu_screenshot', 'xemu_symbols',
                })
                result = await session.call_tool('xemu_symbols', {'query': 'invented'})
                self.assertFalse(result.isError)
                self.assertEqual(result.structuredContent['symbols'], {})
                failure = await session.call_tool('xemu_status', {})
                self.assertTrue(failure.isError)
                self.assertIn('owned', failure.content[0].text)
                guarded = await session.call_tool('xemu_write', {'address': '0x100', 'hex_data': '01'})
                self.assertTrue(guarded.isError)
                self.assertIn('disabled', guarded.content[0].text)


if __name__ == '__main__':
    unittest.main()
