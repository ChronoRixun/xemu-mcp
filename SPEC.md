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

## V-TBD (number assigned at merge)

`tools/check_no_game_content.py --all` restricts the Git index to the explicit
source/documentation allowlist, UTF-8 text below 256 KiB, without NUL bytes or
private machine paths. `--self-test` exercises rejection of synthetic forbidden
files. This inexpensive validator catches accidental staging of runtime files;
it does not establish copyright provenance and must accompany staged-diff review.
