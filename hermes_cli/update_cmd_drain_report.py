"""Name what a draining gateway is waiting on while ``hermes update`` blocks on it.

The gateway's in-band restart (SIGUSR1 → ``request_restart``) defers ``stop()`` until in-flight
work finishes, capped by ``agent.restart_after_turn_timeout`` (30 min by default). From the
updater's side that was a bare "draining (up to 1875s)..." followed by silence, which reads as a
hung update. The gateway publishes each unit it is holding for in ``gateway_state.json``
(``active_work``, written by ``GatewayShutdownMixin._describe_active_work``); this module turns
that into progress lines.
"""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Callable, Optional

# Progress cadence: the gateway refreshes ``active_work`` every 30s; printing faster only repeats it.
DRAIN_REPORT_INTERVAL_S = 30.0


def _fmt_elapsed(seconds: object) -> str:
    try:
        total = int(float(seconds))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return "?"
    return f"{total // 60}m{total % 60:02d}s" if total >= 60 else f"{total}s"


_LONG_HEX = re.compile(r"[0-9a-f]{16,}")


def _short_session(session: str) -> str:
    """Session keys end in a 64-hex id; 8 chars still tell them apart on one screen."""
    return _LONG_HEX.sub(lambda m: m.group(0)[:8] + "…", session)


def _cron_job_name(job_id: str, home: Optional[Path]) -> Optional[str]:
    """``name`` from the profile's ``jobs.json`` (None when unreadable — the id alone still identifies it)."""
    try:
        from hermes_constants import reset_hermes_home_override, set_hermes_home_override
        from cron.jobs import load_jobs

        token = set_hermes_home_override(home) if home else None
        try:
            for job in load_jobs():
                if str(job.get("id")) == job_id:
                    return str(job.get("name") or "") or None
        finally:
            if token is not None:
                reset_hermes_home_override(token)
    except Exception:
        return None
    return None


def describe_active_work_unit(unit: dict, home: Optional[Path] = None) -> str:
    """One human line for one ``active_work`` entry; unknown shapes degrade to their ``kind``."""
    kind = str(unit.get("kind") or "work")
    pid = unit.get("pid")
    pid_part = f" pid {pid}" if pid else ""
    elapsed = unit.get("elapsed_s")
    elapsed_part = f", running {_fmt_elapsed(elapsed)}" if elapsed is not None else ""
    if kind == "cron":
        job_id = str(unit.get("job_id") or "?")
        name = _cron_job_name(job_id, home)
        label = f"cron job {job_id}" + (f" ({name})" if name else "")
        where = f" in external worker{pid_part}" if unit.get("external") else f" in-process{pid_part}"
        return f"{label}{where}{elapsed_part}"
    if kind == "chat":
        # In-process chat turns all carry the gateway's own pid: it names nothing, so it is left out.
        session = _short_session(str(unit.get("session") or "?"))
        model = unit.get("model")
        tool = unit.get("current_tool")
        detail = ", ".join(p for p in (f"model {model}" if model else "", f"tool {tool}" if tool else "") if p)
        return f"chat turn {session}{elapsed_part}" + (f" [{detail}]" if detail else "")
    if kind == "delegation":
        goal = str(unit.get("goal") or "").strip()
        return (f"background delegation {unit.get('delegation_id') or '?'}{pid_part}{elapsed_part}"
                + (f" [{goal}]" if goal else ""))
    session = unit.get("session")
    return f"{kind} run" + (f" {session}" if session else "") + f"{pid_part}{elapsed_part}"


def read_active_work(home: Optional[Path] = None) -> Optional[list]:
    """``active_work`` as the gateway last published it, or None (old gateway / not draining / unreadable)."""
    try:
        from gateway.status import read_runtime_status

        record = read_runtime_status(home / "gateway_state.json" if home else None) or {}
        work = record.get("active_work")
        return list(work) if isinstance(work, list) else None
    except Exception:
        return None


def _tui_command(home: Optional[Path]) -> str:
    """The TUI command that opens *home*'s profile: a bare ``hermes`` opens the default one."""
    from hermes_constants import profile_name_for_home

    name = profile_name_for_home(home)
    return f"hermes -p {name} --tui" if name and name != "default" else "hermes --tui"


def _unit_id(unit: object) -> str:
    """Stable identity of one unit across reports (elapsed and current tool change every tick)."""
    if not isinstance(unit, dict):
        return repr(unit)
    for key in ("session", "job_id", "delegation_id"):
        if unit.get(key):
            return f"{unit.get('kind')}:{unit[key]}"
    return f"{unit.get('kind')}:{unit.get('pid')}"


def _unit_name(unit: object) -> str:
    if not isinstance(unit, dict):
        return "work"
    name = unit.get("session") or unit.get("job_id") or unit.get("delegation_id") or unit.get("kind") or "work"
    return _short_session(str(name))


def _unit_short(unit: object) -> str:
    """Name and age of one unit for the one-line repeat."""
    age = unit.get("elapsed_s") if isinstance(unit, dict) else None
    return _unit_name(unit) + (f" {_fmt_elapsed(age)}" if age is not None else "")


