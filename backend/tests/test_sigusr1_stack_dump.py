"""SIGUSR1 stack-dump registration must survive, and never replace, whatever
disposition SIGUSR1 already had.

`faulthandler.register` defaults to `chain=False`, which *replaces* the signal's
existing handler, and defaults to `chain=True`-with-previous-when-asked otherwise.
Getting this wrong is fatal in two opposite directions, and both were shipped at
some point:

* `chain=True` against the **default** disposition chains to `SIG_DFL`, so the
  very first `kill -USR1 <pid>` during an incident kills the process instead of
  diagnosing it (reproduced: the child died of SIGUSR1 and never printed its
  sentinel, exit 138 at the shell). A diagnostic that terminates its own subject
  is worse than no diagnostic.
* `chain=False` against a **real operator handler** silently seizes SIGUSR1 from
  whoever installed it (a log-reopen hook, a profiler, a supervisor).

So the rule is not "always chain" -- it is "chain only to something that is not
SIG_DFL". These tests pin all of it:

* Argument level: the real keyword arguments handed to `faulthandler.register`,
  under a default disposition and under a pre-existing callable handler.
* End-to-end: child processes take a real SIGUSR1 and must both print a stack
  dump *and* keep running. The pytest process is never signalled.
* Concurrency: the once-per-process guard is read, used and written under a
  process-local lock, so two simultaneous worker starts cannot both arm it.

`SIGUSR1` is resolved through module-level `HAS_SIGUSR1`/`SIGUSR1` so this file
stays collectable on platforms that have no such signal: dereferencing
`signal.SIGUSR1` at import time would turn a skipped platform into a collection
error for the whole suite.
"""
import logging
import os
import signal
import subprocess
import sys
import textwrap
import threading
import time

import pytest

from app import observability

# Resolved once, at import time, so this module is collectable where SIGUSR1
# does not exist. Every SIGUSR1-dependent test carries @requires_sigusr1.
HAS_SIGUSR1 = hasattr(signal, "SIGUSR1")
SIGUSR1 = getattr(signal, "SIGUSR1", None)

requires_sigusr1 = pytest.mark.skipif(not HAS_SIGUSR1, reason="platform has no SIGUSR1")


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def _wait_until(predicate, timeout=5.0, interval=0.01):
    """Poll until `predicate()` is truthy or `timeout` elapses. Returns the last
    value, so a caller can assert on it instead of on a bare sleep."""
    deadline = time.monotonic() + timeout
    value = predicate()
    while not value and time.monotonic() < deadline:
        time.sleep(interval)
        value = predicate()
    return value


@pytest.fixture(autouse=True)
def recorded_register(monkeypatch):
    """Replace the faulthandler boundary for every test in this file.

    Autouse on purpose: `_register_sigusr1_stack_dumps` is called by
    `configure_logging()`, which many tests in this suite call. Without this
    every one of them would install a *real* SIGUSR1 handler inside the pytest
    process. Tests that need different behaviour re-patch `faulthandler.register`
    with the same monkeypatch; undo order restores the original.

    Yields the list of `(signum, kwargs)` the production code actually passed.
    """
    import faulthandler

    calls = []

    def _recorder(signum, **kwargs):
        calls.append((signum, kwargs))

    monkeypatch.setattr(faulthandler, "register", _recorder)
    return calls


@pytest.fixture(autouse=True)
def _reset_registration_flag():
    """Registration is once per *process*; each test starts from a clean guard so
    the guard itself is what is under test. The lock is a module-level constant
    and is deliberately not reset -- it must outlive individual tests."""
    previous = observability._sigusr1_registered
    observability._sigusr1_registered = False
    yield
    observability._sigusr1_registered = previous


@pytest.fixture
def sigusr1_disposition():
    """Set SIGUSR1's disposition for one test, then put the old one back.

    `signal.signal` is main-thread only, which is where pytest runs tests; the
    threads exercised in the concurrency tests only ever *read* the disposition.
    """
    original = signal.getsignal(SIGUSR1)

    def _set(handler):
        signal.signal(SIGUSR1, handler)
        return handler

    yield _set
    signal.signal(SIGUSR1, signal.SIG_DFL if original is None else original)


def _write_child(tmp_path, body):
    path = tmp_path / "sigusr1_child.py"
    path.write_text(textwrap.dedent(body))
    return path


