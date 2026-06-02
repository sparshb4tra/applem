from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass
class DownloadSettings:
    output_dir: Path
    output_format: str
    skip_existing: bool
