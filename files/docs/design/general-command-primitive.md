# Design: General-Command Primitive (restore everyday operations, ledger-native)

Status: **IMPLEMENTED (MVP)** · Author: session 2026-07-23 · Supersedes the `save_output` tool idea

> **Shipped:** run_command (bwrap-confined) · local_probe + make_probe `local:` routing · prompt
> un-fence · operator session grants (read paths + network, §4.2). Fully on the branch. Remaining
> future work is the "Out (later)" list in §12 (seccomp-hardened profile, streaming, discovery
> findings-schema, non-Linux fallback).

---

## 1. Problem

A stock 7B model can already write files, create files, munge CSVs, run shell — ordinary
computer operations. Gorgon deliberately **crippled** that: the model is fenced to a fixed
50-tool VM registry by (a) the tool set and (b) the system prompt. The result is that any new
everyday capability ("export to a file", "extract a column from a CSV", "zip these logs")
would require **a new tool per task** — `save_output`, `create_csv`, `extract_csv`, … — which
is backwards design: the tool count grows without bound and every capability is a code change.

**Goal:** give the model back general operations through **one primitive**, not N tools, and
route it through the **tree-ledger** so every command is decomposed, verified, and booked like
any other leaf — streamlined *and* accountable.

## 2. The key realization — the pattern already exists

Gorgon already ships the ledger-integrated "run an opaque command and verify its effect"
path. It's just aimed at **guests**:

- `run_guest_command` runs an arbitrary command *inside a VM*.
- `guest_probe` independently verifies the effect. Assertions (from `_vm_guest.guest_probe`):
  `path_exists`, `path_is_dir`, `port_listening`, `process_running`, `user_exists`,
  `service_active`, `command_available`, `is_writable`.
- The honesty rule lives in `autonomous.make_probe`: a `probe:` predicate clause
  (`vm:assertion:target[:value]`, e.g. `web01:port_listening:443`) is checked with a real
  read-only `guest_probe`; it returns `True/False`, or **`None` = unverifiable** when it can't
  confirm — and the caller treats `None` as "not done", never silently "done".

So the machinery to let the model run a general command **and have the ledger book it on
reality, not the exit code** is already built and proven. This design is: **generalize that
same pattern from the guest side to the operator/host side.** We are not inventing a gateway
from scratch — we are aiming an existing, ledger-native path at a new target.

## 3. Design overview

Two new primitives + probe routing + a risk check + a prompt change:

| Piece | What | Mirrors |
|---|---|---|
| `run_command` | run a shell command in the operator's scoped workspace | `run_guest_command` |
| `local_probe` | read-only host predicate to verify an effect | `guest_probe` |
| `make_probe` routing | route `local:…` probe specs to `local_probe` | existing `guest_probe` routing |
| `RunCommandCheck` | preflight risk classification + gate | existing `PreflightCheck`s |
| system-prompt diff | un-fence the model; require post-condition declaration | — |

The model decomposes a goal to leaves in `score.py`; at a leaf it can now choose
`run_command` (with a **declared post-condition**) exactly as it chooses any tool. The
post-condition is probe-verified; the reward-cost layer prices it; destructive commands hit
the confirmation gate. Every future "extract CSV / create file / rename / zip" is just a
command the model writes — **zero new tools.**

## 4. `run_command` primitive

`executor/tool_dispatch/tools/run_command.py` — a `Tool` subclass (no manager needed; it
touches the filesystem, not VMs). It is the **host/workspace sibling** of `run_guest_command`:

- `run_command` → runs on the **host**, confined to the operator's workspace (this section).
- `run_guest_command` → runs **inside a VM** (sandbox), unchanged.

Same "run an opaque command, verify by probe" contract; different target.

**Args**

```
run_command(
  code: str,                 # the shell command or python source
  lang: str = "shell",       # "shell" (sh -c) | "python" (python3 -c) — both supported
)
```

Both languages run in the **same confined workspace** (§4.1). One primitive, two surfaces —
still zero per-task tools.

**Returns**