def _run_child(tmp_path, body, timeout=60, **fmt):
    """Write and run a child process, with its own throwaway database and storage
    root so a signal test can never touch the project's real ones.

    `fmt` carries any further placeholders the caller's body needs.
    """
    backend = os.path.dirname(os.path.dirname(os.path.abspath(observability.__file__)))
    fields = {
        "backend": backend,
        "db": str(tmp_path / "child.db"),
        "storage": str(tmp_path / "uploads"),
    }
    fields.update(fmt)
    script = _write_child(tmp_path, body.format(**fields))
    return subprocess.run([sys.executable, str(script)],
                          capture_output=True, text=True, timeout=timeout)


# --------------------------------------------------------------------------
# argument-level contract
# --------------------------------------------------------------------------

@requires_sigusr1
def test_default_disposition_is_not_chained_to_sig_dfl(recorded_register, sigusr1_disposition):
    """The bug that killed the worker: chaining to SIG_DFL makes the diagnostic
    terminate its own subject.

    Asserted on the real keyword arguments, so the fix cannot quietly regress
    into `chain=True` for everyone.
    """
    sigusr1_disposition(signal.SIG_DFL)

    observability.configure_logging()

    assert len(recorded_register) == 1, "expected exactly one SIGUSR1 registration"
    signum, kwargs = recorded_register[0]
    assert signum == SIGUSR1
    assert kwargs.get("chain") is False, (
        "SIGUSR1 left on its default disposition must be registered with "
        "chain=False; chaining to SIG_DFL makes the first `kill -USR1` kill the "
        f"process (exit 128+SIGUSR1) instead of dumping stacks; got {kwargs!r}"
    )


@requires_sigusr1
def test_registration_chains_to_any_pre_existing_sigusr1_handler(recorded_register, sigusr1_disposition):
    """chain=True is the whole point once an operator handler exists: a stack
    dump must not seize the signal from whoever installed it."""
    ran = []
    sigusr1_disposition(lambda signum, frame: ran.append(signum))

    observability.configure_logging()

    assert len(recorded_register) == 1, "expected exactly one SIGUSR1 registration"
    signum, kwargs = recorded_register[0]
    assert signum == SIGUSR1
    assert kwargs.get("chain") is True, (
        "a pre-existing callable SIGUSR1 handler must still run after the dump, "
        f"which requires chain=True; got kwargs={kwargs!r}"
    )


@requires_sigusr1
def test_unknown_disposition_is_treated_as_unknown_not_as_default(
    monkeypatch, recorded_register
):
    """`getsignal` returning `None` means "not installed from Python" -- it does
    *not* mean the default action is in place.

    Per the signal docs, only `SIG_DFL` reports that the default disposition is
    active. `None` reports that whatever is handling SIGUSR1 was installed by C
    code or a native extension, so it is real and unowned by us. Registering with
    `chain=False` there would replace that handler outright: the native owner
    loses its signal, and its `chain_to_previous` plumbing is gone for good. The
    conservative reading of an unknown disposition is to chain, not to clobber.

    Distinguishing the two is the entire fix: `None` is a third disposition,
    neither the terminating default nor a handler we can name.
    """
    monkeypatch.setattr(signal, "getsignal", lambda signum: None)

    observability.configure_logging()

    assert len(recorded_register) == 1, "expected exactly one SIGUSR1 registration"
    signum, kwargs = recorded_register[0]
    assert signum == SIGUSR1
    assert kwargs.get("chain") is True, (
        "an unknown (None) SIGUSR1 disposition must be chained, not replaced: "
        "only SIG_DFL justifies chain=False, because only SIG_DFL's action is "
        "known to be safe to displace. chain=False here would silently seize the "
        f"signal from a native/C-level owner; got kwargs={kwargs!r}"
    )


@requires_sigusr1
def test_registration_covers_all_threads(recorded_register, sigusr1_disposition):
    """A hung worker is a multi-thread story: dump every thread, not just the
    one the signal happens to land on."""
    sigusr1_disposition(signal.SIG_DFL)

    observability.configure_logging()

    _, kwargs = recorded_register[0]
    assert kwargs.get("all_threads") is True, (
        f"stack dumps must cover all threads; got kwargs={kwargs!r}"
    )


# --------------------------------------------------------------------------
# observable, end-to-end behaviour (real signal, in a child process)
# --------------------------------------------------------------------------

