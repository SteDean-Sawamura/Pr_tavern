import runpy
from unittest.mock import Mock

import uvicorn


def test_app_entry_bounds_connection_shutdown_before_lifespan(monkeypatch):
    run_server = Mock()
    monkeypatch.setattr(uvicorn, "run", run_server)

    runpy.run_module("app", run_name="__main__")

    run_server.assert_called_once()
    assert run_server.call_args.kwargs.get("timeout_graceful_shutdown") == 5
