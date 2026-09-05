#!/usr/bin/env python3
import requests
import collections
import colorsys
import functools
from functools import partial
import cairocffi as cairo
import configargparse
from doi import (APOD, Art, ArtMet, ArtNGA, Bluesky, Calendar, MQTT, Music,
                 News, OTD, PrometheusClient, RSSFeed, System, Weather)
from qr_code_service import encode as encode_qr, QREncodeError
import html
import ipaddress
import json
import logging
import math
import os
import pangocairocffi
import pangocffi
from pathlib import Path
from PIL import Image, UnidentifiedImageError
import platform
import re
import secrets
import socket
import stat
import subprocess
from subprocess import Popen
import shlex
import shutil
import signal
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.responses import (JSONResponse, PlainTextResponse, HTMLResponse,
                                 Response)
from starlette.routing import Match, Route
import sys
import time
import threading
import tomllib
from urllib.parse import unquote_plus, urljoin, urlsplit, urlunsplit
from zeroconf import IPVersion, ServiceInfo, Zeroconf
from wayland import draw as view
import wayland.protocol

# Suppress protocol.py INFO messages
logging.getLogger("wayland.protocol").setLevel(logging.WARNING)


# Keeps the last N formatted log lines in memory so a view can tail the
# controller's own log without a file on disk
class RingLog(logging.Handler):

    def __init__(self, capacity=1000):
        super().__init__()
        self.records = collections.deque(maxlen=capacity)

    def emit(self, record):
        try:
            self.records.append(self.format(record))
        except Exception:
            pass

    def tail(self, n):
        return list(self.records)[-max(1, n):]

ring_log = RingLog()


# Ensure child processes inherit the runtime environment (including PATH)
env = os.environ.copy()

stream_sources = ["static-images", "v4l2", "vnc-browser", "mosaic"]
cmds = {"clock":        "humanbeans_clock",
        "media_player": "gst-launch-1.0",
        "servo":        "servo",
        "compositor":   "sway",
        "scream":       os.environ.get("SCREAM_BIN", "scream"),
        "wayvnc":       os.environ.get("WAYVNC_BIN", "wayvnc"),
        "vju":          os.environ.get("VJU_BIN", "vju")}

browser_engines = ("servo", "firefox")

draw_methods = ("python-wayland", "vju")


metric_meta = {
    "iss_display_build_info": ("gauge", "Build and dependency identity, always 1"),
    "iss_display_start_time_seconds": ("gauge", "Unix time the display started"),
    "iss_display_host_uptime_seconds": ("gauge", "Seconds since the host booted"),
    "iss_display_views_running": ("gauge", "View windows currently drawing"),
    "iss_display_surface_commits_total": ("counter", "Surface commits submitted"),
    "iss_display_frames_presented_total": ("counter", "Frames the compositor presented"),
    "iss_display_view_exits_total": ("counter", "View windows that stopped, by reason"),
    "iss_display_empty_views_total": ("counter", "Views drawn with no content, by player and item"),
    "iss_display_window_switches_total": ("counter", "Focus switches performed"),
    "iss_display_item_shown_seconds_total": ("counter", "Seconds each item was on screen"),
    "iss_display_item_enabled": ("gauge", "Whether a playlist item is enabled"),
    "iss_display_item_play_time_seconds": ("gauge", "Configured play time per item"),
    "iss_display_windows": ("gauge", "Windows known to the compositor, by state"),
    "iss_display_sockets": ("gauge", "Live sockets (LISTEN, ESTABLISHED, connecting), by protocol and state"),
    "iss_display_sockets_transient": ("gauge", "Sockets in a closing or wait state (TIME_WAIT etc), by protocol and state"),
    "iss_display_sockets_total": ("gauge", "All sockets seen this scrape, live and transient"),
    "iss_display_content_age_seconds": ("gauge", "Age of the content a view is showing"),
    "iss_display_content_ticks_total": ("counter", "Refresh timer ticks handled"),
    "iss_display_content_refreshes_total": ("counter", "Content refreshes that redrew"),
    "iss_display_fetch_failures_total": ("counter", "Upstream fetches that failed, by source"),
    "iss_display_rss_items": ("gauge", "Items parsed from an RSS feed"),
    "iss_display_swaymsg_errors_total": ("counter", "swaymsg calls that wrote to stderr"),
    "iss_display_state_commands_total": ("counter", "State socket commands served"),
    "iss_display_state_rate_limited_total": ("counter", "State socket datagrams the rate limiter refused, by scope"),
    "iss_display_state_rate_limited_clients": ("gauge", "Distinct source addresses the rate limiter has refused"),
    "iss_display_browser_up": ("gauge", "Whether a browser window is present"),
    "iss_display_compositor_up": ("gauge", "Whether the compositor is running"),
    "iss_display_stream_up": ("gauge", "Whether the stream server is running"),
    "iss_display_vnc_up": ("gauge", "Whether the vnc server is running"),
    "iss_display_stream_subscribers": ("gauge", "Clients connected to a stream, by stream type"),
    "iss_display_stream_snapshots_total": ("counter", "Snapshot stills served by the stream server"),
    "iss_display_http_requests_total": ("counter", "Requests handled, by route path"),
    "iss_display_http_request_duration_seconds": ("summary", "Time spent handling requests"),
    "iss_display_screenshot_failures_total": ("counter", "Screenshot captures that failed"),
    "iss_display_process_cpu_seconds_total": ("counter", "CPU time used, by process name"),
    "iss_display_thread_cpu_seconds_total": ("counter", "CPU time used, by process and thread name"),
    "iss_display_process_resident_memory_bytes": ("gauge", "Resident memory held, by process name"),
    "iss_display_process_threads": ("gauge", "Threads owned, by process name"),
    "iss_display_processes": ("gauge", "Processes running, by process name"),
    "iss_display_container_cpu_seconds_total": ("counter", "CPU time the container cgroup used, by mode"),
    "iss_display_container_memory_bytes": ("gauge", "Container cgroup memory in use, by state"),
    "iss_display_container_memory_limit_bytes": ("gauge", "Container cgroup memory limit"),
    "iss_display_container_tasks": ("gauge", "Tasks in the container cgroup"),
}

class Metrics:

    def __init__(self):
        self.lock = threading.Lock()
        self.counters = collections.defaultdict(int)
        self.gauges = {}

    @staticmethod
    def key(name, labels):
        return (name, tuple(sorted((str(k), str(v)) for k, v in labels.items())))

    def inc(self, name, value=1, **labels):
        with self.lock:
            self.counters[self.key(name, labels)] += value

    def set(self, name, value, **labels):
        with self.lock:
            self.gauges[self.key(name, labels)] = value

    def clear_gauge(self, name):
        with self.lock:
            for key in [k for k in self.gauges if k[0] == name]:
                del self.gauges[key]

    def snapshot(self):
        with self.lock:
            return {"counters": [[k[0], list(k[1]), v] for k, v in self.counters.items()],
                    "gauges": [[k[0], list(k[1]), v] for k, v in self.gauges.items()]}

    def merge(self, snapshot):
        for name, labels, value in (snapshot or {}).get("counters", []):
            with self.lock:
                self.counters[(name, tuple(tuple(l) for l in labels))] += value
        for name, labels, value in (snapshot or {}).get("gauges", []):
            with self.lock:
                self.gauges[(name, tuple(tuple(l) for l in labels))] = value

class ProcessStats:

    max_names = 64
    max_threads = 192
    cgroup_root = "/sys/fs/cgroup"
    digits = re.compile(r'\d+')

    def __init__(self):
        self.lock = threading.Lock()
        self.cpu_seconds = collections.defaultdict(float)
        self.thread_seconds = collections.defaultdict(float)
        self.last_seen = {}
        self.last_thread_seen = {}
        self.ticks = os.sysconf('SC_CLK_TCK')

    # The per-process numbers come from doi's parser, the one stat reader both
    # projects share; only the per-thread walk stays local
    def read_proc(self):
        live = {}
        live_threads = {}
        rss = collections.defaultdict(int)
        threads = collections.defaultdict(int)
        counts = collections.defaultdict(int)
        for proc in System.process_cpu_times():
            name = proc["name"]
            live[(proc["pid"], proc["starttime"])] = (name, proc["cpu_s"])
            rss[name] += proc["rss_b"]
            threads[name] += proc["threads"]
            counts[name] += 1
            live_threads.update(self.read_tasks(str(proc["pid"]), name))

        return live, live_threads, rss, threads, counts

    def read_tasks(self, entry, process):
        tasks = {}
        try:
            tids = os.listdir(f"/proc/{entry}/task")
        except OSError:
            return tasks

        for tid in tids:
            try:
                with open(f"/proc/{entry}/task/{tid}/stat", encoding='utf-8') as f:
                    data = f.read()
            except OSError:
                continue

            start, end = data.find('('), data.rfind(')')
            if start < 0 or end < 0:
                continue

            fields = data[end + 2:].split()
            try:
                tasks[(entry, tid, fields[19])] = (
                    (process, self.digits.sub('N', data[start + 1:end])),
                    (int(fields[11]) + int(fields[12])) / self.ticks)
            except (IndexError, ValueError):
                continue

        return tasks

    @staticmethod
    def accumulate(totals, live, last_seen, limit):
        for key, (label, cpu_s) in live.items():
            previous = last_seen.get(key)
            if previous is None:
                if label not in totals and len(totals) >= limit:
                    continue
                totals[label] += cpu_s
            elif cpu_s > previous:
                totals[label] += cpu_s - previous
            last_seen[key] = cpu_s

        for key in [k for k in last_seen if k not in live]:
            del last_seen[key]

    def sample(self):
        live, live_threads, rss, threads, counts = self.read_proc()
        with self.lock:
            self.accumulate(self.cpu_seconds, live,
                            self.last_seen, self.max_names)
            self.accumulate(self.thread_seconds, live_threads,
                            self.last_thread_seen, self.max_threads)

            tracked = set(self.cpu_seconds)

            return (dict(self.cpu_seconds),
                    dict(self.thread_seconds),
                    {k: v for k, v in rss.items() if k in tracked},
                    {k: v for k, v in threads.items() if k in tracked},
                    {k: v for k, v in counts.items() if k in tracked})

    @classmethod
    def cgroup_value(cls, name):
        try:
            with open(f"{cls.cgroup_root}/{name}", encoding='utf-8') as f:
                return int(f.read().strip())
        except (OSError, ValueError):
            return None

    @classmethod
    def cgroup_cpu(cls):
        usage = {}
        try:
            with open(f"{cls.cgroup_root}/cpu.stat", encoding='utf-8') as f:
                for line in f:
                    key, _, value = line.partition(' ')
                    if key in ("user_usec", "system_usec"):
                        usage[key[:-5]] = int(value) / 1000000
        except (OSError, ValueError):
            pass

        return usage

metrics = Metrics()
process_stats = ProcessStats()
live_views = []
live_views_lock = threading.Lock()
last_content_refresh = {}
last_content_refresh_lock = threading.Lock()
content_refreshers = {}
content_refreshers_lock = threading.Lock()


def playlist_item():
    return getattr(threading.current_thread(), "playlist_item", None)

# A playlist item holds a live fetcher under one of these keys; drop them
# before the item is sent as json
item_fetcher_keys = ("news", "bluesky")

def public_item(item):
    return {k: v for k, v in item.items() if k not in item_fetcher_keys}

def view_num():
    item = playlist_item()

    return str(item["num"]) if item else "0"

def env_bool(name, default=False):
    value = os.environ.get(name)
    if value is None or value.strip() == "":
        return default

    return value.strip().lower() not in ("0", "false", "no", "off")

def parse_colour(value):
    text = str(value or "").strip().lstrip("#")
    if len(text) == 3:
        text = "".join(c * 2 for c in text)
    if len(text) != 6:
        return None
    try:
        return tuple(int(text[i:i + 2], 16) for i in (0, 2, 4))
    except ValueError:
        return None

def hex_colour(rgb):
    return "#%02x%02x%02x" % tuple(rgb)

# A highlight in the complementary hue to the background, kept vivid but not
# garish, and light or dark to sit against whichever the body text is not
def harmonious_accent(bg_rgb, font_rgb):
    h, _, s = colorsys.rgb_to_hls(*(c / 255 for c in bg_rgb))
    font_l = colorsys.rgb_to_hls(*(c / 255 for c in font_rgb))[1]
    h = (h + 0.5) % 1.0
    s = 0.65 if s < 0.15 else max(0.5, min(0.8, s))
    l = 0.6 if font_l >= 0.5 else 0.42

    return hex_colour(tuple(round(c * 255)
                            for c in colorsys.hls_to_rgb(h, l, s)))

# A monospace two-column block, key left and value right, the shape the status
# views share. Blank values are dropped and a multi-line value keeps its extra
# lines under the value column
def kv_table(rows):
    pairs = [(str(key), str(value).strip())
             for key, value in rows if str(value).strip()]
    width = max((len(key) for key, _ in pairs), default=0)
    indent = " " * width
    lines = []
    for key, value in pairs:
        head, *rest = value.split("\n")
        lines.append(f"{key.ljust(width)}  {head}")
        lines.extend(f"{indent}  {line}" for line in rest)

    return "\n".join(lines)

# A blank line between a table's column header and its rows, so the header
# reads as a header rather than the first row
def header_gap(text):
    head, sep, rest = text.partition("\n")

    return f"{head}\n\n{rest}" if sep and rest.strip() else text

# A prometheus value is a float; show a whole number without the .0
def fmt_number(v):
    return str(int(v)) if float(v).is_integer() else f"{v:g}"

def udp_family(host):
    try:
        return (socket.AF_INET6 if ipaddress.ip_address(host).version == 6
                else socket.AF_INET)
    except ValueError:
        return socket.AF_INET

def host_port(host, port):
    return f"[{host}]:{port}" if ":" in host else f"{host}:{port}"

def listen_endpoints(bind_address, port):
    """Every address the web server answers on. A specific bind returns
    itself. A wildcard bind (0.0.0.0, ::, unset) expands to its loopback
    plus each interface address from System.net_addresses().
    """
    wildcard_loopback = {"0.0.0.0": "127.0.0.1", "::": "::1", "": "127.0.0.1"}
    host = (bind_address or "").strip("[]")
    if host not in wildcard_loopback:
        return [host_port(host, port)]

    endpoints, seen = [], set()
    for addr in [wildcard_loopback[host]] + [
            line.split()[1] for line in
            (System.net_addresses() or "").splitlines() if len(line.split()) >= 2]:
        addr = addr.split("%", 1)[0]  # if_inet6 zone id
        if addr and addr not in seen:
            seen.add(addr)
            endpoints.append(host_port(addr, port))

    return endpoints