_CHILD_DEFAULT = '''
    import os, signal, sys, time
    sys.path.insert(0, {backend!r})
    os.environ["DATABASE_URL"] = "sqlite:///{db!r}"
    os.environ["STORAGE_ROOT"] = {storage!r}
    os.environ["ENVIRONMENT"] = "development"

    from app.observability import configure_logging

    # This is the whole point of the test: the child must still be on the
    # default disposition, i.e. nobody installed a handler before configure_logging().
    disposition = signal.getsignal(signal.SIGUSR1)
    print("DISPOSITION", disposition, flush=True)
    if disposition is not signal.SIG_DFL:
        print("CHILD-SETUP-WRONG", flush=True)
        raise SystemExit(3)

    configure_logging()

    # Busy-loop in Python bytecode (not time.sleep) so the signal is handled
    # promptly, and keep running long enough afterwards to prove we survived.
    stop = time.time() + 0.3
    while time.time() < stop:
        pass
    os.kill(os.getpid(), signal.SIGUSR1)
    stop = time.time() + 0.8
    while time.time() < stop:
        pass
    print("CHILD-DONE", flush=True)
'''

_CHILD_PRIOR_HANDLER = '''
    import os, signal, sys, threading, time
    sys.path.insert(0, {backend!r})
    os.environ["DATABASE_URL"] = "sqlite:///{db!r}"
    os.environ["STORAGE_ROOT"] = {storage!r}
    os.environ["ENVIRONMENT"] = "development"

    from app.observability import configure_logging

    marker = {marker!r}

    def prior_handler(signum, frame):
        # The "operator" handler that must survive registration.
        with open(marker, "a") as fh:
            fh.write("RAN\\n")

    signal.signal(signal.SIGUSR1, prior_handler)
    configure_logging()

    threading.Thread(target=lambda: time.sleep(10), daemon=True).start()

    stop = time.time() + 0.3
    while time.time() < stop:
        pass
    os.kill(os.getpid(), signal.SIGUSR1)
    stop = time.time() + 0.8
    while time.time() < stop:
        pass
    print("CHILD-DONE", flush=True)
'''

@requires_sigusr1
def test_real_sigusr1_under_default_disposition_dumps_and_process_survives(tmp_path):
    """`kill -USR1` on a stock worker must produce a dump and leave the worker
    running -- the property that was missing, and that the shell saw as exit 138.

    Runs entirely in a child process: the pytest process is never signalled.
    """
    proc = _run_child(tmp_path, _CHILD_DEFAULT)

    assert "CHILD-SETUP-WRONG" not in proc.stdout, (
        f"the child must start on the default SIGUSR1 disposition; stdout:\n{proc.stdout}"
    )
    assert "Current thread" in proc.stderr, (
        "the default-disposition worker must still print a real stack dump; "
        f"stderr:\n{proc.stderr}"
    )
    assert "CHILD-DONE" in proc.stdout, (
        "the worker must survive its own diagnostic and run to completion, but "
        f"it stopped early: rc={proc.returncode}\nstdout:\n{proc.stdout}\n"
        f"stderr:\n{proc.stderr}"
    )
    assert proc.returncode == 0, (
        f"the worker must exit normally, not die from SIGUSR1 "
        f"(rc={proc.returncode}, i.e. signal {-proc.returncode if proc.returncode < 0 else 'n/a'}); "
        f"stderr:\n{proc.stderr}"
    )


@requires_sigusr1
def test_real_sigusr1_preserves_pre_existing_handler_and_still_dumps(tmp_path):
    """The contract as an operator experiences it, with a real signal.

    Runs entirely in a child process: the pytest process is never signalled.
    """
    marker = tmp_path / "prior.log"
    marker.write_text("")
    proc = _run_child(
        tmp_path,
        _CHILD_PRIOR_HANDLER,
        marker=str(marker),
    )

    assert "CHILD-DONE" in proc.stdout, (
        f"child failed: rc={proc.returncode}\n{proc.stderr}"
    )
    ran = marker.read_text().count("RAN")
    assert ran == 1, (
        f"the pre-existing SIGUSR1 handler must still run exactly once after "
        f"configure_logging(); it ran {ran} time(s). stderr:\n{proc.stderr}"
    )
    assert "Current thread" in proc.stderr, (
        f"configure_logging() must still install a real stack dump; stderr:\n{proc.stderr}"
    )


# --------------------------------------------------------------------------
# idempotence
# --------------------------------------------------------------------------

def test_repeated_calls_do_not_accumulate_logger_handlers(recorded_register):
    """configure_logging() is called on every worker start; handlers must not pile up."""
    logger = logging.getLogger("enterai.observability")
    before = list(logger.handlers)
    try:
        for _ in range(5):
            observability.configure_logging()
        assert len(logger.handlers) == 1, (
            f"expected exactly one handler after 5 calls, got {len(logger.handlers)}"
        )
    finally:
        logger.handlers = before


