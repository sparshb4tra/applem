"""Checks that a missing yt-dlp is reported, not raised as a traceback.

`core.downloader` imports yt-dlp when it is imported, and `ui.main_window`
imports `core.downloader`, so while the window was imported at the top of
`app.py` the startup check could never run: `python app.py` died on the import
line and the user got a ModuleNotFoundError traceback instead of the "run the
setup script" message. `app` now stays importable without yt-dlp and imports
the window only after the check has passed.

Every check here runs `app` in a subprocess on a machine where yt-dlp cannot be
imported, which is the situation the message exists for. Nothing here touches
the network, a Tk window, or an installed yt-dlp.

From the repo root:

    python -m unittest discover -s tests -v
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# None in sys.modules is the stdlib's "this module is not importable" marker,
# and find_spec answers None for it, so both halves of the check see a machine
# without yt-dlp. The sys.path fallback is for interpreters that resolve the
# marker differently, so this premise holds on 3.10 and up either way.
BLOCK_YTDLP = textwrap.dedent(
    """\
    import importlib.util
    import sys

    sys.modules["yt_dlp"] = None
    if importlib.util.find_spec("yt_dlp") is not None:
        sys.path = [entry for entry in sys.path if "site-packages" not in entry]
    assert importlib.util.find_spec("yt_dlp") is None, "yt-dlp is still importable"
    """
)

APP_IMPORTS_WITHOUT_YTDLP = textwrap.dedent(
    """\
    import sys

    del sys.modules["yt_dlp"]
    import app

    for pulled_in in ("yt_dlp", "core.downloader", "ui.main_window"):
        assert pulled_in not in sys.modules, f"importing app pulled in {pulled_in}"
    print("app imported")
    """
)

CHECK_REPORTS_INSTALL_STEPS = textwrap.dedent(
    """\
    import app

    try:
        app._check_required_packages()
    except RuntimeError as exc:
        message = str(exc)
        assert "yt-dlp" in message, message
        assert "requirements.txt" in message, message
        assert "setup/install_windows.bat" in message, message
        print("reported")
    else:
        raise SystemExit("a missing yt-dlp was not reported")
    """
)

MAIN_REPORTS_INSTEAD_OF_CRASHING = textwrap.dedent(
    """\
    import app

    reported = []
    launched = []

    app._show_startup_error = reported.append
    app._launch_main = launched.append

    app.main()

    assert reported, "main() did not report the missing dependency"
    assert "yt-dlp" in reported[0], reported[0]
    assert not launched, "main() built the window without yt-dlp"
    print("reported")
    """
)


class MissingYtDlpTests(unittest.TestCase):
    """How the app behaves on a machine without yt-dlp installed."""

    def run_without_ytdlp(self, body: str) -> str:
        result = subprocess.run(
            [sys.executable, "-c", BLOCK_YTDLP + body],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
        )
        self.assertEqual(
            result.returncode,
            0,
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}",
        )
        return result.stdout

    def test_importing_the_app_does_not_need_yt_dlp(self) -> None:
        self.assertIn("app imported", self.run_without_ytdlp(APP_IMPORTS_WITHOUT_YTDLP))

    def test_the_check_names_the_setup_scripts_and_pip(self) -> None:
        self.assertIn("reported", self.run_without_ytdlp(CHECK_REPORTS_INSTALL_STEPS))

    def test_main_shows_the_message_and_stops_there(self) -> None:
        self.assertIn("reported", self.run_without_ytdlp(MAIN_REPORTS_INSTEAD_OF_CRASHING))
