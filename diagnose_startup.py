"""Temporary bounded startup observation; never log env values or response bodies."""
import http.client
import json
import os
import runpy
import subprocess
import sys
import threading
import time

HEALTH_COMMAND = "import http.client, os; c = http.client.HTTPConnection('127.0.0.1', int(os.environ.get('PORT', '8080')), timeout=5); c.request('GET', '/livez'); s = c.getresponse().status; c.close(); raise SystemExit(0 if 200 <= s < 300 else 1)"


def probe(port):
    result = {"event": "LERA_LOCAL_LIVEZ", "port": port}
    connection = None
    try:
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=4)
        connection.request("GET", "/livez")
        response = connection.getresponse()
        result["http_status"] = response.status
        result["ok"] = 200 <= response.status < 300
    except Exception as error:
        result["error_type"] = type(error).__name__
        result["ok"] = False
    finally:
        if connection is not None:
            connection.close()
    return result


def observe(port):
    started = time.monotonic()
    for deadline in (20, 50, 80):
        time.sleep(max(0, started + deadline - time.monotonic()))
        result = probe(port)
        try:
            completed = subprocess.run(
                [sys.executable, "-c", HEALTH_COMMAND],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                timeout=7, check=False,
            )
            result["exact_health_command_exit"] = completed.returncode
        except Exception as error:
            result["exact_health_command_error_type"] = type(error).__name__
        result["elapsed_seconds"] = round(time.monotonic() - started, 2)
        print(json.dumps(result, sort_keys=True), flush=True)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8080"))
    if not 1 <= port <= 65535:
        raise RuntimeError("Invalid PORT")
    threading.Thread(target=observe, args=(port,), daemon=True).start()
    runpy.run_path("app.py", run_name="__main__")