```
{"success": bool, "returncode": int, "stdout": str, "stderr": str, "workspace": str}
```

`success` = "the command ran", NOT "the goal is achieved". Achievement is decided by the probe
(§6), never by `returncode` alone — same honesty rule as `run_guest_command`.

Config: `run_command_timeout_s`, `run_command_max_output_bytes`, `workspace_dir`
(default `~/.gorgon/workspace`), `read_denylist` (see §4.1).

### 4.1 Confinement & scope — a hard write-gate, not a heuristic

The operator's rule: **anything the command creates must stay under `.gorgon`; it may read
elsewhere only with per-session operator permission.** A hard guarantee, so we enforce it at
the OS level with a **namespace sandbox** (`bubblewrap`/`bwrap`) — NOT by string-matching the
command (which can always be obfuscated). This still "runs on the host as the operator" — it's
a mount/namespace jail, not a VM.

- **Writes:** only `~/.gorgon/workspace/` is bind-mounted **read-write** and is the cwd.
  Everything else is read-only or absent, so `> /etc/x`, `rm ~/file`, writing to `~/.gorgon/operators.json`,
  etc. **fail at the syscall level** — the guarantee holds regardless of what the command tries.
- **Secrets are simply not mounted:** the auth store + keys under `~/.gorgon` (operators.json,
  toolstats, `~/.gorgon.key`) are outside the workspace bind, so a confined command can't read
  them at all. No denylist needed for those — absence is the control.
- **Reads (default):** the workspace (rw) + a minimal read-only system view (`/usr`, `/bin`,
  `/lib`, resolv.conf) so normal tools/python run.
- **Reads (broadened, per session):** the operator grants a path for the session → it's added
  as an extra `--ro-bind <path>`. See §4.2. Ungranted paths outside the workspace are invisible.
- **Network:** off by default (`--unshare-net`); an explicit `allow_net` grant (like the read
  grant) re-enables it for a command — so "fetch X and process it" is possible but deliberate.

**Fallback if `bwrap` is unavailable:** the primitive refuses to run rather than silently
dropping the jail — `run_command` becomes unavailable and says so (better than a false
guarantee). A future mode can fall back to `systemd-run` `ReadOnlyPaths`/`ReadWritePaths` or
Linux user-namespaces directly. (bubblewrap ships in most distros; install is one package.)

### 4.2 Session read-grants

A small in-memory, session-scoped grant table (keyed by session_id):

- Operator action (CLI/chat): `grant read <path>` / `grant net` for the current session →
  recorded; `revoke`/session-end clears it.
- `run_command` reads the grants for its session and adds the ro-binds / net accordingly.
- Grants are **operator-initiated only** (the AI cannot grant itself access), never persisted
  across sessions, and audited. This is the operator-in-the-loop control for "read my Desktop
  CSV" without opening the whole filesystem by default.

## 5. `local_probe` primitive

`executor/tool_dispatch/tools/local_probe.py` — the host-side twin of `guest_probe`,
read-only, scoped to the workspace (+ operator-approved read paths).

