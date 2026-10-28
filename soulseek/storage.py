"""Persistent credentials and networking state; audio is never stored here."""

from pathlib import Path


class RuntimeStorage:
    """Paths needed by the headless core, excluding audio downloads."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.credentials_path = root / "credentials.json"
        self.nicotine_data_path = root / "nicotine"

    async def initialize(self) -> None:
        self.nicotine_data_path.mkdir(parents=True, exist_ok=True)
