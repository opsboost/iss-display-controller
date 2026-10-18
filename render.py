'''
Text for the views from the data doi hands back. Pure functions of plain
values, so they run and test without a compositor
'''

import json


def human_bytes(n):
    for unit in ("B", "K", "M", "G", "T"):
        if n < 1024 or unit == "T":
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024


def duration(seconds):
    days, rest = divmod(int(seconds), 86400)
    hours, rest = divmod(rest, 3600)
    parts = [f"{days}d"] if days else []
    if days or hours:
        parts.append(f"{hours}h")
    parts.append(f"{rest // 60}m")

    return " ".join(parts)


def cpu_time(seconds):
    minutes, rest = divmod(seconds, 60)

    return f"{int(minutes)}m{rest:04.1f}s"


# A monospace table: the header, then the rows, each column as wide as its
# widest cell. The columns named in right are right aligned, the numbers
def table(headers, rows, right=()):
    if not rows:
        return []
    cells = [[str(c) for c in headers]] + [[str(c) for c in row] for row in rows]
    widths = [max(len(row[i]) for row in cells) for i in range(len(headers))]

    return ["  ".join(c.rjust(w) if i in right else c.ljust(w)
                      for i, (c, w) in enumerate(zip(row, widths))).rstrip()
            for row in cells]


def address_lines(rows):
    return "\n".join(f"{row['dev']} {row['ip']}" for row in rows)


def more(shown, total):
    return f"... {total - shown} more of {total}"


def art_caption(meta):
    if not isinstance(meta, dict):
        return str(meta or "")

    caption = ", ".join(p for p in (meta.get("title"), meta.get("artist"),
                                    meta.get("date")) if p)
    source = meta.get("source", "")
    if source:
        caption = f"{caption} - {source}" if caption else source

    return caption


# The month the way cal prints it: a centred title, the two letter day names
# and the weeks. Today's number and its day name pass through the markers,
# so a caller can wrap them in markup
def calendar_lines(month, mark_day=None, mark_name=None):
    mark_day = mark_day or (lambda text: text)
    mark_name = mark_name or (lambda text: text)
    today = month["today"]
    weekday = next((week.index(today) for week in month["weeks"]
                    if today in week), None)
    names = [name[:2] for name in month["weekdays"]]
    title = f"{month['month_name']} {month['year']}".center(20).rstrip()
    name_row = " ".join(mark_name(name) if i == weekday else name
                        for i, name in enumerate(names))
    weeks = [" ".join(mark_day(f"{day:2}") if day == today
                      else (f"{day:2}" if day else "  ")
                      for day in week).rstrip()
             for week in month["weeks"]]

    return [title, "", name_row] + weeks


def holiday_lines(holidays):
    return [f"{h['date']}: {h['name']} ({h['localName']})"
            for h in holidays or []]


moon_glyphs = {"New Moon": "\U0001F311",
               "Waxing Crescent": "\U0001F312",
               "First Quarter": "\U0001F313",
               "Waxing Gibbous": "\U0001F314",
               "Full Moon": "\U0001F315",
               "Waning Gibbous": "\U0001F316",
               "Last Quarter": "\U0001F317",
               "Waning Crescent": "\U0001F318"}
moon_default = "\U0001F319"

uv_levels = [
    (2, "Low", "No protection needed", "No protection needed"),
    (5, "Moderate", "Sunscreen recommended", "Sunscreen and hat recommended"),
    (7, "High", "Sunscreen essential, seek shade",
     "Limit midday exposure, hat and sunscreen"),
    (10, "Very High", "Avoid midday sun", "Avoid midday sun, stay in shade"),
]


def uv_risk(uv_index):
    try:
        uv = int(float(uv_index))
    except (TypeError, ValueError):
        return None

    for max_uv, label, adult, children in uv_levels:
        if uv <= max_uv:
            return (f"UV Index {uv} ({label})\n"
                    f"Children: {children}\n"
                    f"Adults: {adult}")

    return (f"UV Index {uv} (Extreme)\n"
            "Children: Keep indoors during peak hours\n"
            "Adults: Avoid sun exposure")


def weather_lines(now):
    if now["temp_c"] is None:
        lines = ["Weather data unavailable"]
    else:
        lines = [f"{now['temp_c']}°C",
                 f"{now['condition']} {now['wind_kmh']} km/h",
                 now["area"] or "", ""]
    if now["sunrise"] and now["sunset"]:
        lines.append(f"Sunrise {now['sunrise']}  Sunset {now['sunset']}")
    if now["moon_phase"]:
        glyph = moon_glyphs.get(now["moon_phase"], moon_default)
        moon = f"{glyph} Moon {now['moon_phase']}"
        if now["days_to_full_moon"] is not None:
            moon += f" ({now['days_to_full_moon']} days to full)"
        lines += ["", moon, ""]
    risk = uv_risk(now["uv_index"])
    if risk:
        lines.append(risk)
    elif now["uv_index"] is not None:
        lines.append(f"UV Index {now['uv_index']:g}")

    return lines


def mqtt_lines(topic, payload):
    if isinstance(payload, dict):
        body = [f"{key}: {value}" for key, value in payload.items()]
    elif isinstance(payload, (list, tuple)):
        body = [json.dumps(payload, ensure_ascii=False)]
    else:
        body = [str(payload)]

    return [topic] + body