def format_drain_line(work: Optional[list], *, remaining_s: float) -> str:
    """One line for a report whose units did not change since the last full block."""
    head = f"  ⏳ {max(remaining_s, 0):.0f}s left"
    if not work:
        return head + " · nothing reported, the gateway should exit momentarily"
    return head + f" · waiting on {len(work)}: " + ", ".join(_unit_short(u) for u in work)


def format_drain_report(work: Optional[list], *, remaining_s: float, home: Optional[Path] = None) -> str:
    """Multi-line progress block: what the gateway is waiting on plus how to stop waiting."""
    lines = [f"  ⏳ still waiting for idle — {max(remaining_s, 0):.0f}s left of this wait "
             "(live work is never interrupted; past it the gateway keeps its restart pending)"]
    if work is None:
        lines.append("     (gateway did not report what it is waiting on — pre-update gateway or unreadable state file)")
    elif not work:
        lines.append("     (no active work reported; the gateway should exit momentarily)")
    else:
        lines.append(f"     waiting on {len(work)} active work unit(s):")
        lines.extend(f"       • {describe_active_work_unit(u, home)}" for u in work)
        if any(isinstance(u, dict) and u.get("kind") == "chat" for u in work):
            # The draining gateway stays reachable for this (#139041): a chat turn parked on an
            # approval/clarify is answered from a client, not waited out.
            lines.append("     a chat turn waiting on an approval or clarify question can be answered now: "
                         f"open it with `{_tui_command(home)}` (resume the session)")
        lines.append("     finish or kill the work above to restart now, or `hermes update --force` "
                     "to stop it now")
    return "\n".join(lines)


def drain_progress_reporter(home: Optional[Path] = None, *, budget_s: float,
                            interval_s: float = DRAIN_REPORT_INTERVAL_S,
                            emit: Callable[[str], None] = print) -> Callable[[], None]:
    """Return a zero-arg callback for ``_wait_for_pid_exit(on_progress=...)`` that reports the drain
    every ``interval_s``: the full block (units plus how to release them) the first time and whenever
    a new unit appears, then one line while the same units are held, naming any that finished."""
    started = time.monotonic()
    state: dict = {"last": started, "seen": None}

    def _tick() -> None:
        now = time.monotonic()
        if now - state["last"] < interval_s:
            return
        state["last"] = now
        work = read_active_work(home)
        remaining = budget_s - (now - started)
        ids = None if work is None else [_unit_id(u) for u in work]
        seen = state["seen"]
        if seen is None or ids is None or any(i not in seen for i in ids):
            emit(format_drain_report(work, remaining_s=remaining, home=home))
        else:
            done = [name for i, name in seen.items() if i not in ids]
            line = format_drain_line(work, remaining_s=remaining)
            emit(("  ✓ finished: " + ", ".join(done) + "\n" if done else "") + line)
        state["seen"] = None if work is None else {_unit_id(u): _unit_name(u) for u in work}

    return _tick

    return _tick


def restart_left_pending(pid: int, home: Optional[Path] = None, *, start_time: Optional[int]) -> bool:
    """True when gateway *pid* outlived the wait still holding the restart SIGUSR1 asked for: the
    same process (``start_time``, read when it was signalled: a PID recycled during a 30-minute wait
    is someone else, who asked nothing), reachable by that signal (one that could not be sent asked
    nothing) and not provably wedged (a dead loop is not live work; its caller keeps the bounded
    escalation). Such a gateway restarts itself at its first idle moment, so nothing may stop it on
    the wait's account. An unknown start time proves no identity: not pending."""
    import os
    import signal
    if not hasattr(signal, "SIGUSR1") or pid <= 0 or start_time is None:
        return False
    try:
        # Signal 0 probes, and fails exactly where SIGUSR1 could not have been delivered.
        os.kill(pid, 0)  # windows-footgun: ok — POSIX only (SIGUSR1-guarded above)
    except OSError:
        return False
    from gateway.status import get_process_start_time
    if get_process_start_time(pid) != start_time:
        return False
    from hermes_cli.gateway import GATEWAY_LOOP_WEDGED, probe_gateway_loop_liveness
    return probe_gateway_loop_liveness(pid, home=home) != GATEWAY_LOOP_WEDGED


def leave_restart_pending(pid: int, label: str, home: Optional[Path] = None, *, start_time: Optional[int],
                          supervised: bool = True) -> bool:
    """When :func:`restart_left_pending` holds, say so (:func:`report_restart_left_pending`) and return
    True: the caller must not stop, kill or force-restart *pid*."""
    if not restart_left_pending(pid, home, start_time=start_time):
        return False
    report_restart_left_pending(label, home, supervised=supervised)
    return True


def report_restart_left_pending(label: str, home: Optional[Path] = None, *, supervised: bool = True,
                                emit: Callable[[str], None] = print) -> None:
    """The one line a caller prints instead of stopping a gateway whose wait ran out."""
    work = read_active_work(home)
    what = "; ".join(describe_active_work_unit(u, home) for u in work if isinstance(u, dict)) if work else ""
    after = ("restarts itself on the new code once idle" if supervised else
             "exits once idle (start it again with `hermes gateway run` if nothing relaunches it)")
    emit(f"  ↻ {label}: still running live work{f' ({what})' if what else ''} — left running; it {after}. "
         "`hermes update --force` stops it now.")
