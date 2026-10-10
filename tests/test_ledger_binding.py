"""D1 per-session ledger binding: scripts/ledger_bind.py (the PostToolUse
binder — writing OR editing a ledger is the only way to bind), the
spawn/task guard's refusal to adopt a discovered ledger, the close
guard's bound-only resolution, and the injector's carry-forward of the `ledger`
marker key across resume/clear/compact re-injections.

The core behavioral fix under test: two sessions in the same repo no
longer share one newest-mtime "active ledger" — each session, once
bound, sees only its own, and a marker that exists but was never bound
(no Write/Edit/MultiEdit to a ledger) must never hold a close hostage to a ledger it never touched.
"""
import json
import os
import time

from conftest import POSIX, REPO, run_hook, write_ledger, write_marker

BIND = "ledger_bind.py"
SPAWN = "ledger_guard_spawn.py"
STOP = "ledger_guard_stop.py"
INJECT = "inject_instructions.py"

LONG = "x" * 2000  # above the spawn guard's default 1500-char gate


def marker_path(tmp, session="test-session"):
    return tmp / f"fable-orch-model-{session}.json"


def marker(tmp, session="test-session"):
    p = marker_path(tmp, session)
    return json.loads(p.read_text(encoding="utf-8")) if p.is_file() else None


def bind_payload(session_id, file_path, tool_name="Write", **extra):
    payload = {
        "session_id": session_id,
        "tool_name": tool_name,
        "tool_input": {"file_path": str(file_path)},
    }
    payload.update(extra)
    return payload


def _named_ledger(root, name, body="- [ ] 1. item\n"):
    d = root / ".workflow"
    d.mkdir(parents=True, exist_ok=True)
    p = d / name
    p.write_text(body, encoding="utf-8")
    return p


def spawn_payload(repo, session_id="test-session", prompt=LONG):
    return {"tool_name": "Agent", "tool_input": {"prompt": prompt},
            "cwd": str(repo), "session_id": session_id}


def task_payload(repo, session_id="test-session"):
    return {"tool_name": "TaskCreate",
            "tool_input": {"subject": "s", "description": "d", "activeForm": "a"},
            "cwd": str(repo), "session_id": session_id}


def stop_payload(repo, session_id="test-session"):
    return {"cwd": str(repo), "session_id": session_id}


# --- ledger_bind.py: the PostToolUse binder ------------------------------

def test_bind_on_write_to_live_ledger(repo_dir, tmp_path):
    write_marker(tmp_path, time.time())
    ledger = _named_ledger(repo_dir, "LEDGER-topic.md")
    assert run_hook(BIND, bind_payload("test-session", ledger), tmpdir=tmp_path) is None
    assert marker(tmp_path)["ledger"] == os.path.realpath(str(ledger))


def test_rebind_on_second_write(repo_dir, tmp_path):
    write_marker(tmp_path, time.time())
    first = _named_ledger(repo_dir, "LEDGER-first.md")
    second = _named_ledger(repo_dir, "LEDGER-second.md")
    run_hook(BIND, bind_payload("test-session", first), tmpdir=tmp_path)
    assert marker(tmp_path)["ledger"] == os.path.realpath(str(first))
    run_hook(BIND, bind_payload("test-session", second), tmpdir=tmp_path)
    assert marker(tmp_path)["ledger"] == os.path.realpath(str(second))


def test_no_bind_outside_workflow_directory(tmp_path):
    write_marker(tmp_path, time.time())
    outside = tmp_path / "LEDGER.md"  # not inside a .workflow/ dir
    outside.write_text("- [ ] 1. open\n", encoding="utf-8")
    assert run_hook(BIND, bind_payload("test-session", outside), tmpdir=tmp_path) is None
    assert "ledger" not in marker(tmp_path)


def test_no_bind_for_archived_name(repo_dir, tmp_path):
    write_marker(tmp_path, time.time())
    archived = _named_ledger(repo_dir, "LEDGER-done-work-archive.md")
    run_hook(BIND, bind_payload("test-session", archived), tmpdir=tmp_path)
    assert "ledger" not in marker(tmp_path)


