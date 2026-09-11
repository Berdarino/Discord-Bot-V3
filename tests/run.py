"""Run the test suite.

These are plain asyncio scripts with `assert`s, not pytest cases: most of them
drive Discord objects or real APIs, where a fixture would hide more than it
explains. Each one exits non-zero on failure, and this runner reports the lot.

    uv run python tests/run.py                # everything
    uv run python tests/run.py --offline      # no network, Redis or database
    uv run python tests/run.py test_media     # one test, full output

Several tests hit live services. That is deliberate -- every API quirk this bot
works around was found by probing a real endpoint, and a mocked suite would
happily keep passing after the API changed underneath it. The cost is that a
failure here may mean "the API moved", not "the code broke", so read the output
before assuming a regression.
"""

import argparse
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).parent
REPO = HERE.parent

# What each test needs beyond a Python interpreter.
#   network  -- reaches a real API (MyAnimeList, TCGdex)
#   redis    -- needs REDIS_URL reachable
#   mariadb  -- needs MYSQL_* reachable
# Anything needing network, redis or mariadb also needs .env filled in.
SUITE = [
    # (name, requirements)
    ("test_ui", set()),
    ("test_send", set()),
    ("test_ctx", set()),
    ("test_gif", set()),
    ("test_klipy", set()),
    ("test_media", set()),
    ("test_mal", {"network"}),
    ("test_tcg", {"network"}),
    ("test_tcg2", {"network"}),
    ("test_tcg3", {"network"}),
    ("test_pokemon", {"network"}),
    ("test_cache", {"redis"}),
    ("test_fallback", {"redis"}),
    ("test_media_cache", {"redis", "network"}),
    ("test_media_features", {"redis"}),
    ("test_live_flow", {"redis", "network"}),
    ("test_db", {"mariadb"}),
    ("test_reminders", {"mariadb"}),
]


def run_one(name: str, *, verbose: bool) -> tuple[bool, float, str]:
    started = time.monotonic()
    proc = subprocess.run(
        [sys.executable, str(HERE / f"{name}.py")],
        cwd=REPO,
        capture_output=not verbose,
        text=True,
    )
    elapsed = time.monotonic() - started
    output = "" if verbose else (proc.stdout or "") + (proc.stderr or "")
    return proc.returncode == 0, elapsed, output


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("only", nargs="*", help="run only these tests")
    parser.add_argument(
        "--offline",
        action="store_true",
        help="skip tests needing network, Redis or a database",
    )
    args = parser.parse_args()

    selected = [
        (name, needs)
        for name, needs in SUITE
        if (not args.only or name in args.only) and not (args.offline and needs)
    ]
    if not selected:
        print("nothing selected")
        return 1

    # A single named test streams its own output; that is the debugging case.
    verbose = len(selected) == 1 and bool(args.only)

    failures = []
    for name, needs in selected:
        ok, elapsed, output = run_one(name, verbose=verbose)
        if verbose:
            return 0 if ok else 1
        tag = f"[{'+'.join(sorted(needs))}]" if needs else ""
        print(f"{'PASS' if ok else 'FAIL'}  {name:22} {elapsed:5.1f}s {tag}")
        if not ok:
            failures.append((name, output))

    print()
    print(f"{len(selected) - len(failures)}/{len(selected)} passed")

    for name, output in failures:
        print(f"\n{'=' * 60}\n{name}\n{'=' * 60}")
        print("\n".join(output.strip().splitlines()[-25:]))

    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
