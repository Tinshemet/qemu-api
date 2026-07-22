"""
orchestrator/http/image_delivery.py — ship-image delivery.

Streams a VM's primary qcow2 disk (with Range support), its SHA-256, and the whole
VM folder as a tar.gz — served locally or proxied from the executor in remote mode.
Kept out of api_server.py so that module stays routing + auth.
"""
import hashlib
import pathlib
from typing import Any, Dict, Iterator

from fastapi import HTTPException, Request
from fastapi.responses import StreamingResponse

from executor.api._vm_constants import VM_BASE_DIR

from . import context

_CHUNK = context.IO_CHUNK_BYTES  # disk stream chunk (config: io_chunk_bytes)


def executor_url() -> str:
    """Return the executor base URL, or empty string in local mode."""
    from orchestrator.executor_client import API_URL
    return API_URL if API_URL and API_URL != "local" else ""


def exec_headers() -> dict:
    """Return the auth headers for calling the executor server."""
    from orchestrator.executor_client import _TOKEN as _EXEC_TOKEN
    return {"Authorization": f"Bearer {_EXEC_TOKEN}"}


def disk_path(vm_name: str) -> pathlib.Path:
    """Return the path to the first qcow2 disk for *vm_name*, raising HTTP 404 if absent."""
    vm_dir = pathlib.Path(VM_BASE_DIR) / vm_name
    if not vm_dir.is_dir():
        raise HTTPException(status_code=404, detail=f"VM '{vm_name}' not found.")
    candidates = sorted(vm_dir.glob("*.qcow2"))
    if not candidates:
        raise HTTPException(status_code=404, detail=f"No qcow2 disk found for '{vm_name}'.")
    return candidates[0]


def image_sha256(vm_name: str) -> Dict[str, Any]:
    """Return the SHA-256 checksum of the VM's primary disk."""
    exec_url = executor_url()
    if exec_url:
        import requests as _req
        from orchestrator.executor_client import _VERIFY as _EV
        r = _req.get(f"{exec_url}/vms/{vm_name}/disk/sha256",
                     headers=exec_headers(), timeout=context.PROXY_SHA256_TIMEOUT_S, verify=_EV)
        if not r.ok:
            raise HTTPException(status_code=r.status_code, detail=r.text)
        return r.json()
    path = disk_path(vm_name)
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(_CHUNK), b""):
            h.update(chunk)
    return {"vm_name": vm_name, "disk": path.name, "sha256": h.hexdigest(),
            "size_bytes": path.stat().st_size}


def image_download(vm_name: str, request: Request) -> StreamingResponse:
    """Stream the VM's primary qcow2 disk — proxied from executor in remote mode."""
    import requests as _req
    from orchestrator.executor_client import _VERIFY as _EV
    exec_url = executor_url()
    if exec_url:
        upstream = _req.get(
            f"{exec_url}/vms/{vm_name}/disk",
            headers={**exec_headers(), "Range": request.headers.get("range", "")},
            stream=True, timeout=context.PROXY_STREAM_TIMEOUT_S, verify=_EV,
        )
        if not upstream.ok:
            raise HTTPException(status_code=upstream.status_code, detail=upstream.text)
        return StreamingResponse(
            upstream.iter_content(chunk_size=_CHUNK),
            status_code=upstream.status_code,
            media_type="application/octet-stream",
            headers={k: v for k, v in upstream.headers.items()
                     if k in ("Content-Length", "Content-Range", "Accept-Ranges",
                               "X-SHA256", "X-Disk-Size", "Content-Disposition")},
        )
    path  = disk_path(vm_name)
    total = path.stat().st_size
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(_CHUNK), b""):
            h.update(chunk)
    checksum     = h.hexdigest()
    range_header = request.headers.get("range")
    start, end   = 0, total - 1
    if range_header:
        try:
            _, rng = range_header.split("=")
            s, e   = rng.split("-")
            start  = int(s)
            end    = int(e) if e else total - 1
        except Exception:
            raise HTTPException(status_code=416, detail="Invalid Range header.")
        if start >= total or end >= total or start > end:
            raise HTTPException(status_code=416, detail="Range not satisfiable.")
    length = end - start + 1

    def _stream(path: pathlib.Path, start: int, length: int) -> Iterator[bytes]:
        """Yield ``length`` bytes of a file starting at ``start`` in chunks."""
        remaining = length
        with open(path, "rb") as f:
            f.seek(start)
            while remaining > 0:
                data = f.read(min(_CHUNK, remaining))
                if not data:
                    break
                remaining -= len(data)
                yield data

    return StreamingResponse(
        _stream(path, start, length),
        status_code=206 if range_header else 200,
        media_type="application/octet-stream",
        headers={
            "Content-Length":      str(length),
            "Content-Range":       f"bytes {start}-{end}/{total}",
            "Accept-Ranges":       "bytes",
            "X-SHA256":            checksum,
            "X-Disk-Size":         str(total),
            "Content-Disposition": f'attachment; filename="{path.name}"',
        },
    )


def vm_bundle(vm_name: str) -> StreamingResponse:
    """Stream the entire VM folder as a tar.gz — proxied from executor in remote mode."""
    import requests as _req, subprocess as _sp
    from orchestrator.executor_client import _VERIFY as _EV
    exec_url = executor_url()
    if exec_url:
        upstream = _req.get(f"{exec_url}/vms/{vm_name}/bundle",
                            headers=exec_headers(), stream=True, timeout=context.PROXY_STREAM_TIMEOUT_S, verify=_EV)
        if not upstream.ok:
            raise HTTPException(status_code=upstream.status_code, detail=upstream.text)
        return StreamingResponse(
            upstream.iter_content(chunk_size=context.BUNDLE_CHUNK_BYTES),
            media_type="application/gzip",
            headers={"Content-Disposition": f'attachment; filename="{vm_name}.tar.gz"'},
        )
    vm_dir = pathlib.Path(VM_BASE_DIR) / vm_name
    if not vm_dir.is_dir():
        raise HTTPException(status_code=404, detail=f"VM '{vm_name}' not found.")

    def _tar_stream() -> Iterator[bytes]:
        """Yield a tar archive of the VM directory as a byte stream."""
        proc = _sp.Popen(
            ["tar", "czf", "-", "-C", str(vm_dir.parent), vm_name],
            stdout=_sp.PIPE, stderr=_sp.DEVNULL,
        )
        try:
            for chunk in iter(lambda: proc.stdout.read(65536), b""):
                yield chunk
        finally:
            proc.stdout.close()
            proc.wait()

    return StreamingResponse(
        _tar_stream(),
        media_type="application/gzip",
        headers={"Content-Disposition": f'attachment; filename="{vm_name}.tar.gz"'},
    )
