#!/usr/bin/env python3
"""Generate the provisioned Grafana dashboards.

    python3 hack/gen-dashboards.py

Writes base/grafana/dashboards/*.json. Edit this script rather than the JSON.
"""
import json
import pathlib

OUT = pathlib.Path(__file__).resolve().parent.parent / "base/grafana/dashboards"
DS = {"type": "prometheus", "uid": "victoriametrics"}

_id = 0


def nid():
    global _id
    _id += 1
    return _id


def target(expr, legend="{{name}}", ref="A", instant=False, fmt=None):
    t = {"datasource": DS, "expr": expr, "legendFormat": legend, "refId": ref,
         "editorMode": "code"}
    if instant:
        t.update(instant=True, range=False)
    if fmt:
        t["format"] = fmt
    return t


def panel(kind, title, pos, targets, unit=None, desc=None, defaults=None,
          options=None, overrides=None, **extra):
    d = {"unit": unit} if unit else {}
    d.update(defaults or {})
    p = {
        "id": nid(), "type": kind, "title": title, "datasource": DS,
        "gridPos": dict(zip("xywh", pos)), "targets": targets,
        "fieldConfig": {"defaults": d, "overrides": overrides or []},
        "options": options or {},
    }
    if desc:
        p["description"] = desc
    p.update(extra)
    return p


def row(title, y):
    return {"id": nid(), "type": "row", "title": title, "collapsed": False,
            "gridPos": {"x": 0, "y": y, "w": 24, "h": 1}, "panels": []}


def thresholds(*steps):
    return {"mode": "absolute",
            "steps": [{"color": c, "value": v} for v, c in steps]}


def ts(title, pos, targets, unit, desc=None, draw="line", **custom):
    c = {"drawStyle": draw, "lineWidth": 1, "fillOpacity": 8,
         "showPoints": "auto",
         # samples arrive only as often as a target is probed; join points up
         # to 2 minutes apart so slow targets still draw as a line when the
         # graph step is finer than their interval
         "spanNulls": 120000}
    if draw == "bars":
        c.update(fillOpacity=70, lineWidth=0)
    c.update(custom)
    return panel(
        "timeseries", title, pos, targets, unit, desc,
        defaults={"custom": c, "min": 0},
        options={"legend": {"displayMode": "table", "placement": "bottom",
                            "calcs": ["mean", "max", "lastNotNull"]},
                 "tooltip": {"mode": "multi", "sort": "desc"}})


def stat(title, pos, targets, unit, desc=None, th=None, mappings=None,
         color="background", decimals=None):
    d = {"thresholds": th or thresholds((None, "text")),
         "color": {"mode": "thresholds"}, "mappings": mappings or []}
    if decimals is not None:
        d["decimals"] = decimals
    return panel(
        "stat", title, pos, targets, unit, desc, defaults=d,
        options={"reduceOptions": {"calcs": ["lastNotNull"], "fields": "",
                                   "values": False},
                 "colorMode": color, "graphMode": "none",
                 "textMode": "value_and_name", "orientation": "auto"})


def variable(name, label, query):
    return {"name": name, "label": label, "type": "query", "datasource": DS,
            "query": {"query": query, "refId": "v"}, "definition": query,
            "refresh": 2, "sort": 1, "multi": True, "includeAll": True,
            "allValue": ".*", "current": {"text": "All", "value": "$__all"}}


def dashboard(uid, title, desc, variables, panels, refresh="10s"):
    return {
        "uid": uid, "title": title, "description": desc, "tags": ["perfmon"],
        "schemaVersion": 39, "version": 1, "editable": False,
        "graphTooltip": 1, "timezone": "browser", "refresh": refresh,
        "time": {"from": "now-1h", "to": "now"},
        "timepicker": {"refresh_intervals":
                       ["1s", "5s", "10s", "30s", "1m", "5m"]},
        "templating": {"list": variables},
        "links": [{"type": "dashboards", "tags": ["perfmon"], "asDropdown": False,
                   "title": "perfmon", "includeVars": False,
                   "keepTime": True}],
        "panels": panels,
    }


UPDOWN = [{"type": "value", "options": {
    "0": {"text": "DOWN", "color": "red"},
    "1": {"text": "UP", "color": "green"}}}]


