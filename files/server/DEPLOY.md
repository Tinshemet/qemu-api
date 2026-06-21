# Provider Machine Deployment

Deploy these top-level directories from `files/` onto the AI provider machine:

- `ai/` — chat loop, display, context assistant, fingerprint, ollama client, session, tools
- `executioner/executor_client.py` — HTTP seam to the client machine (also needs `executioner/__init__.py`)
- `ollama_wrapper.py` — entry point / re-export facade
- `sanitizer/` — shared: input sanitizer and context gate (used by ai/tools.py)
- `preflight/` — shared: preflight validator (used by executor_client in local mode)

Environment variables required on the provider:
- `API_URL` — URL of the client machine's HTTP service (e.g. `http://192.168.1.x:8080`)
- `API_TOKEN` — shared secret matching the client machine's `API_TOKEN`

The `provider/` subdirectory itself is not required on the provider — it is a convenience
namespace that re-exports from the above paths for code that imports via `provider.*`.