def test_no_bind_for_non_segment_name(repo_dir, tmp_path):
    write_marker(tmp_path, time.time())
    fake = _named_ledger(repo_dir, "ledgers.md")  # "ledger" not a whole segment
    run_hook(BIND, bind_payload("test-session", fake), tmpdir=tmp_path)
    assert "ledger" not in marker(tmp_path)


def test_no_marker_no_crash_no_file_created(repo_dir, tmp_path):
    ledger = _named_ledger(repo_dir, "LEDGER.md")
    assert run_hook(BIND, bind_payload("no-such-session", ledger), tmpdir=tmp_path) is None
    assert not marker_path(tmp_path, "no-such-session").exists()


def test_other_marker_keys_survive_a_bind(repo_dir, tmp_path):
    write_marker(tmp_path, time.time(), model="claude-fable-5", profile="fable")
    before = marker(tmp_path)
    ledger = _named_ledger(repo_dir, "LEDGER.md")
    run_hook(BIND, bind_payload("test-session", ledger), tmpdir=tmp_path)
    after = marker(tmp_path)
    assert after["ledger"] == os.path.realpath(str(ledger))
    for k, v in before.items():
        assert after[k] == v


def test_malformed_input_never_crashes(tmp_path):
    assert run_hook(BIND, raw="not json", tmpdir=tmp_path) is None
    assert run_hook(BIND, raw="[1, 2]", tmpdir=tmp_path) is None


def test_missing_file_path_is_a_noop(tmp_path):
    write_marker(tmp_path, time.time())
    payload = {"session_id": "test-session", "tool_input": {}}
    assert run_hook(BIND, payload, tmpdir=tmp_path) is None
    assert "ledger" not in marker(tmp_path)


# --- spawn/task gate adoption --------------------------------------------

def _is_deny(result):
    return (result is not None
            and result["hookSpecificOutput"]["permissionDecision"] == "deny")


def test_spawn_never_adopts_a_discovered_ledger(repo_dir, tmp_path):
    # 03.10.2026 repro: fresh session, an ended session's open ledger in
    # .workflow/ (no live marker holds it). The spawn used to pass and
    # bind that ledger; now it is denied and the session stays unbound.
    write_marker(tmp_path, time.time())  # marker exists, not yet bound
    write_ledger(repo_dir, "- [ ] 1. someone else's open item\n")
    result = run_hook(SPAWN, spawn_payload(repo_dir), tmpdir=tmp_path)
    assert _is_deny(result)
    reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    assert "no ledger of its own" in reason and "LEDGER-<topic>.md" in reason
    assert "ledger" not in marker(tmp_path)
    # ...and its close is never held on the stranger's items.
    assert run_hook(STOP, stop_payload(repo_dir), tmpdir=tmp_path) is None


def test_fork_spawn_stays_exempt_when_unbound(repo_dir, tmp_path):
    write_marker(tmp_path, time.time())
    write_ledger(repo_dir, "- [ ] 1. open\n")
    payload = spawn_payload(repo_dir)
    payload["tool_input"]["subagent_type"] = "fork"
    assert run_hook(SPAWN, payload, tmpdir=tmp_path) is None


def test_task_create_never_adopts_a_discovered_ledger(repo_dir, tmp_path):
    write_marker(tmp_path, time.time(), session="task-session")
    write_ledger(repo_dir, "- [ ] 1. open\n")
    results = [run_hook(SPAWN, task_payload(repo_dir, session_id="task-session"),
                        tmpdir=tmp_path) for _ in range(3)]
    assert results[:2] == [None, None] and _is_deny(results[2])
    assert "ledger" not in marker(tmp_path, "task-session")


def test_no_adoption_without_a_marker(repo_dir, tmp_path):
    # No SessionStart marker at all: adoption has nothing to bind onto,
    # and the legacy (session-agnostic) discovery path is unchanged.
    write_ledger(repo_dir, "- [ ] 1. open\n")
    assert run_hook(SPAWN, spawn_payload(repo_dir), tmpdir=tmp_path) is None
    assert not marker_path(tmp_path).exists()