# --------------------------------------------------------------------------
# Backhaul (ICMP)
# --------------------------------------------------------------------------
def backhaul():
    sel = 'module=~"icmp.*",probe_node=~"$node",name=~"$target"'
    ok = f"probe_success{{{sel}}}"
    rtt = f'probe_icmp_duration_seconds{{phase="rtt",{sel}}}'
    by = "sum by (name, target, probe_node)"

    # blackbox reports rtt=0 for a lost probe, so a plain average would be
    # dragged down by loss. Sum of RTTs / number of answered probes is the
    # mean over answered probes only.
    def avg_rtt(w):
        return (f"sum_over_time({rtt}[{w}]) / ignoring(phase) "
                f"(sum_over_time({ok}[{w}]) > 0)")

    def loss(w):
        return f"100 * (1 - avg_over_time({ok}[{w}]))"

    jitter = f"stddev_over_time(({rtt} > 0)[2m:1s])"
    W, R = "$__interval", "$__range"

    loss_th = thresholds((None, "green"), (0.1, "yellow"), (1, "red"))
    p = [
        stat("Status", (0, 0, 4, 4), [target(ok, instant=True)], None,
             "Result of the most recent probe.", mappings=UPDOWN,
             th=thresholds((None, "red"), (1, "green"))),
        stat("Packet loss", (4, 0, 5, 4), [target(loss(R), instant=True)],
             "percent", "Share of probes lost over the selected time range.",
             th=loss_th, decimals=2),
        stat("RTT avg", (9, 0, 5, 4), [target(avg_rtt(R), instant=True)], "s",
             "Mean round-trip time of answered probes over the selected range.",
             color="value", decimals=1),
        stat("RTT max", (14, 0, 5, 4),
             [target(f"max_over_time({rtt}[{R}])", instant=True)], "s",
             "Slowest answered probe in the selected range.",
             color="value", decimals=1),
        stat("Jitter", (19, 0, 5, 4), [target(jitter, instant=True)], "s",
             "Standard deviation of RTT over the last 2 minutes.",
             color="value", decimals=2),

        ts("Round-trip time (avg)", (0, 4, 24, 9), [target(avg_rtt(W))], "s",
           "Mean RTT of answered probes per graph interval. Zoom in to see "
           "individual probes."),
        ts("Round-trip time (max)", (0, 13, 12, 8),
           [target(f"max_over_time({rtt}[{W}])")], "s",
           "Worst single probe per graph interval; shows spikes that the "
           "average hides."),
        ts("Jitter", (12, 13, 12, 8), [target(jitter)], "s",
           "Standard deviation of RTT over a rolling 2 minute window."),
        ts("Packet loss", (0, 21, 24, 7), [target(loss(W))], "percent",
           "Share of probes lost per graph interval.", draw="bars"),
        panel("state-timeline", "Reachability", (0, 28, 24, 6),
              [target(f"min_over_time({ok}[{W}])")],
              desc="Red where at least one probe in the interval was lost.",
              defaults={"mappings": UPDOWN, "color": {"mode": "thresholds"},
                        "thresholds": thresholds((None, "red"), (1, "green")),
                        "custom": {"fillOpacity": 80, "lineWidth": 0,
                                   "spanNulls": 120000}},
              options={"showValue": "never", "mergeValues": True,
                       "rowHeight": 0.8,
                       "legend": {"showLegend": False},
                       "tooltip": {"mode": "single"}}),
        panel("table", "Summary for selected time range", (0, 34, 24, 8), [
            target(f"{by} ({loss(R)})", ref="A", instant=True, fmt="table"),
            target(f"{by} ({avg_rtt(R)})", ref="B", instant=True, fmt="table"),
            target(f"{by} (min_over_time(({rtt} > 0)[{R}:]))", ref="C",
                   instant=True, fmt="table"),
            target(f"{by} (max_over_time({rtt}[{R}]))", ref="D", instant=True,
                   fmt="table"),
            target(f"{by} (count_over_time({ok}[{R}]))", ref="E", instant=True,
                   fmt="table"),
        ], transformations=[
            {"id": "merge", "options": {}},
            {"id": "organize", "options": {
                "excludeByName": {"Time": True},
                "indexByName": {"name": 0, "target": 1, "probe_node": 2,
                                "Value #A": 3, "Value #B": 4, "Value #C": 5,
                                "Value #D": 6, "Value #E": 7},
                "renameByName": {"name": "Name", "target": "Target",
                                 "probe_node": "Probe node",
                                 "Value #A": "Loss", "Value #B": "RTT avg",
                                 "Value #C": "RTT min", "Value #D": "RTT max",
                                 "Value #E": "Probes"}}},
        ], overrides=[
            {"matcher": {"id": "byName", "options": "Loss"}, "properties": [
                {"id": "unit", "value": "percent"},
                {"id": "decimals", "value": 3},
                {"id": "thresholds", "value": loss_th},
                {"id": "custom.cellOptions",
                 "value": {"type": "color-text"}}]},
            {"matcher": {"id": "byRegexp", "options": "RTT.*"},
             "properties": [{"id": "unit", "value": "s"},
                            {"id": "decimals", "value": 2}]},
        ], options={"showHeader": True, "cellHeight": "sm"}),
    ]
    return dashboard(
        "perfmon-backhaul", "Backhaul performance",
        "ICMP latency, loss and jitter for the backhaul probe targets.",
        [variable("node", "Probe node",
                  "label_values(probe_success, probe_node)"),
         variable("target", "Target",
                  'label_values(probe_success{module=~"icmp.*",'
                  'probe_node=~"$node"}, name)')],
        p)


