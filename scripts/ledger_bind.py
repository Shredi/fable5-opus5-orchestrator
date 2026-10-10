#!/usr/bin/env python3
"""PostToolUse hook (Write|Edit|MultiEdit|Bash): bind this session to the ledger it writes.

Since 0.23.3 a chair's `ledger mark|note|add|defer -f PATH` (the plugin's
ledger CLI, run through Bash) binds exactly like an Edit of PATH: naming
the file explicitly is the same deliberate act (10.10.2026: a chair
used `ledger -f` all day and stayed unbound). A bare `ledger <write>`
names no file — it resolves through an existing binding, so it is a
no-op here. Read-only `ledger status` never binds.

D1 (per-session ledger binding): the spawn/task/close guards used to
discover the "active" ledger by newest-mtime alone, with no session
dimension — two parallel sessions in the same repo would step on each
other's ledgers (session A's close held on session B's newer file, or
worse, silenced by it). The fix threads a per-session binding through
the injector's marker (`$TMPDIR/fable-orch-model-<sid>.json`, key
`"ledger"`), and this hook is its ONLY write point (03.10.2026: the spawn/task
gate's adoption-on-discovery was removed — it bound fresh sessions to
other tasks' open ledgers; the close guard never writes it). Editing
an existing ledger is therefore THE explicit way to continue it:

    a successful Write/Edit/MultiEdit whose target is the LIVE ledger
    in a `.workflow/` directory binds (or rebinds) this session to
    that exact path.

The live-name filter is the same one `active_ledger_in()` in both
guard scripts uses: `ledger[-._]*.md`, case-insensitive, "ledger" a
whole leading segment (not `ledgers.md`), *-archive.md/_archive.md
excluded. The parent directory must be literally named `.workflow` —
this hook does not walk upward like `find_ledger()`; a Write outside
that directory is not a ledger write.

No session marker: a manual install (neither CLAUDE_PLUGIN_ROOT nor the
harness-neutral FABLE_ORCH_SESSION_BINDING=1) has nothing
to bind onto, so this hook does nothing; a plugin session whose marker
was swept gets a minimal one ({"started", "ledger"}) recreated, because
the gates treat it as unbound and it must be able to bind again. Every other file write, and every write whose
target is not a live ledger name, is a no-op too. The hook checks
`tool_name` itself (Write/Edit/MultiEdit only) and ignores failed tool
results, so a broadened matcher or another harness's payload can never
bind or recreate a marker from a Read. A subagent's write (payload
carries `agent_id`; it shares the chair's session_id) never binds.

Always exits 0. Every failure mode (malformed stdin, unreadable
marker, permission error on the atomic replace, ...) is swallowed —
this hook must never block or fail a Write/Edit/MultiEdit.
"""
import json
import os
import shlex
import sys
import tempfile
import time


def session_marker_path(session_id):
    """Path of the per-session injector marker, or None without an id."""
    if not session_id:
        return None
    safe = "".join(c for c in str(session_id) if c.isalnum() or c in "-_")
    return os.path.join(tempfile.gettempdir(), f"fable-orch-model-{safe}.json")


def _plugin_install():
    """True for a plugin session: Claude Code exports CLAUDE_PLUGIN_ROOT to
    plugin hooks; other harnesses set the neutral FABLE_ORCH_SESSION_BINDING=1.
    Same check in ledger_guard_spawn.py, ledger_guard_stop.py and
    ledger_bind.py (standalone hook scripts, no shared module)."""
    return bool((os.environ.get("CLAUDE_PLUGIN_ROOT") or "").strip()) or \
        (os.environ.get("FABLE_ORCH_SESSION_BINDING") or "").strip() == "1"


BIND_TOOLS = ("Write", "Edit", "MultiEdit")
# Subcommands of the ledger CLI that change the file (scripts/ledger.py).
LEDGER_WRITE_CMDS = ("mark", "note", "add", "defer")
_SHELL_SEPARATORS = {";", "&&", "||", "|", "&", "(", ")", "\n"}


def ledger_cli_targets(command):
    """Ledger paths named by `ledger <write-subcommand> -f PATH` calls in a
    Bash command line (also `--file PATH`, `--file=PATH`, `-fPATH`, the
    flag before or after the subcommand, `bin/ledger` or `ledger.py`).
    Bare calls name no file and yield nothing; `ledger status` never
    counts. Unparseable shell (unbalanced quotes) yields nothing."""
    if not isinstance(command, str) or "ledger" not in command:
        return []
    try:
        lex = shlex.shlex(command.replace("\n", " ; "), posix=True,
                          punctuation_chars=";&|()")
        lex.whitespace_split = True
        tokens = list(lex)
    except ValueError:
        return []
    targets = []
    i = 0
    while i < len(tokens):
        name = os.path.basename(tokens[i])
        if name not in ("ledger", "ledger.py"):
            i += 1
            continue
        path, cmd = None, None
        j = i + 1
        while j < len(tokens) and tokens[j] not in _SHELL_SEPARATORS:
            tok = tokens[j]
            if tok in ("-f", "--file") and j + 1 < len(tokens):
                path = tokens[j + 1]
                j += 2
                continue
            if tok.startswith("--file="):
                path = tok[len("--file="):]
            elif tok.startswith("-f") and len(tok) > 2 and not tok.startswith("--"):
                path = tok[2:]
            elif cmd is None and not tok.startswith("-"):
                cmd = tok
            j += 1
        if cmd in LEDGER_WRITE_CMDS and path:
            targets.append(path)
        i = j
    return targets


