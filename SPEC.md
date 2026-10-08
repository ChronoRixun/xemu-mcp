# xemu MCP invariants

All tools operate on a process started by the current server. A PID, its creation
time, executable path and owned loopback listeners establish the session identity.
The server never attaches to existing emulators and never kills an unrelated PID.
Existing game processes require an explicit private allowlist of test-build roots.

QMP commands and GDB sessions are serialized. QMP events are separated from replies,
IDs must match, and timeouts invalidate the connection. Virtual memory operations
pause the guest and restore its prior run state after debugger cleanup. Paused
callers disconnect without D (which resumes); running callers detach. A paused
caller remains paused; writes require explicit configuration and read-back.

Controller delivery targets only the owned Windows SDL window and always releases
keys. Stick/trigger input is digital. Successful delivery is distinct from proving
that a game acted on it. Screenshots prefer the advertised fork QMP command;
fallback captures a visible Windows client area, reports its limitations and
rejects detected obstruction.

Snapshots accept only safe tags and report HMP errors. Saves require a running
guest because tested builds stall on resume after an externally paused save.
They write into a private
qcow2 HDD; no guest data or runtime evidence belongs in this repository.

Audio capture uses only HMP wavcapture/stopcapture with validated paths and
parameters, attached to a named audiodev reported by the emulator itself.
Monitor errors are surfaced even when QMP reports success; recorded audio is
game content and stays outside this repository.

Research reads (search, captures, struct decoding) hold the guest paused for
one bounded operation and restore its run state: search ≤ 64 MiB, capture
≤ 16 MiB with at most 8 in-memory handles, structs ≤ 4096 bytes each, ≤ 64
structs / 64 KiB per call, results ≤ 1000. Only GDB `E` replies are treated as
unreadable pages; other protocol failures abort the operation.

Breakpoints/watchpoints live only in server memory between calls. They are
inserted for the duration of one `xemu_break_wait` and always removed before it
returns, over a fresh GDB connection if the stream broke. Waits are bounded; a
silent stub is interrupted with `^C`, then QMP stop. The final run state is
always the caller's `leave` choice, including on error or cancellation.

Scripted input validates every step before sending a key and releases each
step's keys. The snapshot catalogue is a private JSON file outside the
repository; catalogue failures never mask the HMP snapshot result.

## V-TBD (number assigned at merge)

`tools/check_no_game_content.py --all` restricts the Git index to the explicit
source/documentation allowlist, UTF-8 text below 256 KiB, without NUL bytes or
private machine paths. `--self-test` exercises rejection of synthetic forbidden
files. This inexpensive validator catches accidental staging of runtime files;
it does not establish copyright provenance and must accompany staged-diff review.
