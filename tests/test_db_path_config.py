import os
import json
import subprocess
import sys
from pathlib import Path


def test_database_path_can_be_isolated_by_environment(tmp_path):
    repository = Path(__file__).resolve().parents[1]
    environment = os.environ.copy()
    environment.pop("TAVERN_DATA_DIR", None)
    environment["TAVERN_DB_PATH"] = str(tmp_path / "isolated.db")

    result = subprocess.run(
        [sys.executable, "-c", "from db.database import DB_PATH; print(DB_PATH)"],
        cwd=repository, env=environment, capture_output=True, text=True, check=True,
    )

    assert Path(result.stdout.strip()) == tmp_path / "isolated.db"
    assert not (tmp_path / "isolated.db").exists()

    environment.pop("TAVERN_DB_PATH")
    default = subprocess.run(
        [sys.executable, "-c", "from db.database import DB_PATH; print(DB_PATH)"],
        cwd=repository, env=environment, capture_output=True, text=True, check=True,
    )
    assert Path(default.stdout.strip()) == repository / "data" / "tavern.db"


def test_all_runtime_data_paths_can_be_external(tmp_path):
    repository = Path(__file__).resolve().parents[1]
    private_data = tmp_path / "private" / "data"
    environment = os.environ.copy()
    environment["TAVERN_DATA_DIR"] = str(private_data)
    environment.pop("TAVERN_DB_PATH", None)
    result = subprocess.run(
        [sys.executable, "-c", (
            "import json; import config; from rpg import routes, session; "
            "from engine import vector_memory; "
            "print(json.dumps([str(path) for path in (config.DATA_DIR, config.DB_PATH, "
            "config.SAVES_DIR, routes.SCRIPTS_DIR, session.SCRIPTS_DIR, vector_memory.DATA_DIR)]))"
        )],
        cwd=repository, env=environment, capture_output=True, text=True, check=True,
    )
    paths = [Path(value) for value in json.loads(result.stdout.strip())]
    assert paths == [
        private_data, private_data / "tavern.db", private_data / "saves",
        private_data / "scripts", private_data / "scripts", private_data,
    ]
    assert (private_data / "scripts").is_dir()
    assert (private_data / "saves").is_dir()
