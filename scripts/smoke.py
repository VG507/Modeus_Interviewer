"""Start the actual API, check health and protected access, then stop it."""
import subprocess
import sys
import time
import socket
import httpx

with socket.socket() as sock:
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
process = subprocess.Popen([sys.executable, "-m", "uvicorn", "backend.app:create_app",
                            "--factory", "--host", "127.0.0.1", "--port", str(port)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
try:
    # Loopback only: do not send local health requests through an external proxy.
    with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=2, trust_env=False) as client:
        for _ in range(100):
            if process.poll() is not None:
                raise RuntimeError("API process exited")
            try:
                response = client.get("/api/v1/health")
                response.raise_for_status()
                break
            except httpx.HTTPError:
                time.sleep(0.1)
        else:
            raise RuntimeError("API did not become ready")
        assert response.json()["database"] == "connected"
        assert client.get("/api/v1/me").status_code == 401
        assert client.get("/openapi.json").status_code == 200
    print("HTTP smoke passed: database health, protected /me, OpenAPI")
finally:
    process.terminate()
    process.wait(timeout=10)
