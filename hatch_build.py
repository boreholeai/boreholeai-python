"""Custom hatch build hook — stamps the version and build date into _version.py."""

import datetime
import re
from pathlib import Path

from hatchling.builders.hooks.plugin.interface import BuildHookInterface


class CustomBuildHook(BuildHookInterface):
    PLUGIN_NAME = "custom"

    def initialize(self, version, build_data):
        today = datetime.date.today().isoformat()
        version_file = Path(self.root) / "src" / "boreholeai" / "_version.py"
        text = version_file.read_text()
        # pyproject.toml is the single source of truth for the version —
        # stamping __version__ here means a release bumps one place only.
        text = re.sub(
            r'__version__\s*=\s*"[^"]*"',
            f'__version__ = "{self.metadata.version}"',
            text,
        )
        text = re.sub(
            r'__version_date__\s*=\s*"[^"]*"',
            f'__version_date__ = "{today}"',
            text,
        )
        version_file.write_text(text)