def test_plugin_session_with_a_swept_marker_is_unbound_not_legacy(repo_dir, tmp_path):
    # A live plugin session whose marker the 96 h sweep deleted must not
    # fall into legacy discovery: another task's open ledger neither
    # satisfies its spawn nor holds its close. Its own ledger Write
    # recreates the marker and binds, so the session is never stuck.
    plugin = {"CLAUDE_PLUGIN_ROOT": str(REPO)}
    write_ledger(repo_dir, "- [ ] 1. someone else's open item\n")
    result = run_hook(SPAWN, spawn_payload(repo_dir), env_extra=plugin, tmpdir=tmp_path)
    assert _is_deny(result)
    assert run_hook(STOP, stop_payload(repo_dir), env_extra=plugin, tmpdir=tmp_path) is None
    mine = _named_ledger(repo_dir, "LEDGER-mine.md")
    run_hook(BIND, bind_payload("test-session", mine), env_extra=plugin, tmpdir=tmp_path)
    assert marker(tmp_path)["ledger"] == os.path.realpath(str(mine))
    assert run_hook(SPAWN, spawn_payload(repo_dir), env_extra=plugin, tmpdir=tmp_path) is None


def test_read_payload_never_binds_or_recreates_a_marker(repo_dir, tmp_path):
    # The hook checks tool_name itself, not just the hooks.json matcher:
    # a Read of a live ledger fed directly binds nothing, with or without
    # a marker, and a failed Write does not bind either.
    plugin = {"CLAUDE_PLUGIN_ROOT": str(REPO)}
    ledger = _named_ledger(repo_dir, "LEDGER-read.md")
    run_hook(BIND, bind_payload("swept", ledger, tool_name="Read"),
             env_extra=plugin, tmpdir=tmp_path)
    assert not marker_path(tmp_path, "swept").exists()
    write_marker(tmp_path, time.time())
    run_hook(BIND, bind_payload("test-session", ledger, tool_name="Read"), tmpdir=tmp_path)
    run_hook(BIND, bind_payload("test-session", ledger,
                                tool_response={"success": False, "error": "denied"}),
             tmpdir=tmp_path)
    assert "ledger" not in marker(tmp_path)


def test_subagent_ledger_edit_never_binds_the_chair(repo_dir, tmp_path):
    # Subagents share the chair's session_id; their payloads carry
    # agent_id + agent_type (live probe, Claude Code 2.1.288).
    write_marker(tmp_path, time.time())
    ledger = _named_ledger(repo_dir, "LEDGER-sub.md")
    run_hook(BIND, bind_payload("test-session", ledger, tool_name="Edit",
                                agent_id="a98e3468a760612f9",
                                agent_type="general-purpose"), tmpdir=tmp_path)
    assert "ledger" not in marker(tmp_path)
    run_hook(BIND, bind_payload("test-session", ledger, tool_name="Edit"), tmpdir=tmp_path)
    assert marker(tmp_path)["ledger"] == os.path.realpath(str(ledger))


def test_neutral_session_binding_switch_acts_like_plugin_root(repo_dir, tmp_path):
    # FABLE_ORCH_SESSION_BINDING=1 is the harness-neutral twin of
    # CLAUDE_PLUGIN_ROOT: no legacy discovery for a marker-less session,
    # and its ledger Write recreates the marker.
    neutral = {"FABLE_ORCH_SESSION_BINDING": "1"}
    write_ledger(repo_dir, "- [ ] 1. someone else's open item\n")
    assert _is_deny(run_hook(SPAWN, spawn_payload(repo_dir), env_extra=neutral, tmpdir=tmp_path))
    assert run_hook(STOP, stop_payload(repo_dir), env_extra=neutral, tmpdir=tmp_path) is None
    mine = _named_ledger(repo_dir, "LEDGER-mine.md")
    run_hook(BIND, bind_payload("test-session", mine), env_extra=neutral, tmpdir=tmp_path)
    assert marker(tmp_path)["ledger"] == os.path.realpath(str(mine))


# --- two-session simulation: the actual D1 fix ---------------------------

