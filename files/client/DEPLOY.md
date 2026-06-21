# Client Machine Deployment

Deploy these top-level directories from `files/` onto the client (QEMU/libvirt) machine:

- `api/` — QemuManager, config, arg builder, QMP client, network manager, VM state
- `server/api_server.py` — FastAPI HTTP service (also needs `server/__init__.py`)
- `executioner/tool_executor.py` — tool dispatch to QemuManager (also needs `executioner/__init__.py`)
- `sanitizer/` — shared: input sanitizer and context gate
- `preflight/` — shared: preflight validator (runs with real QemuManager before every execute_tool call)
- `executioner/config.json` — shared config (tool definitions, etc.)

Environment variables required on the client:
- `API_TOKEN` — shared secret; server refuses to start if unset

Start the service:
    uvicorn server.api_server:app --host 0.0.0.0 --port 8080

The `client/` subdirectory itself is not required on the client — it is a convenience
namespace that re-exports from the above paths for code that imports via `client.*`.
