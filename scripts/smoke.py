"""Opt-in real-emulator smoke test; writes evidence only to --output-dir."""

import argparse
import asyncio
import json
import os
import sys
import time
from datetime import timedelta
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def run(args):
    output = Path(args.output_dir).expanduser().resolve()
    repository = Path(__file__).resolve().parents[1]
    if output == repository or repository in output.parents:
        raise ValueError('Put smoke evidence outside the repository')
    output.mkdir(parents=True, exist_ok=True)
    params = StdioServerParameters(command=sys.executable, args=['-m', 'xemu_mcp'],
                                  env={**os.environ, 'XEMU_MCP_CONFIG': str(Path(args.server_config).resolve())})
    evidence = {}
    tag = args.tag or time.strftime('mcp-smoke-%Y%m%d-%H%M%S', time.gmtime())
    async with stdio_client(params) as (reader, writer):
        async with ClientSession(reader, writer, read_timeout_seconds=timedelta(seconds=180)) as session:
            await session.initialize()

            async def call(name, **arguments):
                result = await session.call_tool(name, arguments)
                if result.isError:
                    raise RuntimeError(f'{name}: {result.content}')
                data = result.structuredContent or json.loads(result.content[0].text)
                print(name, json.dumps(data), flush=True)
                return data

            started = False
            snapshot_created = False
            failed = False
            cleanup_errors = []
            try:
                evidence['start'] = await call('xemu_start', config_path=args.config,
                                               disc_path=args.disc)
                started = True
                await asyncio.sleep(args.boot_wait)
                # No game memory edits or staged progression. This is real
                # controller input; callers must visually verify the menu.
                if args.skip_with_start:
                    evidence['skip'] = await call('xemu_press', buttons=['start'], hold_ms=200)
                    await asyncio.sleep(2)
                evidence['before'] = await call('xemu_screenshot',
                                                filename=str(output / 'before.png'), bring_to_front=True)
                evidence['header'] = await call('xemu_read', address=args.address, length=args.length)
                evidence['saved_registers'] = await call('xemu_registers')
                # HMP savevm freezes the guest internally. An externally paused
                # save stalls on resume in tested xemu builds, so save running.
                evidence['save'] = await call('xemu_save_state', tag=tag)
                snapshot_created = True
                evidence['saved_status'] = await call('xemu_status')
                evidence['press'] = await call('xemu_press', buttons=[args.press], hold_ms=200)
                await asyncio.sleep(args.after_wait)
                evidence['after'] = await call('xemu_screenshot',
                                               filename=str(output / 'after.png'), bring_to_front=True)
                evidence['changed_registers'] = await call('xemu_registers')
                evidence['load'] = await call('xemu_load_state', tag=tag)
                evidence['restored_status'] = await call('xemu_status')
                evidence['restored_registers'] = await call('xemu_registers')
                evidence['restored_header'] = await call('xemu_read', address=args.address, length=args.length)
                evidence['restored'] = await call('xemu_screenshot',
                                                  filename=str(output / 'restored.png'), bring_to_front=True)
                evidence['header_matches'] = evidence['header']['hex'] == evidence['restored_header']['hex']
                evidence['limits'] = ('Snapshot saved while running; memory/register samples are '
                                      'not atomic with savevm. Inspect images for game-state restoration.')
                if args.assert_read_match and not evidence['header_matches']:
                    raise RuntimeError('Requested before/after memory comparison did not match')
            except BaseException as exc:
                failed = True
                evidence['error'] = repr(exc)
                raise
            finally:
                try:
                    if snapshot_created and not args.keep_snapshot:
                        try:
                            evidence['delete'] = await call('xemu_delete_state', tag=tag)
                        except BaseException as exc:
                            cleanup_errors.append(f'snapshot deletion: {exc!r}')
                    if started:
                        try:
                            evidence['stop'] = await call('xemu_stop')
                        except BaseException as exc:
                            cleanup_errors.append(f'owned process stop: {exc!r}')
                finally:
                    evidence['cleanup_errors'] = cleanup_errors
                    (output / 'smoke.json').write_text(json.dumps(evidence, indent=2), encoding='utf-8')
                    print('Evidence:', output / 'smoke.json', flush=True)
                if cleanup_errors and not failed:
                    raise RuntimeError(f'Cleanup failed; see {output / "smoke.json"}')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--server-config', required=True, help='Private xemu-mcp JSON config')
    parser.add_argument('--config', required=True, help='Private xemu TOML config (copied HDD/EEPROM)')
    parser.add_argument('--disc', help='Optional disc override')
    parser.add_argument('--output-dir', required=True, help='Outside the repo; screenshots/memory remain private')
    parser.add_argument('--boot-wait', type=float, default=50)
    parser.add_argument('--after-wait', type=float, default=3)
    parser.add_argument('--skip-with-start', action='store_true', help='Press Start before baseline capture')
    parser.add_argument('--press', default='a', help='Controller name sent after baseline/snapshot')
    parser.add_argument('--address', default='0x10000', help='Virtual XBE header location, or another read address')
    parser.add_argument('--length', type=int, default=16)
    parser.add_argument('--tag', help='Snapshot tag; default is a timestamped mcp-smoke tag')
    parser.add_argument('--keep-snapshot', action='store_true')
    parser.add_argument('--assert-read-match', action='store_true',
                        help='Assert equality for a known stable memory range, such as the XBE header')
    args = parser.parse_args()
    if args.boot_wait < 0 or args.after_wait < 0:
        parser.error('Wait times must be nonnegative')
    asyncio.run(run(args))


if __name__ == '__main__':
    main()
