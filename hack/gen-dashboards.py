#!/usr/bin/env python3
"""Generate the provisioned Grafana dashboard with native tabs.

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
    return {"kind": "QueryVariable", "spec": {
        "name": name, "label": label, "query": data_query({"query": query,
                                                         "refId": "v"}),
        "definition": query, "refresh": "onTimeRangeChanged",
        "sort": "alphabeticalAsc", "multi": True, "includeAll": True,
        "allValue": ".*", "current": {"text": "All", "value": "$__all"},
        "hide": "dontHide", "skipUrlSync": False, "regex": "",
        "options": [], "allowCustomValue": False}}


def data_query(query):
    return {"kind": "DataQuery", "group": DS["type"], "version": "v0",
            "datasource": {"name": DS["uid"]},
            "spec": {k: v for k, v in query.items() if k != "datasource"}}


def element(p):
    spec = {k: p[k] for k in ("id", "title", "description", "transparent")
            if k in p}
    spec.update({
        "links": [],
        "data": {"kind": "QueryGroup", "spec": {
            "queries": [{"kind": "PanelQuery", "spec": {
                "refId": q["refId"], "hidden": False, "query": data_query(q)}}
                for q in p["targets"]],
            "transformations": [
                {"kind": t["id"], "spec": t}
                for t in p.get("transformations", [])],
            "queryOptions": {}}},
        "vizConfig": {"kind": "VizConfig", "group": p["type"],
                      "version": "13.2.6",
                      "spec": {"options": p["options"],
                               "fieldConfig": p["fieldConfig"]}},
    })
    return {"kind": "Panel", "spec": spec}


def layout(panels):
    rows = []
    items = []
    offset = 0
    for p in panels:
        if p["type"] == "row":
            rows.append({"kind": "RowsLayoutRow", "spec": {
                "title": p["title"], "collapse": p["collapsed"],
                "layout": {"kind": "GridLayout", "spec": {"items": []}}}})
            items = rows[-1]["spec"]["layout"]["spec"]["items"]
            offset = p["gridPos"]["y"] + 1
            continue
        if not rows:
            rows.append({"kind": "RowsLayoutRow", "spec": {
                "hideHeader": True,
                "layout": {"kind": "GridLayout", "spec": {"items": items}}}})
        pos = p["gridPos"]
        items.append({"kind": "GridLayoutItem", "spec": {
            "x": pos["x"], "y": pos["y"] - offset,
            "width": pos["w"], "height": pos["h"],
            "element": {"kind": "ElementReference", "name": f'panel-{p["id"]}'}}})
    if len(rows) == 1 and rows[0]["spec"].get("hideHeader"):
        return rows[0]["spec"]["layout"]
    return {"kind": "RowsLayout", "spec": {"rows": rows}}


UPDOWN = [{"type": "value", "options": {
    "0": {"text": "DOWN", "color": "red"},
    "1": {"text": "UP", "color": "green"}}}]


def isp_info(field=None):
    sel = 'probe_node=~"$node"'
    fields = f'field="{field}",' if field else ""
    info = f"perfmon_isp_info{{{fields}{sel}}}"
    success = f"perfmon_isp_lookup_success{{{sel}}}"
    # Historical samples from before interval configuration used five minutes.
    interval = (f"(perfmon_isp_lookup_interval_seconds{{{sel}}} "
                f"or on(probe_node) (0 * {success} + 300))")
    # Exclude retired label sets still visible within the query lookback.
    return (f"{info} and (timestamp({info}) == on(probe_node) group_left() "
            f"timestamp({success})) "
            f"and on(probe_node) ({success} == 1) "
            f'and on(probe_node) (up{{job="isp",{sel}}} == 1) '
            f"and on(probe_node) (time() - "
            f"perfmon_isp_last_success_timestamp_seconds{{{sel}}} "
            f"< on(probe_node) (2 * {interval} + 60))")


def isp_history(pos):
    return panel(
        "state-timeline", "ISP over time", pos,
        [target(isp_info("ISP"), "{{probe_node}} / {{value}}")],
        desc="Last observed default-egress ISP at each point in the selected "
             "time range, with a separate lane per node and ISP. The lookup "
             "interval is configurable (five minutes by default); ISP metrics "
             "are scraped every 30 seconds, not per "
             "probe. Failed, unreachable or stale lookups leave gaps. "
             "Compare switches with the shared crosshair on the graphs below.",
        defaults={"color": {"mode": "palette-classic"},
                  "custom": {"fillOpacity": 80, "lineWidth": 0,
                             "spanNulls": False}},
        options={"showValue": "never", "mergeValues": True,
                 "rowHeight": 0.8,
                 "legend": {"showLegend": False},
                 "tooltip": {"mode": "single"}})


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

        isp_history((0, 4, 24, 6)),
        ts("Round-trip time (max)", (0, 10, 12, 8),
           [target(f"max_over_time({rtt}[{W}])")], "s",
           "Worst single probe per graph interval; shows spikes that the "
           "average hides."),
        ts("Packet loss", (12, 10, 12, 8), [target(loss(W))], "percent",
           "Share of probes lost per graph interval.", draw="bars"),
        panel("state-timeline", "Reachability", (0, 18, 24, 6),
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
        ts("Round-trip time (avg)", (0, 24, 24, 9), [target(avg_rtt(W))], "s",
           "Mean RTT of answered probes per graph interval. Zoom in to see "
           "individual probes."),
        ts("Jitter", (0, 33, 24, 8), [target(jitter)], "s",
           "Standard deviation of RTT over a rolling 2 minute window."),
        panel("table", "Summary for selected time range", (0, 41, 24, 8), [
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
    return p


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
    return p


def isp():
    sel = 'probe_node=~"$node"'
    p = [
        panel("text", "", (0, 0, 24, 4), [], transparent=True, options={
            "mode": "markdown",
            "content": "### Internet connection\n"
                       "Public egress information for each selected **probe node**, "
                       "not your browser. Looked up at the configured interval "
                       "(five minutes by default) via "
                       "[ip0.me](https://ip0.me/). ISP uses its AS organization "
                       "field. Location is approximate. "
                       "VPNs, NAT and proxies may identify the exit provider instead "
                       "of the physical ISP. Only the default egress address family "
                       "is shown; this does not identify every uplink. "
                       "The Target filter applies only to Backhaul.\n\n"
                       "A failed lookup clears the details. Check lookup status, "
                       "exporter reachability and age below."}),
        stat("Lookup status", (0, 4, 8, 4),
             [target(f"perfmon_isp_lookup_success{{{sel}}}",
                     "{{probe_node}}", instant=True)], None,
             mappings=[{"type": "value", "options": {
                 "0": {"text": "FAILED / PENDING", "color": "red"},
                 "1": {"text": "OK", "color": "green"}}}],
             th=thresholds((None, "red"), (1, "green"))),
        stat("Exporter reachable", (8, 4, 8, 4),
             [target(f'up{{job="isp",{sel}}}', "{{probe_node}}", instant=True)],
             None, mappings=UPDOWN,
             th=thresholds((None, "red"), (1, "green"))),
        stat("Last successful lookup age", (16, 4, 8, 4),
             [target(f"time() - (perfmon_isp_last_success_timestamp_seconds"
                     f"{{{sel}}} > 0)", "{{probe_node}}", instant=True)],
             "s", desc="No value until the first successful lookup. Details "
                       "expire after two configured lookup intervals plus "
                       "60 seconds.",
             color="value", decimals=0),
    ]
    for title, x in (("Public IP", 0), ("ISP", 8), ("ASN", 16)):
        p.append(panel(
            "stat", title, (x, 8, 8, 4),
            [target(isp_info(title),
                    "{{value}} ({{probe_node}})", instant=True)],
            options={"reduceOptions": {"calcs": ["lastNotNull"],
                                       "fields": "", "values": False},
                     "textMode": "name", "colorMode": "none",
                     "graphMode": "none"}))
    p.append(panel(
        "table", "Connection details", (0, 12, 24, 15),
        [target(isp_info(), instant=True, fmt="table")],
        transformations=[{"id": "organize", "options": {
            "includeByName": {"probe_node": True, "field": True, "value": True},
            "indexByName": {"probe_node": 0, "field": 1, "value": 2},
            "renameByName": {"probe_node": "Probe node",
                             "field": "Detail", "value": "Value"}}}],
        options={"showHeader": True, "cellHeight": "sm",
                 "sortBy": [{"displayName": "Probe node", "desc": False},
                            {"displayName": "Detail", "desc": False}]}))
    return p


def dashboard():
    tabs = [("Backhaul", backhaul()), ("HTTP / DNS", http_dns()), ("ISP", isp())]
    return {
        "apiVersion": "dashboard.grafana.app/v2beta1", "kind": "Dashboard",
        # Keep the existing backhaul URL and home dashboard filename.
        "metadata": {"name": "perfmon-backhaul"},
        "spec": {
            "title": "Network performance",
            "description": "Backhaul performance, HTTP / DNS checks and node ISP information.",
            "tags": ["perfmon"], "editable": False, "preload": False,
            "cursorSync": "Crosshair", "annotations": [], "links": [],
            "timeSettings": {
                "timezone": "browser", "from": "now-1h", "to": "now",
                "autoRefresh": "10s", "autoRefreshIntervals":
                    ["1s", "5s", "10s", "30s", "1m", "5m"],
                "hideTimepicker": False, "fiscalYearStartMonth": 0},
            "variables": [
                variable("node", "Probe node",
                         'label_values({__name__=~"probe_success|'
                         'perfmon_isp_lookup_success"}, probe_node)'),
                variable("target", "ICMP target",
                         'label_values(probe_success{module=~"icmp.*",'
                         'probe_node=~"$node"}, name)')],
            "elements": {f'panel-{p["id"]}': element(p)
                         for _, panels in tabs for p in panels if p["type"] != "row"},
            "layout": {"kind": "TabsLayout", "spec": {"tabs": [
                {"kind": "TabsLayoutTab", "spec": {
                    "title": title, "layout": layout(panels)}}
                for title, panels in tabs]}},
        },
    }


if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / "backhaul.json"
    path.write_text(json.dumps(dashboard(), indent=2) + "\n")
    print("wrote", path)