def terminate_process(proc, name, timeout_s=5):
    proc.terminate()
    try:
        proc.wait(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        logging.warning(f"{name}: Did not stop, killing it")
        proc.kill()
        try:
            proc.wait(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            logging.warning(f"{name}: Still running after kill, leaving it to the kernel")

def spawn(cmd, **kwargs):
    """Start a long-running child with the shared defaults: the controller's
    environment, closed inherited fds, and its own session so a signal to the
    controller's process group leaves it alone. Children are torn down by
    name through terminate_process. Keyword args override each default.
    """
    kwargs.setdefault("env", env)
    kwargs.setdefault("start_new_session", True)
    kwargs.setdefault("close_fds", True)

    return Popen(cmd, **kwargs)

def escape_metric_label(value):
    return (str(value).replace("\\", "\\\\")
                      .replace('"', '\\"')
                      .replace("\n", "\\n"))

def metric_family(name):
    for suffix in ("_sum", "_count"):
        if name.endswith(suffix):
            base = name[:-len(suffix)]
            if metric_meta.get(base, ("", ""))[0] in ("summary", "histogram"):
                return base

    return name

def render_metrics(*sources):
    families = collections.defaultdict(list)
    for source in sources:
        for kind in ("counters", "gauges"):
            for name, labels, value in (source or {}).get(kind, []):
                families[metric_family(name)].append(
                    (name, tuple(tuple(l) for l in labels), value))

    lines = []
    for family in sorted(families):
        kind, help_text = metric_meta.get(family, ("untyped", family))
        lines.append(f"# HELP {family} {help_text}")
        lines.append(f"# TYPE {family} {kind}")
        for name, labels, value in sorted(families[family]):
            if labels:
                rendered = ",".join(f'{k}="{escape_metric_label(v)}"' for k, v in labels)
                lines.append(f"{name}{{{rendered}}} {value}")
            else:
                lines.append(f"{name} {value}")

    return "\n".join(lines) + "\n"

def draw(texts, method="python-wayland", **options):
    if method not in draw_methods:
        logging.warning(f"draw: Unknown method {method!r}, drawing with python-wayland")
        method = "python-wayland"

    drawer = draw_vju if method == "vju" else draw_python

    return drawer(texts, **options)

def draw_python(texts, img_bg=False, font_sizes=None, alignment=None,
                title=None, refresh=None, refresh_interval_s=None,
                html_escape=True, args=None):
    wv = Wayland_view(display.res_x, display.res_y, max(len(texts), 1), theme)
    for n in range(len(wv.s_objects)):
        if font_sizes:
            wv.s_objects[n]["font_size"] = font_sizes[min(n, len(font_sizes) - 1)]
        if alignment:
            wv.s_objects[n]["alignment"] = alignment

    return wv.show_content(texts, img_bg,
                           refresh=refresh,
                           refresh_interval_s=refresh_interval_s,
                           html_escape=html_escape)

def omit_no_data_views():
    return env_bool("OMIT_NO_DATA_VIEWS", True)

# Display-wide toggles, read from the environment so the main process and the
# uvicorn subprocess agree without passing state around
def view_indicator():
    return env_bool("VIEW_INDICATOR", False)

def show_qr_code():
    return env_bool("SHOW_QR_CODE", True)

# Picture in picture. A double bar splits the playlist into a pinned view and
# a cycling set: "pinned||a|b|c" pins the first view full size and cycles the
# rest in the corner; "a|b|c||pinned" cycles a, b, c full size and pins the
# last view in the corner. The corner window is this fraction of the output
def pip_scale():
    try:
        return max(0.05, min(0.5, float(
            os.environ.get("RELATIVE_PIP_SIZE", "0.25"))))
    except (TypeError, ValueError):
        return 0.25

def pip_corner(item):
    return bool(item) and str(item.get("pip", "")).endswith("corner")

pip_positions = ("lower-right", "lower-left", "upper-right", "upper-left")

# Which corner of the output the small pip view sits in
def pip_position():
    p = os.environ.get("PIP_POSITION", "lower-right").strip().lower()
    return p if p in pip_positions else "lower-right"

# Whether the gstreamer this image ships has an element, so a pipeline branch
# that needs one can be left out on an older build rather than failing whole
@functools.lru_cache(maxsize=None)
def gst_has(element):
    try:
        return subprocess.run(["gst-inspect-1.0", element], env=env,
                              capture_output=True, timeout=5).returncode == 0
    except Exception:
        return False

# The item views share one shape: a column of text lines with fixed font
# sizes and overlay images bottom right. fetch returns the source's current
# item and runs again on every refresh, so what is shown stays current;
# render(view, item) turns an item into (texts, overlay files) and shows a
# placeholder for an empty one. A source with nothing on the first fetch is
# not shown at all unless OMIT_NO_DATA_VIEWS says otherwise
def draw_item_view(fetch, render, font_sizes, img_bg, refresh_interval_s,
                   overlays=1, source="", draw_function=None,
                   alignments=None):
    first = fetch()
    if not first and omit_no_data_views():
        logging.warning(f"view: No data from {source or 'the source'}, "
                        "omitting the view")
        return

    wv = Wayland_view(display.res_x, display.res_y,
                      len(font_sizes) + overlays, theme)
    for n, size in enumerate(font_sizes):
        wv.s_objects[n]["font_size"] = size
    for n, alignment in (alignments or {}).items():
        wv.s_objects[n]["alignment"] = alignment

    unfetched = object()

    def refresh(item=unfetched):
        texts, files = render(wv, fetch() if item is unfetched else item)
        for n, file in enumerate(files):
            wv.s_objects[len(font_sizes) + n]["file"] = file

        return texts

    wv.show_content(refresh(first), img_bg,
                    refresh_when_hidden=True,
                    refresh=refresh,
                    refresh_interval_s=refresh_interval_s,
                    draw_function=draw_function)

def text_top():
    return round(display.res_y * 0.11)

def text_space():
    return display.res_y - text_top() - 40

def text_height(text, font_size):
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, 1, 1)
    layout = pangocairocffi.create_layout(cairo.Context(surface))
    layout._set_width(pangocffi.units_from_double(display.res_x - 80))
    font = theme.font or theme.font_face
    layout.apply_markup(f'<span font="{font} {font_size}">{text}\n</span>')
    _, extents = layout.get_extents()

    return pangocffi.units_to_double(extents.height)

def text_rows(font_size, header_lines=0):
    line_px = (text_height("Xg\nXg", font_size)
               - text_height("Xg", font_size))
    rows = int(text_space() / line_px) - 1

    return max(3, rows - header_lines)

# A long table is shown whole rather than cut off: the rows are split into
# pages that fit the view, the shown page advances on every refresh, and a
# header line carries the position as 1/2, 2/2. The first line is the column
# header and repeats on every page
def draw_paged_view(title, lines_fn, img_bg, refresh_interval_s=5,
                    method="python-wayland", font_size=14, page_lines=None,
                    split=False):
    state = {"page": 0}
    page_lines = page_lines or text_rows(font_size, 4)

    # A monospace glyph is about 0.8px per point wide and the layout keeps a
    # 40px margin on each side. A row past that width is clipped rather than
    # wrapped, since a plain wrapped tail starts back at column 0 and breaks
    # the table. split=True instead drops the overflow to one continuation
    # line, broken on a space and aligned under the last column, then clips it
    max_chars = max(20, int((display.res_x - 80) / (font_size * 0.8)))

    def last_column_indent(header):
        m = re.search(r"\S+\s*$", header)
        start = m.start() if m else 0

        return min(start, max_chars * 2 // 3)

    def fit(line, cont=0):
        if len(line) <= max_chars:
            return [line]
        if not split:
            return [line[:max_chars - 1] + "…"]
        cut = line.rfind(" ", cont or 8, max_chars)
        if cut <= cont:
            cut = max_chars
        tail = line[cut:].strip()
        room = max_chars - cont - 1
        if len(tail) > room:
            tail = tail[:room - 1] + "…"
        return [line[:cut].rstrip(), " " * cont + tail]

    def texts():
        lines = lines_fn()
        if not lines:
            return [f"No {title.lower()} data"]

        cont = last_column_indent(lines[0]) if split else 0
        head = fit(lines[0])[0]
        blocks = [fit(row, cont) for row in lines[1:]]
        pages, cur = [], []
        for block in blocks:
            if cur and len(cur) + len(block) > page_lines:
                pages.append(cur)
                cur = []
            cur.extend(block)
        if cur:
            pages.append(cur)
        pages = pages or [[]]
        page = state["page"] % len(pages)
        state["page"] = page + 1
        counter = "" if len(pages) == 1 else f" {page + 1}/{len(pages)}"

        return ["\n".join([f"{title}{counter} ({len(blocks)})", "", head, ""]
                          + pages[page])]

    draw(texts(), method=method, img_bg=img_bg, font_sizes=[font_size],
         alignment="left", title=title,
         refresh=texts, refresh_interval_s=refresh_interval_s)

# vju reads what it shows from stdin and keeps its window for as long as it
# runs, so a refresh is a fresh process rather than a repaint
def draw_vju(texts, img_bg=False, font_sizes=None, alignment=None,
             title=None, refresh=None, refresh_interval_s=None,
             args=None, **_):
    scale = pip_scale() if pip_corner(playlist_item()) else 1

    def render(content):
        # Sized as well as fullscreened: every window we spawn is floated, and
        # a floating window gets the size it asks for, so fullscreen alone
        # leaves vju at its own default size
        cmd = [cmds["vju"], "--fullscreen",
               "--width", str(round(display.res_x * scale)),
               "--height", str(round(display.res_y * scale)),
               # so a vju view matches the themed python-wayland ones
               "--text-color", theme.font_colour]
        if not img_bg:
            cmd += ["--background-color", theme.bg_colour]
        if alignment == "center":
            cmd.append("--center-text")
        if font_sizes:
            cmd += ["--font-size", str(max(6, round(font_sizes[0] * scale)))]
        if title:
            cmd += ["--title", str(title)]
        if img_bg:
            cmd += ["--background-image", str(img_bg)]
        cmd += [str(a) for a in (args or [])]

        # With --watch vju re-runs the trailing command itself and keeps its
        # own window, so it is left to run rather than fed on stdin
        watching = "--watch" in cmd
        logging.info(f"vju: {' '.join(cmd)}")
        proc = Popen(cmd, env=env, shell=False, close_fds=True,
                     stdin=None if watching else subprocess.PIPE,
                     encoding="utf8")
        try:
            if watching:
                proc.wait()
            else:
                proc.communicate("\n".join(str(t) for t in content))
        except Exception as e:
            logging.error(f"vju: Failed to render: {e}")

        return proc

    if not refresh or not refresh_interval_s:
        render(texts)

        return True

    content = texts
    while True:
        proc = render(content)
        if proc.returncode not in (0, None):
            logging.warning(f"vju: Exited with {proc.returncode}, stopping")
            break
        try:
            content = refresh() or content
        except Exception as e:
            logging.warning(f"vju: Failed to refresh content: {e}")

    return True

# A child process the controller owns: it can probe whether the service is
# already up, and stop follows the same terminate-then-kill path everywhere
class ManagedProcess:

    label = "process"

    def __init__(self):
        self.proc = None

    def answers(self):
        return False

    def running(self):
        if self.proc is not None:
            return self.proc.poll() is None

        return self.answers()

    def stop(self, timeout_s=5):
        if self.proc is None or self.proc.poll() is not None:
            return False

        logging.info(f"{self.label}: Stopping pid {self.proc.pid}")
        terminate_process(self.proc, self.label, timeout_s)

        return True

# The controller starts the compositor rather than being started by it, so
# the socket is known to be up before any view is spawned, and a compositor
# that dies is something we can see and act on instead of losing the container
class Compositor(ManagedProcess):

    label = "compositor"

    def __init__(self, socket_path=None):
        super().__init__()
        self.socket_path = (socket_path or os.environ.get("SWAYSOCK")
                            or "/tmp/sway-ipc.sock")

    def answers(self):
        try:
            done = subprocess.run(
                ['swaymsg', '-s', self.socket_path, '-t', 'get_version'],
                capture_output=True, env=env, timeout=2)

            return done.returncode == 0
        except Exception:
            return False

    @staticmethod
    def wayland_display():
        runtime = env.get("XDG_RUNTIME_DIR", "/tmp")
        try:
            sockets = sorted(name for name in os.listdir(runtime)
                             if name.startswith("wayland-")
                             and not name.endswith(".lock"))
        except OSError:
            return None

        return sockets[-1] if sockets else None

    def start(self, timeout_s=20):
        if self.answers():
            logging.info(f"compositor: Already running on {self.socket_path}")
            self.adopt()

            return True

        env_mod = env.copy()
        env_mod["SWAYSOCK"] = self.socket_path
        logging.info(f"compositor: Starting {cmds['compositor']}")
        try:
            self.proc = spawn([cmds["compositor"]], env=env_mod)
        except Exception as e:
            logging.error(f"compositor: Failed to start: {e}")

            return False

        deadline = time.time() + timeout_s
        while time.time() < deadline:
            if self.proc.poll() is not None:
                logging.error(f"compositor: Exited with {self.proc.returncode}")

                return False
            if self.answers():
                logging.info(f"compositor: Up as pid {self.proc.pid}")
                self.adopt()

                return True
            time.sleep(0.2)

        logging.error(f"compositor: Not up after {timeout_s}s")

        return False

    # Our own views and every player we spawn are its clients, so they need to
    # find the socket it just created
    def adopt(self):
        for key, value in (("SWAYSOCK", self.socket_path),
                           ("WAYLAND_DISPLAY", self.wayland_display())):
            if not value:
                continue
            env[key] = value
            os.environ[key] = value
        logging.info(f"compositor: WAYLAND_DISPLAY={env.get('WAYLAND_DISPLAY')} "
                     f"SWAYSOCK={env.get('SWAYSOCK')}")

    def stop(self, timeout_s=10):
        return super().stop(timeout_s)

# scream turns the compositor output into the stream the web ui plays, so it
# is a compositor client like the views and is started by the controller once
# the compositor is up, rather than by an exec line in the sway config
class StreamServer(ManagedProcess):

    label = "stream"

    def __init__(self, port=None):
        super().__init__()
        self.port = port or stream_http_port()

    def answers(self):
        try:
            with socket.create_connection(("127.0.0.1", self.port), timeout=1):
                return True
        except OSError:
            return False

    def start(self):
        if self.answers():
            logging.info(f"stream: Already serving on port {self.port}")

            return True

        logging.info(f"stream: Starting {cmds['scream']}")
        try:
            self.proc = spawn([cmds["scream"]])
        except Exception as e:
            logging.error(f"stream: Failed to start: {e}")

            return False

        return True

default_vnc_port = 5900

# wayvnc serves the compositor output over vnc. Like scream it is a compositor
# client the controller owns, rather than an exec line in the sway config, so
# the connect secret is minted here and printed once on start. The password is
# taken from WAYVNC_PASSWORD or generated
class WayvncServer(ManagedProcess):

    label = "vnc"

    def __init__(self, port=None):
        super().__init__()
        self.port = port or env_port("VNC_PORT", default_vnc_port)
        self.address = os.environ.get("VNC_ADDRESS", "0.0.0.0")
        self.username = os.environ.get("WAYVNC_USERNAME") or "iss"
        self.password = (os.environ.get("WAYVNC_PASSWORD")
                         or secrets.token_urlsafe(12))
        self.dir = os.path.join(env.get("XDG_RUNTIME_DIR", "/tmp"), "wayvnc")

    def answers(self):
        try:
            with socket.create_connection(("127.0.0.1", self.port), timeout=1):
                return True
        except OSError:
            return False

    # wayvnc's RSA-AES auth needs an RSA key, and naming a file that is not
    # there is fatal, not a fallback. certtool with --no-text writes it as
    # PKCS#1, which is what nettle loads; a leading text summary is rejected.
    # Without certtool the config leaves the line out and wayvnc mints its own
    def rsa_file(self):
        path = os.path.join(self.dir, "rsa_key.pem")
        if os.path.exists(path):
            return path

        try:
            subprocess.run(["certtool", "--generate-privkey", "--key-type=rsa",
                            "--bits=2048", "--no-text", "--outfile", path],
                           check=True, capture_output=True, env=env)
            os.chmod(path, 0o600)

            return path
        except FileNotFoundError:
            logging.info("vnc: certtool not found, wayvnc will make its own key")
        except (OSError, subprocess.CalledProcessError) as e:
            logging.warning(f"vnc: Could not make an rsa key: {e}")

        return None

    # A self-signed cert keeps older VeNCrypt clients working; without certtool
    # wayvnc still has RSA-AES, so a missing cert is not fatal
    def tls_files(self):
        key = os.path.join(self.dir, "tls_key.pem")
        cert = os.path.join(self.dir, "tls_cert.pem")
        if os.path.exists(key) and os.path.exists(cert):
            return key, cert

        template = os.path.join(self.dir, "cert.tmpl")
        try:
            with open(template, "w") as f:
                f.write("cn = iss-display\ndns_name = localhost\n"
                        "ip_address = 127.0.0.1\nip_address = ::1\n"
                        "expiration_days = 3650\ntls_www_server\n")
            subprocess.run(["certtool", "--generate-privkey", "--key-type=rsa",
                            "--bits=2048", "--no-text", "--outfile", key],
                           check=True, capture_output=True, env=env)
            subprocess.run(["certtool", "--generate-self-signed",
                            "--load-privkey", key, "--template", template,
                            "--outfile", cert],
                           check=True, capture_output=True, env=env)
            os.chmod(key, 0o600)

            return key, cert
        except FileNotFoundError:
            logging.info("vnc: certtool not found, serving RSA-AES only")
        except (OSError, subprocess.CalledProcessError) as e:
            logging.warning(f"vnc: Could not make a tls cert: {e}")

        return None, None

    def write_config(self):
        os.makedirs(self.dir, exist_ok=True)
        os.chmod(self.dir, 0o700)

        lines = [f"address={self.address}",
                 f"port={self.port}",
                 "enable_auth=true",
                 f"username={self.username}",
                 f"password={self.password}"]
        rsa = self.rsa_file()
        if rsa:
            lines.append(f"rsa_private_key_file={rsa}")
        key, cert = self.tls_files()
        if key and cert:
            lines += [f"private_key_file={key}", f"certificate_file={cert}"]

        path = os.path.join(self.dir, "config")
        with open(path, "w") as f:
            f.write("\n".join(lines) + "\n")
        os.chmod(path, 0o600)

        return path

    def start(self):
        if not shutil.which(cmds["wayvnc"]):
            logging.info("vnc: wayvnc not installed, no remote desktop")

            return False

        if self.answers():
            logging.info(f"vnc: Already serving on port {self.port}")

            return True

        try:
            config = self.write_config()
        except OSError as e:
            logging.error(f"vnc: Could not write the config: {e}")

            return False

        border = "vnc: " + "-" * 44
        logging.info(border)
        logging.info(f"vnc: Serving on {self.address}:{self.port}")
        logging.info(f"vnc: username {self.username}")
        logging.info(f"vnc: password {self.password}")
        logging.info(border)

        try:
            self.proc = spawn([cmds["wayvnc"], "-C", config])
        except Exception as e:
            logging.error(f"vnc: Failed to start: {e}")

            return False

        return True

# Running a command is arbitrary code execution, so it is only offered when
# the display was started in debug mode. Read from the environment rather than
# a parsed flag, so the web process and the display process agree
def debug_mode():
    return (env_bool("DEBUG")
            or os.environ.get("LOGLEVEL", "").strip().upper() == "DEBUG")

def web_main(request):
    return HTMLResponse(HtmlPage.page_display())


# Readiness
def healthy(request):
    return PlainTextResponse("OK")


# Liveness
def healthz(request):
    return PlainTextResponse("OK")


def get_playlist_items():
    """
    Return the current playlist's items, preferring the live global `playlist`
    (set when running as __main__) and falling back to rebuilding it from the
    URI/URIS env var (needed when running as a separate Daphne subprocess,
    which never executes the __main__ block).
    """
    pl_global = globals().get('playlist') if 'playlist' in globals() else None
    if pl_global:
        return pl_global.playlist

    live = Display.query_playlist()
    if live:
        return live

    uris_env = os.environ.get('URI') or os.environ.get('URIS')
    if not uris_env:
        return []
    for sep in ["|", " "]:
        uris_env = uris_env.replace(sep, " ")
    uris_list = [u.strip() for u in uris_env.split() if u.strip()]
    if not uris_list:
        return []
    try:
        tmp_pl = Playlist(uris_list, 5, Theme('default'), [], None)
        return tmp_pl.create(uris_list)
    except Exception:
        return []

def screen(request):
    state = Display.query_state()
    if not state:
        return PlainTextResponse("Service Unavailable", status_code=503)
    playlist_items = get_playlist_items()
    data = {
        "name": socket.gethostname(),
        "os_release": "iss-display",
        "listen_address": state.get('address'),
        "listen_port": state.get('port'),
        "res_x": state.get('res_x'),
        "res_y": state.get('res_y'),
        "pinned": state.get('pinned'),
        "playlist": playlist_items,
        "uris": [item.get("uri") for item in playlist_items],
        "streams": getattr(globals().get('stream'), "streams", []),
    }
    return JSONResponse(data)

def display_screenshot(request):
    data = snapshot_bytes()
    if not data:
        metrics.inc("iss_display_screenshot_failures_total")

        return PlainTextResponse("Not Found", status_code=404)

    return Response(content=data, media_type="image/jpeg",
                    headers={"Cache-Control": "no-store"})

def list_routes(app_instance=None):
    """
    Return a list of all registered routes in the ASGI app.
    Each entry contains: path, methods, and route name.
    """
    return [{"path": getattr(r, "path", ""),
             "methods": sorted(getattr(r, "methods", None) or []),
             "name": getattr(r, "name", "")}
            for r in (app_instance or app).routes]

def api_routes(request):
    return JSONResponse(list_routes())

# Counted by the route's own path, not the requested one, so path parameters
# stay grouped and an unmatched request cannot add a key of its choosing
def counted_path(scope):
    for route in app.routes:
        match, _ = route.matches(scope)
        if match == Match.FULL:
            return getattr(route, "path", scope.get("path", ""))

    return "<unmatched>"

class RequestCounter:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            await self.app(scope, receive, send)

            return

        path = counted_path(scope)
        metrics.inc("iss_display_http_requests_total", path=path)
        started = time.time()
        try:
            await self.app(scope, receive, send)
        finally:
            metrics.inc("iss_display_http_request_duration_seconds_sum",
                        time.time() - started, path=path)
            metrics.inc("iss_display_http_request_duration_seconds_count", path=path)

# The controller runs twice: the main process owns the display and this
# module's globals, while the uvicorn subprocess imports it bare and reaches
# the main process over the state udp socket. Every api handler faces that
# split, so it lives in one place
def display_call(local, remote, *args):
    display_global = globals().get('display')
    if display_global:
        return JSONResponse(getattr(display_global, local)(*args))

    reply = getattr(Display, remote)(*args)
    if reply is None:
        return JSONResponse({"error": "display not reachable"}, status_code=503)

    return JSONResponse(reply)

def api_playlist_remove(request):
    return display_call("remove_playlist_item", "remove_item",
                        request.path_params.get("num"))

def api_playlist_time(request):
    return display_call("set_item_play_time", "set_item_time",
                        request.path_params.get("num"),
                        request.query_params.get("seconds"))

def api_background(request):
    return display_call("set_view_background", "set_background",
                        request.query_params.get("colour"),
                        request.query_params.get("num"))

def api_requests(request):
    paths = collections.Counter()
    for name, labels, value in metrics.snapshot().get("counters", []):
        if name == "iss_display_http_requests_total":
            paths[dict(labels).get("path", "<unknown>")] += value

    return JSONResponse({"total": sum(paths.values()),
                         "paths": dict(sorted(paths.items()))})

def api_metrics(request):
    body = render_metrics(metrics.snapshot(), Display.query_metrics())

    return PlainTextResponse(body,
                             media_type="text/plain; version=0.0.4; charset=utf-8")

def api_resolution(request):
    return display_call("set_resolution", "set_output_resolution",
                        request.path_params.get("mode"))

def api_shell(request):
    if not debug_mode():
        return JSONResponse({"error": "shell is only available in debug mode"},
                            status_code=403)

    return display_call("run_command", "run_shell",
                        request.query_params.get("command"))

def api_settings(request):
    result = {}
    for value, local, remote in (
            (request.query_params.get("name"),
             "set_playlist_name", "set_name"),
            (request.query_params.get("default_time"),
             "set_default_play_time", "set_default_time")):
        if value is None:
            continue
        reply = display_call(local, remote, value)
        if reply.status_code != 200:
            return reply
        result.update(json.loads(reply.body))

    if not result:
        return JSONResponse({"error": "nothing to set"}, status_code=400)

    return JSONResponse(result)

def api_next(request):
    return display_call("step_rotation", "step", 1)

def api_previous(request):
    return display_call("step_rotation", "step", -1)

def api_pin(request):
    return display_call("toggle_view_pin", "pin_view")

def api_media_next(request):
    return display_call("media_step", "media_next", 1)

def api_media_previous(request):
    return display_call("media_step", "media_next", -1)

def api_playlist_add(request):
    uri = request.query_params.get("uri")
    play_time_s = request.query_params.get("t")
    if uri and play_time_s and "t=" not in urlsplit(uri).query:
        joiner = "&" if urlsplit(uri).query else "?"
        uri = f"{uri}{joiner}t={play_time_s}"

    return display_call("add_playlist_item", "add_item", uri)

def api_playlist_toggle(request):
    return display_call("toggle_playlist_item", "toggle_item",
                        request.path_params.get("num"))

# Prepare ASGI app for API and web ui
app = Starlette(routes=[
    Route("/", web_main, methods=["GET"], name="web_main"),
    Route("/display", web_main, methods=["GET"], name="web_display"),
    Route("/healthy", healthy, methods=["GET"], name="healthy"),
    Route("/healthz", healthz, methods=["GET"], name="healthz"),
    Route("/api/v1/display", screen, methods=["GET"], name="api_display"),
    Route("/screenshot", display_screenshot, methods=["GET"], name="screenshot"),
    Route("/api/v1/screenshot", display_screenshot, methods=["GET"], name="api_screenshot"),
    Route("/api/v1/routes", api_routes, methods=["GET"], name="api_routes"),
    Route("/api/v1/settings", api_settings, methods=["POST"], name="api_settings"),
    Route("/api/v1/shell", api_shell, methods=["POST"], name="api_shell"),
    Route("/api/v1/next", api_next, methods=["POST"], name="api_next"),
    Route("/api/v1/previous", api_previous, methods=["POST"], name="api_previous"),
    Route("/api/v1/pin", api_pin, methods=["POST"], name="api_pin"),
    Route("/api/v1/media/next", api_media_next,
          methods=["POST"], name="api_media_next"),
    Route("/api/v1/media/previous", api_media_previous,
          methods=["POST"], name="api_media_previous"),
    Route("/api/v1/playlist", api_playlist_add,
          methods=["POST"], name="api_playlist_add"),
    Route("/api/v1/playlist/{num:int}/toggle", api_playlist_toggle,
          methods=["POST"], name="api_playlist_toggle"),
    Route("/api/v1/playlist/{num:int}", api_playlist_remove,
          methods=["DELETE"], name="api_playlist_remove"),
    Route("/api/v1/playlist/{num:int}/time", api_playlist_time,
          methods=["POST"], name="api_playlist_time"),
    Route("/api/v1/background", api_background,
          methods=["POST"], name="api_background"),
    Route("/api/v1/requests", api_requests, methods=["GET"], name="api_requests"),
    Route("/api/v1/resolution/{mode}", api_resolution,
          methods=["POST"], name="api_resolution"),
    Route("/metrics", api_metrics, methods=["GET"], name="metrics"),
], middleware=[Middleware(RequestCounter)])


def download_file(url, path):
    logging.info(f"Downloading: {url}")

    # path is the destination directory and the filename comes from the url,
    # which is what the curl -LO --output-dir this replaces did. curl itself
    # is no longer in the image
    os.makedirs(path, exist_ok=True)
    filename = url.rstrip("/").rsplit("/", 1)[-1] or "download"
    try:
        response = requests.get(url, timeout=30)
        response.raise_for_status()
    except requests.RequestException as e:
        logging.error(f"download: {url}: {e}")

        return False

    with open(os.path.join(path, filename), "wb") as file:
        file.write(response.content)

    return True


# An older doi has no caption method and may hand back a plain string
# instead of a meta dict; the title carries until it does
def image_caption(meta, source):
    if isinstance(meta, dict):
        if hasattr(source, "caption"):
            return source.caption(meta)

        return meta.get("title") or ""

    return str(meta or "")

# PIL opens it -- so draw_image can too. Not every "picture of the day" is an
# image: APOD is a video some days, and a downloaded embed page is not one
# either, and Image.open on that kills the view thread
def is_image_file(path):
    try:
        with Image.open(path) as im:
            im.verify()
        return True
    except (OSError, UnidentifiedImageError, ValueError):
        return False

# A full-bleed image with a centred line above and a caption below, the shape
# the apod and museum views share. Black behind the letterboxing rather than
# transparent, or the sway backdrop shows through if the window is ever seen
# before the rotation has placed it
def draw_captioned_image(img_path, caption, bg_colour="#000000"):
    wv = Wayland_view(display.res_x, display.res_y, 2, theme)
    wv.s_objects[0]["font_size"] = 20
    wv.s_objects[0]["alignment"] = "center"
    wv.s_objects[1]["font_size"] = 20

    if not img_path or not is_image_file(img_path):
        logging.warning(f"view: {img_path} is not an image, showing the caption")
        wv.s_objects[0]["alignment"] = "left"
        wv.show_content([caption or "No picture today"])
        return

    wv.set_texts(["", caption])
    wv.show_image(img_path, bg_colour=bg_colour)

def draw_apod():
    """
    Draws the Astronomy Picture of the Day (APOD) for the current day,
    with the attribution captioned under it the way the art views do.
    """
    key = globals().get('apod_api_key') or os.environ.get('APOD_API_KEY') or 'DEMO_KEY'
    img_path, meta = APOD(api_key=key).apod_data()
    if not meta:
        logging.error("Failed to fetch APOD data.")
        metrics.inc("iss_display_fetch_failures_total", source="apod")
        return

    caption = image_caption(meta, Art) if isinstance(meta, dict) else ""
    # A video day has meta but no image; fall back to the caption and blurb
    if not img_path and isinstance(meta, dict) and meta.get("description"):
        caption = f"{caption}\n\n{meta['description']}".strip()
    draw_captioned_image(img_path, caption, bg_colour="#000000")

# iss://system/filesizes/<path> and iss://system/dirsizes/<path>. The walk is
# done once when the view starts rather than on a timer: the numbers barely
# move on an appliance and a walk is the most expensive thing any view does
def draw_sizes(kind, path=None):
    """
    Draws the biggest files or directories under a path.
    Args:
        kind (str): 'files' or 'dirs'.
        path (str): Where to look, defaults to the item's path or /.
    """
    if path is None:
        item = playlist_item()
        path = (item or {}).get("scan_path") or "/"

    reader = System.biggest_files if kind == "files" else System.biggest_dirs
    try:
        text = reader(path, 12)
    except Exception as e:
        logging.error(f"sizes: Failed to scan {path}: {e}")
        metrics.inc("iss_display_fetch_failures_total", source=kind)

        return

    wv = Wayland_view(display.res_x, display.res_y, 1, theme)
    wv.s_objects[0]["font_size"] = 18
    wv.s_objects[0]["alignment"] = "left"
    wv.show_content([header_gap(text)])

# iss://art/<source>; the bare iss://art keeps working and means ngoa
art_sources = {"ngoa": ArtNGA, "mmoa": ArtMet}
default_art_source = "ngoa"

def draw_art(source=None):
    """
    Draws a work from one of the museum collections.
    Args:
        source (str): 'ngoa' for the National Gallery, 'mmoa' for the Met.
    """
    if source is None:
        item = playlist_item()
        source = (item or {}).get("art_source", default_art_source)

    collection = art_sources.get(source)
    if collection is None:
        logging.warning(f"art: Unknown source {source!r}, using {default_art_source}")
        collection = art_sources[default_art_source]

    art = collection(width=display.res_x, height=display.res_y)
    img_path, meta = art.art_data()
    if not img_path:
        logging.error(f"Failed to fetch a work of art from {source}.")
        metrics.inc("iss_display_fetch_failures_total", source=f"art-{source}")

        return

    draw_captioned_image(img_path, image_caption(meta, collection))

def draw_calendar():
    """
    Draws a calendar for the current month, highlighting the current day and
    day name, with a big day-of-month numeral to the right of the grid, sized
    to the grid's own height, and the next bank holidays underneath.
    """
    import datetime

    # The month lines come from doi; the pango markup for the highlights is
    # ours, since it belongs to this renderer. Colours come from the theme
    accent = theme.highlight_colour
    body = theme.font_colour
    face = theme.font_face or "Monospace"

    def highlight(match):
        return (f'</span><span foreground="{accent}" font="{face} 23">'
                f'{match.group(0)}</span>'
                f'<span foreground="{body}" font="{face} 20">')

    def highlight_day_name(match):
        return (f'<span foreground="{accent}" font="{face} 23">'
                f'{match.group(0)}</span>')

    if hasattr(Calendar, "month_text"):
        lines = Calendar.month_text(highlight, highlight_day_name)
        holidays = Calendar.holiday_lines(location="Germany", count=3)
    else:
        lines = ["Calendar view needs a newer doi"]
        holidays = []

    texts = ["\n".join(lines), str(datetime.date.today().day)]
    if holidays:
        texts.append("\n".join(holidays))

    wv = Wayland_view(display.res_x, display.res_y, len(texts), theme)
    wv.s_objects[0]["font_size"] = 20
    wv.s_objects[0]["alignment"] = "left"
    accent_rgb = parse_colour(accent)
    if accent_rgb:
        (wv.s_objects[1]["font_colour_r"], wv.s_objects[1]["font_colour_g"],
         wv.s_objects[1]["font_colour_b"]) = accent_rgb
    wv.s_objects[1]["scale"] = 1.5
    wv.s_objects[1]["gap"] = 100
    if len(texts) > 2:
        wv.s_objects[2]["font_size"] = 16
        wv.s_objects[2]["alignment"] = "left"
    wv.show_content(texts, html_escape=False, draw_function=view.draw_scaled_pair)


class Zeroconf_service:

    def __init__(self,
                 name_prefix,
                 service_type,
                 hostname,
                 listen_address,
                 listen_port,
                 properties):

        self.service_type = service_type
        self.service_name = f"{name_prefix}-{hostname}.{service_type}"

        try:
            addrs = [socket.inet_pton(udp_family(listen_address), listen_address)]
        except OSError:
            logging.warning(f"zeroconf: invalid listen_address '{listen_address}', "
                            "defaulting to 127.0.0.1")
            addrs = [socket.inet_pton(socket.AF_INET, '127.0.0.1')]

        self.zc_service = ServiceInfo(
            self.service_type,
            self.service_name,
            addresses=addrs,
            port=int(listen_port),
            properties=properties,
            server=f"{hostname}.local.",
        )

        self.zc = Zeroconf(ip_version=IPVersion.All)

    def register(self):
        self.zc.register_service(self.zc_service)

        return True

    def unregister(self):
        self.zc.unregister_service(self.zc_service)
        self.zc.close()

        return True


def load_toml_table(filename, name):
    here = os.path.dirname(os.path.abspath(__file__))
    for path in (os.path.join(here, filename), os.path.abspath(filename)):
        try:
            with open(path, "rb") as f:
                return tomllib.load(f).get(name, {})
        except FileNotFoundError:
            continue
        except (OSError, tomllib.TOMLDecodeError) as e:
            logging.warning(f"config: Could not read {path}: {e}")

            return {}

    return {}


theme_env_vars = {
    "bg_colour": "THEME_BG_COLOUR",
    "font_colour": "THEME_FONT_COLOUR",
    "highlight_colour": "THEME_HIGHLIGHT_COLOUR",
    "font": "THEME_FONT",
    "font_face": "THEME_FONT_FACE",
    "pip_size": "RELATIVE_PIP_SIZE",
    "pip_position": "PIP_POSITION",
    "view_indicator": "VIEW_INDICATOR",
    "view_indicator_colour": "THEME_VIEW_INDICATOR_COLOUR",
}

def apply_theme(name):
    for key, value in load_toml_table("themes.toml", name).items():
        var = theme_env_vars.get(key)
        if var is None:
            logging.warning(f"theme: {name} sets unknown key {key}")

            continue
        if isinstance(value, bool):
            value = "1" if value else "0"
        os.environ.setdefault(var, str(value))

def playlist_uris(name):
    name = str(name or "")
    if "://" in name:
        return [name]
    uris = load_toml_table("playlists.toml", name).get("uris", [])

    return [os.path.expandvars(str(uri)) for uri in uris]

if __name__ != "__main__":
    apply_theme(os.environ.get("THEME", "infinit"))
    if not (os.environ.get("URI") or os.environ.get("URIS")):
        os.environ["URI"] = "|".join(
            playlist_uris(os.environ.get("PLAYLIST", "infinit")))


class Theme:

    default_bg_colour = "#280f28"
    # Not pure white: a hair off full brightness reads with less halation on a
    # panel and re-encodes with fewer ringing artefacts in scream's stream.
    # Override with THEME_FONT_COLOUR
    default_font_colour = "#ededed"

    def __init__(self, name="default"):
        self.name = name
        self.font = os.environ.get("THEME_FONT", "")
        self.font_face = os.environ.get("THEME_FONT_FACE", "Monospace")

        self.bg_colour = os.environ.get("THEME_BG_COLOUR",
                                       self.default_bg_colour)
        self.font_colour = os.environ.get("THEME_FONT_COLOUR",
                                          self.default_font_colour)
        self.view_indicator_colour = os.environ.get(
            "THEME_VIEW_INDICATOR_COLOUR", self.font_colour)
        # A complementary accent for the bits a view wants to stand out, the
        # calendar's current day among them; overridable, else derived
        bg = parse_colour(self.bg_colour) or parse_colour(self.default_bg_colour)
        fg = parse_colour(self.font_colour) \
            or parse_colour(self.default_font_colour)
        self.highlight_colour = os.environ.get(
            "THEME_HIGHLIGHT_COLOUR", harmonious_accent(bg, fg))

        path = "themes/" + self.name + "/background.jpg"
        if os.path.exists(path):
            self.img_bg = path
        else:
            self.img_bg = False

class Playlist:

    def __init__(self,
                 uris,
                 default_play_time_s,
                 theme,
                 topics,
                 location=None,
                 name=""):

        self.name = name
        self.download_path = "/tmp/"
        self.location = location
        self.default_play_time_s = default_play_time_s
        self.news = None
        self.theme = theme
        self.topics = topics or []

        self.probe_ip_address = None
        self.browser = None
        self.media_player = None
        # When the media item is an .m3u, its entries and the one playing now,
        # so media_step can move through them
        self.media_entries = []
        self.media_index = 0
        self.playlist = self.create(uris)

    our_params = ("t", "enabled", "refresh", "method")

    # The rest of the query is carried over as it was written rather than
    # re-encoded, or a uri like windy's ?radar,50.5,9.8,5,m:e2BagII comes back
    # percent encoded and with an = appended
    @classmethod
    def split_params(cls, uri):
        parts = urlsplit(uri)
        if not parts.query:
            return uri, {}

        kept, params = [], {}
        for segment in parts.query.split("&"):
            key, sep, value = segment.partition("=")
            if key in cls.our_params:
                params[key] = unquote_plus(value) if sep else ""
            else:
                kept.append(segment)

        if not params:
            return uri, {}

        return urlunsplit(parts._replace(query="&".join(kept))), params

    @staticmethod
    def parse_play_time(params, uri):
        if "t" not in params:
            return None
        try:
            return int(params["t"])
        except ValueError:
            logging.warning(f"playlist: Ignoring play time {params['t']!r} in {uri}")

            return None

    @staticmethod
    def parse_method(params, uri):
        method = params.get("method")
        if method is None:
            return None
        if method in draw_methods:
            return method

        logging.warning(f"playlist: Ignoring method {method!r} in {uri}")

        return None

    @staticmethod
    def parse_refresh(params, uri):
        if "refresh" not in params:
            return None

        try:
            refresh_s = int(params["refresh"])
        except ValueError:
            logging.warning(f"playlist: Ignoring refresh {params['refresh']!r} in {uri}")

            return None

        if refresh_s < 1:
            logging.warning(f"playlist: Ignoring refresh {refresh_s} in {uri}")

            return None

        return refresh_s

    @staticmethod
    def parse_enabled(params, uri):
        if "enabled" not in params:
            return True

        value = params["enabled"].strip().lower()
        if value in ("true", "yes", "1", ""):
            return True
        if value in ("false", "no", "0"):
            return False

        logging.warning(f"playlist: Ignoring enabled {params['enabled']!r} in {uri}")

        return True

    # A longer prefix must come before its prefix string, so the network
    # sub-paths are listed before network
    uri_players = {"iss://apod": "apod",
                   "iss://art": "art",
                   "iss://bluesky": "bluesky",
                   "iss://calendar": "calendar",
                   "iss://clock": "clock",
                   "iss://date": "date",
                   "iss://log": "log",
                   "iss://mqtt": "mqtt",
                   "iss://music": "music",
                   "iss://network/neighbours": "neighbours",
                   "iss://network/sockets": "sockets",
                   "iss://network/traceroute": "traceroute",
                   "iss://network": "network",
                   "iss://news": "news",
                   "iss://onthisday": "onthisday",
                   "iss://playlist": "playlist",
                   "iss://prometheus": "prometheus",
                   "iss://shell": "shell",
                   "iss://system/dirsizes": "dirsizes",
                   "iss://system/filesizes": "filesizes",
                   "iss://system/internals": "system",
                   "iss://system/processes": "processes",
                   "iss://system/top": "top",
                   "iss://weather": "weather"}

    feed_suffixes = ('/rss', '/feed', '/atom', '.rss', '.atom', '.xml')

    def classify(self, uri):
        '''
        The player for a uri, and the uri as the item will carry it.
        (None, uri) for one we do not know how to show.
        '''
        if uri.endswith((".m3u8", ".m3u")):
            return "mediaplayer", uri
        if uri.startswith("https://"):
            if any(uri.rstrip('/').lower().endswith(s)
                   for s in self.feed_suffixes):
                return "news", uri
            return "browser", uri
        if uri.endswith(".svg"):
            download_file(uri.strip(), self.download_path)
            return "imageviewer", \
                "file:///" + self.download_path + uri.rsplit("/", 1)[-1]
        if uri.endswith(".jpg"):
            return "imageviewer", uri

        for prefix, player in self.uri_players.items():
            if uri.startswith(prefix):
                return player, uri

        return None, uri

    def item_extras(self, uri, player):
        '''
        The fields and constructions a player needs beyond the uri.
        None drops the item.
        '''
        if player == "news":
            if uri.startswith("https://"):
                return {"news": RSSFeed(uri)}
            self.news = News({"hn": "",
                              "db": "/home/mue/.local/share/russ/feeds.db"})
            return {}
        if player == "onthisday":
            self.otd = OTD({"wikipedia": ""})
            return {}
        if player == "bluesky":
            # iss://bluesky/@handle or iss://bluesky/handle
            actor = uri[len("iss://bluesky/"):].strip().lstrip("@")
            if not actor:
                logging.warning(f"playlist: Dropping {uri}, no bluesky handle")
                return None
            return {"bluesky": Bluesky(actor)}
        if player == "weather":
            self.weather = Weather(self.location)
            return {}
        if player == "art":
            wanted = uri[len("iss://art"):].strip("/").lower()
            return {"art_source": (wanted if wanted in art_sources
                                   else default_art_source)}
        if player in ("filesizes", "dirsizes"):
            wanted = uri[len(f"iss://system/{player}"):].strip()
            return {"scan_path": wanted if wanted.startswith("/") else "/"}
        if player == "prometheus":
            # iss://prometheus/<endpoint>/<metric>[,<metric>...]; the endpoint
            # keeps its own path, so the metric names are the last segment
            endpoint, _, wanted = uri[len("iss://prometheus/"):].rpartition("/")
            names = [n for n in wanted.split(",") if n]
            if not endpoint or not names:
                logging.warning(f"playlist: Dropping {uri}, want "
                                "iss://prometheus/<url>/<metric>")
                return None
            return {"prom_url": endpoint, "prom_metrics": names}
        if player == "log":
            wanted = uri[len("iss://log"):].strip("/")
            try:
                return {"log_lines": max(1, min(200, int(wanted)))}
            except ValueError:
                return {"log_lines": 10}
        if player == "traceroute" and not Playlist.traceroute_target:
            logging.warning(f"playlist: Dropping {uri}, "
                            "TRACEROUTE_TARGET is not set")
            return None

        return {}

    def create(self, uris):
        playlist = list()

        # The double bar survives the split as an empty element. After the
        # first uri it pins that view full and cycles the rest in the corner;
        # before the last uri it cycles the rest full and pins the last in
        # the corner
        head = len(uris) > 1 and str(uris[1]).strip() == ""
        tail = not head and len(uris) > 2 and str(uris[-2]).strip() == ""
        if head:
            uris = [uris[0], *uris[2:]]
        elif tail:
            uris = [*uris[:-2], uris[-1]]
        last = len(uris)

        for n, uri in enumerate(uris, start=1):
            uri, params = self.split_params(uri)
            play_time_s = self.parse_play_time(params, uri)

            player, uri = self.classify(uri)
            if player is None:
                continue
            extras = self.item_extras(uri, player)
            if extras is None:
                continue

            item = {"num": n,
                    "uri": uri,
                    "player": player,
                    "play_time_s": play_time_s or self.default_play_time_s,
                    "play_time_explicit": bool(play_time_s),
                    "refresh_s": self.parse_refresh(params, uri),
                    "method": self.parse_method(params, uri)
                              or "python-wayland",
                    "enabled": self.parse_enabled(params, uri)}
            item.update(extras)
            if head:
                item["pip"] = "pinned-full" if n == 1 else "cycle-corner"
            elif tail:
                item["pip"] = "pinned-corner" if n == last else "cycle-full"
            playlist.append(item)

        return playlist

    def set_default_play_time(self, play_time_s):
        self.default_play_time_s = play_time_s
        for item in self.playlist:
            if not item.get("play_time_explicit"):
                item["play_time_s"] = play_time_s

    def add(self, uri):
        items = self.create([uri])
        if not items:
            return None

        item = items[0]
        item["num"] = max((i["num"] for i in self.playlist), default=0) + 1
        self.playlist.append(item)

        return item

    def start_player(self, probe_ip_address):
        self.probe_ip_address = probe_ip_address
        threads = list()
        for item in self.playlist:
            if not item.get("enabled", True):
                logging.info(f"Skipping disabled item {item['num']}: {item['uri']}")
                continue

            x = self.start_item(item)
            if x:
                threads.append(x)

        return threads

    # An item has two times: play_time_s is how long the view is shown
    # (the t= param), refresh_s how often its content is fetched again
    # (the refresh= param). Unless a uri says otherwise, content refreshes
    # once per showing
    @staticmethod
    def item_refresh_s(item):
        return item.get("refresh_s") or item["play_time_s"]

    def refresh_args(self, item):
        return (self.theme.img_bg,
                self.item_refresh_s(item),
                item.get("method", "python-wayland"))

    # player name -> (self, item, probe) -> the zero-arg callable its thread
    # runs. draw_* render once and return, start_* keep a loop or a child
    # process alive for the item's life
    players = {
        "apod":        lambda s, i, p: draw_apod,
        "art":         lambda s, i, p: draw_art,
        "calendar":    lambda s, i, p: draw_calendar,
        "clock":       lambda s, i, p: s.start_clock,
        "dirsizes":    lambda s, i, p: partial(draw_sizes, "dirs"),
        "filesizes":   lambda s, i, p: partial(draw_sizes, "files"),
        "browser":     lambda s, i, p: partial(s.start_browser, [i["uri"]]),
        "imageviewer": lambda s, i, p: partial(s.start_image_view, i["uri"]),
        "mediaplayer": lambda s, i, p: partial(s.start_mediaplayer, i["uri"]),
        "date":        lambda s, i, p: partial(s.start_date_view,
                                               i.get("refresh_s") or 1,
                                               f"{i['player']}-{i['num']}"),
        "log":         lambda s, i, p: partial(s.start_log_view,
                                               *s.refresh_args(i),
                                               i.get("log_lines", 10)),
        "mqtt":        lambda s, i, p: partial(s.start_mqtt_views,
                                               s.topics, s.theme),
        "music":       lambda s, i, p: partial(s.start_music_view,
                                               s.theme.img_bg,
                                               s.item_refresh_s(i)),
        "bluesky":     lambda s, i, p: partial(s.start_bluesky_view,
                                               i["bluesky"],
                                               s.theme.img_bg,
                                               s.item_refresh_s(i)),
        "network":     lambda s, i, p: partial(s.start_net_view,
                                               s.theme.img_bg, p),
        "neighbours":  lambda s, i, p: partial(s.start_neighbours_view,
                                               *s.refresh_args(i)),
        "news":        lambda s, i, p: partial(s.start_news_view,
                                               i.get("news") or s.news,
                                               s.theme.img_bg,
                                               s.item_refresh_s(i)),
        "onthisday":   lambda s, i, p: partial(s.start_onthisday_view, s.otd,
                                               s.theme.img_bg,
                                               s.item_refresh_s(i)),
        "playlist":    lambda s, i, p: partial(s.start_playlist_view,
                                               *s.refresh_args(i)),
        "prometheus":  lambda s, i, p: partial(s.start_prometheus_view,
                                               i["prom_url"], i["prom_metrics"],
                                               *s.refresh_args(i)),
        "system":      lambda s, i, p: partial(s.start_sys_view,
                                               s.theme.img_bg, p),
        "weather":     lambda s, i, p: partial(s.start_weather_view,
                                               s.weather, s.theme.img_bg),
        "processes":   lambda s, i, p: partial(s.start_proc_view,
                                               *s.refresh_args(i)),
        "shell":       lambda s, i, p: partial(s.start_shell_view,
                                               *s.refresh_args(i)),
        "sockets":     lambda s, i, p: partial(s.start_sockets_view,
                                               *s.refresh_args(i)),
        "top":         lambda s, i, p: partial(s.start_top_view,
                                               *s.refresh_args(i)),
        "traceroute":  lambda s, i, p: partial(s.start_traceroute_view,
                                               *s.refresh_args(i)),
    }

    def start_item(self, item, probe_ip_address=None):
        probe = probe_ip_address or self.probe_ip_address
        build = self.players.get(item["player"])
        if build is None:
            logging.warning(f"No player for {item['player']}, item {item['num']}")

            return None

        logging.info(f"Starting {item['player']} view for item {item['num']}")
        x = threading.Thread(target=build(self, item, probe))
        x.playlist_item = item
        x.start()
        if not x.is_alive():
            logging.error(f"Failed to start {item['player']}")

            return None

        item["started"] = True

        return x

    def start_browser(self, urls, engine=None):
        engine = engine or os.environ.get("BROWSER_ENGINE", "servo")
        if engine not in browser_engines:
            logging.warning(f"Unknown browser engine {engine!r}, using servo")
            engine = "servo"

        if engine == "firefox":
            cmd, env_mod = self.browser_firefox(urls)
        else:
            cmd, env_mod = self.browser_servo(urls)

        # Held on the instance so it can be waited on, restarted and stopped
        # like any other player
        self.stop_browser()
        self.browser = spawn(cmd, env=env_mod, encoding='utf8')
        logging.info(f"Started {engine} as pid {self.browser.pid}: {' '.join(cmd)}")

        return True

    # servo renders the page itself, with no driver in between. It cannot
    # be scripted, so a page needing a login wants the firefox engine. This
    # is the chromeless flavour, so there is no minibrowser toolbar to hide
    @staticmethod
    def browser_servo(urls):
        # Sized like the other players: everything we spawn is floated, and a
        # floating window is given the size it asks for
        cmd = [cmds["servo"],
               "--no-native-titlebar",
               f"--window-size={display.res_x}x{display.res_y}",
               f"--screen-size={display.res_x}x{display.res_y}"]
        cmd += shlex.split(os.environ.get("BROWSER_ARGS", ""))
        cmd += list(urls[:1])

        return cmd, env.copy()

    # Driven through geckodriver by webdriver_util, which is what can fill in
    # a login form and cycle tabs
    @staticmethod
    def browser_firefox(urls):
        cmd = [sys.executable, '-m', 'webdriver_util']
        for url in urls:
            cmd.append("--url")
            cmd.append(url)

        # Disable Selenium Manager and provide explicit driver/browser paths
        env_mod = env.copy()
        env_mod['SE_DISABLE_DRIVER_MANAGEMENT'] = '1'
        env_mod['GECKODRIVER'] = env_mod.get('GECKODRIVER', '/usr/bin/geckodriver')
        env_mod['FIREFOX_BIN'] = env_mod.get('FIREFOX_BIN', '/usr/bin/firefox')

        return cmd, env_mod

    def browser_running(self):
        return self.browser is not None and self.browser.poll() is None

    def stop_browser(self, timeout_s=5):
        if not self.browser_running():
            self.browser = None

            return False

        logging.info(f"Stopping browser pid {self.browser.pid}")
        terminate_process(self.browser, "browser", timeout_s)
        self.browser = None

        return True

    def media_player_running(self):
        return self.media_player is not None and self.media_player.poll() is None

    def stop_mediaplayer(self, timeout_s=5):
        if not self.media_player_running():
            self.media_player = None

            return False

        logging.info(f"Stopping media player pid {self.media_player.pid}")
        terminate_process(self.media_player, "mediaplayer", timeout_s)
        self.media_player = None

        return True

    def start_clock(self):
        try:
            spawn([cmds["clock"]])
            return True
        except Exception as e:
            logging.error(f"Clock start failed: {e}")
            return False

    def start_mqtt_views(self, topics, theme):
        mqtt_client_id = "iss-display-42"
        mqtt = MQTT(mqtt_broker,
                    mqtt_client_id,
                    mqtt_port,
                    mqtt_user,
                    mqtt_pw)
        if not topics:
            logging.info("MQTT: No topics configured; skipping MQTT view subscriptions")
            return False
        try:
            mqttc = mqtt.connect()
        except Exception:
            logging.info("MQTT: Failed to connect")
            return False

        def render_mqtt_view(topic, texts):
            wv = Wayland_view(display.res_x, display.res_y, len(texts), theme)
            for i in range(len(texts)):
                wv.s_objects[i]["font_size"] = 64 if i == 0 else 48
                wv.s_objects[i]["alignment"] = "center"
            wv.show_content(texts, theme.img_bg)

        for topic in topics:
            mqtt.subscribe(mqttc, topic, render_mqtt_view)
            logging.info(f"MQTT: Subscribed to {topic}")

        mqttc.loop_forever()

    # (video variant to play, audio rendition playlist or None). Broadcast HLS
    # commonly carries the audio as a separate rendition rather than muxed into
    # the video segments, and legacy hlsdemux plays only the one stream it is
    # given -- so the rendition is fetched as a second source
    @staticmethod
    def hls_sources(url):
        max_h = int(os.environ.get("MEDIA_MAX_HEIGHT") or 720)
        try:
            text = requests.get(url, timeout=15).text
        except requests.RequestException as e:
            logging.warning(f"mediaplayer: reading variants of {url}: {e}")
            return url, None
        if "#EXT-X-STREAM-INF" not in text:
            return url, None
        lines = text.splitlines()

        # Resolve against the master's url: some masters use a bare filename,
        # others a /-rooted path
        def absolute(u):
            return urljoin(url, u)

        best = None
        for i, line in enumerate(lines):
            if not line.startswith("#EXT-X-STREAM-INF"):
                continue
            m = re.search(r"RESOLUTION=\d+x(\d+)", line)
            height = int(m.group(1)) if m else 0
            nxt = lines[i + 1].strip() if i + 1 < len(lines) else ""
            if not nxt or nxt.startswith("#"):
                continue
            fits = height <= max_h if height else best is None
            if fits and (best is None or height > best[0]):
                g = re.search(r'AUDIO="([^"]+)"', line)
                best = (height, nxt, g.group(1) if g else None)
        if best is None:
            return url, None

        video = absolute(best[1])
        logging.info(f"mediaplayer: variant {best[0]}p -> {video}")

        group = best[2]
        audio = None
        for line in lines:
            if not (line.startswith("#EXT-X-MEDIA:") and "TYPE=AUDIO" in line):
                continue
            g = re.search(r'GROUP-ID="([^"]+)"', line)
            u = re.search(r'URI="([^"]+)"', line)
            if not u or (group and g and g.group(1) != group):
                continue
            audio = absolute(u.group(1))
            break
        if audio:
            logging.info(f"mediaplayer: audio rendition -> {audio}")
        return video, audio

    # Whether an audio rendition's segments are MPEG-TS (needs tsdemux) rather
    # than raw ADTS (aacparse takes them straight from hlsdemux)
    @staticmethod
    def hls_audio_in_ts(playlist_url):
        try:
            text = requests.get(playlist_url, timeout=15).text
        except requests.RequestException:
            return False
        seg = next((l.strip() for l in text.splitlines()
                    if l.strip() and not l.startswith("#")), "")
        return seg.split("?", 1)[0].lower().endswith((".ts", ".m2ts"))

    # The stream urls in an .m3u (an IPTV channel list), each paired with the
    # name from its #EXTINF line
    @staticmethod
    def m3u_entries(url):
        try:
            text = requests.get(url, timeout=15).text
        except requests.RequestException as e:
            logging.error(f"mediaplayer: fetching {url}: {e}")

            return []

        entries, name = [], ""
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            if line.startswith("#EXTINF"):
                name = line.rpartition(",")[2].strip()
            elif line.startswith("#"):
                continue
            else:
                entries.append((name or line, line))
                name = ""

        return entries

    def start_mediaplayer(self, url):
        self.media_entries = []
        self.media_index = 0
        if url.endswith(".m3u"):
            self.media_entries = self.m3u_entries(url)
            if not self.media_entries:
                logging.error("mediaplayer: playlist has no stream entry")
                return None
            url = self.media_entries[0][1]

        return self._play_media(url)

    # Restart the media player on the next (+1) or previous (-1) entry of the
    # current .m3u. Does nothing when the media item is a single stream
    def media_step(self, direction):
        n = len(self.media_entries)
        if n < 2:
            return {"error": "the media item has no playlist"}

        self.media_index = (self.media_index + direction) % n
        name, url = self.media_entries[self.media_index]
        logging.info(f"mediaplayer: playlist {self.media_index + 1}/{n} {name}")
        self._play_media(url)

        return {"index": self.media_index, "count": n, "name": name}

    def _play_media(self, url):
        audio_url = None
        if url.endswith(".m3u8"):
            url, audio_url = self.hls_sources(url)

        # Held on the instance, stopped on the next start, on rotation away
        # and on item removal
        self.stop_mediaplayer()
        logging.info(f"Starting media player with stream: {url}")
        # An explicit chain, no decodebin. playbin and gst-play need a
        # streams-aware adaptive demuxer, which this GStreamer lacks, and
        # decodebin's multiqueue never fills against the broadcast's
        # wall-clock PTS, so the pipeline hangs in PAUSED. curlhttpsrc keeps
        # libsoup out of the image. connection-speed selects the HLS variant.
        # Every variant is 50fps, so videorate drops it to 25 ahead of the
        # convert, scale, compositing and scream re-encode that all run in
        # software, and drop-only leaves the rate alone when a segment is
        # short. waylandsink sync=true then paces the 25fps
        # Audio, where a decoder exists, is opus over RTP to scream on
        # udpsink, sync=true there as well so the packets leave paced to the
        # clock. Without it the 8s queue hands a whole segment to the encoder
        # at once, the RTP arrives as a burst every couple of seconds, and
        # scream's 400ms jitterbuffer keeps resetting its skew estimate and
        # stutters. It comes off the tsdemux pad when the segments carry it,
        # otherwise off a second hlsdemux on the audio rendition, which legacy
        # hlsdemux does not follow on its own
        # MEDIA_PIPELINE replaces the whole pipeline, {url} substituted
        # A corner pip takes its size from the compositor, so only a full
        # view asks the sink for fullscreen. Read the role off the playlist
        # item rather than the thread, so media_step off the state socket
        # gets it right too
        media_item = next((i for i in (self.playlist or [])
                           if i.get("player") == "mediaplayer"),
                          playlist_item())
        sink = ["waylandsink", "sync=true"]
        if not pip_corner(media_item):
            sink.insert(1, "fullscreen=true")

        audio_port = stream_audio_port()
        audio = (audio_port and gst_has("fdkaacdec") and gst_has("opusenc")
                 and gst_has("rtpopuspay"))

        override = os.environ.get("MEDIA_PIPELINE", "").strip()
        if override:
            cmd = [cmds["media_player"], "-q",
                   *shlex.split(override.replace("{url}", url))]
        else:
            # h264parse and aacparse sit right after the demux so gst-launch
            # links each branch by caps. big_queue buffers the compressed
            # side across an 8s segment-fetch gap
            big_queue = ["queue", "max-size-buffers=0", "max-size-bytes=0",
                         "max-size-time=8000000000"]
            cmd = [cmds["media_player"], "-q",
                   "curlhttpsrc", f"location={url}", "!",
                   "hlsdemux", "connection-speed=2500", "!",
                   "tsdemux", "name=demux",
                   "demux.", "!", "h264parse", "!", *big_queue, "!",
                   "openh264dec", "!",
                   "videorate", "drop-only=true", "!",
                   "video/x-raw,framerate=25/1", "!",
                   "videoconvert", "!",
                   "queue", "max-size-buffers=0", "max-size-bytes=0",
                   "max-size-time=500000000", "!", *sink]
            if audio:
                logging.info(f"mediaplayer: relaying audio to udp {audio_port}")
                if audio_url:
                    src = ["curlhttpsrc", f"location={audio_url}", "!",
                           "hlsdemux", "!"]
                    if self.hls_audio_in_ts(audio_url):
                        src += ["tsdemux", "!"]
                else:
                    src = ["demux.", "!"]
                cmd += [
                    *src, "aacparse", "!", *big_queue, "!",
                    "fdkaacdec", "!", "audioconvert", "!", "audioresample", "!",
                    "opusenc", "bitrate=96000", "!",
                    "rtpopuspay", "pt=97", "!",
                    "queue", "!",
                    "udpsink", "host=127.0.0.1", f"port={audio_port}",
                    "sync=true", "async=false"]

        self.media_player = spawn(cmd, encoding='utf8')

        return True

    def start_net_view(self, img_bg, probe_ip_address):
        net = System.net_data(probe_ip_address)
        text = kv_table([("Network Address", net["address"]),
                         ("Network Addresses", net["addresses"]),
                         ("Public IP", net["public_ip"]),
                         ("resolv.conf", net["resolvconf"])])
        wv = Wayland_view(display.res_x, display.res_y, 1, theme)
        wv.s_objects[0]["font_size"] = 20
        wv.s_objects[0]["alignment"] = "left"
        wv.show_content([text], img_bg)

    def start_sockets_view(self, img_bg, refresh_interval_s=5,
                           method="python-wayland"):
        draw_paged_view("Sockets",
                        lambda: (System.net_sockets() or "").splitlines(),
                        img_bg, refresh_interval_s, method=method)

    def start_neighbours_view(self, img_bg, refresh_interval_s=5,
                              method="python-wayland"):
        def lines():
            if not hasattr(System, "net_neighbours"):
                return ["Neighbours view needs a newer doi"]
            return (System.net_neighbours() or "").splitlines()

        draw_paged_view("Neighbours", lines, img_bg, refresh_interval_s,
                        method=method)

    def start_date_view(self, refresh_interval_s=1, title=None):
        draw([], method="vju", alignment="center", title=title,
             args=["--watch", f"{refresh_interval_s}s", "date"])

    def start_shell_view(self, img_bg, refresh_interval_s=2,
                         method="python-wayland"):
        def shell_texts():
            return [getattr(display, "command_output", "") or "No command run yet"]

        draw(shell_texts(), method=method, img_bg=img_bg,
             font_sizes=[20], alignment="left", title="Shell",
             refresh=shell_texts, refresh_interval_s=refresh_interval_s)

    traceroute_target = os.environ.get("TRACEROUTE_TARGET", "")

    def start_traceroute_view(self, img_bg, refresh_interval_s=300,
                              method="python-wayland"):
        target = Playlist.traceroute_target
        num = view_num()

        state = {"text": f"Tracing {target} ...", "running": False}
        lock = threading.Lock()

        def trace():
            text = System.traceroute(target)
            with lock:
                state["text"] = text
                state["running"] = False

            with content_refreshers_lock:
                refresher = content_refreshers.get(num)
            if refresher:
                refresher.due_now()

        def start_trace():
            if not target:
                return
            with lock:
                if state["running"]:
                    return
                state["running"] = True
            threading.Thread(target=trace, daemon=True).start()

        def traceroute_texts():
            start_trace()
            with lock:
                return [state["text"]]

        start_trace()
        with lock:
            texts = [state["text"]]

        draw(texts, method=method, img_bg=img_bg,
             font_sizes=[20], alignment="left", title="Traceroute",
             refresh=traceroute_texts, refresh_interval_s=refresh_interval_s)

    def start_top_view(self, img_bg, refresh_interval_s=5,
                       method="python-wayland"):
        def top_texts():
            return [header_gap(System.top(20) or "No process data")]

        draw(top_texts(), method=method, img_bg=img_bg,
             font_sizes=[20], alignment="left", title="Top",
             refresh=top_texts, refresh_interval_s=refresh_interval_s)

    def start_log_view(self, img_bg, refresh_interval_s=5,
                       method="python-wayland", lines=10):
        font_size = 16

        def log_texts():
            tail = ring_log.tail(lines)
            while len(tail) > 1 and text_height(
                    html.escape("\n".join(tail)), font_size) > text_space():
                tail = tail[1:]

            return ["\n".join(tail) or "No log yet"]

        draw(log_texts(), method=method, img_bg=img_bg,
             font_sizes=[font_size], alignment="left", title="Log",
             refresh=log_texts, refresh_interval_s=refresh_interval_s)

    # iss://prometheus/<endpoint>/<metric>[,<metric>...] -- fetch those metrics
    # from a /metrics endpoint and show each series as a key/value row
    def start_prometheus_view(self, url, metrics, img_bg,
                              refresh_interval_s=10, method="python-wayland"):
        client = PrometheusClient(url)

        # Underscores out of the metric name for a readable key, but not out
        # of the labels -- a path label is real data
        def label(series):
            name, brace, rest = series.partition("{")
            return name.replace("_", " ") + brace + rest

        def rows():
            data = client.values(*metrics)
            if data:
                pairs = [(label(series), fmt_number(value))
                         for series, value in sorted(data.items())]
            else:
                pairs = [(name.replace("_", " "), "no data")
                         for name in metrics]

            return [kv_table(pairs)]

        draw(rows(), method=method, img_bg=img_bg, font_sizes=[20],
             alignment="left", title="Prometheus",
             refresh=rows, refresh_interval_s=refresh_interval_s)

    def start_playlist_view(self, img_bg, refresh_interval_s=5,
                            method="python-wayland"):
        font_size = 20
        cols = max(48, int((display.res_x - 80) / (font_size * 0.8)))
        page_lines = text_rows(font_size, 2)
        state = {"page": 0}

        def entry_blocks():
            items = get_playlist_items()
            if not items:
                return []
            player_w = max((len(i["player"]) for i in items), default=6)
            uri_w = max(24, cols - 4 - 1 - player_w)
            blocks = []
            for item in items:
                uri = item["uri"]
                head, tail = uri, ""
                if len(uri) > uri_w:
                    cut = uri.rfind("/", 1, uri_w)
                    cut = cut + 1 if cut > 0 else uri_w
                    head, tail = uri[:cut], uri[cut:]
                    if len(tail) > uri_w:
                        tail = tail[:uri_w - 1] + "…"
                block = [f"{item['num']:>2}  {head:<{uri_w}} {item['player']}"]
                if tail:
                    block.append(f"{'':4}{tail}")
                blocks.append(block)
            return blocks

        def texts():
            blocks = entry_blocks()
            if not blocks:
                return ["Playlist unavailable"]
            pages, cur = [], []
            for block in blocks:
                if cur and len(cur) + len(block) > page_lines:
                    pages.append(cur)
                    cur = []
                cur.extend(block)
            if cur:
                pages.append(cur)
            page = state["page"] % len(pages)
            state["page"] = page + 1
            name = self.name or "Playlist"
            counter = "" if len(pages) == 1 else f" {page + 1}/{len(pages)}"
            return ["\n".join([f"{name}{counter} ({len(blocks)})", ""]
                              + pages[page])]

        draw(texts(), method=method, img_bg=img_bg, font_sizes=[font_size],
             alignment="left", title="Playlist",
             refresh=texts, refresh_interval_s=refresh_interval_s)

    def start_proc_view(self, img_bg, refresh_interval_s=5,
                        method="python-wayland"):
        draw_paged_view("Processes",
                        lambda: (System.list_processes() or "").splitlines(),
                        img_bg, refresh_interval_s, method=method, split=True)

    def start_sys_view(self, img_bg, probe_ip_address):
        net = System.net_data(probe_ip_address)
        sys_info = System.sys_data()

        uptime_info = System.uptime(env)
        if isinstance(uptime_info, dict):
            uptime, users, load = (uptime_info.get("uptime", ""),
                                   uptime_info.get("users", ""),
                                   uptime_info.get("load", ""))
        else:
            uptime, users, load = str(uptime_info), "", ""

        online = (net["online_status"] or "") + " " + (net["public_ip"] or "")
        # The host_uptime kernel is the container's host or its vm
        rows = [("OS", System.os_release()),
                ("Uptime", uptime),
                ("Users", users),
                ("Load", load),
                ("Display started", display.started),
                ("Host up", System.host_uptime()),
                ("System uptime", sys_info["uptime"]),
                ("Resolution", f"{display.res_x}x{display.res_y}"),
                ("Memory", (System.mem_data() or "").replace("Memory: ", "", 1)),
                ("System", sys_info["data"]),
                ("Address", net["address"]),
                ("Addresses", net["addresses"]),
                ("Online", online),
                ("Listen address",
                 "\n".join(listen_endpoints(display.address, display.port)))]

        wv = Wayland_view(display.res_x, display.res_y, 1, theme)
        wv.s_objects[0]["font_size"] = 20
        wv.s_objects[0]["alignment"] = "left"
        wv.show_content([kv_table(rows)], img_bg)

    def start_weather_view(self, weather, img_bg):
        if hasattr(weather, "report"):
            texts, icon = weather.report()
        else:
            texts, icon = ["Weather view needs a newer doi"], None

        # Sized for the headline lines even when the fetch came back short,
        # so a failed lookup does not index past the drawing objects
        font_sizes = [80, 40, 20]
        wv = Wayland_view(display.res_x, display.res_y,
                          max(len(texts), len(font_sizes)), theme)
        for i in range(len(wv.s_objects)):
            wv.s_objects[i]["font_size"] = font_sizes[min(i, len(font_sizes) - 1)]
            wv.s_objects[i]["alignment"] = "left"

        wv.show_content(texts, img_bg)
        if icon:
            wv.show_image(icon)

    def start_music_view(self, img_bg, refresh_interval_s):
        music = Music()
        num = view_num()

        spotify_ready = getattr(Music, "spotify_configured",
                                lambda: False)()

        if not spotify_ready:
            draw_item_view(music.mpd,
                           lambda wv, data:
                               ([data or "No music data available"], []),
                           [60], img_bg, refresh_interval_s,
                           overlays=0, source="mpd")
            return

        def fetch():
            item = music.spotify()
            if item is None:
                metrics.inc("iss_display_fetch_failures_total",
                            source="spotify")
                metrics.inc("iss_display_empty_views_total",
                            player="music", num=num)

            return item

        def render(wv, item):
            if not item:
                return ["Spotify", "",
                        "Spotify unavailable" if item is None
                        else "Nothing playing", "", ""], ["", ""]

            state = "" if item["is_playing"] else " (paused)"
            return [item["artists"] + state, "",
                    item["title"], "",
                    item["album"]], [wv.qr(item["url"]), item["art_file"]]

        draw_item_view(fetch, render, [30, 20, 60, 20, 30], img_bg,
                       refresh_interval_s, overlays=2, source="spotify")

    # The rotation advances this view as it leaves the screen; the interval is
    # only a fallback for the case where it is never shown
    # Encoding is cached on disk by url, so a view redrawing the same item
    # re-uses the png rather than shelling out again
    # A qr-code is a grid of modules and its pixel size follows from how much
    # data it carries, so a fixed scale makes a long url a far bigger image
    # than a short one. The view has room for a fixed box, so ask for that
    qr_target_px = 90

    @staticmethod
    def qr_code(url, target_px=None, dark=None, light=None):
        if not url:
            return ""
        try:
            try:
                return encode_qr(url,
                                 target_px=target_px or Playlist.qr_target_px,
                                 dark=dark, light=light)
            except TypeError:
                logging.warning("qr: encoder does not take colours, "
                                "encoding without")
                return encode_qr(url,
                                 target_px=target_px or Playlist.qr_target_px)
        except QREncodeError as e:
            logging.warning(f"qr: {e}")
            return ""

    def start_news_view(self, news, img_bg, refresh_interval_s):
        num = view_num()

        def fetch():
            item = news.news_item()
            if not item:
                metrics.inc("iss_display_fetch_failures_total", source="news")
                metrics.inc("iss_display_empty_views_total",
                            player="news", num=num)
            elif hasattr(news, "item_count"):
                metrics.set("iss_display_rss_items", news.item_count(),
                            feed=item.get("feed", "") or "unknown")

            return item

        def render(wv, item):
            if not item:
                return ["No news available", "", "", "", ""], [""]

            rank = item.get("rank")
            title = item.get("title", "")
            url = item.get("url", "")

            return [item.get("feed", ""),
                    "",
                    f"{title} #{rank}" if rank else title,
                    "",
                    url], [wv.qr(url)]

        draw_item_view(fetch, render, [30, 20, 60, 20, 30], img_bg,
                       refresh_interval_s, source="news")

    def start_bluesky_view(self, bsky, img_bg, refresh_interval_s):
        num = view_num()

        def fetch():
            item = bsky.post_item()
            if not item:
                metrics.inc("iss_display_fetch_failures_total", source="bluesky")
                metrics.inc("iss_display_empty_views_total",
                            player="bluesky", num=num)
            elif hasattr(bsky, "item_count"):
                metrics.set("iss_display_rss_items", bsky.item_count(),
                            feed=item.get("handle", "") or "unknown")

            return item

        def render(wv, item):
            if not item:
                return [f"@{bsky.actor}", "", "No posts", "", ""], [""]

            name = item.get("feed", "")
            handle = item.get("handle", "")
            header = f"{name}  @{handle}" if name and name != handle \
                else f"@{handle}"
            text = item.get("text") or item.get("title", "")
            link = item.get("link") or item.get("url", "")

            # The qr is a small corner overlay; the post's picture, when it
            # has one, is the last file and gets the full-height side column
            return ([header, "", text, "", link],
                    [wv.qr(link), item.get("image", "")])

        draw_item_view(fetch, render, [28, 18, 42, 18, 22], img_bg,
                       refresh_interval_s, overlays=2, source="bluesky",
                       draw_function=view.draw_text_and_image,
                       alignments={2: "justify"})

    def start_onthisday_view(self, otd, img_bg, refresh_interval_s):
        num = view_num()

        def fetch():
            item = otd.otd_item()
            if item is None:
                metrics.inc("iss_display_fetch_failures_total",
                            source="onthisday")
                metrics.inc("iss_display_empty_views_total",
                            player="onthisday", num=num)

            return item

        def render(wv, item):
            if item is None:
                return ["No 'On This Day' data available.",
                        "", "", "", ""], [""]

            url = item.get("url", "")

            # Add a margin (empty line) between the year and the text
            return [item["year"], "", item["text"], "", url], [wv.qr(url)]

        draw_item_view(fetch, render, [40, 30, 30, 20, 20], img_bg,
                       refresh_interval_s, source="onthisday")

    def start_image_view(self, file):
        wv = Wayland_view(display.res_x, display.res_y, 1, theme)
        wv.show_image(file)


# A second path alongside the rotation: the display keeps showing one view at
# a time while this composes every live view into one frame and streams it.
# The views draw into cairo surfaces we allocated ourselves, so their pixels
# are already here and no capture protocol is involved. Foreign windows, the
# browser and vju, are not in these buffers and would need one
class Mosaic:

    def __init__(self, res_x, res_y, fps=5):
        self.res_x = res_x
        self.res_y = res_y
        self.fps = fps
        self.gst = None
        self.target = None

    @staticmethod
    def grid(count):
        if count < 1:
            return 1, 1
        cols = math.ceil(math.sqrt(count))

        return cols, math.ceil(count / cols)

    def surfaces(self):
        with live_views_lock:
            windows = [w for _, w in live_views]

        return [w for w in windows if getattr(w, "s", None) is not None]

    def compose(self):
        if self.target is None:
            self.target = cairo.ImageSurface(cairo.FORMAT_ARGB32,
                                             self.res_x, self.res_y)
        target = self.target
        ctx = cairo.Context(target)
        ctx.set_source_rgb(0, 0, 0)
        ctx.paint()

        windows = self.surfaces()
        cols, rows = self.grid(len(windows))
        cell_x = self.res_x / cols
        cell_y = self.res_y / rows

        for n, window in enumerate(windows):
            width = getattr(window, "width", 0) or 1
            height = getattr(window, "height", 0) or 1
            ctx.save()
            ctx.translate((n % cols) * cell_x, (n // cols) * cell_y)
            ctx.scale(cell_x / width, cell_y / height)
            try:
                ctx.set_source_surface(window.s, 0, 0)
                ctx.paint()
            except Exception as e:
                logging.debug(f"mosaic: Skipping a surface: {e}")
            ctx.restore()

        target.flush()

        return target

    def start(self):
        sink = os.environ.get("MOSAIC_PIPELINE",
                              "x264enc tune=zerolatency ! rtph264pay config-interval=1 "
                              "! udpsink host=127.0.0.1 port=5004")
        cmd = ['gst-launch-1.0', '-q',
               'fdsrc', '!',
               'rawvideoparse', f'width={self.res_x}', f'height={self.res_y}',
               'format=bgra', f'framerate={self.fps}/1', '!',
               'videoconvert', '!'] + shlex.split(sink.replace("!", " ! "))

        logging.info(f"mosaic: {' '.join(cmd)}")
        self.gst = Popen(cmd, env=env, shell=False, stdin=subprocess.PIPE)

        return self.gst

    def run(self):
        self.start()
        interval = 1 / self.fps
        while self.gst and self.gst.poll() is None:
            started = time.time()
            try:
                self.gst.stdin.write(self.compose().get_data())
                self.gst.stdin.flush()
            except (BrokenPipeError, OSError) as e:
                logging.warning(f"mosaic: Stream ended: {e}")
                break
            time.sleep(max(interval - (time.time() - started), 0))

        logging.info("mosaic: Stopped")

class Stream():

    def __init__(self, stream_source):
        self.streams = list()

        if stream_source == "v4l2":
            logging.info("Setting up source")
            self.stream_create_v4l2_src(stream_source_device)
            logging.info("Setting up stream")
            time.sleep(3)
            self.stream_v4l2_ffmpeg()

        elif stream_source == "mosaic":
            self.mosaic = Mosaic(display.res_x, display.res_y,
                                 int(os.environ.get("MOSAIC_FPS", "5")))
            x = threading.Thread(target=self.mosaic.run, daemon=True)
            x.start()
            self.streams.append("mosaic")

        elif stream_source == "static-images":
            gst = self.stream_setup_gstreamer(stream_source,
                                              stream_source_device,
                                              local_ip,
                                              listen_port)

            self.gst_stream_images(gst, img_path)
            gst.stdin.close()
            gst.wait()

    def stream_setup_gstreamer(self, stream_source, source_device, ip, port):
        if stream_source == "static-images":
            gstreamer = subprocess.Popen([
                'gst-launch-1.0', '-v', '-e',
                'fdsrc',
                '!', 'jpegdec',
                '!', 'videoconvert',
                '!', 'videorate',
                '!', 'video/x-raw,framerate=25/2',
                '!', 'theoraenc',
                '!', 'oggmux',
                '!', 'tcpserversink', 'host=' + ip + '',
                'port=' + str(port) + ''
                ], stdin=subprocess.PIPE, env=env)

        elif stream_source == "v4l2":
            gstreamer = subprocess.Popen([
                'gst-launch-1.0', '-v', '-e',
                'v4l2src', 'device=' + source_device,
                '!', 'videorate',
                '!', 'video/x-raw,framerate=25/2',
                '!', 'queue',
                '!', 'tcpserversink', 'host=' + ip + '',
                'port=' + str(port) + ''
                ], stdin=subprocess.PIPE, env=env)

        return gstreamer

    def gst_stream_images(self, gstreamer, img_path):
        filename = '/tmp/screenshot.jpg'
        streaming = False

        while True:
            if not Path(filename).is_file():
                logging.info("Startup: No file yet to stream, waiting..")
                time.sleep(3)
                continue

            if not streaming:
                logging.info("Found first file, starting stream")
                streaming = True

            with open(filename, 'rb') as f:
                gstreamer.stdin.write(f.read())
            time.sleep(0.1)

    def stream_create_v4l2_src(self, device):
        # Check if device is an existing character device
        if not stat.S_ISCHR(os.lstat(device)[stat.ST_MODE]):
            logging.error(f"{device} does not exist, aborting..")
            sys.exit(1)

        # Create v4l2 recording of screen
        logging.info(f"Creating v4l2 stream with device: {device}")
        p = subprocess.Popen([
                    'wf-recorder',
                    '--muxer=v4l2',
                    '--file=' + device,
                    ],
                    stdin=subprocess.PIPE,
                    start_new_session=True,
                    close_fds=False,
                encoding='utf8',
                env=env)

        # Handle wf-recorder prompt for overwriting the file
        p.stdin.write('Y\n')
        p.stdin.flush()

        return True

    def stream_v4l2_ffmpeg(self):
        subprocess.Popen([
            'ffmpeg', '-f', 'v4l2', '-i', '/dev/video0',
            '-codec', 'copy',
            '-f', 'mpegts', 'udp:0.0.0.0:6000'
            ], env=env)


# Timer for the wayland eventlist, which expects a nexttime attribute
# and an alarm() method. Pulls the next set of texts and repaints,
# so a window shows fresh content instead of whatever it spawned with
class Content_refresh:

    def __init__(self, wayland_view, window, refresh, interval_s, html_escape,
                 refresh_when_hidden=False):
        item = playlist_item()
        self.source = item["player"] if item else "unknown"
        self.num = str(item["num"]) if item else "0"
        self.wayland_view = wayland_view
        self.window = window
        self.refresh = refresh
        self.interval_s = interval_s
        self.html_escape = html_escape
        self.nexttime = time.time() + interval_s
        self.lock = threading.Lock()
        self.last_texts = None
        self.refresh_when_hidden = refresh_when_hidden
        self.forced = False
        with content_refreshers_lock:
            content_refreshers[self.num] = self

    # Called from the rotation when the view leaves the screen. Only the
    # deadline is moved here; the refresh itself happens on the view's own
    # thread next time round its loop, which the waker brings on at once.
    # A zero would never fire: the loop tests mainloopnexttime for truth
    def due_now(self):
        with self.lock:
            self.nexttime = time.time() - 1
            self.forced = True
        try:
            self.wayland_view.conn.waker.wake()
        except Exception as e:
            logging.debug(f"view: Could not wake {self.source}-{self.num}: {e}")

    def on_screen(self):
        shown = getattr(globals().get('display'), "current_num", None)

        return shown is not None and str(shown) == self.num

    def alarm(self):
        # Every view thread runs the shared eventlist, so a timer that comes
        # due is alarmed by all of them. Without this only one caller advances
        # the deadline, the rest would each pull another item and repaint the
        # same surface, leaving several items composited onto one view
        with self.lock:
            now = time.time()
            if now < self.nexttime:
                return
            self.nexttime = now + self.interval_s
            forced = self.forced
            self.forced = False

        metrics.inc("iss_display_content_ticks_total",
                    player=self.source, num=self.num)

        # A carousel view shows a different item on every refresh, so refreshing
        # while it is the one on screen swaps the item out from under the
        # viewer. Its timer and the rotation run at the same period and are not
        # in step, so that happened roughly once per showing. Views that
        # re-render the same subject with new values, like top, do not set this
        # and keep refreshing whatever is on screen
        # The rotation asks for this as the view leaves the screen, while it is
        # still the current one, so an explicit advance overrides the guard
        if self.refresh_when_hidden and not forced and self.on_screen():
            return

        if self.window.surface.destroyed:
            return

        try:
            texts = self.refresh()
        except Exception as e:
            logging.warning(f"view: Failed to refresh content: {e}")
            metrics.inc("iss_display_fetch_failures_total", source=self.source)
            return

        if not texts or texts == self.last_texts:
            return

        self.last_texts = list(texts)
        metrics.inc("iss_display_content_refreshes_total",
                    player=self.source, num=self.num)
        with last_content_refresh_lock:
            last_content_refresh[(self.source, self.num)] = time.time()
        self.wayland_view.set_texts(texts, self.html_escape)
        if self.window.redraw_func:
            self.window.redraw_func(self.window)


# A Protocol is an immutable description of an xml file, so one parse serves
# every view instead of each view start re-reading the file
@functools.lru_cache(maxsize=None)
def wayland_protocol(*paths):
    for path in paths:
        if os.path.isfile(path):
            return wayland.protocol.Protocol(path)

    return None

# The playlist-position dots along a view's bottom edge. python-wayland only
# calls after_draw(w, ctx) once its own content is painted and has no
# opinion on what that draws
def draw_view_indicator(count, num, rgb, w, ctx):
    if not count or num is None:
        return

    radius, spacing, margin = 7, 28, 24
    ctx.identity_matrix()
    ctx.set_line_width(2)
    ctx.set_source_rgba(rgb[0] / 255, rgb[1] / 255, rgb[2] / 255, 0.8)
    x = (w.orig_width - (count - 1) * spacing) / 2
    y = w.orig_height - margin
    for n in range(count):
        ctx.new_path()
        ctx.arc(x + n * spacing, y, radius, 0, 2 * math.pi)
        if n == num:
            ctx.fill()
        else:
            ctx.stroke()


class Wayland_view:

    def __init__(self, res_x, res_y, num_objects, theme):
        wp_base = wayland_protocol(
            "/usr/share/wayland/wayland.xml",
            "/usr/local/share/wayland/wayland.xml")
        if wp_base is None:
            logging.error("wayland: Failed to find wayland protocol xml")
            sys.exit(1)

        wp_xdg_shell = wayland_protocol(
            "/usr/share/wayland-protocols/stable/xdg-shell/xdg-shell.xml",
            "/usr/local/share/wayland-protocols/stable/xdg-shell/xdg-shell.xml")
        if wp_xdg_shell is None:
            logging.error("wayland: Failed to find wayland protocol shell xml")
            sys.exit(1)

        try:
            self.conn = view.WaylandConnection(wp_base, wp_xdg_shell)
        except FileNotFoundError as e:
            if e.errno == 2:
                print("Unable to connect to the compositor - "
                      "is one running?")
                sys.exit(1)
            raise

        item = playlist_item()
        # A corner view renders into a small surface so the compositor floats
        # it at its natural size rather than cropping a full-res buffer
        self.pip = pip_corner(item)
        if self.pip:
            res_x = max(1, round(res_x * pip_scale()))
            res_y = max(1, round(res_y * pip_scale()))
        self.window = {}
        self.window["res_x"] = res_x
        self.window["res_y"] = res_y
        self.window["title"] = f"{item['player']}-{item['num']}" if item else "iss-view"

        if item and view_indicator():
            items = get_playlist_items()
            count = len(items)
            num = next((i for i, it in enumerate(items)
                       if it.get("num") == item["num"]), None)
            rgb = (parse_colour(theme.view_indicator_colour)
                  or parse_colour(Theme.default_font_colour))
            self.window["after_draw"] = partial(draw_view_indicator,
                                                count, num, rgb)

        # The theme sets the view background and text colours; a per-item
        # bg_colour from the web ui overrides the background
        bg_r, bg_g, bg_b = parse_colour(theme.bg_colour) or (40, 15, 40)
        stored = (item or {}).get("bg_colour")
        if stored:
            parsed = parse_colour(stored)
            if parsed:
                bg_r, bg_g, bg_b = parsed
        fg_r, fg_g, fg_b = (parse_colour(theme.font_colour)
                            or parse_colour(Theme.default_font_colour))

        s_object = {"alignment": "center",
                    "offset_x": 10,
                    "offset_y": text_top(),
                    "bg_alpha": 1,
                    "bg_colour_r": bg_r,
                    "bg_colour_g": bg_g,
                    "bg_colour_b": bg_b,
                    "font": theme.font,
                    "font_face": theme.font_face,
                    "font_size": 60,
                    "font_colour_r": fg_r,
                    "font_colour_g": fg_g,
                    "font_colour_b": fg_b,
                    "file": "",
                    "img_scale_up": True,
                    "img_scale_down": True,
                    "text": list()}

        self.repaint_pending = False
        self.live_window = None
        self.s_objects = [s_object.copy() for _ in range(num_objects)]

    def colours(self):
        obj = self.s_objects[0]

        return (hex_colour((obj["font_colour_r"], obj["font_colour_g"],
                            obj["font_colour_b"])),
                hex_colour((obj["bg_colour_r"], obj["bg_colour_g"],
                            obj["bg_colour_b"])))

    def qr(self, url):
        if not show_qr_code():
            return ""

        fg, bg = self.colours()

        return Playlist.qr_code(url, dark=fg, light=bg)

    # The surface is already pip_scale() of the output; shrink the fonts and
    # offsets to match so the layout is the full view in miniature
    def scale_pip(self):
        if not self.pip:
            return
        for obj in self.s_objects:
            obj["font_size"] = max(6, round(obj["font_size"] * pip_scale()))
            obj["offset_x"] = round(obj["offset_x"] * pip_scale())
            obj["offset_y"] = round(obj["offset_y"] * pip_scale())

    def repaint_tick(self):
        if not self.repaint_pending:
            return

        self.repaint_pending = False
        window = self.live_window
        if window is None:
            return

        try:
            if window.redraw_func:
                window.redraw_func(window)
        except Exception as e:
            logging.warning(f"view: Failed to repaint: {e}")

    def create_window(self, w):
        metrics.inc("iss_display_views_running")
        w.iss_view = self
        self.live_window = w
        self.conn.ticklist.append(self.repaint_tick)
        with live_views_lock:
            live_views.append((self.conn, w))
        reason = "clean"
        try:
            self.conn.eventloop()
        except view.connection_lost as e:
            reason = "connection_lost"
            logging.info(f"view: Compositor connection lost: {e!r}")
        except Exception:
            reason = "error"
            raise
        finally:
            with live_views_lock:
                for entry in [e for e in live_views if e[0] is self.conn]:
                    live_views.remove(entry)
            metrics.inc("iss_display_views_running", -1)
            metrics.inc("iss_display_view_exits_total", reason=reason)

        try:
            w.close()
            self.conn.display.roundtrip()
            self.conn.disconnect()
        except (*view.connection_lost, OSError) as e:
            logging.info(f"view: Connection already gone, skipping teardown: {e!r}")

        logging.info(f"Exiting wayland view: {self.conn.shutdowncode}")

    def set_texts(self, texts, html_escape=True):
        if len(texts) > len(self.s_objects):
            logging.debug(f"view: Ignoring {len(texts) - len(self.s_objects)} "
                          f"text block(s), have {len(self.s_objects)}")

        # Objects the new texts do not cover are cleared, so nothing lingers
        # from the previous content
        for n, obj in enumerate(self.s_objects):
            text = texts[n] if n < len(texts) else ""
            obj["text"] = html.escape(str(text)) if html_escape else str(text)

    def show_content(self, texts, img_bg=False, fullscreen=False, html_escape=True,
                     refresh=None, refresh_interval_s=None,
                     refresh_when_hidden=False, draw_function=None):
        logging.debug(f"view: Have {len(texts)} text block(s)")

        self.set_texts(texts, html_escape)
        self.scale_pip()

        for idx, obj in enumerate(self.s_objects):
            if obj.get("file"):
                logging.debug(f"view: s_objects[{idx}] file {obj['file']}")

        if draw_function is None:
            use_images = bool(img_bg) or bool(self.s_objects[0].get("file"))
            if use_images:
                if img_bg:
                    self.s_objects[0]["file"] = img_bg
                draw_function = view.draw_images_with_text
            else:
                draw_function = view.draw_text

        w = view.Window(self.conn,
                        self.window,
                        self.s_objects,
                        redraw=draw_function,
                        fullscreen=fullscreen,
                        class_="iss-view")

        if refresh and refresh_interval_s:
            self.conn.eventlist.append(
                Content_refresh(self, w, refresh, refresh_interval_s,
                                html_escape, refresh_when_hidden))
            logging.info(f"view: Refreshing content every {refresh_interval_s}s"
                         f"{' while hidden' if refresh_when_hidden else ''}")

        self.create_window(w)

    def show_image(self, img_file, fullscreen=False, bg_colour=None):
        self.scale_pip()
        self.s_objects[0]["text"] = list()
        self.s_objects[0]["file"] = img_file
        rgb = parse_colour(bg_colour) if bg_colour else None
        if rgb:
            self.s_objects[0]["bg_colour_r"], self.s_objects[0]["bg_colour_g"], \
                self.s_objects[0]["bg_colour_b"] = rgb
            self.s_objects[0]["bg_alpha"] = 1
        else:
            self.s_objects[0]["bg_alpha"] = 0
        self.s_objects[0]["offset_y"] = 0
        w = view.Window(self.conn,
                        self.window,
                        self.s_objects,
                        redraw=view.draw_image,
                        fullscreen=fullscreen,
                        class_="iss-view")

        self.create_window(w)


# A refilling token bucket for rate limiting. allow() reports whether a token
# is available, spend() consumes one -- kept separate so a caller can check
# several buckets and only commit when all of them permit
class TokenBucket:

    def __init__(self, rate, burst):
        self.rate = float(rate)
        self.capacity = float(burst)
        self.tokens = float(burst)
        self.updated = time.monotonic()

    def allow(self, now):
        self.tokens = min(self.capacity,
                          self.tokens + (now - self.updated) * self.rate)
        self.updated = now

        return self.tokens >= 1

    def spend(self):
        self.tokens -= 1

    def idle(self):
        return self.tokens >= self.capacity


class Display:

    # The app_ids of the windows we spawn ourselves, the only ones we cycle
    browser_app_ids = ("firefox", "org.servo.Servo", "servoshell", "servo")
    window_app_ids = ("iss-view", "gst-launch-1.0") + browser_app_ids
    # Every window we cycle gets a workspace to itself, named with this prefix
    workspace_prefix = "iss-"
    # Where the state server binds and where its clients look for it
    default_state_udp_host = "127.0.0.1"
    default_state_udp_port = 7042
    # Token-bucket limits on the state socket: enough headroom for button
    # presses and a metrics poll, tight enough that a stuck sender or a loop
    # cannot thrash the display. Per source address and across all of them
    state_rate_per_client = 5
    state_burst_per_client = 10
    state_rate_global = 20
    state_burst_global = 40

    def __init__(self, address, port, res_x=1366, res_y=768):
        self.address = address
        self.port = port
        self.res_x = res_x
        self.res_y = res_y
        self.started = time.strftime("%Y-%m-%d %H:%M:%S %Z", time.localtime())
        self.start_time = time.time()
        self.window_blacklist = list()
        self.rotation_index = 0
        self.rotation_step = 0
        # Pinned holds the rotation on the current view; the view keeps
        # refreshing on its own clock, only the automatic advance stops
        self.pinned = False
        self.current_num = None
        self.pip_pinned_num = None
        self.pip_pinned_corner = False
        self.pip_pinned_id = None
        self.pip_paused = False
        self.media_step_gen = 0
        self.holding = dict()
        self.command = ""
        self.command_output = ""
        self.hold_outputs = os.environ.get("HOLD_OUTPUTS", "0") == "1"
        self.display_output = os.environ.get("DISPLAY_OUTPUT", "HEADLESS-1")
        self.refresh_hz = int(os.environ.get("DISPLAY_REFRESH_HZ") or 60)
        self.skip_event = threading.Event()
        self.play_items = dict()
        self.playlist = None
        self.screenshot_path = "/tmp"
        self.screenshot_file = "screenshot.jpg"
        self.socket_path = self.get_socket_path()

        resolution = self.output_resolution()
        if resolution:
            self.res_x, self.res_y = resolution
        self.set_output_mode()
        # Where the UDP state server binds, env over the class default
        self.state_udp_host = os.environ.get('DISPLAY_STATE_UDP_HOST',
                                             self.default_state_udp_host)
        self.state_udp_port = int(os.environ.get('DISPLAY_STATE_UDP_PORT',
                                                 self.default_state_udp_port))
        self._state_server_thread = None
        self._state_server_stop = threading.Event()
        self._state_server_sock = None

        logging.info(f"Python executable: {sys.executable}")
        logging.info(f"Resolution: {self.res_x} x {self.res_y}")

        # We do not want to handle existing windows,
        # so we put their IDs on a blacklist
        # This is usually only useful in development/testing scenarios
        # e.g. when run locally with an existing sway session
        existing_windows = []
        try:
            existing_windows = self.get_windows()
        except Exception as e:
            logging.warning(f"display: Failed to get existing windows; sway may be unavailable: {e}")
            existing_windows = []
        for win in existing_windows:
            try:
                self.window_blacklist.append(win["id"])
            except Exception:
                pass

        logging.info(f"Blacklisted {len(self.window_blacklist)} windows")

        self.set_window_rules()

    def start_state_server(self, host=None, port=None):
        if host:
            self.state_udp_host = host
        if port:
            self.state_udp_port = port
        if self._state_server_thread and self._state_server_thread.is_alive():
            logging.info("Display UDP state server already running")
            return True
        self._state_server_stop.clear()
        self._state_server_thread = threading.Thread(target=self._udp_state_server_loop, daemon=True)
        self._state_server_thread.start()
        logging.info(f"Started Display UDP state server on {self.state_udp_host}:{self.state_udp_port}")
        return True

    def stop_state_server(self):
        # The loop wakes every 0.5s on its own timeout and checks this flag
        self._state_server_stop.set()
        if self._state_server_thread:
            self._state_server_thread.join(timeout=1.0)
        if self._state_server_sock:
            try:
                self._state_server_sock.close()
            except Exception:
                pass
        logging.info("Stopped Display UDP state server")
        return True

    def state_payload(self):
        return {'address': self.address,
                'port': self.port,
                'res_x': self.res_x,
                'res_y': self.res_y,
                'pinned': self.pinned,
                'pip_paused': self.pip_paused,
                'current_num': self.current_num,
                'playlist_name': self.playlist.name if self.playlist else "",
                'default_play_time_s': (self.playlist.default_play_time_s
                                        if self.playlist else 0)}

    def state_reply(self, msg):
        '''
        The reply to one state command, or None for a message that is not
        one. A command is a word, optionally followed by its arguments.
        '''
        command, _, arg = msg.partition(' ')
        arg = arg.strip()
        fields = arg.split()

        queries = {
            'GET_STATE': lambda: self.state_payload(),
            'GET_METRICS': lambda: self.metrics_snapshot(),
            'GET_PLAYLIST': lambda: {'playlist': self.playlist_items()},
            'NEXT': lambda: self.step_rotation(1),
            'PREVIOUS': lambda: self.step_rotation(-1),
            'pin-view': lambda: self.toggle_view_pin(),
            'media-play-next': lambda: self.media_step(1),
            'media-play-previous': lambda: self.media_step(-1),
            'stop-pip': lambda: self.set_pip(False),
            'start-pip': lambda: self.set_pip(True),
        }
        commands = {
            'DEFAULT_TIME': self.set_default_play_time,
            'SHELL': self.run_command,
            'NAME': self.set_playlist_name,
            'ADD': self.add_playlist_item,
            'RESOLUTION': self.set_resolution,
            'TOGGLE': self.toggle_playlist_item,
            'REMOVE': self.remove_playlist_item,
            'ITEM_TIME': lambda arg: (
                self.set_item_play_time(fields[0], fields[1])
                if len(fields) > 1
                else {"error": "ITEM_TIME needs a number and seconds"}),
            'BG': lambda arg: self.set_view_background(
                fields[0], fields[1] if len(fields) > 1 else None),
        }

        if command in queries and not arg:
            metrics.inc("iss_display_state_commands_total", command=command)
            return queries[command]()

        # NAME may come bare, which clears the name
        if command in commands and (arg or command == 'NAME'):
            metrics.inc("iss_display_state_commands_total", command=command)
            return commands[command](arg)

        return None

    def _udp_state_server_loop(self):
        sock = None
        bind_host = self.state_udp_host
        bind_port = self.state_udp_port
        try:
            logging.info(f"Display UDP server binding to {bind_host}:{bind_port}")
            try:
                ipaddress.ip_address(bind_host)
            except ValueError:
                logging.warning(f"Display UDP server: invalid bind host '{bind_host}', falling back to 0.0.0.0")
                bind_host = '0.0.0.0'

            family = udp_family(bind_host)
            sock = socket.socket(family, socket.SOCK_DGRAM)
            if family == socket.AF_INET6:
                try:
                    sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
                except Exception:
                    pass
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            except Exception:
                pass
            sock.settimeout(0.5)
            sock.bind((bind_host, bind_port))
            self._state_server_sock = sock

            # Buckets live in this thread, its only reader, so no lock. Client
            # buckets are dropped once idle so the map cannot grow unbounded
            # when the socket is not loopback-only
            global_bucket = TokenBucket(self.state_rate_global,
                                        self.state_burst_global)
            client_buckets = {}
            # Every source address the limiter has ever refused, kept for the
            # unique-client gauge (not pruned like the buckets)
            limited_clients = set()
            metrics.set("iss_display_state_rate_limited_clients", 0)
            last_prune = time.monotonic()
            dropped_logged = 0.0

            while not self._state_server_stop.is_set():
                try:
                    data, addr = self._state_server_sock.recvfrom(65535)
                except socket.timeout:
                    continue
                except Exception as e:
                    logging.error(f"Display UDP server recv error: {e}")
                    continue

                if not data:
                    continue
                msg = data.decode('utf-8', errors='ignore').strip()
                if msg == 'STOP':
                    break

                now = time.monotonic()
                if now - last_prune > 60:
                    client_buckets = {ip: b for ip, b in client_buckets.items()
                                      if not b.idle()}
                    last_prune = now

                ip = addr[0]
                client = client_buckets.get(ip)
                if client is None:
                    client = TokenBucket(self.state_rate_per_client,
                                         self.state_burst_per_client)
                    client_buckets[ip] = client

                g_ok = global_bucket.allow(now)
                c_ok = g_ok and client.allow(now)
                if not (g_ok and c_ok):
                    scope = "global" if not g_ok else "client"
                    metrics.inc("iss_display_state_rate_limited_total",
                                scope=scope)
                    if ip not in limited_clients:
                        limited_clients.add(ip)
                        metrics.set("iss_display_state_rate_limited_clients",
                                    len(limited_clients))
                    if now - dropped_logged > 10:
                        logging.warning(f"state socket: rate limit hit "
                                        f"({scope}), dropping from {ip}")
                        dropped_logged = now
                    continue
                global_bucket.spend()
                client.spend()

                reply = self.state_reply(msg)
                if reply is not None:
                    self.send_json(addr, reply)
        except Exception as e:
            logging.error(f"Display UDP server bind failed on {self.state_udp_host}:{self.state_udp_port}: {e}")
        finally:
            if sock:
                try:
                    sock.close()
                except Exception:
                    pass
            self._state_server_sock = None
            logging.info("Display UDP server socket closed")

    @staticmethod
    def send_command(message, host=None, port=None, timeout=0.5):
        host = host or os.environ.get('DISPLAY_STATE_UDP_HOST',
                                      Display.default_state_udp_host)
        port = int(port or os.environ.get('DISPLAY_STATE_UDP_PORT',
                                          Display.default_state_udp_port))
        try:
            with socket.socket(udp_family(host), socket.SOCK_DGRAM) as s:
                s.settimeout(timeout)
                s.sendto(message.encode('utf-8'), (host, port))
                data, _ = s.recvfrom(65535)
                return json.loads(data.decode('utf-8'))
        except socket.timeout:
            logging.warning(f"Display UDP client: timeout on {message}")
            return None
        except Exception as e:
            logging.error(f"Display UDP client error: {e}")
            return None

    @staticmethod
    def query_state(host=None, port=None, timeout=0.5):
        return Display.send_command('GET_STATE', host, port, timeout)

    @staticmethod
    def query_playlist(host=None, port=None, timeout=0.5):
        reply = Display.send_command('GET_PLAYLIST', host, port, timeout)

        return (reply or {}).get('playlist')

    @staticmethod
    def toggle_item(num, host=None, port=None, timeout=1.0):
        return Display.send_command(f'TOGGLE {num}', host, port, timeout)

    @staticmethod
    def query_metrics(host=None, port=None, timeout=1.0):
        return Display.send_command('GET_METRICS', host, port, timeout)

    @staticmethod
    def step(direction, host=None, port=None, timeout=2.0):
        return Display.send_command('NEXT' if direction > 0 else 'PREVIOUS',
                                    host, port, timeout)

    @staticmethod
    def pin_view(host=None, port=None, timeout=2.0):
        return Display.send_command('pin-view', host, port, timeout)

    @staticmethod
    def media_next(direction=1, host=None, port=None, timeout=20.0):
        cmd = 'media-play-next' if direction > 0 else 'media-play-previous'

        return Display.send_command(cmd, host, port, timeout)

    @staticmethod
    def set_default_time(play_time_s, host=None, port=None, timeout=2.0):
        return Display.send_command(f'DEFAULT_TIME {play_time_s}', host, port, timeout)

    @staticmethod
    def run_shell(command, host=None, port=None, timeout=35.0):
        return Display.send_command(f'SHELL {command}', host, port, timeout)

    @staticmethod
    def remove_item(num, host=None, port=None, timeout=0.5):
        return Display.send_command(f'REMOVE {num}', host, port, timeout)

    @staticmethod
    def set_item_time(num, seconds, host=None, port=None, timeout=0.5):
        return Display.send_command(f'ITEM_TIME {num} {seconds}',
                                    host, port, timeout)

    @staticmethod
    def set_background(colour, num=None, host=None, port=None, timeout=0.5):
        message = f'BG {colour}' + (f' {num}' if num is not None else '')

        return Display.send_command(message, host, port, timeout)

    @staticmethod
    def set_name(name, host=None, port=None, timeout=2.0):
        return Display.send_command(f'NAME {name}'.strip(), host, port, timeout)

    @staticmethod
    def add_item(uri, host=None, port=None, timeout=15.0):
        return Display.send_command(f'ADD {uri}', host, port, timeout)

    @staticmethod
    def set_output_resolution(mode, host=None, port=None, timeout=15.0):
        return Display.send_command(f'RESOLUTION {mode}', host, port, timeout)

    def restart_views(self, timeout_s=5):
        with live_views_lock:
            stopping = [conn for conn, _ in live_views]
        for conn in stopping:
            try:
                conn.stop(0)
            except Exception as e:
                logging.warning(f"display: Failed to stop a view: {e}")
        logging.info(f"display: Stopping {len(stopping)} view(s)")

        deadline = time.time() + timeout_s
        while time.time() < deadline:
            with live_views_lock:
                if not live_views:
                    break
            time.sleep(0.1)

        with live_views_lock:
            left = len(live_views)
        if left:
            logging.warning(f"display: {left} view(s) did not stop in time")

        if not self.playlist:
            return 0

        self.playlist.stop_browser()
        self.playlist.stop_mediaplayer()
        # the pinned view gets a new con_id on respawn, so place it again
        self.pip_pinned_id = None

        started = 0
        for item in self.playlist.playlist:
            if not item.get("enabled", True):
                continue
            item["started"] = False
            if self.playlist.start_item(item):
                started += 1

        logging.info(f"display: Restarted {started} view(s)")

        return started

    def set_output_mode(self):
        name = (self.outputs() or [{}])[0].get("name") or self.display_output
        self.swaymsg('output', name, 'mode',
                     f"{self.res_x}x{self.res_y}@{self.refresh_hz}Hz",
                     log_prefix="set_output_mode")

    def set_resolution(self, mode):
        match = re.fullmatch(r"(\d{3,5})x(\d{3,5})", str(mode or "").strip())
        if not match:
            return {"error": f"bad resolution {mode!r}"}

        outputs = self.outputs()
        if not outputs:
            return {"error": "no outputs"}

        name = outputs[0].get('name')
        was = (self.res_x, self.res_y)
        self.swaymsg('output', name, 'mode',
                     f"{match.group(1)}x{match.group(2)}@{self.refresh_hz}Hz",
                     log_prefix="set_resolution")

        resolution = self.output_resolution()
        if resolution:
            self.res_x, self.res_y = resolution
        logging.info(f"display: Resolution of {name} now {self.res_x}x{self.res_y}")

        # Views size themselves once and cannot be resized, so they are torn
        # down and spawned again to pick the new resolution up
        restarted = None
        if (self.res_x, self.res_y) != was:
            restarted = self.restart_views()

        return {"output": name, "res_x": self.res_x, "res_y": self.res_y,
                "restarted": restarted}

    def output_resolution(self):
        try:
            for output in self.outputs():
                mode = output.get('current_mode') or {}
                if mode.get('width') and mode.get('height'):
                    return mode['width'], mode['height']
        except Exception as e:
            logging.warning(f"display: Failed to read output resolution: {e}")

        return None

    def get_socket_path(self):
        cmd = ['sway', '--get-socketpath']
        p = subprocess.Popen(cmd,
                     shell=False,
                     stdout=subprocess.PIPE,
                     stderr=subprocess.STDOUT,
                     encoding="utf8",
                     env=env)

        out, _ = p.communicate()

        return out.rstrip()

    @staticmethod
    def swaymsg_send_message(cmd, env=None, log_prefix=None):
        """
        Run a swaymsg command via subprocess, log stdout/stderr, and return stdout.
        Args:
            cmd (list): Command list for subprocess.
            env (dict): Environment variables.
            log_prefix (str): Prefix for log messages.
        Returns:
            str: stdout from the command.
        """
        p = subprocess.Popen(cmd,
                            shell=False,
                            stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE,
                            encoding='utf8',
                            env=env)

        out, err = p.communicate()
        prefix = log_prefix or "swaymsg"
        if out:
            logging.debug(f"{prefix}: stdout: {out}")
        if err:
            logging.warning(f"{prefix}: stderr: {err}")
            metrics.inc("iss_display_swaymsg_errors_total")

        return out

    def swaymsg(self, *args, log_prefix="swaymsg"):
        return self.swaymsg_send_message(
            ['swaymsg', '-s', self.socket_path, *args],
            env=env, log_prefix=log_prefix)

    def sway_tree(self, log_prefix="sway_tree"):
        return json.loads(self.swaymsg('-t', 'get_tree', log_prefix=log_prefix))

    # Every node in the sway tree, paired with the output it sits under
    @staticmethod
    def walk_tree(node, output=None):
        if not isinstance(node, dict):
            return
        if node.get('type') == 'output':
            output = node.get('name')
        yield node, output
        for key in ('nodes', 'floating_nodes'):
            for child in node.get(key) or []:
                yield from Display.walk_tree(child, output)

    def get_windows_whitelist(self):
        windows = self.get_windows(self.window_blacklist)
        # The blacklist is a snapshot taken at startup, which races anything
        # the compositor is still mapping, so match on what we spawn instead.
        # Keeps the background and any foreign window out of the rotation
        windows = [w for w in windows
                   if (w.get("app_id") in self.window_app_ids
                       or self.window_item(w) is not None)
                   and (self.window_item(w) or {}).get("enabled", True)]
        logging.debug(f"display: {len(windows)} windows in whitelist")

        return windows

    # A window is a leaf container: xwayland windows have no app_id, so match
    # on the node being a con with no children of its own instead
    def get_windows(self, blacklist=None):
        windows = [node
                   for node, _ in self.walk_tree(self.sway_tree("get_windows"))
                   if node.get('type') in ('con', 'floating_con')
                   and not node.get('nodes') and 'id' in node]

        if blacklist:
            return [w for w in windows if w.get("id") not in blacklist]

        return windows

    def active_window(self):
        """Return the active window id or None if unavailable."""
        try:
            for node, _ in self.walk_tree(self.sway_tree("active_window")):
                if node.get('focused') is True and 'id' in node:
                    return node['id']
        except Exception as e:
            logging.debug(f"active_window: failed to determine active window: {e}")

        return None

    # Float every window we spawn. Our views set a fixed size and ignore the
    # size the compositor asks them to take, so tiling them, which resizes
    # them to fill their workspace, leaves the buffer and the window disagreeing
    # New windows map onto the focused workspace, which after start-up is the
    # one a view is showing on -- a slow view (art waits on a museum fetch)
    # then floats over the live picture until the rotation reaches it. Every
    # window we cycle maps here first instead, off screen, and the rotation
    # moves it out when its turn comes
    holding_workspace = "iss-holding"

    def set_window_rules(self):
        try:
            self.swaymsg('for_window', '[app_id=".*"]', 'floating', 'enable',
                         log_prefix="set_window_rules")
            # No titlebar: the views are full-bleed content, and a border eats
            # 25px off the top of a tiled one
            self.swaymsg('for_window', '[app_id=".*"]', 'border', 'none',
                         log_prefix="set_window_rules")
            for app_id in self.window_app_ids:
                self.swaymsg('for_window', f'[app_id="{app_id}"]', 'move',
                             'to', 'workspace', self.holding_workspace,
                             log_prefix="set_window_rules")
            logging.info("display: Set new windows to float, off screen")

            # servo is the chromeless build so it has no toolbar to hide, but
            # fullscreening it still makes the page fill the output rather than
            # sit at the requested window size. firefox fullscreens itself, so
            # this only has to cover the browsers that do not
            for app_id in self.browser_app_ids:
                self.swaymsg('for_window', f'[app_id="{app_id}"]',
                             'fullscreen', 'enable', log_prefix="set_window_rules")
            logging.info(f"display: Fullscreening browser windows "
                         f"{', '.join(self.browser_app_ids)}")
        except Exception as e:
            logging.warning(f"display: Failed to set new windows to float: {e}")

    def window_workspace(self, win_id):
        return f"{self.workspace_prefix}{win_id}"

    # One workspace holds the whole pip layout: one window tiled so it fills
    # the output, one floated over a corner. Which of the two is pinned and
    # which cycles depends on where the double bar was
    pip_workspace = "iss-pip"

    # Tiled, so sway sizes it to fill the workspace
    def place_pip_full(self, window, previous_id):
        wid = window["id"]
        self.swaymsg(f"[con_id={wid}]", "floating", "disable", log_prefix="pip")
        self.swaymsg(f"[con_id={wid}]", "border", "none", log_prefix="pip")
        self.swaymsg(f"[con_id={wid}]", "move", "workspace", self.pip_workspace,
                     log_prefix="pip")
        if previous_id and previous_id != wid:
            self.swaymsg(f"[con_id={previous_id}]", "move", "workspace",
                         self.window_workspace(previous_id), log_prefix="pip")
        self.swaymsg("workspace", self.pip_workspace, log_prefix="pip")

    # Floated and sized to pip_scale() of the output, in the pip_position()
    # corner. Sticky so it stays on screen over whichever workspace the cycling
    # set is showing, rather than depending on a move to pip_workspace landing
    # while the compositor is still mapping the other windows. Fullscreen is
    # dropped first: a fullscreen surface (waylandsink, a fullscreened browser)
    # ignores resize and move and stays stuck to its workspace
    def place_pip_corner(self, window, previous_id):
        wid = window["id"]
        pw = max(1, round(self.res_x * pip_scale()))
        ph = max(1, round(self.res_y * pip_scale()))
        # One value, so the gap to the near edges is equal on both axes
        margin = max(12, round(min(self.res_x, self.res_y) * 0.04)) + 8
        pos = pip_position()
        x = margin if pos.endswith("left") else self.res_x - pw - margin
        y = margin if pos.startswith("upper") else self.res_y - ph - margin

        self.swaymsg(f"[con_id={wid}]", "fullscreen", "disable", log_prefix="pip")
        self.swaymsg(f"[con_id={wid}]", "floating", "enable", log_prefix="pip")
        self.swaymsg(f"[con_id={wid}]", "sticky", "enable", log_prefix="pip")
        self.swaymsg(f"[con_id={wid}]", "border", "none", log_prefix="pip")
        self.swaymsg(f"[con_id={wid}]", "resize", "set", str(pw), str(ph),
                     log_prefix="pip")
        self.swaymsg(f"[con_id={wid}]", "move", "position", str(x), str(y),
                     log_prefix="pip")
        if previous_id and previous_id != wid:
            self.swaymsg(f"[con_id={previous_id}]", "sticky", "disable",
                         log_prefix="pip")
            self.swaymsg(f"[con_id={previous_id}]", "move", "workspace",
                         self.window_workspace(previous_id), log_prefix="pip")

    def outputs(self):
        try:
            return json.loads(self.swaymsg('-t', 'get_outputs',
                                           log_prefix="outputs"))
        except Exception as e:
            logging.warning(f"display: Failed to read outputs: {e}")

            return []

    def window_outputs(self):
        """Map con_id to the output the window currently sits on."""
        try:
            tree = self.sway_tree("window_outputs")
        except Exception as e:
            logging.warning(f"display: Failed to read the tree: {e}")

            return {}

        return {node['id']: output
                for node, output in self.walk_tree(tree)
                if node.get('app_id') is not None and 'id' in node}

    # A window only renders while its output is being composited, so parking
    # each one on an output of its own keeps them all drawing. The rotation
    # then moves whichever is due onto the output the display shows
    def holding_output(self, win_id):
        name = self.holding.get(win_id)
        if name:
            return name

        before = {o.get('name') for o in self.outputs()}
        self.swaymsg('create_output', log_prefix="holding_output")
        new = [o.get('name') for o in self.outputs() if o.get('name') not in before]
        if not new:
            logging.warning(f"display: Could not create an output for {win_id}")

            return None

        name = new[0]
        self.swaymsg('output', name, 'mode',
                     f"{self.res_x}x{self.res_y}@{self.refresh_hz}Hz",
                     log_prefix="holding_output")
        self.holding[win_id] = name
        logging.info(f"display: Window {win_id} holds output {name}")

        return name

    # Only windows drawn by someone else need an output of their own. Our own
    # views paint whether or not they are composited, and every extra output
    # is composited continuously, which on a software renderer is not cheap
    def needs_holding_output(self, window):
        return window.get("app_id") not in ("iss-view",)

    def park_windows(self, windows, shown_id):
        placed = self.window_outputs()
        for window in windows:
            win_id = window['id']
            if win_id == shown_id or not self.needs_holding_output(window):
                continue
            name = self.holding_output(win_id)
            if not name or placed.get(win_id) == name:
                continue
            self.swaymsg(f"[con_id={win_id}]", 'move', 'container', 'to',
                         'output', name, log_prefix="park_windows")

    # Views title their window after the playlist item they were spawned for,
    # which is how a window found in the tree is matched back to its item.
    # The browser titles its own window, so it is matched on app_id instead
    def set_playlist(self, playlist):
        self.playlist = playlist
        pinned = next((i for i in playlist.playlist
                       if str(i.get("pip", "")).startswith("pinned")), None)
        if pinned:
            self.pip_pinned_num = pinned["num"]
            self.pip_pinned_corner = pinned["pip"] == "pinned-corner"
            where = "corner" if self.pip_pinned_corner else "full"
            logging.info(f"display: PIP mode, item {self.pip_pinned_num} "
                         f"pinned {where}")
        for item in playlist.playlist:
            if item["player"] == "browser":
                # The engine decides the app_id, so every one it could be maps
                # back to this item
                for key in self.browser_app_ids:
                    self.play_items[key] = item
                continue

            if item["player"] == "mediaplayer":
                self.play_items[cmds["media_player"]] = item
                continue

            self.play_items[f"{item['player']}-{item['num']}"] = item

        logging.info(f"display: Tracking {len(self.play_items)} playlist items")

    max_datagram = 60000

    @staticmethod
    def trim_payload(payload):
        if not isinstance(payload, dict):
            return payload

        trimmed = dict(payload)
        for kind in ("counters", "gauges"):
            series = trimmed.get(kind)
            if series is None:
                continue
            trimmed[kind] = [entry for entry in series
                             if not any(label[0] == "num" for label in entry[1])]
        if isinstance(trimmed.get("playlist"), list):
            trimmed["playlist"] = trimmed["playlist"][:50]
        trimmed["partial"] = True

        return trimmed

    def send_json(self, addr, payload):
        data = json.dumps(payload).encode('utf-8')
        if len(data) > self.max_datagram:
            logging.warning(f"display: State payload is {len(data)} bytes, trimming")
            data = json.dumps(self.trim_payload(payload)).encode('utf-8')
        if len(data) > self.max_datagram:
            logging.error(f"display: State payload still {len(data)} bytes, dropping")
            data = json.dumps({"error": "payload too large",
                               "bytes": len(data)}).encode('utf-8')

        try:
            self._state_server_sock.sendto(data, addr)
        except Exception as e:
            logging.error(f"Display UDP server send error: {e}")

    def metrics_snapshot(self):
        metrics.set("iss_display_start_time_seconds", self.start_time)
        host_up = System.host_uptime_seconds()
        if host_up is not None:
            metrics.set("iss_display_host_uptime_seconds", host_up)
        metrics.set("iss_display_build_info", 1,
                    python=platform.python_version(),
                    deps_ref=self.deps_ref())

        try:
            all_windows = self.get_windows()
            rotating = self.get_windows_whitelist()
        except Exception:
            all_windows, rotating = [], []

        metrics.set("iss_display_windows", len(all_windows), state="total")
        metrics.set("iss_display_windows", len(rotating), state="rotating")

        sockets = System.net_socket_list() or []

        # A socket's proto and state are a bounded label set, so the count by
        # type stays a handful of series. The closing and wait states go to
        # their own gauge: a TIME_WAIT holds no fd and clears itself in ~60s,
        # so counting it as an open socket only makes loopback scrape churn
        # look like a leak
        transient_states = {"TIME_WAIT", "CLOSE_WAIT", "FIN_WAIT1",
                            "FIN_WAIT2", "CLOSING", "LAST_ACK", "CLOSE"}
        metrics.clear_gauge("iss_display_sockets")
        metrics.clear_gauge("iss_display_sockets_transient")
        socket_counts = collections.Counter(
            (s.get("proto", "?"), s.get("state", "?")) for s in sockets)
        for (proto, state), count in socket_counts.items():
            name = ("iss_display_sockets_transient" if state in transient_states
                    else "iss_display_sockets")
            metrics.set(name, count, proto=proto, state=state)
        metrics.set("iss_display_sockets_total", len(sockets))

        scream_clients, snapshots_total = scream_metrics()
        metrics.clear_gauge("iss_display_stream_subscribers")
        for stream, count in stream_subscribers(sockets, scream_clients).items():
            metrics.set("iss_display_stream_subscribers", count, stream=stream)
        if snapshots_total is not None:
            metrics.set("iss_display_stream_snapshots_total", snapshots_total)

        running = self.playlist.browser_running() if self.playlist else False
        metrics.set("iss_display_browser_up", 1 if running else 0)
        alive = globals().get('compositor')
        metrics.set("iss_display_compositor_up",
                    1 if alive and alive.running() else 0)
        server = globals().get('stream_server')
        metrics.set("iss_display_stream_up",
                    1 if server and server.running() else 0)
        vnc = globals().get('vnc_server')
        metrics.set("iss_display_vnc_up",
                    1 if vnc and vnc.running() else 0)

        metrics.clear_gauge("iss_display_surface_commits_total")
        metrics.clear_gauge("iss_display_frames_presented_total")
        with live_views_lock:
            windows = [w for _, w in live_views]
        for window in windows:
            title = getattr(window, "title", "") or "unknown"
            metrics.set("iss_display_surface_commits_total",
                        getattr(window, "commits", 0), view=title)
            metrics.set("iss_display_frames_presented_total",
                        getattr(window, "frames", 0), view=title)

        metrics.clear_gauge("iss_display_content_age_seconds")
        now = time.time()
        with last_content_refresh_lock:
            ages = {k: now - v for k, v in last_content_refresh.items()}
        for (source, num), age in ages.items():
            metrics.set("iss_display_content_age_seconds", age,
                        player=source, num=num)

        metrics.clear_gauge("iss_display_item_enabled")
        metrics.clear_gauge("iss_display_item_play_time_seconds")
        for item in (self.playlist.playlist if self.playlist else []):
            labels = {"player": item["player"], "num": item["num"]}
            metrics.set("iss_display_item_enabled",
                        1 if item.get("enabled", True) else 0, **labels)
            metrics.set("iss_display_item_play_time_seconds",
                        item.get("play_time_s", 0), **labels)

        self.sample_processes()

        return metrics.snapshot()

    @staticmethod
    def sample_processes():
        for name in ("iss_display_process_cpu_seconds_total",
                     "iss_display_thread_cpu_seconds_total",
                     "iss_display_process_resident_memory_bytes",
                     "iss_display_process_threads",
                     "iss_display_processes",
                     "iss_display_container_cpu_seconds_total",
                     "iss_display_container_memory_bytes"):
            metrics.clear_gauge(name)

        cpu, thread_cpu, rss, threads, counts = process_stats.sample()
        for (process, thread), seconds in thread_cpu.items():
            metrics.set("iss_display_thread_cpu_seconds_total", seconds,
                        process=process, thread=thread)

        for metric, values in (("iss_display_process_cpu_seconds_total", cpu),
                               ("iss_display_process_resident_memory_bytes", rss),
                               ("iss_display_process_threads", threads),
                               ("iss_display_processes", counts)):
            for name, value in values.items():
                metrics.set(metric, value, process=name)

        for state, source in (("current", "memory.current"),
                              ("peak", "memory.peak")):
            value = ProcessStats.cgroup_value(source)
            if value is not None:
                metrics.set("iss_display_container_memory_bytes", value,
                            state=state)

        for metric, source in (("iss_display_container_memory_limit_bytes", "memory.max"),
                               ("iss_display_container_tasks", "pids.current")):
            value = ProcessStats.cgroup_value(source)
            if value is not None:
                metrics.set(metric, value)

        for mode, seconds in ProcessStats.cgroup_cpu().items():
            metrics.set("iss_display_container_cpu_seconds_total", seconds,
                        mode=mode)

    @staticmethod
    def deps_ref():
        try:
            with open("/venv/deps-ref") as f:
                return f.read().strip() or "unpinned"
        except OSError:
            return "unknown"

    def playlist_items(self):
        if not self.playlist:
            return []

        return [public_item(item) for item in self.playlist.playlist]

    def set_default_play_time(self, play_time_s):
        if not self.playlist:
            return {"error": "no playlist"}

        try:
            play_time_s = int(play_time_s)
        except (TypeError, ValueError):
            return {"error": f"bad play time {play_time_s!r}"}

        if play_time_s < 1 or play_time_s > 86400:
            return {"error": f"play time {play_time_s} out of range"}

        self.playlist.set_default_play_time(play_time_s)
        logging.info(f"display: Default play time is now {play_time_s}s")

        return {"default_play_time_s": play_time_s}

    # The output goes three ways: back to the caller, to our stdout so it
    # lands in the container log, and into the shell view
    def run_command(self, command, timeout_s=30):
        if not debug_mode():
            logging.warning("shell: Refused, the display is not in debug mode")

            return {"error": "shell is only available in debug mode"}

        command = str(command or "").strip()
        if not command:
            return {"error": "no command given"}

        logging.info(f"shell: $ {command}")
        try:
            done = subprocess.run(["/bin/sh", "-c", command], env=env,
                                  capture_output=True, text=True,
                                  timeout=timeout_s)
            output = ((done.stdout or "") + (done.stderr or "")).strip()
            returncode = done.returncode
        except subprocess.TimeoutExpired:
            output, returncode = f"timed out after {timeout_s}s", 124
        except Exception as e:
            output, returncode = f"{type(e).__name__}: {e}", 1

        for line in output.splitlines():
            logging.info(f"shell: {line}")
        logging.info(f"shell: exit {returncode}")

        self.command = command
        self.command_output = f"$ {command}\n\n{output}" if output else f"$ {command}"

        return {"command": command, "returncode": returncode,
                "output": output[:8000]}

    def set_playlist_name(self, name):
        if not self.playlist:
            return {"error": "no playlist"}

        self.playlist.name = str(name or "").strip()
        logging.info(f"display: Playlist name is now {self.playlist.name!r}")

        return {"playlist_name": self.playlist.name}

    def add_playlist_item(self, uri):
        if not self.playlist:
            return {"error": "no playlist"}

        uri = str(uri or "").strip()
        if not uri:
            return {"error": "no uri given"}

        try:
            item = self.playlist.add(uri)
        except Exception as e:
            return {"error": f"could not add {uri}: {e}"}

        if not item:
            return {"error": f"no player for {uri}"}

        if item["player"] == "browser":
            for key in self.browser_app_ids:
                self.play_items[key] = item
        elif item["player"] == "mediaplayer":
            self.play_items[cmds["media_player"]] = item
        else:
            self.play_items[f"{item['player']}-{item['num']}"] = item

        logging.info(f"display: Added item {item['num']}: {item['uri']}")
        if item.get("enabled", True):
            self.playlist.start_item(item)

        return public_item(item)

    def toggle_playlist_item(self, num):
        try:
            num = int(num)
        except (TypeError, ValueError):
            return {"error": f"bad item number {num!r}"}

        if not self.playlist:
            return {"error": "no playlist"}

        for item in self.playlist.playlist:
            if item.get("num") != num:
                continue

            item["enabled"] = not item.get("enabled", True)
            logging.info(f"display: Item {num} ({item['uri']}) "
                         f"now {'enabled' if item['enabled'] else 'disabled'}")
            if item["enabled"] and not item.get("started"):
                self.playlist.start_item(item)

            return {"num": num, "enabled": item["enabled"]}

        return {"error": f"no item {num}"}

    # Dropping the item takes it out of the rotation, but its view would keep
    # running and keep its window, so the connection is stopped too. stop()
    # only sets a flag and wakes the loop, so it is safe from this thread
    def remove_playlist_item(self, num):
        try:
            num = int(num)
        except (TypeError, ValueError):
            return {"error": f"bad item number {num!r}"}

        if not self.playlist:
            return {"error": "no playlist"}

        target = next((i for i in self.playlist.playlist
                       if i.get("num") == num), None)
        if target is None:
            return {"error": f"no item {num}"}

        self.playlist.playlist.remove(target)
        stopped = self.stop_view(f"{target['player']}-{target['num']}")
        if target["player"] == "browser":
            try:
                self.playlist.stop_browser()
            except Exception as e:
                logging.warning(f"display: Failed to stop the browser: {e}")
        elif target["player"] == "mediaplayer":
            try:
                self.playlist.stop_mediaplayer()
            except Exception as e:
                logging.warning(f"display: Failed to stop the media player: {e}")

        logging.info(f"display: Removed item {num} ({target['uri']}), "
                     f"view stopped {stopped}")

        return {"num": num, "uri": target["uri"], "removed": True,
                "view_stopped": stopped}

    @staticmethod
    def stop_view(title):
        with live_views_lock:
            conns = [conn for conn, w in live_views
                     if getattr(w, "title", None) == title]

        stopped_all = bool(conns)
        for conn in conns:
            try:
                conn.stop()
            except Exception as e:
                logging.warning(f"display: Failed to stop view {title}: {e}")
                stopped_all = False

        return stopped_all

    def set_item_play_time(self, num, play_time_s):
        try:
            num = int(num)
            play_time_s = int(play_time_s)
        except (TypeError, ValueError):
            return {"error": f"bad item {num!r} or play time {play_time_s!r}"}

        if play_time_s < 1 or play_time_s > 86400:
            return {"error": f"play time {play_time_s} out of range"}

        if not self.playlist:
            return {"error": "no playlist"}

        for item in self.playlist.playlist:
            if item.get("num") != num:
                continue

            item["play_time_s"] = play_time_s
            logging.info(f"display: Item {num} ({item['uri']}) now shows "
                         f"for {play_time_s}s")

            return {"num": num, "play_time_s": play_time_s}

        return {"error": f"no item {num}"}

    # A colour set here outlives the current window: it is kept on the playlist
    # item so a view redrawn or recreated later comes back in the same colour
    def set_view_background(self, colour, num=None):
        rgb = parse_colour(colour)
        if rgb is None:
            return {"error": f"bad colour {colour!r}"}

        if not self.playlist:
            return {"error": "no playlist"}

        if num is None:
            num = self.current_num
        if num is None:
            return {"error": "no view is currently shown"}

        try:
            num = int(num)
        except (TypeError, ValueError):
            return {"error": f"bad item number {num!r}"}

        target = next((i for i in self.playlist.playlist
                       if i.get("num") == num), None)
        if target is None:
            return {"error": f"no item {num}"}

        target["bg_colour"] = hex_colour(rgb)
        title = f"{target['player']}-{target['num']}"
        repainted = self.repaint_view(title, rgb)
        logging.info(f"display: Item {num} ({target['uri']}) background now "
                     f"{target['bg_colour']}, live repaint {repainted}")

        return {"num": num, "bg_colour": target["bg_colour"],
                "repainted": repainted}

    # The colour is only written here; the redraw itself has to happen on the
    # view's own event loop thread, because everything it touches ends in
    # wayland requests on that thread's connection. So the view is asked to
    # repaint and its select is woken to make it happen now rather than at the
    # next timer tick
    @staticmethod
    def repaint_view(title, rgb):
        with live_views_lock:
            views = [(conn, w) for conn, w in live_views
                     if getattr(w, "title", None) == title]

        for conn, window in views:
            view_obj = getattr(window, "iss_view", None)
            if view_obj is None:
                continue

            for s_object in view_obj.s_objects:
                s_object["bg_colour_r"], s_object["bg_colour_g"], \
                    s_object["bg_colour_b"] = rgb
            view_obj.repaint_pending = True
            try:
                conn.waker.wake()
            except Exception as e:
                logging.warning(f"display: Failed to wake {title}: {e}")

            return True

        return False

    def window_item(self, window):
        for key in (window.get("name"), window.get("app_id")):
            if key in self.play_items:
                return self.play_items[key]

        return None

    # waylandsink and a browser on a titleless page report no window name, so
    # fall back to the playlist item the window is matched to
    def window_name(self, window):
        item = self.window_item(window)
        if item:
            return f"{item['player']}-{item['num']}"

        return window.get("name") or window.get("app_id") or "?"

    def window_play_time(self, window, default_s):
        item = self.window_item(window)

        return (item or {}).get("play_time_s") or default_s

    # Sorted descending and popped from the end, so the rotation runs in
    # playlist order and anything we cannot place in it comes last
    def window_order(self, window):
        item = self.window_item(window)

        return (item is None, item["num"] if item else 0)

    def start_window_switching(self, t_focus_s):
        self.x = threading.Thread(target=self.focus_next_window,
                                  args=(t_focus_s,))
        self.x.start()

    def step_rotation(self, step):
        self.rotation_step = step
        self.skip_event.set()

        return {"step": step}

    # Move the media player through its .m3u. The restart maps a fresh window,
    # so place it as soon as the compositor shows it rather than waiting for
    # the next rotation turn to notice
    def media_step(self, direction):
        if not self.playlist:
            return {"error": "no playlist"}

        result = self.playlist.media_step(direction)
        if isinstance(result, dict) and "error" not in result:
            self.media_step_gen += 1
            threading.Thread(target=self._place_stepped_media,
                             args=(self.media_step_gen, self.pip_pinned_id),
                             daemon=True).start()

        return result

    def _place_stepped_media(self, gen, previous_id):
        place = (self.place_pip_corner if self.pip_pinned_corner
                 else self.place_pip_full)
        deadline = time.time() + 15
        while time.time() < deadline and gen == self.media_step_gen:
            time.sleep(0.25)
            if self.pip_pinned_num is None or self.pip_paused:
                self.skip_event.set()

                return
            media = next((w for w in self.get_windows_whitelist()
                          if (self.window_item(w) or {}).get("num")
                          == self.pip_pinned_num), None)
            if media is None or media["id"] in (previous_id, self.pip_pinned_id):
                continue

            self.pip_pinned_id = media["id"]
            place(media, None)
            logging.info(f"display: placed stepped media window {media['id']}")

            return

    # Freeze or resume the rotation on the current view. A manual next/previous
    # still moves while pinned; only the timed advance is held
    def toggle_view_pin(self):
        self.pinned = not self.pinned
        self.skip_event.set()
        logging.info(f"display: rotation {'pinned' if self.pinned else 'resumed'}"
                     f" on view {self.current_num}")

        return {"pinned": self.pinned, "num": self.current_num}

    def park_pip_windows(self, windows):
        for window in windows:
            if not pip_corner(self.window_item(window) or {}):
                continue
            wid = window["id"]
            self.swaymsg(f"[con_id={wid}]", "sticky", "disable", log_prefix="pip")
            self.swaymsg(f"[con_id={wid}]", "move", "workspace",
                         self.holding_workspace, log_prefix="pip")

    # stop-pip holds the corner window off screen and lets the full slot run as
    # a plain rotation; start-pip hands the layout back to pip_cycle
    def set_pip(self, active):
        if self.pip_pinned_num is None:
            return {"error": "no pip in this playlist"}
        self.pip_paused = not active
        self.pip_pinned_id = None
        if self.pip_paused:
            self.park_pip_windows(self.get_windows_whitelist())
        self.skip_event.set()
        logging.info(f"display: pip {'resumed' if active else 'stopped'}")

        return {"pip": "running" if active else "stopped",
                "num": self.pip_pinned_num}

    def window_pip(self, window):
        return str((self.window_item(window) or {}).get("pip", ""))

    # Rebuilt every cycle rather than kept as a queue, so an item disabled or
    # added since the last switch is picked up at once, and next and previous
    # are a move of the index rather than surgery on a half consumed list
    def focus_next_window(self, t_focus_s):
        focused_id = None
        pip_shown_id = None
        while True:
            windows = self.get_windows_whitelist()
            windows.sort(key=self.window_order)

            if self.pip_pinned_num is not None and not self.pip_paused:
                pip_shown_id = self.pip_cycle(windows, pip_shown_id, t_focus_s)

                continue

            # stop-pip: the corner window is parked, the full slot rotates on
            # its own as a plain playlist
            if self.pip_paused:
                pip_shown_id = None
                windows = [w for w in windows
                           if not pip_corner(self.window_item(w) or {})]

            if not windows:
                logging.debug("display: Found no windows to switch to")
                time.sleep(t_focus_s)

                continue

            self.rotation_index %= len(windows)
            next_window = windows[self.rotation_index]
            win_id = next_window['id']
            play_time_s = self.window_play_time(next_window, t_focus_s)
            item = self.window_item(next_window) or {}
            self.current_num = item.get("num")
            labels = {"player": item.get("player", "unknown"),
                      "num": item.get("num", 0)}

            # The one view already on screen is left where it is: moving and
            # focusing it every cycle only churns the compositor and the logs
            settled = len(windows) == 1 and win_id == focused_id
            if not settled:
                logging.info(f"display: Switching focus to: {win_id} "
                             f"({self.window_name(next_window)}) for {play_time_s}s")

                if self.hold_outputs:
                    move = ('move', 'container', 'to', 'output',
                            self.display_output)
                else:
                    # One window to a workspace, so showing the next one never
                    # asks any window to change size or state, only the
                    # compositor to show a different workspace. A window already
                    # there is left alone, and the browser needs no special
                    # case: its own fullscreen covers the workspace it is alone
                    # on
                    move = ('move', 'workspace', self.window_workspace(win_id))
                self.swaymsg(f"[con_id={win_id}]", *move,
                             log_prefix="focus_next_window")
                self.swaymsg(f"[con_id={win_id}]", 'border', 'none',
                             log_prefix="focus_next_window")

                # Focus follows the window, so sway switches to its workspace
                self.swaymsg(f"[con_id={win_id}]", 'focus',
                             log_prefix="focus_next_window")

                if self.hold_outputs:
                    self.park_windows(windows, win_id)

                metrics.inc("iss_display_window_switches_total", **labels)
                focused_id = win_id

            shown_from = time.time()
            self.skip_event.wait(play_time_s)
            self.skip_event.clear()

            # Pinned: hold here, the view refreshes itself. A manual step
            # (rotation_step set) still gets through
            if self.pinned and not self.rotation_step:
                continue

            metrics.inc("iss_display_item_shown_seconds_total",
                        time.time() - shown_from, **labels)
            self.advance_item(item.get("num"))

            step = self.rotation_step or 1
            self.rotation_step = 0
            self.rotation_index = (self.rotation_index + step) % len(windows)

    # One turn of the pip rotation. The pinned window is placed once, in its
    # slot; the cycling set takes the other slot, one at a time
    def pip_cycle(self, windows, shown_id, t_focus_s):
        place_pinned = (self.place_pip_corner if self.pip_pinned_corner
                        else self.place_pip_full)
        place_cycled = (self.place_pip_full if self.pip_pinned_corner
                        else self.place_pip_corner)

        pinned = next((w for w in windows
                       if (self.window_item(w) or {}).get("num")
                       == self.pip_pinned_num), None)
        cycled = [w for w in windows
                  if self.window_pip(w).startswith("cycle")]

        if pinned is not None and pinned["id"] != self.pip_pinned_id:
            self.pip_pinned_id = pinned["id"]
            place_pinned(pinned, None)
            logging.info(f"display: PIP pinned {pinned['id']} "
                         f"({self.window_name(pinned)})")
        elif pinned is None:
            self.pip_pinned_id = None

        if not cycled:
            self.skip_event.wait(t_focus_s)
            self.skip_event.clear()

            return shown_id

        self.rotation_index %= len(cycled)
        due = cycled[self.rotation_index]
        play_time_s = self.window_play_time(due, t_focus_s)
        item = self.window_item(due) or {}
        self.current_num = item.get("num")
        labels = {"player": item.get("player", "unknown"),
                  "num": item.get("num", 0)}

        if due["id"] != shown_id:
            logging.info(f"display: pip -> {str(due['id']).zfill(2)} "
                         f"{self.window_name(due)} for {play_time_s}s")
            place_cycled(due, shown_id)
            metrics.inc("iss_display_window_switches_total", **labels)
            shown_id = due["id"]

        shown_from = time.time()
        self.skip_event.wait(play_time_s)
        self.skip_event.clear()

        # Pinned: hold on this view; its own refresh timer keeps it current
        if self.pinned and not self.rotation_step:
            return shown_id

        metrics.inc("iss_display_item_shown_seconds_total",
                    time.time() - shown_from, **labels)
        self.advance_item(item.get("num"))

        step = self.rotation_step or 1
        self.rotation_step = 0
        self.rotation_index = (self.rotation_index + step) % len(cycled)

        return shown_id

    # A view whose content is a carousel gets its next item as it leaves the
    # screen, so each showing is one item and the change is never seen. Views
    # that refresh on their own clock are not registered for this
    @staticmethod
    def advance_item(num):
        if num is None:
            return

        with content_refreshers_lock:
            refresher = content_refreshers.get(str(num))

        if refresher is None or not refresher.refresh_when_hidden:
            return

        refresher.due_now()

    def switch_workspace(self, ws):
        self.swaymsg('workspace', str(ws), log_prefix="switch_workspace")

    def screenshot(self):
        return screenshot(f"{self.screenshot_path}/{self.screenshot_file}")

default_stream_http_port = 7002
default_stream_audio_port = 7005

# The stream types scream tells us about at its own /metrics, plus vnc, whose
# viewers the controller counts from the socket table
stream_types = ("webm", "mjpeg", "mkv", "snapshot", "rtsp", "vnc")

def env_port(name, default):
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default

# scream serves the stream a browser can play; the page needs the port to build
# its own url, and the controller needs it to fetch a still
def stream_http_port():
    return env_port("STREAM_HTTP_PORT", default_stream_http_port)

# The media player relays the stream's audio, opus over rtp, to scream on this
# port; scream mixes it into pay1 and the webm. 0 turns the relay off
def stream_audio_port():
    return env_port("STREAM_AUDIO_PORT", default_stream_audio_port)

def is_loopback_endpoint(endpoint):
    host = endpoint.rsplit(":", 1)[0].strip("[]")
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False

# Both scrapes below hit scream on the loopback on a timer. A shared session
# keeps one connection alive across them instead of opening a fresh socket
# per call and leaving a TIME_WAIT behind; scream serves /metrics and
# /snapshot with keep-alive. The connection pool is thread-safe
_scream_session = requests.Session()
_scream_session.headers["Connection"] = "keep-alive"

# scream's own /metrics: the per-stream-type client counts and the cumulative
# snapshot request total. Empty counts and None on any failure so a scrape
# never resets the counter
def scream_metrics():
    clients, snapshots_total = {}, None
    try:
        r = _scream_session.get(
            f"http://127.0.0.1:{stream_http_port()}/metrics", timeout=1)
        body = r.text if r.status_code == 200 else ""
    except Exception as e:
        logging.debug(f"stream: no metrics from scream: {e}")

        return clients, snapshots_total

    for line in body.splitlines():
        client = re.match(r'scream_stream_clients\{stream="(\w+)"\}\s+(\d+)', line)
        if client:
            clients[client.group(1)] = int(client.group(2))
        elif line.startswith("scream_snapshot_requests_total "):
            try:
                snapshots_total = int(line.split()[1])
            except (IndexError, ValueError):
                pass

    return clients, snapshots_total

# One subscriber per stream type. scream reports its http and rtsp clients; a
# vnc viewer holds an established connection to wayvnc's port, minus the
# controller's own loopback polls
def stream_subscribers(sockets, scream_clients):
    counts = {name: 0 for name in stream_types}
    counts.update({k: v for k, v in scream_clients.items() if k in counts})

    vnc_port = str(env_port("VNC_PORT", default_vnc_port))
    counts["vnc"] = sum(
        1 for s in sockets or []
        if s.get("proto") == "tcp" and s.get("state") == "ESTABLISHED"
        and s.get("local", "").rsplit(":", 1)[-1] == vnc_port
        and not is_loopback_endpoint(s.get("remote", "")))

    return counts

def snapshot_url():
    return os.environ.get(
        "SNAPSHOT_URL",
        f"http://127.0.0.1:{stream_http_port()}/snapshot")

# scream captures the output continuously for the stream it serves, so a still
# is one of those frames rather than a fresh grab. That drops the fork per
# request and the temp file with it
def snapshot_bytes(timeout_s=5):
    try:
        r = _scream_session.get(snapshot_url(), timeout=timeout_s)
        if r.status_code != 200:
            logging.error(f"snapshot: {snapshot_url()} returned {r.status_code}")

            return None

        return r.content
    except Exception as e:
        logging.error(f"snapshot: Failed to read {snapshot_url()}: {e}")

        return None

def screenshot(path=None):
    if path is None:
        path = "/tmp/screenshot.jpg"
    logging.debug(f"Saving screenshot to {path}")
    data = snapshot_bytes()
    if data:
        try:
            with open(path, "wb") as f:
                f.write(data)

            return path
        except OSError as e:
            logging.error(f"screenshot: Failed to write {path}: {e}")

            return None

    return None

class HtmlPage:

    resolutions = ("1024x768", "1280x800", "1366x768", "1600x900", "1920x1080")

    # An author display rule beats the user-agent [hidden] one no matter the
    # specificity, so hidden is re-asserted or toggling video.hidden and
    # still.hidden would leave both visible
    css = """
<style>
*, *::before, *::after { box-sizing: border-box; }
[hidden] { display: none !important; }
:root { --ambilight: #111; }
body, .player:fullscreen {
    background: var(--ambilight);
    transition: background 1.2s linear;
}
body {
    margin: 0;
    padding: 1.5em;
}
#stream, #still { max-width: 100%; display: block; }
.player {
    position: relative;
    display: inline-block;
    line-height: 0;
}
.player:fullscreen {
    display: flex;
    align-items: center;
    justify-content: center;
}
.player:fullscreen #stream, .player:fullscreen #still {
    max-width: 100vw;
    max-height: 100vh;
}
.overlay {
    position: absolute;
    inset: 0;
    display: none;
    overflow: auto;
    padding: 1em;
    background: rgba(0,0,0,0.7);
    color: #fff;
    line-height: normal;
}
.overlay th, .overlay td { color: #fff; }
.settings {
    position: absolute;
    inset: 0;
    overflow: auto;
    padding: 1em;
    background: rgba(0,0,0,0.85);
    color: #fff;
    line-height: normal;
}
.settings th, .settings td { color: #fff; }
.settings table { margin: 0 auto 1em auto; }
.settings-edit { text-align: center; }
.settings-edit label { margin-right: 1em; }
.overlay .tables-row {
    margin: auto;
    justify-content: center;
    align-items: flex-start;
}
.player:hover .overlay, .player:hover .controls { display: flex; }
#shell-output {
    margin: 1em auto 0 auto;
    padding: 0.75em;
    max-width: 90%;
    max-height: 40vh;
    overflow: auto;
    background: rgba(0,0,0,0.6);
    border: 1px solid #fff;
    border-radius: 6px;
    white-space: pre-wrap;
    font-family: monospace;
}
.controls {
    position: absolute;
    bottom: 0.75em;
    right: 0.75em;
    display: none;
    gap: 0.5em;
    z-index: 20;
}
/* The gap closes the transport group off from the rest, so it goes
   after the last of them rather than after play */
.controls #next-btn { margin-right: 1.5em; }
.controls #pin-btn svg { display: block; width: 1em; height: 1em; fill: currentColor; }
.controls #pin-btn.pinned { background: #fff; color: #000; opacity: 1; }
.controls input[type="color"] {
    position: absolute;
    width: 1px;
    height: 1px;
    opacity: 0;
    pointer-events: none;
}
.menu {
    position: absolute;
    bottom: 3.6em;
    right: 0.75em;
    display: none;
    flex-direction: column;
    gap: 0.3em;
    z-index: 25;
}
.player:hover .menu.open { display: flex; }
.controls button, .menu button, .overlay button {
    background: rgba(0,0,0,0.7);
    color: #fff;
    border: 2px solid #fff;
    border-radius: 8px;
    cursor: pointer;
}
.controls button, .menu button {
    font-weight: bold;
    opacity: 0.85;
}
.controls button {
    padding: 0.2em 0.5em;
    font-size: 1.8em;
    line-height: 1;
}
.menu button { padding: 0.25em 0.6em; }
.overlay button {
    border-width: 1px;
    border-radius: 6px;
    padding: 0.1em 0.5em;
}
table td { padding: 0.5em 2em 0.5em 0.5em; }
.tables-row {
    display: flex;
    flex-wrap: wrap;
    gap: 2em;
    margin-bottom: 1em;
}
.tables-row table { margin-bottom: 0; }
</style>
"""

    @staticmethod
    def show_display_data():
        state = Display.query_state()
        if not state:
            return "<p>Display state unavailable</p>"
        name = socket.gethostname()
        address = state.get('address')
        port = state.get('port')
        res_x = state.get('res_x')
        res_y = state.get('res_y')

        rows = [
            ("Playlist", state.get('playlist_name') or "", "playlist-name"),
            ("Default time", state.get('default_play_time_s') or "", "default-time"),
            ("Name", name),
            ("Address", address),
            ("Port", port),
            ("Resolution", f"{res_x} x {res_y}")
        ]
        table = ["<table>"]
        for row in rows:
            key, value = row[0], row[1]
            css = f' class="{row[2]}"' if len(row) > 2 else ""
            table.append(f"<tr><td>{html.escape(str(key))}</td>"
                         f"<td{css}>{html.escape(str(value))}</td></tr>")
        table.append("</table>")
        return "".join(table)

    @staticmethod
    def show_playlist_data():
        playlist_items = get_playlist_items()

        if not playlist_items:
            return "<p>No playlist items available</p>"

        table = [
            "<table>",
            "<tbody>",
        ]
        for item in playlist_items:
            num = str(item.get('num', ''))
            uri = str(item.get('uri', ''))
            player = str(item.get('player', ''))
            play_time_s = str(item.get('play_time_s', ''))
            refresh_s = str(item.get('refresh_s') or "")
            enabled = "enabled" if item.get('enabled', True) else "disabled"
            toggle = (f'<button data-action="toggle" data-num="{html.escape(num)}" '
                      'title="Enable or disable the playlist item">'
                      f"{html.escape(enabled)}</button>")
            shown_time = f"{play_time_s}s" if play_time_s else ""
            time_cell = (f'<button data-action="time" data-num="{html.escape(num)}" '
                         'title="Edit the display time">'
                         f"{html.escape(shown_time)}</button>")
            table.append(
                f"<tr><td>{html.escape(num)}</td><td>{html.escape(uri)}</td><td>{html.escape(player)}</td><td>{time_cell}</td><td>{html.escape(refresh_s)}</td><td>{toggle}</td></tr>"
            )
        table.append("</tbody>")
        table.append('<tfoot><tr><td colspan="5"></td>'
                     '<td><button data-action="add" title="Add an item">+</button>'
                     '<button data-action="remove" title="Remove an item">\u2212</button></td>'
                     '</tr></tfoot>')
        table.append("</table>")
        return "".join(table)

    # vp8 in webm over a chunked response plays natively in <video>;
    # hls would have needed hls.js in chrome and firefox, and webrtc a
    # gstreamer plugin alpine does not package
    player_open = """
<div class="player" id="player">
  <video id="stream" autoplay muted playsinline></video>
  <img id="still" alt="Display" hidden>
  <div class="overlay"><div class="tables-row">
"""

    script = r"""
<script>
const player = document.getElementById("player");
const video = document.getElementById("stream");
const still = document.getElementById("still");
const overlay = document.querySelector(".overlay");
const resMenu = document.getElementById("res-menu");
const bgInput = document.getElementById("bg-input");
let paused = false;
let polling = null;

async function api(url, options){
  const res = await fetch(url, Object.assign({method: "POST"}, options));
  const data = await res.json();
  if(data.error){ alert(data.error); return null; }
  return data;
}

async function fetchShot(){
  const res = await fetch("/api/v1/screenshot", {cache: "no-store"});
  if(!res.ok) return null;
  const blob = await res.blob();
  return blob.size ? blob : null;
}

function togglePause(btn){
  paused = !paused;
  btn.innerText = paused ? "▶" : "⏸";
  btn.title = paused ? "Play" : "Pause";
}

async function togglePin(btn){
  const data = await api("/api/v1/pin");
  if(!data) return;
  btn.classList.toggle("pinned", data.pinned);
  btn.title = data.pinned ? "Unpin view (resume the rotation)"
                          : "Pin view (stop the rotation)";
}

async function saveScreenshot(){
  const blob = await fetchShot();
  if(!blob) return;
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = "iss-display.jpg";
  a.click();
  URL.revokeObjectURL(a.href);
}

function toggleFullscreen(){
  if(document.fullscreenElement) document.exitFullscreen();
  else player.requestFullscreen();
}

async function runShell(){
  const command = prompt("Command to run in the container", "");
  if(!command) return;
  const res = await fetch("/api/v1/shell?command=" + encodeURIComponent(command),
                          {method: "POST"});
  const data = await res.json();
  const out = document.getElementById("shell-output");
  out.textContent = "$ " + command + "\n\n"
    + (data.error || data.output || "") + "\n\nexit " + data.returncode;
  out.hidden = false;
}

function prefillSettings(panel){
  const name = panel.querySelector("td.playlist-name");
  const time = panel.querySelector("td.default-time");
  panel.querySelector("#set-name").value = name ? name.textContent : "";
  panel.querySelector("#set-time").value = time ? time.textContent : "";
}

function editSettings(){
  const panel = document.getElementById("settings");
  if(panel.hidden) prefillSettings(panel);
  panel.hidden = !panel.hidden;
}

async function saveSettings(){
  const panel = document.getElementById("settings");
  const name = panel.querySelector("#set-name").value;
  const t = panel.querySelector("#set-time").value;
  let url = "/api/v1/settings?name=" + encodeURIComponent(name);
  if(t) url += "&default_time=" + encodeURIComponent(t);
  if(await api(url)){
    await refreshOverlay();
    prefillSettings(panel);
  }
}

async function refreshOverlay(){
  const res = await fetch(location.href, {cache: "no-store"});
  const doc = new DOMParser().parseFromString(await res.text(), "text/html");
  overlay.innerHTML = doc.querySelector(".overlay").innerHTML;
  const panel = document.getElementById("settings");
  const fresh = doc.getElementById("settings");
  if(panel && fresh) panel.innerHTML = fresh.innerHTML;
}

async function addItem(){
  const uri = prompt("URI to add", "iss://apod");
  if(!uri) return;
  const t = prompt("Display time in seconds (blank for the default)", "");
  let url = "/api/v1/playlist?uri=" + encodeURIComponent(uri);
  if(t) url += "&t=" + encodeURIComponent(t);
  if(await api(url)) await refreshOverlay();
}

async function removeItem(){
  const rows = overlay.querySelectorAll("tbody tr");
  const last = rows.length ? rows[rows.length - 1].cells[0].textContent : "";
  const num = prompt("Number of the item to remove", last);
  if(!num) return;
  const row = Array.from(rows).find(
    r => r.cells[0].textContent.trim() === num.trim());
  const what = row ? row.cells[1].textContent : ("item " + num);
  if(!confirm("Remove " + what + "?")) return;
  if(await api("/api/v1/playlist/" + encodeURIComponent(num.trim()),
               {method: "DELETE"}))
    await refreshOverlay();
}

async function editTime(btn){
  const num = btn.dataset.num;
  const t = prompt("Display time in seconds for item " + num,
                   btn.textContent.trim().replace(/s$/, ""));
  if(t === null) return;
  const data = await api("/api/v1/playlist/" + num + "/time?seconds="
                         + encodeURIComponent(t));
  if(data && data.play_time_s) btn.textContent = data.play_time_s + "s";
}

async function toggleItem(btn){
  const data = await api("/api/v1/playlist/" + btn.dataset.num + "/toggle");
  if(!data || data.enabled === undefined) return;
  btn.textContent = data.enabled ? "enabled" : "disabled";
}

const controlActions = {
  "prev-btn": () => fetch("/api/v1/previous", {method: "POST"}),
  "next-btn": () => fetch("/api/v1/next", {method: "POST"}),
  "pause-btn": togglePause,
  "pin-btn": togglePin,
  "shot-btn": saveScreenshot,
  "fs-btn": toggleFullscreen,
  "res-btn": () => resMenu.classList.toggle("open"),
  "bg-btn": () => bgInput.click(),
  "shell-btn": runShell,
  "settings-btn": editSettings
};
document.querySelector(".controls").addEventListener("click", function(e){
  const btn = e.target.closest("button");
  const action = btn && controlActions[btn.id];
  if(!action) return;
  e.stopPropagation();
  Promise.resolve(action(btn)).catch(err => console.warn(err));
});

const overlayActions = {toggle: toggleItem, time: editTime,
                        add: addItem, remove: removeItem};
overlay.addEventListener("click", function(e){
  const btn = e.target.closest("button");
  const action = btn && overlayActions[btn.dataset.action];
  if(!action) return;
  Promise.resolve(action(btn)).catch(err => console.warn(err));
});

document.getElementById("settings").addEventListener("click", function(e){
  const btn = e.target.closest("button");
  if(btn && btn.id === "settings-save")
    Promise.resolve(saveSettings()).catch(err => console.warn(err));
});

resMenu.addEventListener("click", function(e){
  const btn = e.target.closest("button");
  if(!btn || !btn.dataset.mode) return;
  resMenu.classList.remove("open");
  fetch("/api/v1/resolution/" + btn.dataset.mode, {method: "POST"})
    .catch(err => console.warn(err));
});

bgInput.addEventListener("change", function(){
  fetch("/api/v1/background?colour=" + encodeURIComponent(bgInput.value),
        {method: "POST"}).catch(err => console.warn(err));
});

document.addEventListener("fullscreenchange", function(){
  document.getElementById("fs-btn").title =
    document.fullscreenElement ? "Exit fullscreen" : "Fullscreen";
});

const sampler = document.createElement("canvas");
const sctx = sampler.getContext("2d", {willReadFrequently: true});
function sampleSource(){
  if(!video.hidden && video.videoWidth)
    return [video, video.videoWidth, video.videoHeight];
  if(!still.hidden && still.naturalWidth)
    return [still, still.naturalWidth, still.naturalHeight];
  return [null, 0, 0];
}
function ambilight(){
  const [src, sourceW, sourceH] = sampleSource();
  if(!src || !sourceW || !sourceH) return;
  const sw = 64, sh = 36, band = 2;
  sampler.width = sw; sampler.height = sh;
  sctx.drawImage(src, 0, 0, sw, sh);
  let data;
  try { data = sctx.getImageData(0, 0, sw, sh).data; } catch(e) { return; }
  let r = 0, g = 0, b = 0, n = 0;
  for(let y = 0; y < sh; y++){
    for(let x = 0; x < sw; x++){
      if(x >= band && x < sw - band && y >= band && y < sh - band) continue;
      const i = (y * sw + x) * 4;
      r += data[i]; g += data[i+1]; b += data[i+2]; n++;
    }
  }
  if(!n) return;
  const rgb = "rgb(" + Math.round(r/n) + "," + Math.round(g/n) + "," + Math.round(b/n) + ")";
  document.documentElement.style.setProperty("--ambilight", rgb);
}
still.addEventListener("load", ambilight);
setInterval(ambilight, 2000);
ambilight();

// Reflect a pin set from anywhere (udp, another client) on load
fetch("/api/v1/display").then(r => r.ok && r.json()).then(d => {
  if(d && d.pinned){
    const b = document.getElementById("pin-btn");
    b.classList.add("pinned");
    b.title = "Unpin view (resume the rotation)";
  }
}).catch(() => {});

async function refreshScreenshot(){
  if(paused) return;
  const blob = await fetchShot();
  if(!blob) return;
  const url = URL.createObjectURL(blob);
  const old = still.src;
  still.src = url;
  if(old.startsWith("blob:")) URL.revokeObjectURL(old);
}
function useStills(why){
  if(polling) return;
  console.warn("falling back to stills:", why);
  video.hidden = true; still.hidden = false;
  const poll = () => refreshScreenshot().catch(err => console.warn(err));
  polling = setInterval(poll, 1000);
  poll();
}
video.addEventListener("error", () => useStills("video error"));
video.crossOrigin = "anonymous";
video.src = location.protocol + "//" + location.hostname
            + ":" + STREAM_PORT + "/";
video.play().catch(e => useStills(e && e.name ? e.name : e));
setTimeout(() => { if(!video.videoWidth) useStills("no frames"); }, 8000);
</script>
"""

    @staticmethod
    def page_display():
        shell_button = ('<button id="shell-btn" title="Run a command">&gt;_</button>'
                        if debug_mode() else "")
        resolution_buttons = "".join(
            f'<button data-mode="{mode}">{mode}</button>'
            for mode in HtmlPage.resolutions)
        page_tail = f"""
</div><pre id="shell-output" hidden></pre></div>
  <div class="settings" id="settings" hidden>
    {HtmlPage.show_display_data()}
    <div class="settings-edit">
      <label>Playlist <input id="set-name"></label>
      <label>Default time <input id="set-time" size="4"></label>
      <button id="settings-save">Save</button>
    </div>
  </div>
  <div class="controls">
    <button id="prev-btn" title="Previous">⏮</button>
    <button id="pause-btn" title="Pause">⏸</button>
    <button id="pin-btn" title="Pin view (stop the rotation)"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M14 4V2H6v2h1l1 6-3 2v2h5v6l1 2 1-2v-6h5v-2l-3-2 1-6h1z"/></svg></button>
    <button id="next-btn" title="Next">⏭</button>
    <button id="shot-btn" title="Save screenshot">⤓</button>
    <button id="fs-btn" title="Fullscreen">⛶</button>
    <button id="res-btn" title="Resolution">⇲</button>
    <button id="bg-btn" title="Background colour">▨</button>
    <input type="color" id="bg-input" value="#280f28">
    {shell_button}<button id="settings-btn" title="Settings">⚙</button>
  </div>
  <div class="menu" id="res-menu">{resolution_buttons}</div>
</div>
"""
        return ("<!DOCTYPE html>"
                '<html lang="en"><head><meta charset="utf-8">'
                '<meta name="viewport" content="width=device-width, initial-scale=1">'
                "<title>ISS Display</title>"
                + HtmlPage.css
                + "</head><body>"
                + HtmlPage.player_open
                + HtmlPage.show_playlist_data()
                + page_tail
                + f"<script>const STREAM_PORT = {stream_http_port()};</script>"
                + HtmlPage.script
                + "</body></html>")


if __name__ == "__main__":

    parser = configargparse.ArgParser(description="")
    parser.add_argument('--debug',
                        dest='debug',
                        env_var='DEBUG',
                        help="Show debug output",
                        action='store_true')
    parser.add_argument('--uri',
                        dest='uris',
                        env_var='URI',
                        help="The URIs to open, can be used multiple times",
                        type=str,
                        action='append')
    parser.add_argument('--playlist',
                        dest='playlist_name',
                        env_var='PLAYLIST',
                        help="A playlist named in playlists.toml, used when "
                             "no --uri / URI is given",
                        type=str,
                        default="infinit")
    parser.add_argument('--stream-source',
                        dest='stream_source',
                        env_var='STREAM_SOURCE',
                        help="The source of the stream",
                        type=str,
                        default="")
    parser.add_argument('--stream-source-device',
                        dest='stream_source_device',
                        env_var='STREAM_SOURCE_DEVICE',
                        help="The source device to stream from",
                        type=str,
                        default="/dev/video0")
    parser.add_argument('--listen-address',
                        dest='listen_address',
                        env_var='LISTEN_ADDRESS',
                        help="The address to listen on",
                        type=str,
                        default="0.0.0.0")
    parser.add_argument('--listen-port',
                        dest='listen_port',
                        env_var='LISTEN_PORT',
                        help="The port to listen on",
                        type=int,
                        default=7000)
    parser.add_argument('--img-path',
                        dest='img_path',
                        env_var='IMAGES_PATH',
                        help="Path to image files",
                        type=str,
                        default="/tmp/screenshots/")
    parser.add_argument('--location',
                        dest='location',
                        env_var='LOCATION',
                        help="The location to gather data like weather for",
                        type=str,
                        default="Berlin")
    parser.add_argument('--logfile',
                        dest='logfile',
                        env_var='LOGFILE',
                        help="Path to optional logfile",
                        type=str)
    parser.add_argument('--loglevel',
                        dest='loglevel',
                        env_var='LOGLEVEL',
                        help="Loglevel, default: INFO",
                        type=str,
                        default='INFO')
    parser.add_argument('--mqtt-broker',
                        dest='mqtt_broker',
                        env_var='MQTT_BROKER',
                        help="The MQTT broker address",
                        type=str,
                        default="")
    parser.add_argument('--mqtt-port',
                        dest='mqtt_port',
                        env_var='MQTT_PORT',
                        help="The MQTT port",
                        type=int,
                        default=1883)
    parser.add_argument('--mqtt-user',
                        dest='mqtt_user',
                        env_var='MQTT_USER',
                        help="The MQTT user",
                        type=str,
                        default="")
    parser.add_argument('--mqtt-password',
                        dest='mqtt_pw',
                        env_var='MQTT_PASSWORD',
                        help="The MQTT password",
                        type=str,
                        default="")
    parser.add_argument('--mqtt-topics',
                        dest='mqtt_topics',
                        env_var='MQTT_TOPICS',
                        help="The MQTT topics to subscribe to",
                        type=str,
                        action='append')
    parser.add_argument('--probe-ip-address',
                        dest='probe_ip_address',
                        env_var='PROBE_IP_ADDRESS',
                        help="Host or IP the local outbound address is "
                             "discovered against (no traffic is sent)",
                        type=str,
                        default="x.org")
    parser.add_argument('--theme',
                        dest='theme_name',
                        env_var='THEME',
                        help="The theme to use, a table in themes.toml",
                        type=str,
                        default="infinit")
    parser.add_argument('--zeroconf-publish-service',
                        dest='zeroconf_publish_service',
                        env_var='ZEROCONF_PUBLISH',
                        help="Publish service via mDNS",
                        action='store_true')
    parser.add_argument('--zeroconf-service-name-prefix',
                        dest='zeroconf_service_name_prefix',
                        help="The name prefix of the service",
                        type=str,
                        default="controller")
    parser.add_argument('--zeroconf-service-type',
                        dest='zeroconf_service_type',
                        env_var='ZEROCONF_TYPE',
                        help="The type of service",
                        type=str,
                        default="_http._tcp.local.")
    parser.add_argument('--apod-api-key',
                        dest='apod_api_key',
                        env_var='APOD_API_KEY',
                        help='API key for NASA APOD',
                        type=str,
                        default=None)

    args = parser.parse_args()
    if isinstance(args.uris, str):
        uris_list = [args.uris]
    else:
        uris_list = args.uris
    if not uris_list:
        uris_list = playlist_uris(args.playlist_name)
        os.environ.setdefault("URI", "|".join(uris_list))
    if uris_list and isinstance(uris_list[0], str) and "|" in uris_list[0]:
        uris_list = uris_list[0].split("|")
    args.uris = uris_list
    # Same for MQTT topics: normalize to list, split common separators
    if args.mqtt_topics:
        raw_topics = args.mqtt_topics if isinstance(args.mqtt_topics, list) else [args.mqtt_topics]
        topics_flat = []
        for entry in raw_topics:
            if isinstance(entry, str):
                for token in re.split(r"[|,;\s]+", entry):
                    token = token.strip()
                    if token:
                        topics_flat.append(token)
        args.mqtt_topics = topics_flat if topics_flat else []
    else:
        args.mqtt_topics = []

    globals().update(vars(args))
    apply_theme(args.theme_name)
    zc_service_name_prefix = args.zeroconf_service_name_prefix
    zc_service_type = args.zeroconf_service_type
    log_format = ('[%(asctime)s] {%(filename)s:%(lineno)d} '
                  '%(levelname)s - %(message)s')

    # Optional file logging, before the logger is set up so it uses print
    file_handler = None
    if logfile:
        logpath, _, logname = logfile.rpartition('/')
        if not os.access(logpath or ".", os.W_OK):
            print("Logging: Can not write to directory. Skipping file handler")
        else:
            fn = f"{logpath}/{logname}"
            file_handler = logging.FileHandler(filename=fn)
            print("Logging: Logging to " + fn)

    ring_log.setFormatter(logging.Formatter(log_format))
    handlers = [logging.StreamHandler(sys.stdout), ring_log]
    if file_handler:
        handlers.insert(0, file_handler)

    level = logging.getLevelName(loglevel)
    logging.basicConfig(level=level, format=log_format, handlers=handlers)
    logging.getLogger(__name__).setLevel(level)

    # python-wayland logs every registry global, protocol message and draw
    # call at info -- the xdg/wayland chatter. Keep it for LOGLEVEL=DEBUG only,
    # here rather than in the library so it holds whichever build is installed
    logging.getLogger("wayland").setLevel(
        logging.DEBUG if level == logging.DEBUG else logging.WARNING)

    env = os.environ.copy()

    if debug:
        for k, v in env.items():
            logging.debug(f"{k}={v}")
            logging.debug(System.list_processes())

    if listen_port < 1025 or listen_port > 65535:
        logging.error(f"Invalid port {listen_port}, aborting..")
        sys.exit(1)

    if stream_source not in stream_sources:
        sources_str = " ".join(str(x) for x in stream_sources)
        logging.error(f"Invalid source: {stream_source}, aborting..")
        logging.info(f"Possible choices are: {sources_str}")
        sys.exit(1)

    hostname = socket.gethostname()
    local_ip = System.net_iface_address(probe_ip_address)

    compositor = Compositor()
    if not compositor.start():
        logging.error("Could not bring the compositor up, aborting..")
        sys.exit(1)

    stream_server = StreamServer()
    if not stream_server.start():
        logging.warning("stream: The web ui will have no stream or stills")

    vnc_server = WayvncServer()
    if not vnc_server.start():
        logging.warning("vnc: No remote desktop will be available")

    display = Display(local_ip, listen_port)

    # Start UDP state server so external processes (e.g., Daphne) can query Display
    display.start_state_server()

    nwins = len(display.get_windows())
    if nwins > 0:
        logging.warning(f"Expected no windows but found {nwins}")

    theme = Theme(theme_name)
    logging.info(f"PATH: {env.get('PATH', '')}")
    logging.info(f"Using theme: {theme_name}")
    logging.info(f"Using playlist: {args.playlist_name}")
    logging.info(f"URIs: {uris}")
    playlist = Playlist(uris, 5, theme, mqtt_topics, location)
    for item in playlist.playlist:
        logging.info(f"Playlist item {item['num']}: {item['uri']} -> {item['player']}")
    display.set_playlist(playlist)
    display.start_window_switching(5)
    threads = playlist.start_player(probe_ip_address)
    started = len(threads)
    expected = len(playlist.playlist)
    logging.info(f"Started {started} {'player' if started == 1 else 'players'}")
    if expected != started:
        logging.info(f"Player mismatch: expected {expected} from URIs, started {started}")

    stream = Stream(stream_source)

    # Publish service on the network via mDNS
    if zeroconf_publish_service:
        zc_listen_address = listen_address

        if "0.0.0.0" == zc_listen_address:
            zc_listen_address = System.net_iface_address(probe_ip_address)

        zc_listen_port = listen_port

        logging.info("zeroconf: Publishing service of type "
                     + zc_service_type + " with name prefix "
                     + zc_service_name_prefix + " on "
                     + zc_listen_address + ":" + str(zc_listen_port))

        zc_service_properties = {"stream_source": stream_source}

        zc = Zeroconf_service(zc_service_name_prefix,
                              zc_service_type,
                              hostname,
                              zc_listen_address,
                              zc_listen_port,
                              zc_service_properties)

        r = zc.register()

        if not r:
            logging.error("zeroconf: Failed to publish service")

    web_server = None
    stopping = threading.Event()
    stopping_since = 0.0
    force_exit_after_s = 2

    # Set up signal handler
    def signal_handler(number, *args):
        nonlocal_since = time.time() - stopping_since
        if stopping.is_set():
            if nonlocal_since < force_exit_after_s:
                return

            logging.warning(f"Signal {number} while already stopping, exiting now")
            os._exit(1)

        globals()['stopping_since'] = time.time()
        stopping.set()
        logging.info(f"Signal received: {number}, shutting down")

        # Unpublish service
        if zeroconf_publish_service and zc:
            try:
                zc.unregister()
            except Exception as e:
                logging.warning(f"zeroconf: Failed to unregister the service: {e}")

        # Stop UDP state server
        try:
            display.stop_state_server()
        except Exception as e:
            logging.warning(f"Failed to stop the state server: {e}")

        if display.playlist:
            for stop in (display.playlist.stop_browser,
                         display.playlist.stop_mediaplayer):
                try:
                    stop()
                except Exception as e:
                    logging.warning(f"Failed to stop {stop.__name__}: {e}")

        if web_server and web_server.poll() is None:
            logging.info(f"Stopping the web server, pid {web_server.pid}")
            try:
                terminate_process(web_server, "web server")
            except Exception as e:
                logging.warning(f"Failed to stop the web server: {e}")

        try:
            stream_server.stop()
        except Exception as e:
            logging.warning(f"Failed to stop the stream server: {e}")

        try:
            vnc_server.stop()
        except Exception as e:
            logging.warning(f"Failed to stop the vnc server: {e}")

        try:
            compositor.stop()
        except Exception as e:
            logging.warning(f"Failed to stop the compositor: {e}")

        logging.shutdown()

        os._exit(0)

    # Register signal handler
    for received in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(received, signal_handler)

    # Start ASGI server via uvicorn
    try:
        cmd = [sys.executable, '-m', 'uvicorn', 'controller:app',
               '--host', listen_address, '--port', str(listen_port)]
        if str(loglevel).upper() != 'DEBUG':
            cmd.append('--no-access-log')
        logging.info(f"Starting uvicorn ASGI server on {listen_address}:{listen_port}")
        # Ensure the controller directory is importable so uvicorn can import 'controller:app'
        module_dir = os.path.dirname(os.path.abspath(__file__))
        env_mod = os.environ.copy()
        env_mod['PYTHONPATH'] = module_dir + (os.pathsep + env_mod['PYTHONPATH'] if 'PYTHONPATH' in env_mod else '')
        web_server = Popen(cmd, env=env_mod)
        web_server.wait()
    except FileNotFoundError:
        logging.error("uvicorn not found. Install 'uvicorn' to run the ASGI server.")
        sys.exit(1)
