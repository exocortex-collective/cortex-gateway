"""The container must be able to start without the network.

`uv run` re-resolves the project on a container's first start and fetches
hatchling from pypi to do it, so every fresh container needs working DNS --
and a redeploy, or an ofelia job with `delete: true`, makes fresh ones.

SCOPE, because it is narrower than it looks: this covers the IMAGE DEFAULT
only. A compose `command:` OVERRIDES the image CMD, and nine of the eleven
cortex services in Portainer stack 48 pass their own `uv run python -m ...`,
so the image being correct does not make production correct. Tracked in
cortex-4da6.
"""

from __future__ import annotations

import json
import pathlib
import re

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent

# The one correct start command. Asserted whole rather than probed for
# properties: there is exactly one right answer here, so equality is both the
# simplest assertion and the strongest. It rejects a boot-time `pip install`,
# a `python -c` that shells out, and a `/app/.venv/bin/../../..` path that
# escapes the venv, without needing to name any of them.
EXPECTED_START = [
    "/app/.venv/bin/gunicorn",
    "-w",
    "1",
    "-b",
    "0.0.0.0:8080",
    "-b",
    "0.0.0.0:8098",
    "gateway.app:create_app()",
]


def _effective_argv(dockerfile: str) -> list[str]:
    """The argv docker would start the container with, from this file alone.

    Three rules, each measured against `docker inspect` rather than read off
    the docs:

    1. Only the LAST ENTRYPOINT and the LAST CMD count; an earlier one of
       either is dead text. Collecting every match reads a stale line as live,
       which both passes a Dockerfile whose final CMD is wrong and fails one
       whose final CMD is right.
    2. Only the final build stage ships, so an earlier stage's CMD is not ours.
    3. An instruction whose argument is a valid JSON string array is exec
       form; ANYTHING ELSE is shell form and becomes /bin/sh -c <the raw
       text>. That includes an exec-looking array with a trailing comment,
       because docker treats `#` as a comment only at the start of a line.

    Resolution is then Entrypoint + Cmd, with no special case: for a
    shell-form ENTRYPOINT docker still keeps the CMD and passes it as $0 of
    the shell, so it never reaches the command -- but it is in the argv, and
    this returns the argv.

    Only what the Dockerfile DECLARES; an inherited CMD or ENTRYPOINT from the
    base image is not visible here.
    """
    # A command split across lines is ONE command; a per-line scan reads its
    # halves as unrelated.
    joined = re.sub(r"\\\n\s*", " ", dockerfile)
    joined = joined[joined.rfind("\nFROM ") + 1 :] if "\nFROM " in joined else joined

    def last(kind: str) -> str | None:
        # re.IGNORECASE: docker instruction keywords are case-insensitive, so
        # a lowercase `cmd` is a real CMD.
        found = re.findall(rf"^\s*{kind}\s+(.+)$", joined, re.MULTILINE | re.IGNORECASE)
        return found[-1].strip() if found else None

    def parse(raw: str) -> list[str]:
        try:
            argv = json.loads(raw)
        except json.JSONDecodeError:
            return ["/bin/sh", "-c", raw]
        # A JSON value that is not an array is shell form: measured, docker
        # reads CMD "a", CMD 42 and CMD {"a": 1} all as /bin/sh -c <raw>.
        return argv if isinstance(argv, list) else ["/bin/sh", "-c", raw]

    return [a for part in (last("ENTRYPOINT"), last("CMD")) if part for a in parse(part)]


def _uv_syncs(dockerfile: str) -> list[str]:
    """Every `uv sync` that actually runs, with its flags.

    Taken per shell command rather than per line, so `RUN echo uv sync` is not
    a sync while `RUN cd /app && uv sync` and
    `RUN --mount=type=cache,... uv sync` are.
    """
    joined = re.sub(r"\\\n\s*", " ", dockerfile)
    syncs = []
    for body in re.findall(r"^\s*RUN\s+(.+)$", joined, re.MULTILINE | re.IGNORECASE):
        for segment in re.split(r"&&|\|\||;|\|", body):
            # RUN flags (--mount=...) and inline env assignments sit before
            # the command itself.
            segment = re.sub(r"^\s*(?:(?:--\S+|\w+=\S*)\s+)*", "", segment)
            # `uv --no-cache sync` puts a flag before the subcommand.
            if re.match(r"uv\s+(?:-\S+\s+)*sync\b", segment):
                syncs.append(segment)
    return syncs


def _installs_the_project(dockerfile: str) -> bool:
    """Whether some `uv sync` puts the project itself into the venv."""
    return any("--no-install-" not in s for s in _uv_syncs(dockerfile))


