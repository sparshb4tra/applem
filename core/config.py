from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path


CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.json"


@dataclass
class AppConfig:
    output_dir: str = str(Path.home() / "Music" / "AppleDownloads")
    output_format: str = "mp3"
    skip_existing: bool = True


def load_config(config_path: Path = CONFIG_PATH) -> AppConfig:
    if not config_path.exists():
        config = AppConfig()
        save_config(config, config_path=config_path)
        return config

    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
        return AppConfig(
            output_dir=data.get("output_dir", str(Path.home() / "Music" / "AppleDownloads")),
            output_format=data.get("output_format", "mp3"),
            skip_existing=bool(data.get("skip_existing", True)),
        )
    except Exception:
        config = AppConfig()
        save_config(config, config_path=config_path)
        return config


def save_config(config: AppConfig, config_path: Path = CONFIG_PATH) -> None:
    config_path.write_text(json.dumps(asdict(config), indent=2), encoding="utf-8")