def test_two_sessions_each_bound_to_their_own_ledger(repo_dir, tmp_path):
    # A's ledger is OLDER, B's is NEWER — under plain newest-mtime
    # discovery B's ledger would win for both sessions. Once each is
    # bound, each session's guards see only its own.
    ledger_a = _named_ledger(repo_dir, "LEDGER-a.md", "- [ ] 1. a's item\n")
    old = time.time() - 3600
    os.utime(ledger_a, (old, old))
    ledger_b = _named_ledger(repo_dir, "LEDGER-b.md", "- [ ] 1. b's item\n")  # newer

    write_marker(tmp_path, time.time(), session="session-a", ledger=ledger_a)
    write_marker(tmp_path, time.time(), session="session-b", ledger=ledger_b)

    # A's spawn gate is satisfied by its OWN (older) ledger, despite B's
    # newer one existing in the same directory.
    assert run_hook(
        SPAWN, spawn_payload(repo_dir, session_id="session-a"), tmpdir=tmp_path
    ) is None

    # A's close blocks citing a's item, never b's.
    result_a = run_hook(STOP, stop_payload(repo_dir, "session-a"), tmpdir=tmp_path)
    assert result_a["decision"] == "block"
    assert "a's item" in result_a["reason"]
    assert "b's item" not in result_a["reason"]

    # B symmetrically.
    result_b = run_hook(STOP, stop_payload(repo_dir, "session-b"), tmpdir=tmp_path)
    assert result_b["decision"] == "block"
    assert "b's item" in result_b["reason"]
    assert "a's item" not in result_b["reason"]


def test_unbound_session_with_marker_is_never_stop_blocked(repo_dir, tmp_path):
    # Session C's marker exists (SessionStart fired) but it never wrote
    # or spawned against a ledger. This is the actual fix: previously,
    # mtime-based "ownership" could let a stranger's fresh ledger block
    # a session that never touched it at all.
    write_ledger(repo_dir, "- [ ] 1. someone else's open item\n")
    write_marker(tmp_path, time.time(), session="session-c")  # no ledger=
    assert run_hook(STOP, stop_payload(repo_dir, "session-c"), tmpdir=tmp_path) is None


# --- unbind on a bound path going away ------------------------------------

def test_unbind_when_bound_path_is_deleted(repo_dir, tmp_path):
    ledger = write_ledger(repo_dir, "- [ ] 1. open\n")
    write_marker(tmp_path, time.time(), ledger=ledger)
    ledger.unlink()
    assert run_hook(STOP, stop_payload(repo_dir), tmpdir=tmp_path) is None
    assert "ledger" not in marker(tmp_path)


def test_unbind_when_bound_path_is_archived(repo_dir, tmp_path):
    ledger = write_ledger(repo_dir, "- [ ] 1. open\n")
    write_marker(tmp_path, time.time(), ledger=ledger)
    archived = ledger.with_name("LEDGER-topic-archive.md")
    ledger.rename(archived)
    assert run_hook(STOP, stop_payload(repo_dir), tmpdir=tmp_path) is None
    assert "ledger" not in marker(tmp_path)


def test_unbind_then_rebind_only_via_edit(repo_dir, tmp_path):
    # After an unbind the session is simply unbound (no auto-block, no
    # fallback to discovery) until it binds again by editing a ledger.
    ledger = write_ledger(repo_dir, "- [ ] 1. open\n")
    write_marker(tmp_path, time.time(), ledger=ledger)
    ledger.unlink()
    assert run_hook(STOP, stop_payload(repo_dir), tmpdir=tmp_path) is None

    fresh = write_ledger(repo_dir, "- [ ] 1. new open item\n")
    assert _is_deny(run_hook(SPAWN, spawn_payload(repo_dir), tmpdir=tmp_path))
    assert "ledger" not in marker(tmp_path)

    run_hook(BIND, bind_payload("test-session", fresh, tool_name="Edit"), tmpdir=tmp_path)
    assert marker(tmp_path)["ledger"] == os.path.realpath(str(fresh))
    assert run_hook(SPAWN, spawn_payload(repo_dir), tmpdir=tmp_path) is None
    result = run_hook(STOP, stop_payload(repo_dir), tmpdir=tmp_path)
    assert result["decision"] == "block"