def _tool_failed(response):
    """True when the PostToolUse payload reports a failed tool call.
    Claude Code fires PostToolUse on success only (failures go to
    PostToolUseFailure), but another harness or a broadened matcher may
    deliver errors here: an explicit `success: false`, `is_error`, an
    `error` field, or a bare error string never binds."""
    if isinstance(response, dict):
        return response.get("success") is False or bool(response.get("is_error")) \
            or bool(response.get("error"))
    if isinstance(response, str):
        return response.lstrip().lower().startswith("error")
    return False


def _is_live_ledger_name(name):
    """Same live-name filter as active_ledger_in() in both guards:
    `LEDGER*.md`, case-insensitive, "ledger" a whole segment, and not
    retired via a trailing -archive.md/_archive.md."""
    low = name.lower()
    if not (low.startswith("ledger") and low.endswith(".md")):
        return False
    if low[6:7] not in (".", "-", "_"):
        return False
    if low.endswith("-archive.md") or low.endswith("_archive.md"):
        return False
    return True


def _bind(data):
    # Check the tool itself instead of trusting the hooks.json matcher
    # alone: only a successful Write/Edit/MultiEdit binds or recreates
    # a marker — a Read (or anything else) fed here is a no-op.
    tool_name = data.get("tool_name")
    if tool_name not in BIND_TOOLS and tool_name != "Bash":
        return
    if _tool_failed(data.get("tool_response")):
        return
    # A subagent shares the chair's session_id, so its ledger Edit would
    # bind the CHAIR. Its payloads carry agent_id (+ agent_type); the
    # chair's never do (live probe, Claude Code 2.1.288, PreToolUse and
    # PostToolUse alike — same discriminator as chair_read_guard.py).
    if data.get("agent_id"):
        return
    session_id = data.get("session_id")
    cache = session_marker_path(session_id)
    if not cache:
        return
    recreate = False
    if not os.path.isfile(cache):
        # Plugin session whose marker was swept (96 h SessionEnd sweep, OS
        # temp cleanup): the gates treat it as unbound, so recreate a
        # minimal marker or it could never bind again. Manual install
        # (no CLAUDE_PLUGIN_ROOT): nothing to bind onto, as before.
        if not _plugin_install():
            return
        recreate = True

    tool_input = data.get("tool_input")
    if not isinstance(tool_input, dict):
        return
    if tool_name == "Bash":
        # `ledger -f PATH <write>`: the last explicitly named ledger wins;
        # a relative PATH is relative to the command's cwd.
        targets = ledger_cli_targets(tool_input.get("command"))
        if not targets:
            return
        file_path = os.path.expanduser(targets[-1])
        if not os.path.isabs(file_path):
            cwd = data.get("cwd")
            if not isinstance(cwd, str) or not cwd:
                return
            file_path = os.path.join(cwd, file_path)
    else:
        file_path = tool_input.get("file_path")
    if not isinstance(file_path, str) or not file_path:
        return

    real = os.path.realpath(file_path)
    if not os.path.isfile(real):
        return  # Write payload for a path that was never actually created
    if not _is_live_ledger_name(os.path.basename(real)):
        return
    # Accept EITHER the raw path's parent or the realpath's parent being
    # named ".workflow" (case-insensitively): a symlinked .workflow/ dir
    # resolves its realpath parent to the symlink's TARGET directory name,
    # which find_ledger() (walking the raw tree) still treats as live.
    raw_parent = os.path.basename(os.path.dirname(file_path)).lower()
    real_parent = os.path.basename(os.path.dirname(real)).lower()
    if raw_parent != ".workflow" and real_parent != ".workflow":
        return

    try:
        with open(cache, encoding="utf-8") as f:
            marker = json.load(f)
    except Exception:
        marker = {}
    if not isinstance(marker, dict):
        marker = {}
    if recreate:
        marker = {"started": round(time.time(), 3)}
    marker["ledger"] = real

    # Atomic replace: same pattern as inject_instructions.py's marker
    # rewrite — a crash mid-write must never leave a truncated marker.
    tmp = f"{cache}.{os.getpid()}.tmp.json"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(marker, f)
    os.replace(tmp, cache)


def plain_mode():
    """True when FABLE_ORCH_MODE=plain (case-insensitive) is set.

    Plain mode turns the orchestration layer off for a session — no chair
    profile, no ledger gates, no cold-cache guard — while the
    destructive-command guard stays fully active (its scripts never read
    this switch). Any other value, or unset, is the normal behaviour.
    Duplicated verbatim in every hook it affects: the hooks run as
    standalone scripts with no shared module to import from."""
    return (os.environ.get("FABLE_ORCH_MODE") or "").strip().lower() == "plain"


def main():
    try:
        data = json.load(sys.stdin)
    except Exception:
        return  # malformed input -> never block
    if not isinstance(data, dict):
        return
    if plain_mode():
        return  # plain mode: no ledger, so nothing to bind
    try:
        _bind(data)
    except Exception:
        return  # fail open; this hook never crashes the pipeline


if __name__ == "__main__":
    main()
