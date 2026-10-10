import subprocess
import sys


def test_tool_schemas_import_without_game_session():
    result = subprocess.run(
        [sys.executable, "-c", "import sys; import engine.tool_schemas; assert 'engine.game_session' not in sys.modules"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