# --- injector carry-forward ----------------------------------------------

def test_injector_carries_ledger_forward_on_reinjection(tmp_path):
    env = {"CLAUDE_PLUGIN_ROOT": str(REPO)}
    run_hook(INJECT, {"model": "claude-fable-5", "session_id": "s-carry"},
             env_extra=env, tmpdir=tmp_path)
    cache = marker_path(tmp_path, "s-carry")
    data = json.loads(cache.read_text(encoding="utf-8"))
    data["ledger"] = "/fake/repo/.workflow/LEDGER.md"
    cache.write_text(json.dumps(data), encoding="utf-8")

    # Re-injection (resume/clear/compact) must not drop the binding.
    run_hook(INJECT, {"model": "claude-fable-5", "session_id": "s-carry",
                      "source": "compact"},
             env_extra=env, tmpdir=tmp_path)
    assert (json.loads(cache.read_text(encoding="utf-8"))["ledger"]
            == "/fake/repo/.workflow/LEDGER.md")


def test_injector_omits_ledger_key_when_never_bound(tmp_path):
    env = {"CLAUDE_PLUGIN_ROOT": str(REPO)}
    run_hook(INJECT, {"model": "claude-fable-5", "session_id": "s-nocarry"},
             env_extra=env, tmpdir=tmp_path)
    data = json.loads(marker_path(tmp_path, "s-nocarry").read_text(encoding="utf-8"))
    assert "ledger" not in data


# --- verifier findings: regressions -------------------------------------

def test_no_bind_for_a_write_payload_naming_a_nonexistent_ledger(repo_dir, tmp_path):
    # A Write payload naming a ledger that was never actually created on
    # disk (failed tool call, or a path the model only intends to write)
    # must not rebind the marker away from an already-live ledger.
    live = write_ledger(repo_dir, "- [ ] 1. open\n")
    write_marker(tmp_path, time.time(), ledger=live)
    ghost = repo_dir / ".workflow" / "LEDGER-ghost.md"  # never created
    run_hook(BIND, bind_payload("test-session", ghost), tmpdir=tmp_path)
    assert marker(tmp_path)["ledger"] == os.path.realpath(str(live))


def test_edit_and_multiedit_tool_names_actually_bind(repo_dir, tmp_path):
    for tool_name in ("Edit", "MultiEdit"):
        session_id = f"s-{tool_name}"
        write_marker(tmp_path, time.time(), session=session_id)
        ledger = _named_ledger(repo_dir, f"LEDGER-{tool_name.lower()}.md")
        run_hook(
            BIND, bind_payload(session_id, ledger, tool_name=tool_name), tmpdir=tmp_path
        )
        assert marker(tmp_path, session_id)["ledger"] == os.path.realpath(str(ledger))


def test_archived_bound_ledger_never_falls_to_a_live_sibling(repo_dir, tmp_path):
    bound = write_ledger(repo_dir, "- [x] 1. done\n")
    write_marker(tmp_path, time.time(), ledger=bound)
    bound.rename(bound.with_name("LEDGER-topic-archive.md"))
    _named_ledger(repo_dir, "LEDGER-sibling.md", "- [ ] 1. open\n")
    assert _is_deny(run_hook(SPAWN, spawn_payload(repo_dir), tmpdir=tmp_path))
    assert "ledger" not in marker(tmp_path)


def test_corrupt_marker_json_never_blocks_or_crashes(repo_dir, tmp_path):
    write_ledger(repo_dir, "- [ ] 1. open\n")
    marker_path(tmp_path).write_text("not json at all", encoding="utf-8")
    assert run_hook(STOP, stop_payload(repo_dir), tmpdir=tmp_path) is None


def test_non_dict_marker_json_never_blocks_or_crashes(repo_dir, tmp_path):
    write_ledger(repo_dir, "- [ ] 1. open\n")
    marker_path(tmp_path).write_text("[1, 2, 3]", encoding="utf-8")
    assert run_hook(STOP, stop_payload(repo_dir), tmpdir=tmp_path) is None


