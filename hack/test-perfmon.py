#!/usr/bin/env python3
"""Focused tests for ingress, the tabbed dashboard and ISP exporter."""
import importlib.util
import json
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from io import BytesIO, StringIO
from unittest.mock import patch
from urllib.error import URLError


ROOT = pathlib.Path(__file__).resolve().parent.parent


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


dashboards = load("dashboards", "hack/gen-dashboards.py")
exporter = load("exporter", "base/prober/isp-exporter.py")


class IngressTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("kustomize"), "kustomize is not installed")
    def test_optional_default_ingress_preserves_named_ingress(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            shutil.copytree(ROOT / "base", root / "base")
            overlay = root / "overlays" / "example"
            shutil.copytree(ROOT / "overlays" / "example", overlay,
                            ignore=shutil.ignore_patterns("grafana-admin.env"))
            (overlay / "grafana-admin.env").write_text("user=test\npassword=test\n")
            kustomization = overlay / "kustomization.yaml"

            def ingresses():
                rendered = subprocess.run(
                    ["kustomize", "build", str(overlay)],
                    check=True, capture_output=True, text=True).stdout
                return [doc.strip() for doc in rendered.split("\n---\n")
                        if "\nkind: Ingress\n" in doc]

            disabled = ingresses()
            self.assertEqual(len(disabled), 1)
            config = kustomization.read_text()
            option = "  # - grafana-default-ingress.yaml"
            self.assertIn(option, config)
            kustomization.write_text(config.replace(
                option, "  - grafana-default-ingress.yaml"))
            enabled = ingresses()
            self.assertEqual(len(enabled), 2)
            self.assertIn(disabled[0], enabled)
            default = next(doc for doc in enabled if "\n  name: grafana-default\n" in doc)
            self.assertIn("traefik.ingress.kubernetes.io/router.tls: \"true\"", default)
            self.assertIn("traefik.ingress.kubernetes.io/router.priority: \"1\"", default)
            self.assertIn("  namespace: perfmon\n", default)
            self.assertIn("  ingressClassName: traefik\n", default)
            self.assertNotIn("host:", default)
            self.assertIn("            name: grafana\n", default)
            self.assertIn("              name: http\n", default)
            self.assertIn("        path: /\n        pathType: Prefix", default)
            self.assertNotIn("cert-manager.io/", default)


def payload():
    return {
        "ip": "192.0.2.1", "asn": "AS64500",
        "as_organization": 'Example "ISP"',
        "country": "Example", "region": "Region", "city": "City",
        "postal_code": "12345", "latitude": "10", "longitude": "-20",
        "timezone": "Etc/UTC",
    }


class DashboardTests(unittest.TestCase):
    def test_native_tabs_reference_every_panel_once(self):
        dashboard = dashboards.dashboard()
        spec = dashboard["spec"]
        tabs = spec["layout"]["spec"]["tabs"]
        self.assertEqual([t["spec"]["title"] for t in tabs],
                         ["Backhaul", "HTTP / DNS", "ISP"])
        references = []

        def walk(value):
            if isinstance(value, dict):
                if value.get("kind") == "ElementReference":
                    references.append(value["name"])
                if value.get("kind") == "GridLayoutItem":
                    self.assertGreaterEqual(value["spec"]["y"], 0)
                for child in value.values():
                    walk(child)
            elif isinstance(value, list):
                for child in value:
                    walk(child)

        walk(spec["layout"])
        self.assertCountEqual(references, spec["elements"])
        self.assertEqual(len(references), len(set(references)))
        self.assertEqual(dashboard["metadata"]["name"], "perfmon-backhaul")

    def test_conversion_preserves_queries_and_visualization(self):
        panels = dashboards.backhaul() + dashboards.http_dns()
        for panel in panels:
            if panel["type"] == "row":
                continue
            converted = dashboards.element(panel)["spec"]
            queries = converted["data"]["spec"]["queries"]
            for old, new in zip(panel["targets"], queries):
                self.assertEqual(new["spec"]["query"]["spec"]["expr"], old["expr"])
                self.assertEqual(new["spec"]["query"]["datasource"]["name"],
                                 "victoriametrics")
            self.assertEqual(converted["vizConfig"]["spec"]["fieldConfig"],
                             panel["fieldConfig"])
            self.assertEqual(converted["vizConfig"]["spec"]["options"],
                             panel["options"])
            self.assertEqual(
                [t["spec"] for t in converted["data"]["spec"]["transformations"]],
                panel.get("transformations", []))

    def test_http_rows_have_relative_positions(self):
        rows = dashboards.layout(dashboards.http_dns())["spec"]["rows"]
        self.assertEqual([r["spec"].get("title") for r in rows],
                         [None, "HTTP / HTTPS", "DNS"])
        for row in rows[1:]:
            self.assertEqual(row["spec"]["layout"]["spec"]["items"][0]["spec"]["y"], 0)

    def test_backhaul_history_and_latency_loss_precede_average_without_overlap(self):
        panels = dashboards.backhaul()
        average = next(p for p in panels if p["title"] == "Round-trip time (avg)")
        for title in ("ISP over time", "Round-trip time (max)", "Packet loss",
                      "Reachability"):
            panel = next(p for p in panels
                         if p["title"] == title and p["type"] != "stat")
            self.assertLessEqual(panel["gridPos"]["y"] + panel["gridPos"]["h"],
                                 average["gridPos"]["y"])
        for index, left in enumerate(panels):
            a = left["gridPos"]
            for right in panels[index + 1:]:
                b = right["gridPos"]
                self.assertFalse(
                    a["x"] < b["x"] + b["w"] and b["x"] < a["x"] + a["w"]
                    and a["y"] < b["y"] + b["h"] and b["y"] < a["y"] + a["h"],
                    f'{left["title"]} overlaps {right["title"]}')

    def test_isp_history_is_range_query_with_current_scrape_and_health_guards(self):
        history = next(p for p in dashboards.backhaul()
                       if p["title"] == "ISP over time")
        query = history["targets"][0]
        self.assertFalse(query.get("instant", False))
        self.assertEqual(query["expr"], dashboards.isp_info("ISP"))
        self.assertIn("{{probe_node}}", query["legendFormat"])
        self.assertIn("{{value}}", query["legendFormat"])
        self.assertFalse(history["fieldConfig"]["defaults"]["custom"]["spanNulls"])
        self.assertIn("timestamp(perfmon_isp_info", query["expr"])
        self.assertIn("== on(probe_node) group_left() timestamp("
                      "perfmon_isp_lookup_success", query["expr"])
        self.assertIn('perfmon_isp_lookup_success{probe_node=~"$node"} == 1',
                      query["expr"])
        self.assertIn('up{job="isp",probe_node=~"$node"} == 1', query["expr"])
        self.assertIn('perfmon_isp_lookup_interval_seconds'
                      '{probe_node=~"$node"}', query["expr"])
        self.assertIn('< on(probe_node) (2 * ', query["expr"])
        self.assertIn('or on(probe_node) (0 * perfmon_isp_lookup_success'
                      '{probe_node=~"$node"} + 300)', query["expr"])


class ExporterTests(unittest.TestCase):
    def test_configured_interval_drives_scheduler_and_metrics(self):
        for interval in (None, 60):
            lookup = exporter.Lookup() if interval is None else exporter.Lookup(interval)
            expected = 300 if interval is None else interval
            with patch.object(lookup, "update") as update:
                with patch.object(exporter.time, "sleep",
                                  side_effect=InterruptedError("stop")) as sleep:
                    with self.assertRaises(InterruptedError):
                        lookup.run()
            update.assert_called_once_with()
            sleep.assert_called_once_with(expected)
            self.assertIn(f"perfmon_isp_lookup_interval_seconds {expected}\n",
                          lookup.metrics())

    def test_cli_interval_is_applied_before_server_starts(self):
        for arguments, interval in (([], 300), (["--interval-seconds", "60"], 60)):
            with patch.object(sys, "argv", ["isp-exporter.py", *arguments]):
                with patch.object(exporter, "Lookup") as lookup:
                    with patch.object(exporter, "ThreadingHTTPServer"):
                        with patch.object(exporter.threading, "Thread"):
                            exporter.main()
            lookup.assert_called_once_with(interval=interval)

    def test_invalid_cli_interval_fails_before_server_starts(self):
        for value in ("0", "-1", "1.5", "1m", ""):
            with patch.object(sys, "argv", ["isp-exporter.py",
                                           "--interval-seconds", value]):
                with patch.object(exporter, "ThreadingHTTPServer") as server:
                    with redirect_stderr(StringIO()) as stderr:
                        with self.assertRaises(SystemExit) as error:
                            exporter.main()
            self.assertEqual(error.exception.code, 2)
            self.assertIn("--interval-seconds", stderr.getvalue())
            server.assert_not_called()

    def test_isp_switch_replaces_series_and_failure_leaves_no_isp(self):
        lookup = exporter.Lookup()
        for isp in ("First ISP", "Second ISP", "First ISP"):
            data = payload()
            data["as_organization"] = isp
            with patch.object(exporter.urllib.request, "urlopen",
                              return_value=BytesIO(json.dumps(data).encode())):
                lookup.update()
            isp_lines = [line for line in lookup.metrics().splitlines()
                         if line.startswith('perfmon_isp_info{field="ISP",')]
            self.assertEqual(isp_lines,
                             [f'perfmon_isp_info{{field="ISP",value="{isp}"}} 1'])
        with patch.object(exporter.urllib.request, "urlopen",
                          side_effect=URLError("unavailable")):
            with self.assertLogs(level="ERROR"):
                lookup.update()
        self.assertNotIn('perfmon_isp_info{field="ISP",', lookup.metrics())

    def test_ip_families_and_escaping(self):
        for ip in ("192.0.2.1", "2001:db8::1"):
            data = payload()
            data["ip"] = ip
            info = exporter.parse_info(data)
            self.assertEqual(info["Public IP"], ip)
            self.assertEqual(info["IP version"], "IPv6" if ":" in ip else "IPv4")
            self.assertEqual(info["ISP"], data["as_organization"])
            self.assertEqual(info["Organization"], data["as_organization"])
            self.assertEqual(info["Postal code"], "12345")
            self.assertEqual(info["Timezone"], "Etc/UTC")
            self.assertEqual(info["Coordinates"], "10.0, -20.0")
            lookup = exporter.Lookup()
            lookup.info = info
            metrics = lookup.metrics()
            self.assertIn('value="Example \\"ISP\\""', metrics)
            self.assertIn('field="ASN",value="AS64500"', metrics)
        self.assertEqual(exporter.label('a\\b\n"c'), 'a\\\\b\\n\\"c')

    def test_rejects_failed_or_malformed_responses(self):
        for data in (None, [], "invalid"):
            with self.assertRaises(ValueError):
                exporter.parse_info(data)
        for change in ({"ip": "invalid"},
                       {"latitude": float("nan")}, {"longitude": 181},
                       {"asn": 0}, {"asn": "AS0"}, {"asn": "AS-1"},
                       {"asn": "AS64500junk"}, {"as_organization": ""},
                       {"as_organization": None}, {"timezone": None}):
            data = payload()
            data.update(change)
            with self.assertRaises((ValueError, KeyError)):
                exporter.parse_info(data)
        with self.assertRaises(KeyError):
            exporter.parse_info({"error": "service unavailable"})

    def test_failure_clears_old_details_and_recovery_replaces_them(self):
        lookup = exporter.Lookup()
        with patch.object(exporter.urllib.request, "urlopen",
                          return_value=BytesIO(json.dumps(payload()).encode())):
            lookup.update()
        self.assertEqual(lookup.success, 1)
        last_success = lookup.last_success
        self.assertGreater(last_success, 0)
        for error in (URLError("unavailable"), ValueError("invalid JSON"),
                      TimeoutError("timed out")):
            with patch.object(exporter.urllib.request, "urlopen", side_effect=error):
                with self.assertLogs(level="ERROR"):
                    lookup.update()
            self.assertEqual(lookup.success, 0)
            self.assertEqual(lookup.last_success, last_success)
            self.assertNotIn("perfmon_isp_info{", lookup.metrics())
        data = payload()
        data["ip"] = "192.0.2.2"
        with patch.object(exporter.urllib.request, "urlopen",
                          return_value=BytesIO(json.dumps(data).encode())):
            lookup.update()
        self.assertEqual(lookup.success, 1)
        self.assertEqual(lookup.info["Public IP"], "192.0.2.2")


if __name__ == "__main__":
    unittest.main()