def test_the_container_starts_with_the_baked_venv():
    """The whole point: the start command must be the interpreter already
    built into the image, so nothing is resolved or fetched at boot."""
    argv = _effective_argv((REPO / "Dockerfile").read_text())
    assert argv, "the Dockerfile declares no CMD or ENTRYPOINT"
    assert argv == EXPECTED_START, (
        f"the container's start command is not the baked venv, so it resolves "
        f"the project at boot.\n  expected: {EXPECTED_START}\n  found:    {argv}"
    )


def test_a_uv_sync_installs_the_project_itself():
    """The other half of the bug: pointing at the baked venv only works if the
    project is IN it. A sync that skips installing the project leaves no .pth,
    so the boot fails with ModuleNotFoundError rather than a network error --
    a different symptom, the same outage.

    A STATIC APPROXIMATION of a build-time property, and nothing in this
    repo's CI boots the image, so this is all there is. It cannot see a later
    `RUN rm -rf /app/.venv`, nor `ENV UV_NO_INSTALL_PROJECT=1`, which is a
    real uv variable (triage-0520). Only a built image could.
    """
    assert _uv_syncs(
        (REPO / "Dockerfile").read_text()
    ), "the Dockerfile never runs `uv sync`, so no venv is built"
    assert _installs_the_project((REPO / "Dockerfile").read_text()), (
        "every `uv sync` carries a --no-install-* flag, so the project itself "
        "is never installed and the baked venv cannot import it"
    )


# The rules above are exercised by exactly ONE branch of the real Dockerfile
# (one stage, no ENTRYPOINT, one exec-form CMD), so mutating any of the others
# away leaves every test green. These cases are the regression test for the
# rules themselves: synthetic Dockerfiles, derived from EXPECTED_START where
# that is portable, so they hold for whichever service this repo builds.
_RIGHT = json.dumps(EXPECTED_START)
_WRONG = json.dumps(["python"] + EXPECTED_START[1:])
_VIA_UV = json.dumps(["uv", "run"] + EXPECTED_START)

START_CASES = [
    ("the exec-form start command", f"CMD {_RIGHT}", True),
    ("a stale earlier CMD with a correct final one", f"CMD {_VIA_UV}\nCMD {_RIGHT}", True),
    ("an ENTRYPOINT [] reset", f"ENTRYPOINT []\nCMD {_RIGHT}", True),
    ("a lowercase cmd keyword", f"cmd {_RIGHT}", True),
    (
        "a CMD split over continuation lines",
        "CMD [" + ", \\\n    ".join(json.dumps(a) for a in EXPECTED_START) + "]",
        True,
    ),
    (
        "an earlier stage's CMD, which is dead",
        f"FROM busybox AS build\nCMD {_WRONG}\nFROM python:3.11-slim\nCMD {_RIGHT}",
        True,
    ),
    ("the correct CMD followed by a wrong one", f"CMD {_RIGHT}\nCMD {_WRONG}", False),
    ("the command run through uv", f"CMD {_VIA_UV}", False),
    ("a bare interpreter from PATH", f"CMD {_WRONG}", False),
    ("a shell wrapper hiding an install", 'CMD ["sh", "-c", "pip install -e . && x"]', False),
    # Docker reads this whole line as shell form, so it is NOT the baked venv.
    # A parser that stops at the end of the JSON array passes it.
    ("the right argv with a trailing comment", f"CMD {_RIGHT} # baked venv", False),
    (
        "a shell-form ENTRYPOINT, whose CMD never reaches the command",
        f"ENTRYPOINT {EXPECTED_START[0]}\nCMD {_RIGHT}",
        False,
    ),
    (
        "an exec ENTRYPOINT with a shell-form CMD",
        f'ENTRYPOINT ["{EXPECTED_START[0]}"]\nCMD some args',
        False,
    ),
    ("a lone shell-form CMD", "CMD " + " ".join(EXPECTED_START), False),
]

SYNC_CASES = [
    ("a plain sync", "RUN uv sync --frozen --no-dev", True),
    ("a sync after a cd", "RUN cd /app && uv sync --frozen", True),
    ("a sync after a semicolon", "RUN cd /app; uv sync --frozen", True),
    ("a sync behind a cache mount", "RUN --mount=type=cache,target=/r uv sync --frozen", True),
    ("a sync behind an inline env assignment", "RUN FOO=1 uv sync --frozen", True),
    ("a flag before the subcommand", "RUN uv --no-cache sync --frozen", True),
    ("a lowercase run keyword", "run uv sync --frozen", True),
    (
        "a flagged sync and a clean one",
        "RUN uv sync --no-install-project\nRUN uv sync --frozen",
        True,
    ),
    ("no sync at all", "RUN pip install .", False),
    ("only a decoy", "RUN echo uv sync", False),
    ("a different subcommand that starts the same way", "RUN uv synchronize", False),
    ("--no-install-project", "RUN uv sync --frozen --no-install-project", False),
    ("--no-install-package", "RUN uv sync --frozen --no-install-package x", False),
    ("--no-install-workspace", "RUN uv sync --frozen --no-install-workspace", False),
    ("the flag on a continuation line", "RUN uv sync --frozen \\\n    --no-install-project", False),
    ("a decoy beside a flagged sync", "RUN echo uv sync\nRUN uv sync --no-install-project", False),
]