@POSIX  # metrics land under expanduser("~"); HOME= only redirects it on posixpath
def test_ordinary_no_ledger_session_writes_no_metrics_line(repo_dir, tmp_path):
    # Regression for the upstream-parity fix: a marker exists (SessionStart
    # fired) but is unbound, AND the repo has no ledger at all — this is
    # the overwhelmingly common case (most turns, most sessions) and must
    # not cost a metrics line every single time.
    session_id = "s-no-ledger"
    write_marker(tmp_path, time.time(), session=session_id)
    home = tmp_path / "home"
    home.mkdir()
    env = {"HOME": str(home), "FABLE_ORCH_METRICS": "1"}
    result = run_hook(STOP, stop_payload(repo_dir, session_id), env_extra=env, tmpdir=tmp_path)
    assert result is None
    assert not (home / ".claude" / "fable-orch" / "metrics.jsonl").exists()


# --- foreign ledgers: never satisfy, never bind --------------------------

def _foreign_setup(repo_dir, tmp_path):
    """S1 bound to ledger A; S2 has a marker but no binding yet."""
    ledger_a = _named_ledger(repo_dir, "LEDGER-a.md", "- [ ] 1. a's item\n")
    write_marker(tmp_path, time.time(), session="s1", ledger=ledger_a)
    write_marker(tmp_path, time.time(), session="s2")
    return ledger_a


def test_spawn_never_adopts_a_ledger_another_live_session_holds(repo_dir, tmp_path):
    _foreign_setup(repo_dir, tmp_path)
    # S1's open ledger does not satisfy S2's gate, and S2 stays unbound...
    assert _is_deny(run_hook(SPAWN, spawn_payload(repo_dir, "s2"), tmpdir=tmp_path))
    assert "ledger" not in marker(tmp_path, "s2")
    # ...so its Stop is never held on S1's open items.
    assert run_hook(STOP, stop_payload(repo_dir, "s2"), tmpdir=tmp_path) is None


def test_task_create_never_adopts_a_foreign_ledger(repo_dir, tmp_path):
    _foreign_setup(repo_dir, tmp_path)
    assert run_hook(SPAWN, task_payload(repo_dir, "s2"), tmpdir=tmp_path) is None
    assert "ledger" not in marker(tmp_path, "s2")
    assert run_hook(STOP, stop_payload(repo_dir, "s2"), tmpdir=tmp_path) is None


def test_session_writing_its_own_ledger_holds_on_it_not_the_foreign_one(repo_dir, tmp_path):
    _foreign_setup(repo_dir, tmp_path)
    run_hook(SPAWN, spawn_payload(repo_dir, "s2"), tmpdir=tmp_path)
    ledger_b = _named_ledger(repo_dir, "LEDGER-b.md", "- [ ] 1. b's item\n")
    run_hook(BIND, bind_payload("s2", ledger_b), tmpdir=tmp_path)
    assert marker(tmp_path, "s2")["ledger"] == os.path.realpath(str(ledger_b))
    result = run_hook(STOP, stop_payload(repo_dir, "s2"), tmpdir=tmp_path)
    assert result["decision"] == "block"
    assert "b's item" in result["reason"]
    assert "a's item" not in result["reason"]


def test_ended_sessions_ledger_is_continued_only_by_an_edit(repo_dir, tmp_path):
    # resume / after /clear / a /spawn child: the ledger's owner marker is
    # gone, yet nothing adopts it implicitly — one Edit is the explicit act.
    ledger_a = _foreign_setup(repo_dir, tmp_path)
    marker_path(tmp_path, "s1").unlink()  # SessionEnd cleanup
    assert _is_deny(run_hook(SPAWN, spawn_payload(repo_dir, "s2"), tmpdir=tmp_path))
    assert "ledger" not in marker(tmp_path, "s2")
    run_hook(BIND, bind_payload("s2", ledger_a, tool_name="Edit"), tmpdir=tmp_path)
    assert run_hook(SPAWN, spawn_payload(repo_dir, "s2"), tmpdir=tmp_path) is None
    result = run_hook(STOP, stop_payload(repo_dir, "s2"), tmpdir=tmp_path)
    assert result["decision"] == "block" and "a's item" in result["reason"]