**Assertion set** (mirror guest_probe's vocabulary, host filesystem):

| assertion | target | holds when |
|---|---|---|
| `file_exists` | path | file exists |
| `dir_exists` | path | directory exists |
| `file_contains` | path (value=substring) | file contains the substring |
| `file_matches` | path (value=regex) | a line matches the regex |
| `is_writable` | path | path is writable |
| `command_available` | name | binary on PATH |

Same shape as guest_probe: `local_probe(assertion, target, value=None)` →
`{"success": True, "holds": bool}` (or `success:False` on error → unverifiable).

## 6. Grounding — how the ledger follows a command

Reuse the `probe:` clause + `make_probe` unchanged in spirit, with **one routing tweak**:

- Extend the `probe:` spec to allow a `local` sentinel in the VM slot:
  `local:file_exists:~/.gorgon/workspace/vms.csv`
- In `autonomous.make_probe`, branch on the first field: `local`/`host` → `execute("local_probe", …)`;
  anything else → `execute("guest_probe", …)` (today's behavior). The `None = unverifiable`
  contract is identical.

Flow for a leaf that runs a command:
1. Model emits `run_command(command=…)` **with** a `probe:` post-condition on the node
   (`local:file_exists:…`).
2. Command executes; result recorded.
3. The node's success predicate runs the probe → `True` (booked done), `False` (booked
   failed → backtrack/replan), or `None` (**unverifiable** — never booked done).
4. Optionally record a finding via the existing `findings.extract_value` / `yield_fact` path
   if the command is a *discovery* (e.g. "count rows" → a fact), so the findings ledger
   generalizes here too.

A command with **no** declared post-condition is `unverifiable` by construction — the exact
honesty rule already enforced for guest commands. This is the load-bearing piece: it is what
keeps a general command from becoming a trust hole.

## 7. Risk gate — now a light secondary layer

Because §4.1's sandbox is a **hard** control, the blast radius of any command is the workspace
(recoverable scratch) — it cannot write outside it, read secrets, or (by default) touch the
network. That collapses most of what a risk gate would otherwise guard. So the preflight check
(`orchestrator/preflight/validator/run_command.py`, a `RunCommandCheck`) is deliberately thin:

| situation | action |
|---|---|
| ordinary command, no grants | `ok` — the sandbox contains it |
| command requests network (`allow_net`) | `ask_user` — confirm the egress once per session |
| command uses a session read-grant | `ok` (the grant was already an operator action, §4.2) |
| `bwrap` unavailable | `abort` — refuse rather than run unconfined (§4.1) |

No heuristic shell-string denylist for secrets/system paths — the sandbox makes those
**impossible**, not merely discouraged, which is strictly stronger than string-matching. The
only operator-in-the-loop confirmations are the deliberate capability grants (net, broadened
reads), consistent with Gorgon's "high-impact acts need a fresh operator act" invariant.

The reward-cost layer still assigns `run_command` a **conservative fixed commitment/risk** so
the worth-it gate prices it as a real action; `p_world` is learned from the event log like any
tool.

## 8. Execution boundary — DECIDED

Two distinct primitives for two distinct targets — **not** one primitive with a mode switch:

- **`run_command` → host, namespace-confined workspace** (§4.1). Runs on the host as the
  operator, but jailed by `bwrap` so writes stay under `~/.gorgon/workspace` and reads are
  workspace + granted paths only. This is the "everyday operations" surface — artifacts land
  where the operator can use them, with a hard write-gate rather than a VM trapping the I/O.
- **`run_guest_command` → VM (sandbox)**, unchanged. In-guest execution over the guest-agent
  transport, for anything that must run *inside* a target VM.

The model (and the ledger) sees two siblings with the same probe-verified contract; it picks
by target (host glue vs in-VM action). No sandbox-VM mode on `run_command` — if you ever want
untrusted-code isolation on the host side, tighten the `bwrap` profile (seccomp, no-net,
tmpfs-home) rather than reaching for a VM.

## 9. Prompt changes (half the work)

The system prompt (`orchestrator/ai/chat/ollama_client.py`, and the agent contract in
`agent/contract/core.py`) currently fences the model to the VM tools. Changes:

1. **Grant general operations:** introduce `run_command` (shell *or* python, `lang=`) as the way
   to do ordinary file/data work; explicitly say "you are not limited to the VM tools for
   everyday operations."
2. **Require a post-condition:** whenever it runs a command that should *produce* something, it
   must declare the effect as a `probe:` clause (`local:file_exists:…`) — otherwise the result
   is unverifiable and won't count.
3. **Prefer a command over a new tool:** "If a task is a normal shell/python operation, write
   the command — do not ask for a specialized tool."
4. **Teach the scope:** artifacts must be written under the workspace (writes elsewhere fail);
   reading outside the workspace or using the network needs the **operator** to grant it this
   session — the model asks for the grant, it cannot self-grant.
5. Keep the VM tools as the first-class path for VM lifecycle (create/launch/snapshot/…);
   `run_command` is host glue, `run_guest_command` is in-VM.

## 10. Where each piece slots in

| File | Change |
|---|---|
| `executor/tool_dispatch/tools/run_command.py` | **new** — the primitive |
| `executor/tool_dispatch/tools/local_probe.py` | **new** — host probe |
| `executor/command_catalog.json` | register both in `TOOL_SPECS` (req/vm/effect/rev), add help entries + `tool_triggers`; `run_command` effect = writes (rev? no), `local_probe` effect = None (read-only) |
| `orchestrator/ai/tools.json` | **new** AI-facing schemas for both |
| `orchestrator/ai/planner/autonomous.py` | `make_probe`: route `local:`/`host:` specs → `local_probe` |
| `orchestrator/preflight/validator/run_command.py` | **new** `RunCommandCheck` (light gate §7 — net-grant confirm, bwrap-missing abort) |
| `orchestrator/ai/chat/ollama_client.py` (+ contract core) | system-prompt diff §9 |
| session-grant store (new, small) | in-memory per-session read/net grants (§4.2); operator-set via CLI/chat |
| `executor/config.json` | `workspace_dir`, `run_command_timeout_s`, `run_command_max_output_bytes` (no denylist — §7) |
| `tests/` | `test_run_command.py` (confinement holds: out-of-workspace write fails; probe grounding; unverifiable-without-postcondition) + registry drift assert stays green |

**Runtime dependency:** `bubblewrap` (`bwrap`). Add to `requirements`/install docs; the
primitive self-disables with a clear message if it's absent (§4.1).

## 11. Worked example — "export my vms to a csv"

1. `list_vms` (existing tool) → the VM list is in context.
2. Model decomposes: leaf = `run_command(code="printf '…csv…' > vms.csv", lang="shell")` with
   node `probe: local:file_exists:~/.gorgon/workspace/vms.csv`.
3. `RunCommandCheck`: ordinary command, no grants → `ok` (the sandbox contains it).
4. Command runs `bwrap`-confined, cwd `~/.gorgon/workspace/`.
5. `local_probe(file_exists, …/vms.csv)` → `holds:true` → node booked **done**; ledger records
   reward/cost; `p_world` for `run_command` updates.
6. If the file wasn't created → probe `false` → backtrack/replan (not silently "done").

Two nodes, ledger-tracked, no `save_output` / `create_csv` tool anywhere.

## 12. MVP scope

**In:** `run_command` (**shell + python**, `bwrap`-confined workspace) · `local_probe`
(file/dir predicates) · `make_probe` `local:` routing · session read/net grants · light
`RunCommandCheck` · prompt diff · config knobs · tests (incl. a confinement-escape test).

**Out (later):** streaming / long-running commands · richer `local_probe` predicates
(hash/size/mtime) · a findings-schema for discovery commands (row counts, etc.) ·
seccomp-hardened `bwrap` profile · non-Linux confinement fallback.

## 13. Decisions (resolved 2026-07-23)

1. **Boundary** — DECIDED (§8): `run_command` = host, `bwrap`-confined workspace;
   `run_guest_command` = VM/sandbox. Two sibling primitives, not a mode switch.
2. **Read/write scope** — DECIDED (§4.1/§4.2): **writes hard-confined to `~/.gorgon/workspace`**
   at the OS level; **reads = workspace by default**, broader reads (and network) require an
   **operator session-grant** the AI cannot issue itself.
3. **Surface** — DECIDED: **both** shell and python, via `lang=` on the one primitive.
4. **Denylist** — MOOT: the sandbox makes secrets/system paths *unreadable/unwritable* rather
   than string-denied; no heuristic denylist. Config holds only `workspace_dir`, timeouts,
   output cap.
5. **Availability** — DECIDED: **enabled by default** for every agent; an agent's `.grgn` can
   **disable/blacklist** it via the existing contract tool-policy blacklist (it is *not*
   default-deny). Parity with how other tools are scoped, just defaulting ON.

Remaining to settle during build: the exact `bwrap` profile flags (seccomp? tmpfs `$HOME`?),
and whether `python` mode shares the same probe vocabulary verbatim (it should).
