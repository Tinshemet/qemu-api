# Compatibility shim — 'server' is the legacy name; 'orchestrator' is the new name.
# This makes server.* and orchestrator.* refer to the same module objects so that
# mock.patch("server.http.api_server._ALLOWED_TOOLS") affects the live app.
import sys
import importlib

def _alias(legacy: str, real: str) -> None:
    try:
        mod = importlib.import_module(real)
        sys.modules[legacy] = mod
    except ImportError:
        pass  # legacy alias is optional — skip it when the real module isn't importable

# Register each time this package is imported so that after a test deletes
# "server.http.api_server", re-importing "server" re-establishes the aliases.
_alias("server.ai",                 "orchestrator.ai")
_alias("server.ai.cli",             "orchestrator.ai.cli")
_alias("server.ai.ollama_client",   "orchestrator.ai.ollama_client")
_alias("server.event_log",          "orchestrator.event_log")
_alias("server.executor_client",    "orchestrator.executor_client")
# server.http and server.http.api_server are handled by real shim files
# in server/http/ so that mock.patch always patches orchestrator.http.api_server.