# --- 0.23.3: the ledger CLI binds; a binding survives SessionEnd + resume

CLEANUP = "cleanup_session_cache.py"


def bash_payload(repo, command, session_id="test-session", **extra):
    payload = {"session_id": session_id, "tool_name": "Bash", "cwd": str(repo),
               "tool_input": {"command": command},
               "tool_response": {"stdout": "ok", "stderr": "", "interrupted": False}}
    payload.update(extra)
    return payload


def test_ledger_cli_write_with_file_binds_like_an_edit(repo_dir, tmp_path):
    ledger = _named_ledger(repo_dir, "LEDGER-cli.md")
    real = os.path.realpath(str(ledger))
    for cmd in (f'ledger -f {ledger} mark 1 "done"',
                f"ledger note 1 'x' -f {ledger}",
                f"ledger add 'new item' --file={ledger}",
                f"ledger -f{ledger} defer 1 'later'",
                f'cd /tmp && /some/plugin/bin/ledger --file "{ledger}" mark 1'):
        write_marker(tmp_path, time.time())
        assert run_hook(BIND, bash_payload(repo_dir, cmd), tmpdir=tmp_path) is None
        assert marker(tmp_path)["ledger"] == real, cmd
    # the bound session now passes the spawn gate
    assert run_hook(SPAWN, spawn_payload(repo_dir), tmpdir=tmp_path) is None


def test_ledger_cli_relative_file_resolves_against_cwd(repo_dir, tmp_path):
    write_marker(tmp_path, time.time())
    ledger = _named_ledger(repo_dir, "LEDGER-rel.md")
    run_hook(BIND, bash_payload(repo_dir, "ledger -f .workflow/LEDGER-rel.md mark 1"),
             tmpdir=tmp_path)
    assert marker(tmp_path)["ledger"] == os.path.realpath(str(ledger))


def test_ledger_cli_status_bare_and_other_commands_never_bind(repo_dir, tmp_path):
    ledger = _named_ledger(repo_dir, "LEDGER-ro.md")
    for cmd in (f"ledger -f {ledger} status",       # read-only
                "ledger mark 1",                      # bare: names no file
                f"cat {ledger}",                      # not the CLI
                ):
        write_marker(tmp_path, time.time())
        run_hook(BIND, bash_payload(repo_dir, cmd), tmpdir=tmp_path)
        assert "ledger" not in marker(tmp_path), cmd
    write_marker(tmp_path, time.time())
    assert _is_deny(run_hook(SPAWN, spawn_payload(repo_dir), tmpdir=tmp_path))


def test_ledger_cli_on_a_non_ledger_or_archived_file_never_binds(repo_dir, tmp_path):
    archived = _named_ledger(repo_dir, "LEDGER-old-archive.md")
    outside = tmp_path / "LEDGER-x.md"
    outside.write_text("- [ ] 1. a\n", encoding="utf-8")
    for path in (archived, outside, repo_dir / ".workflow" / "LEDGER-missing.md"):
        write_marker(tmp_path, time.time())
        run_hook(BIND, bash_payload(repo_dir, f"ledger -f {path} mark 1"), tmpdir=tmp_path)
        assert "ledger" not in marker(tmp_path), path


def test_subagent_ledger_cli_never_binds_the_chair(repo_dir, tmp_path):
    write_marker(tmp_path, time.time())
    ledger = _named_ledger(repo_dir, "LEDGER-sub.md")
    run_hook(BIND, bash_payload(repo_dir, f"ledger -f {ledger} mark 1",
                                agent_id="a1", agent_type="scout"), tmpdir=tmp_path)
    assert "ledger" not in marker(tmp_path)


def test_ledger_cli_rebinds_to_the_named_file(repo_dir, tmp_path):
    first = _named_ledger(repo_dir, "LEDGER-a.md")
    second = _named_ledger(repo_dir, "LEDGER-b.md")
    write_marker(tmp_path, time.time(), ledger=os.path.realpath(str(first)))
    run_hook(BIND, bash_payload(repo_dir, f"ledger -f {second} note 1 'x'"),
             tmpdir=tmp_path)
    assert marker(tmp_path)["ledger"] == os.path.realpath(str(second))