# --------------------------------------------------------------------------
# HTTP / DNS (placeholder checks)
# --------------------------------------------------------------------------
def http_dns():
    h = 'module=~"http.*",probe_node=~"$node"'
    d = 'module=~"dns.*",probe_node=~"$node"'
    W = "$__interval"
    note = (
        "No HTTP or DNS targets are enabled by default. Uncomment the "
        "placeholder entries in `targets.yaml` (modules `http_2xx` and "
        "`dns_udp`) and re-apply; these panels fill in on their own.")
    p = [
        panel("text", "", (0, 0, 24, 3), [],
              options={"mode": "markdown", "content": note},
              transparent=True),
        row("HTTP / HTTPS", 3),
        stat("HTTP status", (0, 4, 6, 4),
             [target(f"probe_success{{{h}}}", instant=True)], None,
             mappings=UPDOWN, th=thresholds((None, "red"), (1, "green"))),
        stat("Response code", (6, 4, 6, 4),
             [target(f"probe_http_status_code{{{h}}}", instant=True)], None,
             color="value",
             th=thresholds((None, "red"), (200, "green"), (400, "red"))),
        stat("TLS certificate expires in", (12, 4, 6, 4),
             [target(f"probe_ssl_earliest_cert_expiry{{{h}}} - time()",
                     instant=True)], "dtdurations", color="value",
             th=thresholds((None, "red"), (7 * 86400, "yellow"),
                           (30 * 86400, "green")), decimals=0),
        stat("Availability", (18, 4, 6, 4),
             [target(f"100 * avg_over_time(probe_success{{{h}}}[$__range])",
                     instant=True)], "percent", color="value", decimals=3,
             desc="Share of successful checks over the selected range."),
        ts("HTTP response time", (0, 8, 12, 8),
           [target(f"avg_over_time(probe_duration_seconds{{{h}}}[{W}])")],
           "s", "Total time for the check, including DNS, connect and TLS."),
        ts("HTTP time by phase", (12, 8, 12, 8),
           [target(f"avg_over_time(probe_http_duration_seconds{{{h}}}[{W}])",
                   legend="{{name}} {{phase}}")], "s",
           "Where the time goes: resolve, connect, tls, processing, "
           "transfer.", stacking={"mode": "normal"}, fillOpacity=40),
        row("DNS", 16),
        stat("DNS status", (0, 17, 6, 4),
             [target(f"probe_success{{{d}}}", instant=True)], None,
             mappings=UPDOWN, th=thresholds((None, "red"), (1, "green"))),
        stat("Availability", (6, 17, 6, 4),
             [target(f"100 * avg_over_time(probe_success{{{d}}}[$__range])",
                     instant=True)], "percent", color="value", decimals=3,
             desc="Share of successful lookups over the selected range."),
        ts("DNS lookup time", (12, 17, 12, 8),
           [target(f"avg_over_time(probe_duration_seconds{{{d}}}[{W}])")],
           "s", "Time for the configured query to be answered."),
        ts("DNS failures", (0, 21, 12, 4),
           [target(f"100 * (1 - avg_over_time(probe_success{{{d}}}[{W}]))")],
           "percent", "Share of failed lookups per graph interval.",
           draw="bars"),
    ]
    return dashboard(
        "perfmon-http-dns", "HTTP and DNS checks",
        "Placeholder dashboard for HTTP/HTTPS and DNS probe targets.",
        [variable("node", "Probe node",
                  "label_values(probe_success, probe_node)")],
        p, refresh="30s")


if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    for name, fn in (("backhaul", backhaul), ("http-dns", http_dns)):
        (OUT / f"{name}.json").write_text(json.dumps(fn(), indent=2) + "\n")
        print("wrote", OUT / f"{name}.json")
