#!/usr/bin/env python3
"""Expose the node's public IP and ISP as Prometheus metrics."""
import ipaddress
import json
import logging
import math
import threading
import time
import urllib.error
import urllib.request
from http.client import HTTPException
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


LOOKUP_URL = "https://ipwho.is/"
INTERVAL = 300


def parse_info(data):
    if not isinstance(data, dict) or data.get("success") is not True:
        raise ValueError("IP lookup service reported failure")
    address = ipaddress.ip_address(data["ip"])
    connection = data["connection"]
    asn = connection["asn"]
    if isinstance(asn, bool) or not isinstance(asn, int) or asn <= 0:
        raise ValueError("Invalid ASN")
    latitude, longitude = float(data["latitude"]), float(data["longitude"])
    if not (math.isfinite(latitude) and -90 <= latitude <= 90
            and math.isfinite(longitude) and -180 <= longitude <= 180):
        raise ValueError("Invalid coordinates")
    info = {
        "Public IP": str(address),
        "IP version": f"IPv{address.version}",
        "ASN": f"AS{asn}",
        "ISP": connection["isp"],
        "Organization": connection["org"],
        "Country": data["country"],
        "Region": data["region"],
        "City": data["city"],
        "Postal code": data.get("postal", ""),
        "Coordinates": f"{latitude}, {longitude}",
        "Timezone": data["timezone"]["id"],
    }
    if any(not isinstance(value, str) for value in info.values()):
        raise ValueError("Invalid text field in IP lookup")
    if not info["ISP"]:
        raise ValueError("Missing ISP")
    return info


def label(value):
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


class Lookup:
    def __init__(self):
        self.lock = threading.Lock()
        self.info = {}
        self.success = 0
        self.last_success = 0

    def update(self):
        try:
            request = urllib.request.Request(
                LOOKUP_URL, headers={"User-Agent": "perfmon-isp/1.0"})
            with urllib.request.urlopen(request, timeout=10) as response:
                data = json.load(response)
            info = parse_info(data)
        except (urllib.error.URLError, HTTPException, OSError, ValueError,
                KeyError, TypeError) as error:
            logging.error("ISP lookup failed: %s: %s", type(error).__name__, error)
            with self.lock:
                self.info = {}
                self.success = 0
        else:
            with self.lock:
                self.info = info
                self.success = 1
                self.last_success = time.time()

    def run(self):
        while True:
            self.update()
            time.sleep(INTERVAL)

    def metrics(self):
        with self.lock:
            lines = [
                "# HELP perfmon_isp_lookup_success Whether the last ISP lookup succeeded.",
                "# TYPE perfmon_isp_lookup_success gauge",
                f"perfmon_isp_lookup_success {self.success}",
                "# HELP perfmon_isp_last_success_timestamp_seconds Last successful lookup.",
                "# TYPE perfmon_isp_last_success_timestamp_seconds gauge",
                f"perfmon_isp_last_success_timestamp_seconds {self.last_success}",
                "# HELP perfmon_isp_info Public egress information from ipwho.is.",
                "# TYPE perfmon_isp_info gauge",
            ]
            for field, value in self.info.items():
                lines.append(
                    f'perfmon_isp_info{{field="{label(field)}",value="{label(value)}"}} 1')
        return "\n".join(lines) + "\n"


def main():
    lookup = Lookup()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path not in ("/metrics", "/health"):
                self.send_error(404)
                return
            body = lookup.metrics().encode() if self.path == "/metrics" else b"ok\n"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt, *args):
            pass

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    server = ThreadingHTTPServer(("127.0.0.1", 9116), Handler)
    threading.Thread(target=lookup.run, daemon=True).start()
    server.serve_forever()


if __name__ == "__main__":
    main()