def test_binding_survives_compact_and_the_spawn_gate_accepts_it(repo_dir, tmp_path):
    env = {"CLAUDE_PLUGIN_ROOT": str(REPO)}
    ledger = _named_ledger(repo_dir, "LEDGER-compact.md")
    run_hook(INJECT, {"model": "claude-fable-5", "session_id": "test-session"},
             env_extra=env, tmpdir=tmp_path)
    run_hook(BIND, bash_payload(repo_dir, f"ledger -f {ledger} mark 1"),
             env_extra=env, tmpdir=tmp_path)
    out = run_hook(INJECT, {"model": "claude-fable-5", "session_id": "test-session",
                            "source": "compact"}, env_extra=env, tmpdir=tmp_path)
    real = os.path.realpath(str(ledger))
    assert f"Live ledger for this session: {real}" in \
        out["hookSpecificOutput"]["additionalContext"]
    assert run_hook(SPAWN, spawn_payload(repo_dir), env_extra=env, tmpdir=tmp_path) is None


def test_binding_survives_session_end_and_resume(repo_dir, tmp_path):
    # 10.10.2026: SessionEnd deleted the marker, the resumed session got a
    # fresh one without `ledger`, and the spawn guard denied the chair.
    env = {"CLAUDE_PLUGIN_ROOT": str(REPO)}
    ledger = _named_ledger(repo_dir, "LEDGER-resume.md")
    real = os.path.realpath(str(ledger))
    run_hook(INJECT, {"model": "claude-fable-5", "session_id": "test-session"},
             env_extra=env, tmpdir=tmp_path)
    run_hook(BIND, bind_payload("test-session", ledger), env_extra=env, tmpdir=tmp_path)
    run_hook(CLEANUP, {"session_id": "test-session"}, env_extra=env, tmpdir=tmp_path)
    assert marker(tmp_path) is None
    out = run_hook(INJECT, {"model": "claude-fable-5", "session_id": "test-session",
                            "source": "resume"}, env_extra=env, tmpdir=tmp_path)
    assert marker(tmp_path)["ledger"] == real
    assert f"Live ledger for this session: {real}" in \
        out["hookSpecificOutput"]["additionalContext"]
    assert not (tmp_path / "fable-orch-binding-test-session.json").exists()  # consumed
    assert run_hook(SPAWN, spawn_payload(repo_dir), env_extra=env, tmpdir=tmp_path) is None


def test_stashed_binding_is_never_adopted_by_another_session(repo_dir, tmp_path):
    env = {"CLAUDE_PLUGIN_ROOT": str(REPO)}
    ledger = _named_ledger(repo_dir, "LEDGER-mine.md")
    write_marker(tmp_path, time.time(), session="s-old", ledger=os.path.realpath(str(ledger)))
    run_hook(CLEANUP, {"session_id": "s-old"}, env_extra=env, tmpdir=tmp_path)
    run_hook(INJECT, {"model": "claude-fable-5", "session_id": "s-new",
                      "source": "startup"}, env_extra=env, tmpdir=tmp_path)
    assert "ledger" not in marker(tmp_path, "s-new")
    assert _is_deny(run_hook(SPAWN, spawn_payload(repo_dir, session_id="s-new"),
                             env_extra=env, tmpdir=tmp_path))


def test_stashed_binding_to_an_archived_ledger_is_dropped(repo_dir, tmp_path):
    env = {"CLAUDE_PLUGIN_ROOT": str(REPO)}
    ledger = _named_ledger(repo_dir, "LEDGER-gone.md")
    write_marker(tmp_path, time.time(), ledger=os.path.realpath(str(ledger)))
    run_hook(CLEANUP, {"session_id": "test-session"}, env_extra=env, tmpdir=tmp_path)
    ledger.rename(ledger.with_name("LEDGER-gone-archive.md"))
    run_hook(INJECT, {"model": "claude-fable-5", "session_id": "test-session",
                      "source": "resume"}, env_extra=env, tmpdir=tmp_path)
    assert "ledger" not in marker(tmp_path)