@requires_sigusr1
def test_repeated_calls_register_the_signal_at_most_once(recorded_register, sigusr1_disposition):
    """Re-arming SIGUSR1 on every call would stack dump handlers on top of each
    other. The contract is: register once per process, then be a no-op."""
    sigusr1_disposition(signal.SIG_DFL)

    for _ in range(4):
        observability.configure_logging()

    assert len(recorded_register) == 1, (
        f"SIGUSR1 must be registered at most once per process; got "
        f"{len(recorded_register)} registrations"
    )


def test_repeated_calls_keep_access_log_filter_unique(recorded_register):
    access = logging.getLogger("uvicorn.access")
    before = list(access.filters)
    try:
        for _ in range(3):
            observability.configure_logging()
        ours = [f for f in access.filters if type(f).__name__ == "AccessQueryFilter"]
        assert len(ours) == 1, f"AccessQueryFilter duplicated: {len(ours)} copies"
    finally:
        access.filters = before


# --------------------------------------------------------------------------
# concurrency: the once-per-process guard must be atomic
# --------------------------------------------------------------------------

@requires_sigusr1
def test_concurrent_callers_register_exactly_once(monkeypatch, sigusr1_disposition):
    """Two worker starts racing must arm SIGUSR1 once, not twice.

    The fake `faulthandler.register` blocks on release, so the *only* thing that
    can keep the second caller out is a lock held across the check/register/set.
    Unsynchronised code lets caller #2 walk straight past the guard and reach
    `register()` while caller #1 is still inside it -- two handlers, each of
    which the other then chains through.
    """
    import faulthandler

    sigusr1_disposition(signal.SIG_DFL)

    entered = []          # registrations that have *entered* register()
    completed = []        # registrations that have returned
    release = threading.Event()
    errors = []

    def _blocking_register(signum, **kwargs):
        entered.append((signum, kwargs))
        release.wait(30)
        completed.append((signum, kwargs))

    monkeypatch.setattr(faulthandler, "register", _blocking_register)

    def _call():
        try:
            observability.configure_logging()
        except BaseException as exc:  # noqa: BLE001 - surfaced in the assert below
            errors.append(exc)

    first = threading.Thread(target=_call)
    second = threading.Thread(target=_call)

    first.start()
    # Wait until caller #1 is provably parked inside register().
    assert _wait_until(lambda: len(entered) == 1, timeout=5), (
        f"caller #1 never reached faulthandler.register; entered={len(entered)}"
    )

    second.start()
    # If the guard is unsynchronised, #2 finishes (or blocks in register) fast.
    # If it is locked, #2 is stuck on the lock and stays alive. Wait for
    # whichever happens, up to a bound, so the assertion below is not timing-fragile.
    _wait_until(lambda: not second.is_alive(), timeout=1.0)

    assert len(entered) == 1, (
        f"a concurrent caller passed the once-per-process guard: {len(entered)} "
        f"call(s) are inside faulthandler.register() at once"
    )

    release.set()
    first.join(30)
    second.join(30)

    assert not errors, f"concurrent callers raised: {errors!r}"
    assert len(completed) == 1, (
        f"exactly one registration must complete; {len(completed)} completed"
    )
    assert observability._sigusr1_registered is True, (
        "the successful registration must have set the once-per-process guard"
    )


# --------------------------------------------------------------------------
# failure handling: graceful where graceful is correct, loud where it is not
# --------------------------------------------------------------------------

@requires_sigusr1
def test_non_main_thread_registration_is_graceful(monkeypatch, sigusr1_disposition):
    """A RuntimeError (e.g. registering off the main thread under some test
    runners) must not take worker startup down with it."""
    import faulthandler

    sigusr1_disposition(signal.SIG_DFL)

    def _raise(signum, **kwargs):
        raise RuntimeError("signal only works in main thread")

    monkeypatch.setattr(faulthandler, "register", _raise)
    observability.configure_logging()  # must not raise

    # and the rest of configure_logging still did its job
    assert logging.getLogger("enterai.observability").handlers


@requires_sigusr1
def test_unrelated_registration_failure_is_not_swallowed(monkeypatch, sigusr1_disposition):
    """Only the known-unsupported cases degrade silently. A TypeError means we
    called faulthandler wrongly, and hiding it would ship a broken signal hook."""
    import faulthandler

    sigusr1_disposition(signal.SIG_DFL)

    def _raise(signum, **kwargs):
        raise TypeError("register() got an unexpected keyword argument")

    monkeypatch.setattr(faulthandler, "register", _raise)
    with pytest.raises(TypeError):
        observability.configure_logging()