def memory_text(mem):
    if not mem:
        return ""
    text = f"{human_bytes(mem['total_b'])} total"
    if mem.get("available_b") is not None:
        text += (f", {human_bytes(mem['total_b'] - mem['available_b'])} used"
                 f", {human_bytes(mem['available_b'])} available")
    if mem.get("swap_total_b"):
        used = mem["swap_total_b"] - (mem.get("swap_free_b") or 0)
        text += (f", swap {human_bytes(mem['swap_total_b'])} total"
                 f" {human_bytes(used)} used")

    return text


def top_lines(procs, limit=20):
    procs = sorted(procs, key=lambda p: p["cpu_s"], reverse=True)
    lines = table(("PID", "CPU TIME", "MEM", "COMMAND"),
                  [(p["pid"], cpu_time(p["cpu_s"]), human_bytes(p["rss_b"]),
                    p["name"]) for p in procs[:limit]], right={0, 1, 2})
    if len(procs) > limit:
        lines.append(more(limit, len(procs)))

    return lines


def process_lines(procs):
    return table(("PID", "COMMAND"), [(p["pid"], p["cmdline"]) for p in procs],
                 right={0})


# Listeners first, then live connections, then the closing and wait states
# last: a row of TIME_WAIT from loopback scrape churn should not push the
# sockets that matter off the top of the view
socket_transient = {"TIME_WAIT", "CLOSE_WAIT", "FIN_WAIT1", "FIN_WAIT2",
                    "CLOSING", "LAST_ACK", "CLOSE"}
socket_order = {"LISTEN": 0, "ESTABLISHED": 1, "UNCONN": 2}


def socket_lines(sockets, limit=0):
    sockets = sorted(sockets, key=lambda s: (
        4 if s["state"] in socket_transient else socket_order.get(s["state"], 3),
        s["proto"], s["local"]))
    shown = sockets[:limit] if limit else sockets
    lines = table(("PROTO", "LOCAL", "REMOTE", "STATE", "SENT", "RECV"),
                  [(s["proto"], s["local"][:24], s["remote"][:24], s["state"],
                    human_bytes(s["sent"]) if s.get("sent") is not None else "-",
                    human_bytes(s["received"]) if s.get("received") is not None
                    else "-") for s in shown], right={4, 5})
    if limit and len(sockets) > limit:
        lines.append(more(limit, len(sockets)))

    return lines


neighbour_order = {"REACHABLE": 0, "PERMANENT": 0, "DELAY": 1, "PROBE": 1,
                   "STALE": 2, "NOARP": 3, "FAILED": 4, "INCOMPLETE": 4}


def neighbour_lines(rows, limit=0):
    import ipaddress

    def key(row):
        try:
            addr = ipaddress.ip_address(row["ip"])
            ip_key = (addr.version, int(addr))
        except ValueError:
            ip_key = (9, 0)

        return (neighbour_order.get(row["state"], 3), row["dev"], ip_key)

    rows = sorted(rows, key=key)
    shown = rows[:limit] if limit else rows
    lines = table(("IP", "MAC", "DEV", "STATE"),
                  [(r["ip"], r["mac"] or "-", r["dev"], r["state"]) for r in shown])
    if limit and len(rows) > limit:
        lines.append(more(limit, len(rows)))

    return lines


def size_lines(scan, kind):
    entries = scan[kind]
    if not entries:
        return [f"No {'files' if kind == 'files' else 'directories'} found "
                f"under {scan['path']}"]

    lines = table(("SIZE", "FILE" if kind == "files" else "DIRECTORY"),
                  [(human_bytes(size), name) for size, name in entries],
                  right={0})
    if scan["truncated"]:
        lines.append("... scan stopped early, results are partial")

    return lines


def traceroute_lines(trace):
    if trace["error"]:
        return [f"Traceroute: {trace['error']}"]
    if not trace["hops"]:
        return [f"No reply from any hop towards {trace['dest']}"]

    dest, target = trace["dest"], trace["target"]
    shown = dest if dest == target else f"{target} ({dest})"
    rows = []
    for hop in trace["hops"]:
        host = hop["hosts"][0] if hop["hosts"] else "???"
        if len(hop["hosts"]) > 1:
            host += f" +{len(hop['hosts']) - 1}"
        times = hop["times"]
        loss = 100.0 * (hop["sent"] - len(times)) / hop["sent"]
        stats = ([f"{times[-1]:.1f}", f"{sum(times) / len(times):.1f}",
                  f"{min(times):.1f}", f"{max(times):.1f}"]
                 if times else ["-"] * 4)
        rows.append([hop["ttl"], host, f"{loss:.1f}%", hop["sent"], *stats])

    lines = [f"Trace to {shown}, {trace['cycles']} cycles", ""]
    lines += table(("HOP", "HOST", "LOSS%", "SNT", "LAST", "AVG", "BEST", "WRST"),
                   rows, right={0, 2, 3, 4, 5, 6, 7})
    if trace["stopped"]:
        lines.append("... stopped, the time budget ran out")

    return lines
