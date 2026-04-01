"""Custom hatch build hook — stamps the build date into _version.py."""

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
        text = re.sub(
            r'__version_date__\s*=\s*"[^"]*"',
            f'__version_date__ = "{today}"',
            text,
        )
        version_file.write_text(text)