# Rules 1-3 as a docker MODEL, asserted on the resolved argv itself. The
# boolean cases cannot distinguish every rule: a rule may yield something
# differently wrong rather than something EQUAL to EXPECTED_START, which is
# how a missing rule survived a whole round. Each expectation below was taken
# from `docker inspect` on a busybox build.
RESOLUTION_CASES = [
    ("only the last CMD counts", 'CMD ["a"]\nCMD ["b"]', ["b"]),
    (
        "only the last ENTRYPOINT counts",
        'ENTRYPOINT ["a"]\nENTRYPOINT ["b"]\nCMD ["c"]',
        ["b", "c"],
    ),
    ("CMD supplies arguments to an exec ENTRYPOINT", 'ENTRYPOINT ["a"]\nCMD ["b"]', ["a", "b"]),
    # docker keeps the CMD and hands it to the shell as $0, so it is in the
    # argv but never reaches the command. Measured: $0=c, args empty.
    (
        "a shell-form ENTRYPOINT keeps the CMD as $0",
        'ENTRYPOINT a b\nCMD ["c"]',
        ["/bin/sh", "-c", "a b", "c"],
    ),
    ("a shell-form CMD runs through the shell", "CMD a b", ["/bin/sh", "-c", "a b"]),
    (
        "an exec ENTRYPOINT with a shell-form CMD",
        'ENTRYPOINT ["a"]\nCMD b c',
        ["a", "/bin/sh", "-c", "b c"],
    ),
    # `#` is a comment only at the start of a line, so the array is not valid
    # JSON and the whole argument is shell form.
    ("a trailing comment is not a comment", 'CMD ["a"] # note', ["/bin/sh", "-c", '["a"] # note']),
    ("only the final build stage counts", 'FROM x\nCMD ["a"]\nFROM y\nCMD ["b"]', ["b"]),
    # The case that actually NEEDS rule 2: with a CMD only in an earlier
    # stage, "the last CMD wins" alone would resurrect it.
    ("an earlier stage's CMD does not carry over", 'FROM x\nCMD ["a"]\nFROM y\nRUN true', []),
    # docker trims the instruction's argument, so the shell string it stores
    # has no trailing spaces. Measured: ["/bin/sh","-c","a b"].
    ("trailing whitespace is trimmed", "CMD a b   ", ["/bin/sh", "-c", "a b"]),
    ("CRLF line endings", 'CMD ["a"]\r', ["a"]),
    # Non-array JSON is shell form. Each measured with docker inspect.
    ("a bare JSON string", 'CMD "a"', ["/bin/sh", "-c", '"a"']),
    ("a JSON number", "CMD 42", ["/bin/sh", "-c", "42"]),
    ("a JSON object", 'CMD {"a": 1}', ["/bin/sh", "-c", '{"a": 1}']),
    ("neither instruction", "RUN true", []),
]


@pytest.mark.parametrize(
    "body,is_baked", [(b, ok) for _, b, ok in START_CASES], ids=[n for n, _, _ in START_CASES]
)
def test_the_start_command_rules(body: str, is_baked: bool):
    dockerfile = "FROM python:3.11-slim\n" + body + "\n"
    assert (_effective_argv(dockerfile) == EXPECTED_START) is is_baked


@pytest.mark.parametrize(
    "body,installs", [(b, ok) for _, b, ok in SYNC_CASES], ids=[n for n, _, _ in SYNC_CASES]
)
def test_the_uv_sync_rules(body: str, installs: bool):
    dockerfile = "FROM python:3.11-slim\n" + body + "\n"
    assert _installs_the_project(dockerfile) is installs


@pytest.mark.parametrize(
    "body,argv", [(b, a) for _, b, a in RESOLUTION_CASES], ids=[n for n, _, _ in RESOLUTION_CASES]
)
def test_the_resolution_rules(body: str, argv: list[str]):
    assert _effective_argv("FROM python:3.11-slim\n" + body + "\n") == argv
