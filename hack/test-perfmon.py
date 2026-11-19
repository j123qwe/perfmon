#!/usr/bin/env python3
"""Focused tests for the tabbed dashboard and ISP exporter."""
import importlib.util
import json
import pathlib
import unittest
from io import BytesIO
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


def payload():
    return {
        "success": True, "ip": "192.0.2.1",
        "connection": {"asn": 64500, "isp": 'Example "ISP"', "org": "Example"},
        "country": "Example", "region": "Region", "city": "City",
        "latitude": 10, "longitude": -20, "timezone": {"id": "Etc/UTC"},
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


class ExporterTests(unittest.TestCase):
    def test_ip_families_and_escaping(self):
        for ip in ("192.0.2.1", "2001:db8::1"):
            data = payload()
            data["ip"] = ip
            info = exporter.parse_info(data)
            self.assertEqual(info["Public IP"], ip)
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
        for change in ({"success": False}, {"ip": "invalid"},
                       {"latitude": float("nan")}, {"longitude": 181},
                       {"connection": {"asn": 0}}):
            data = payload()
            data.update(change)
            with self.assertRaises((ValueError, KeyError)):
                exporter.parse_info(data)

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