# --------------------------------------------------------------------------
# retry: the once-only guard records a *successful* registration, not an attempt
# --------------------------------------------------------------------------

@requires_sigusr1
@pytest.mark.parametrize("error", [
    RuntimeError("signal only works in main thread"),
    ValueError("signal number out of range"),
], ids=["runtime_error", "value_error"])
def test_failed_registration_is_retried_exactly_once_by_a_later_call(
    monkeypatch, sigusr1_disposition, error
):
    """A failed attempt must not permanently disable stack dumps.

    faulthandler.register() fails for transient, environmental reasons (the call
    landed off the main thread, a signal number this platform rejects). If the
    once-only guard were set by the *attempt* rather than by its success, a later
    valid configure_logging() -- the normal main-thread worker start -- would
    silently never arm diagnostics, and the operator's `kill -USR1` during an
    incident would do nothing at all.

    The contract pinned here: failure stays graceful and leaves the guard unset;
    the next call retries exactly once with the full argument contract; that
    success is then terminal.
    """
    import faulthandler

    sigusr1_disposition(signal.SIG_DFL)

    calls = []
    state = {"should_fail": True}

    def _flaky(signum, **kwargs):
        calls.append((signum, kwargs))
        if state["should_fail"]:
            state["should_fail"] = False
            raise error

    monkeypatch.setattr(faulthandler, "register", _flaky)

    # 1. First call: registration is unavailable. Must not raise...
    observability.configure_logging()
    assert len(calls) == 1
    # ...and must not consume the once-only guard.
    assert observability._sigusr1_registered is False, (
        "a failed registration attempt must not set the once-only guard, or a "
        "later valid call can never install diagnostics; got "
        f"_sigusr1_registered={observability._sigusr1_registered!r}"
    )

    # 2. Later call: retries, exactly once, with the full argument contract.
    observability.configure_logging()
    assert len(calls) == 2, (
        f"a later configure_logging() must retry registration after a failed "
        f"attempt; saw {len(calls)} call(s) in total"
    )
    signum, kwargs = calls[-1]
    assert signum == SIGUSR1
    assert kwargs.get("all_threads") is True, (
        f"the retry must preserve all_threads=True; got kwargs={kwargs!r}"
    )
    # The default disposition is still in place, so the retry must not chain
    # to SIG_DFL -- that is the bug that killed the worker.
    assert kwargs.get("chain") is False, (
        f"the retry must not chain to SIG_DFL; got kwargs={kwargs!r}"
    )
    assert observability._sigusr1_registered is True

    # 3. A success is terminal: the guard, not the error, is what stops re-arming.
    observability.configure_logging()
    assert len(calls) == 2, (
        f"after a successful registration no further attempts may be made; saw "
        f"{len(calls)} call(s) in total"
    )


@requires_sigusr1
def test_retry_still_leaves_the_rest_of_logging_configured(monkeypatch, sigusr1_disposition):
    """A failed registration degrades one feature, not all of logging setup:
    the retry path must keep the logger handler and access filter in place."""
    import faulthandler

    sigusr1_disposition(signal.SIG_DFL)

    state = {"should_fail": True}

    def _flaky(signum, **kwargs):
        if state["should_fail"]:
            state["should_fail"] = False
            raise RuntimeError("signal only works in main thread")

    monkeypatch.setattr(faulthandler, "register", _flaky)

    logger = logging.getLogger("enterai.observability")
    saved = list(logger.handlers)
    logger.handlers = []  # other tests in this file leave a handler behind
    try:
        observability.configure_logging()
        after_failure = list(logger.handlers)
        observability.configure_logging()  # the retry
        assert len(after_failure) == 1, (
            "logging setup must complete even when signal registration fails"
        )
        assert logger.handlers == after_failure, (
            "the retry must not duplicate the logger handler; got "
            f"{logger.handlers!r} after {after_failure!r}"
        )
    finally:
        logger.handlers = saved


def test_missing_sigusr1_platform_stays_terminal(monkeypatch):
    """The one case that must *not* retry: no SIGUSR1 on this platform means there
    is nothing to arm, so every later call would repeat a hopeless check."""
    import faulthandler

    calls = []

    def _recorder(signum, **kwargs):
        calls.append((signum, kwargs))

    monkeypatch.setattr(faulthandler, "register", _recorder)
    monkeypatch.delattr(signal, "SIGUSR1", raising=False)

    observability.configure_logging()
    assert observability._sigusr1_registered is True
    observability.configure_logging()

    assert calls == [], f"no SIGUSR1 means no registration at all; got {calls!r}"
