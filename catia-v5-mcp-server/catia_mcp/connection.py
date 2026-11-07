"""CATIA V5 COM Connection Manager.

Manages the connection to CATIA V5 via Windows COM Automation API (win32com).
Supports connecting to a running instance or launching a new one.
"""

from __future__ import annotations

import logging
import os
import subprocess
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger("catia_mcp")


def _cnext_process_count() -> int:
    """How many CNEXT.exe processes exist (they may not be automation-ready)."""
    try:
        completed = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq CNEXT.exe", "/FO", "CSV", "/NH"],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=10,
        )
    except Exception:  # pragma: no cover - tasklist missing or blocked
        return 0
    lines = [line for line in completed.stdout.splitlines() if line.strip().startswith('"')]
    return len(lines)


def _wait_for_registration(progid: str, timeout: float = 25.0):
    """Wait for an already-running CATIA to register itself for automation."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            return win32com.client.GetActiveObject(progid)
        except Exception:
            time.sleep(1.0)
    return None


def _repair_launch_environment() -> dict[str, str]:
    """Point CATIA's user-settings and temp directories at the real user profile.

    CATIA derives ``CATUserSettingPath`` and ``CATTemp`` from the host environment.
    A host that redirects ``TEMP`` (an agent sandbox, a CI runner) makes CATIA pick a
    settings directory it cannot write to, and it then starts degraded: ``SaveAs`` is
    refused with E_FAIL and ``Save`` silently drops the change (踩坑报告 66). Setting
    the two variables explicitly before ``Dispatch`` restores saving.
    """
    applied: dict[str, str] = {}
    roaming = os.environ.get("APPDATA")
    local = os.environ.get("LOCALAPPDATA")
    candidates = (
        ("CATUserSettingPath", roaming, ("DassaultSystemes", "CATSettings")),
        ("CATTemp", local, ("DassaultSystemes", "CATTemp")),
    )
    for variable, root, parts in candidates:
        if not root:
            continue
        target = Path(root).joinpath(*parts)
        try:
            target.mkdir(parents=True, exist_ok=True)
        except OSError as exc:  # pragma: no cover - depends on the machine
            logger.warning("could not create %s (%s): %s", target, variable, exc)
            continue
        os.environ[variable] = str(target)
        applied[variable] = str(target)
    return applied

# COM imports are deferred to runtime (Windows only)
try:
    import pythoncom
    import win32com.client

    HAS_COM = True
except ImportError:
    HAS_COM = False


class CATIAConnection:
    """Manages connection to CATIA V5 via COM Automation."""

    # CATIA V5 COM ProgID
    CATIA_PROGID = "CATIA.Application"

    def __init__(self) -> None:
        self.app: Any | None = None
        self._initialized_com = False

    @property
    def is_connected(self) -> bool:
        """Check if we have an active CATIA connection."""
        if self.app is None:
            return False
        try:
            # Try accessing a property to verify the connection is alive
            _ = self.app.Caption
            return True
        except Exception:
            self.app = None
            return False

    def connect(self) -> str:
        """Connect to CATIA V5. Tries running instance first, then launches new one.

        Returns a status message string.
        """
        if not HAS_COM:
            raise RuntimeError(
                "pywin32 is not installed. Install it with: pip install pywin32\n"
                "Note: This MCP server requires Windows with CATIA V5 installed."
            )

        if self.is_connected:
            version = self._get_version()
            return f"Already connected to CATIA V5 ({version})"

        # Initialize COM for this thread
        if not self._initialized_com:
            pythoncom.CoInitialize()
            self._initialized_com = True

        if os.environ.get("CATIA_REQUIRED_CATTEMP"):
            from catia_mcp.catia_instance import get_active_catia

            # A scoped run must never silently attach to or launch another session.
            self.app = get_active_catia()
            version = self._get_version()
            logger.info("Connected to the required CATIA V5 instance (%s)", version)
            return f"Connected to the required CATIA V5 instance ({version})"

        # Phase 1: Try to attach to a running CATIA instance
        try:
            self.app = win32com.client.GetActiveObject(self.CATIA_PROGID)
            version = self._get_version()
            logger.info("Connected to running CATIA V5 instance (%s)", version)
            return f"Connected to running CATIA V5 instance ({version})"
        except Exception:
            self.app = None

        # Phase 2: A CATIA process may exist but not be automation-ready yet (still starting),
        # or may be shutting down and still holding its CATSettings files. Launching a second
        # instance in that window yields a degraded instance whose "settings directory is not
        # writable" warning is real for it (踩坑 66), so wait for the registration instead.
        running = _cnext_process_count()
        if running:
            logger.info("CATIA process is running but not registered yet (%d process(es)); waiting", running)
            self.app = _wait_for_registration(self.CATIA_PROGID, timeout=25.0)
            if self.app is not None:
                version = self._get_version()
                logger.info("Attached to CATIA V5 after it finished starting (%s)", version)
                return f"Attached to running CATIA V5 instance ({version})"
            raise RuntimeError(
                "a CATIA process is running but does not answer automation after 25 s "
                f"({running} CNEXT.exe process(es)). Finish or close that instance, then retry; "
                "starting a second instance here would produce one that cannot save files."
            )

        # Phase 3: Launch a new CATIA instance
        try:
            applied = _repair_launch_environment()
            if applied:
                logger.info("CATIA launch environment repaired: %s", applied)
            self.app = win32com.client.Dispatch(self.CATIA_PROGID)
            self.app.Visible = True
            version = self._get_version()
            logger.info("Launched new CATIA V5 instance (%s)", version)
            return f"Launched new CATIA V5 instance ({version})"
        except Exception as e:
            self.app = None
            raise RuntimeError(
                f"Failed to connect to CATIA V5: {e}\n"
                "Make sure CATIA V5 is installed and licensed on this machine."
            ) from e

    def disconnect(self) -> str:
        """Disconnect from CATIA V5 (does not close CATIA)."""
        if self.app is not None:
            self.app = None
        if self._initialized_com:
            try:
                pythoncom.CoUninitialize()
            except Exception:
                pass
            self._initialized_com = False
        return "Disconnected from CATIA V5"

    def ensure_connected(self) -> None:
        """Ensure we have an active CATIA connection, connecting if needed."""
        if not self.is_connected:
            self.connect()

    def _get_version(self) -> str:
        """Get CATIA version string."""
        try:
            # CATIA V5 exposes SystemService.Environ or Caption
            caption = self.app.Caption
            return caption if caption else "unknown version"
        except Exception:
            return "unknown version"

    # ── Helper properties for quick access to CATIA objects ──

    @property
    def documents(self) -> Any:
        """Get the CATIA Documents collection."""
        self.ensure_connected()
        return self.app.Documents

    @property
    def active_document(self) -> Any:
        """Get the active CATIA document."""
        self.ensure_connected()
        try:
            return self.app.ActiveDocument
        except Exception:
            raise RuntimeError("No active document in CATIA. Create or open a document first.")

    @property
    def active_window(self) -> Any:
        """Get the active window (CATIA V5 has no ActiveEditor)."""
        self.ensure_connected()
        return self.app.ActiveWindow

    @property
    def hso(self) -> Any:
        """Get the Highlighted Set of Objects (selection)."""
        self.ensure_connected()
        return self.active_document.Selection

    def refresh_display(self) -> None:
        """Refresh the CATIA 3D view."""
        try:
            self.active_window.ActiveViewer.Reframe()
        except Exception:
            pass

    # ── Document type detection ──

    def get_active_part(self) -> Any:
        """Get the Part object from the active PartDocument."""
        doc = self.active_document
        try:
            return doc.Part
        except Exception:
            raise RuntimeError(
                "Active document is not a Part document. "
                "Open or create a Part document first."
            )

    def get_active_product(self) -> Any:
        """Get the Product object from the active ProductDocument."""
        doc = self.active_document
        try:
            return doc.Product
        except Exception:
            raise RuntimeError(
                "Active document is not a Product document. "
                "Open or create an Assembly (Product) document first."
            )

    def get_active_part_body(self) -> Any:
        """Get the main PartBody from the active Part document."""
        part = self.get_active_part()
        return part.MainBody

    def get_origin_elements(self) -> dict[str, Any]:
        """Get the origin planes (XY, YZ, ZX) from the active Part."""
        part = self.get_active_part()
        origin = part.OriginElements
        return {
            "xy": origin.PlaneXY,
            "yz": origin.PlaneYZ,
            "zx": origin.PlaneZX,
        }
