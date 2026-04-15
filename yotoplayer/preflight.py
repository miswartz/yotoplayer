"""Pre-flight checks for the yotoplayer pipeline.

Verifies that all required tools, packages, credentials, and browser
binaries are present before starting a long download/process run.
"""

import shutil
import subprocess
import sys
from pathlib import Path

_SETTINGS_DIR = Path.home() / ".yotoplayer"
_SETUP_MARKER = _SETTINGS_DIR / ".setup_done"


def is_setup_complete() -> bool:
    """Check whether one-time setup has been run."""
    return _SETUP_MARKER.exists()


def mark_setup_complete() -> None:
    """Write the marker file indicating setup has been run."""
    _SETTINGS_DIR.mkdir(parents=True, exist_ok=True)
    _SETUP_MARKER.write_text("1", encoding="utf-8")


def check_pipeline_ready(*, upload: bool = True, icons: bool = False) -> None:
    """Verify all dependencies are available.  Exits on first failure.

    Parameters
    ----------
    upload : bool
        If True, check Yoto auth tokens.
    icons : bool
        If True, check OpenAI + Retro Diffusion API keys.
    """
    errors: list[str] = []

    # -- Python packages ---------------------------------------------------
    _check_package("click", errors)
    _check_package("requests", errors)
    _check_package("mutagen", errors)
    _check_package("tqdm", errors)
    _check_package("PIL", errors, pip_name="Pillow")
    _check_package("playwright", errors)

    try:
        from odmpy.libby import LibbyClient  # noqa: F401
    except ImportError:
        errors.append(
            "Python package 'odmpy' not installed.\n"
            "  pip install odmpy@git+https://github.com/ping/odmpy.git"
        )

    # -- OS-level tools ----------------------------------------------------
    if not shutil.which("ffmpeg"):
        errors.append(
            "ffmpeg not found in PATH.\n"
            "  Install: winget install ffmpeg   (then restart your terminal)"
        )

    # -- Playwright browser ------------------------------------------------
    _check_playwright_browser(errors)

    # -- Credentials -------------------------------------------------------
    settings_dir = Path.home() / ".yotoplayer"

    edge_token = settings_dir / "edge_identity.txt"
    if not edge_token.exists():
        errors.append(
            "Libby edge identity token not found.\n"
            "  1. Open Edge → libbyapp.com → open an audiobook\n"
            "  2. DevTools (F12) → Application → Local Storage → libbyapp.com\n"
            '  3. Copy the "dewey:sentry.identity" value (starts with eyJ…)\n'
            f"  4. Save it to {edge_token}"
        )

    # Libby auth (chip) — odmpy stores chip inside libby.json
    chip_path = settings_dir / "libby.json"
    if not chip_path.exists():
        errors.append(
            "Libby not authenticated — no chip found.\n"
            "  Run: yotoplayer auth"
        )

    # Yoto auth
    if upload:
        yoto_tokens = settings_dir / "yoto_tokens.json"
        if not yoto_tokens.exists():
            errors.append(
                "Yoto not authenticated — no tokens found.\n"
                "  Run: yotoplayer yoto-auth"
            )

    # -- Optional: icon generation keys ------------------------------------
    if icons:
        from yotoplayer import config

        if not config.get("openai_api_key", "OPENAI_API_KEY"):
            errors.append(
                "Icon generation requires an OpenAI API key.\n"
                "  Set OPENAI_API_KEY env var or add openai_api_key to ~/.yotoplayer/config.json"
            )
        if not config.get("retro_diffusion_api_key", "RETRO_DIFFUSION_API_KEY"):
            errors.append(
                "Icon generation requires a Retro Diffusion API key.\n"
                "  Set RETRO_DIFFUSION_API_KEY env var or add retro_diffusion_api_key to ~/.yotoplayer/config.json"
            )

    # -- Report ------------------------------------------------------------
    if errors:
        print("Pre-flight check failed:\n", file=sys.stderr)
        for i, err in enumerate(errors, 1):
            print(f"  {i}. {err}\n", file=sys.stderr)
        if not is_setup_complete():
            print(
                "Tip: Run 'yotoplayer setup' to install everything interactively.",
                file=sys.stderr,
            )
        else:
            print(
                f"{len(errors)} issue(s) found. Fix them before running the pipeline.",
                file=sys.stderr,
            )
        sys.exit(1)

    print("Pre-flight check passed — all dependencies ready.")


def _check_package(
    import_name: str,
    errors: list[str],
    *,
    pip_name: str | None = None,
) -> None:
    """Try importing a package; append an error message on failure."""
    try:
        __import__(import_name)
    except ImportError:
        pkg = pip_name or import_name
        errors.append(f"Python package '{pkg}' not installed.\n  pip install {pkg}")


def _check_playwright_browser(errors: list[str]) -> None:
    """Check that the Chromium browser binary for Playwright is installed."""
    try:
        import playwright  # noqa: F401
    except ImportError:
        return  # already flagged by _check_package

    try:
        result = subprocess.run(
            [sys.executable, "-m", "playwright", "install", "--dry-run", "chromium"],
            capture_output=True, text=True, timeout=15,
        )
        # --dry-run exits 0 if already installed, non-zero otherwise
        if result.returncode != 0:
            errors.append(
                "Playwright Chromium browser not installed.\n"
                "  Run: python -m playwright install chromium"
            )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        # Fall back to checking the registry path directly
        try:
            from playwright._impl._driver import compute_driver_executable

            driver = Path(compute_driver_executable())
            browsers_dir = driver.parent / "driver" / "package" / ".local-browsers"
            if not any(browsers_dir.glob("chromium-*")) if browsers_dir.exists() else True:
                errors.append(
                    "Playwright Chromium browser may not be installed.\n"
                    "  Run: python -m playwright install chromium"
                )
        except Exception:
            pass  # can't determine — skip rather than false-positive
