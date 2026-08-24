#!/usr/bin/env python3
import requests
import collections
import configargparse
from doi import APOD, Calendar, MQTT, Music, News, OTD, RSSFeed, System, Weather
import html
import ipaddress
import json
import logging
from logging import DEBUG
import os
from pathlib import Path
import platform
import re
import socket
import stat
import subprocess
from subprocess import Popen, PIPE
import shutil
import signal
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.responses import JSONResponse, PlainTextResponse, HTMLResponse, FileResponse
from starlette.routing import Match, Route
import sys
import tempfile
import time
import threading
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
import xml.etree.ElementTree as ET
from zeroconf import IPVersion, ServiceInfo, Zeroconf
from wayland import draw as view
import wayland.protocol

# Suppress protocol.py INFO messages
import logging
logging.getLogger("wayland.protocol").setLevel(logging.WARNING)


# Ensure child processes inherit the runtime environment (including PATH)
env = os.environ.copy()

dependencies = []
stream_sources = ["static-images", "v4l2", "vnc-browser"]
cmds = {"clock":        "humanbeans_clock",
        "image_viewer": "imv",
        "media_player": "mpv",
        "screenshot":   "grim"}


metric_meta = {
    "iss_display_build_info": ("gauge", "Build and dependency identity, always 1"),
    "iss_display_start_time_seconds": ("gauge", "Unix time the display started"),
    "iss_display_views_running": ("gauge", "View windows currently drawing"),
    "iss_display_surface_commits_total": ("counter", "Surface commits submitted"),
    "iss_display_frames_presented_total": ("counter", "Frames the compositor presented"),
    "iss_display_view_exits_total": ("counter", "View windows that stopped, by reason"),
    "iss_display_window_switches_total": ("counter", "Focus switches performed"),
    "iss_display_item_shown_seconds_total": ("counter", "Seconds each item was on screen"),
    "iss_display_item_enabled": ("gauge", "Whether a playlist item is enabled"),
    "iss_display_item_play_time_seconds": ("gauge", "Configured play time per item"),
    "iss_display_windows": ("gauge", "Windows known to the compositor, by state"),
    "iss_display_content_age_seconds": ("gauge", "Age of the content a view is showing"),
    "iss_display_content_ticks_total": ("counter", "Refresh timer ticks handled"),
    "iss_display_content_refreshes_total": ("counter", "Content refreshes that redrew"),
    "iss_display_fetch_failures_total": ("counter", "Upstream fetches that failed, by source"),
    "iss_display_rss_items": ("gauge", "Items parsed from an RSS feed"),
    "iss_display_swaymsg_errors_total": ("counter", "swaymsg calls that wrote to stderr"),
    "iss_display_state_commands_total": ("counter", "State socket commands served"),
    "iss_display_browser_up": ("gauge", "Whether a browser window is present"),
    "iss_display_http_requests_total": ("counter", "Requests handled, by route path"),
    "iss_display_http_request_duration_seconds": ("summary", "Time spent handling requests"),
    "iss_display_screenshot_failures_total": ("counter", "Screenshot captures that failed"),
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

metrics = Metrics()
live_views = []
live_views_lock = threading.Lock()
last_content_refresh = {}
last_content_refresh_lock = threading.Lock()

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

def web_main(request):
    return HTMLResponse(HtmlPage.page_display())

def web_display(request):
    return HTMLResponse(HtmlPage.page_display())


# Readiness
def healthy(request):
    return PlainTextResponse("OK")


# Liveness
def healthz(request):
    return PlainTextResponse(probe_liveness())


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
        "playlist": playlist_items,
        "uris": [item.get("uri") for item in playlist_items],
        "streams": globals().get('stream').streams if 'stream' in globals() else [],
    }
    return JSONResponse(data)

def display_screenshot(request):
    fn = screenshot()
    if not fn or not os.path.exists(fn):
        logging.error(f"screenshot: capture failed or file does not exist")
        metrics.inc("iss_display_screenshot_failures_total")
        return PlainTextResponse("Not Found", status_code=404)
    return FileResponse(fn, media_type='image/png')

def api_screenshot(request):
    return display_screenshot(request)

def list_routes(app_instance=None):
    """
    Return a list of all registered routes in the ASGI app.
    Each entry contains: path, methods, and route name.
    """
    app_instance = app_instance or app

    def _walk(routes, prefix=""):
        collected = []
        for r in routes:
            # Starlette Mount has .routes for sub-paths
            subroutes = getattr(r, "routes", None)
            if subroutes is not None:
                sub_prefix = prefix + getattr(r, "path", "")
                collected.extend(_walk(subroutes, sub_prefix))
                continue

            path = prefix + getattr(r, "path", "")
            methods = sorted(list(getattr(r, "methods", set()))) if hasattr(r, "methods") else []
            name = getattr(r, "name", "")
            collected.append({
                "path": path,
                "methods": methods,
                "name": name,
            })
        return collected

    return _walk(app_instance.routes)

def api_routes(request):
    return JSONResponse(list_routes())

# Counted by the route's own path, not the requested one, so path parameters
# stay grouped and an unmatched request cannot add a key of its choosing
request_counts = collections.Counter()
request_counts_lock = threading.Lock()

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
        with request_counts_lock:
            request_counts[path] += 1
        metrics.inc("iss_display_http_requests_total", path=path)
        started = time.time()
        try:
            await self.app(scope, receive, send)
        finally:
            metrics.inc("iss_display_http_request_duration_seconds_sum",
                        time.time() - started, path=path)
            metrics.inc("iss_display_http_request_duration_seconds_count", path=path)

def api_requests(request):
    with request_counts_lock:
        counts = dict(request_counts)

    return JSONResponse({"total": sum(counts.values()),
                         "paths": dict(sorted(counts.items()))})

def api_metrics(request):
    body = render_metrics(metrics.snapshot(), Display.query_metrics())

    return PlainTextResponse(body,
                             media_type="text/plain; version=0.0.4; charset=utf-8")

def api_resolution(request):
    mode = request.path_params.get("mode")
    display_global = globals().get('display') if 'display' in globals() else None
    if display_global:
        return JSONResponse(display_global.set_resolution(mode))

    reply = Display.set_output_resolution(mode)
    if reply is None:
        return JSONResponse({"error": "display not reachable"}, status_code=503)

    return JSONResponse(reply)

def api_settings(request):
    name = request.query_params.get("name")
    default_time = request.query_params.get("default_time")
    display_global = globals().get('display') if 'display' in globals() else None

    result = {}
    for value, local, remote in ((name, "set_playlist_name", "set_name"),
                                 (default_time, "set_default_play_time", "set_default_time")):
        if value is None:
            continue
        if display_global:
            reply = getattr(display_global, local)(value)
        else:
            reply = getattr(Display, remote)(value)
        if reply is None:
            return JSONResponse({"error": "display not reachable"}, status_code=503)
        result.update(reply)

    if not result:
        return JSONResponse({"error": "nothing to set"}, status_code=400)

    return JSONResponse(result)

def api_step(step):
    display_global = globals().get('display') if 'display' in globals() else None
    if display_global:
        return JSONResponse(display_global.step_rotation(step))

    reply = Display.step(step)
    if reply is None:
        return JSONResponse({"error": "display not reachable"}, status_code=503)

    return JSONResponse(reply)

def api_next(request):
    return api_step(1)

def api_previous(request):
    return api_step(-1)

def api_playlist_add(request):
    uri = request.query_params.get("uri")
    play_time_s = request.query_params.get("t")
    if uri and play_time_s and "t=" not in urlsplit(uri).query:
        joiner = "&" if urlsplit(uri).query else "?"
        uri = f"{uri}{joiner}t={play_time_s}"
    display_global = globals().get('display') if 'display' in globals() else None
    if display_global:
        return JSONResponse(display_global.add_playlist_item(uri))

    reply = Display.add_item(uri)
    if reply is None:
        return JSONResponse({"error": "display not reachable"}, status_code=503)

    return JSONResponse(reply)

def api_playlist_toggle(request):
    num = request.path_params.get("num")
    pl_global = globals().get('display') if 'display' in globals() else None
    if pl_global:
        return JSONResponse(pl_global.toggle_playlist_item(num))

    reply = Display.toggle_item(num)
    if reply is None:
        return JSONResponse({"error": "display not reachable"}, status_code=503)

    return JSONResponse(reply)

def probe_liveness():
    return "OK"

# Prepare ASGI app for API and web ui
app = Starlette(routes=[
    Route("/", web_main, methods=["GET"], name="web_main"),
    Route("/display", web_display, methods=["GET"], name="web_display"),
    Route("/healthy", healthy, methods=["GET"], name="healthy"),
    Route("/healthz", healthz, methods=["GET"], name="healthz"),
    Route("/api/v1/display", screen, methods=["GET"], name="api_display"),
    Route("/screenshot", display_screenshot, methods=["GET"], name="screenshot"),
    Route("/api/v1/screenshot", api_screenshot, methods=["GET"], name="api_screenshot"),
    Route("/api/v1/routes", api_routes, methods=["GET"], name="api_routes"),
    Route("/api/v1/settings", api_settings, methods=["POST"], name="api_settings"),
    Route("/api/v1/next", api_next, methods=["POST"], name="api_next"),
    Route("/api/v1/previous", api_previous, methods=["POST"], name="api_previous"),
    Route("/api/v1/playlist", api_playlist_add,
          methods=["POST"], name="api_playlist_add"),
    Route("/api/v1/playlist/{num:int}/toggle", api_playlist_toggle,
          methods=["POST"], name="api_playlist_toggle"),
    Route("/api/v1/requests", api_requests, methods=["GET"], name="api_requests"),
    Route("/api/v1/resolution/{mode}", api_resolution,
          methods=["POST"], name="api_resolution"),
    Route("/metrics", api_metrics, methods=["GET"], name="metrics"),
], middleware=[Middleware(RequestCounter)])
asgi_app = app


def which(cmd):
    """Return absolute path to executable or None if not found."""
    return shutil.which(cmd)

def download_file(url, path, use_curl=True):
    logging.info(f"Downloading: {url}")

    if not use_curl:
        # Use requests to download
        response = requests.get(url)

        with open(path, "wb") as file:
           file.write(response.content)

    else:
          cmd = ['curl', '-LO', '--create-dirs', '--output-dir', path,  url]

          Popen(cmd,
              env=env,
              start_new_session=True,
              close_fds=True)

    return True


def reexec_self():
    logging.info("Restarting after update..")
    # Re-execute the current script
    os.execv(sys.executable, [sys.executable] + sys.argv)


def create_playlist_item_crd(num, uri, player, playtime_s, name="playlistitem-sample", namespace="default"):
    """
    Create a Kubernetes CRD manifest for a PlaylistItem.

    Args:
        num (int): The item number.
        uri (str): The URI of the item.
        player (str): The player type.
        playtime_s (int): Play time in seconds.
        name (str): Name of the PlaylistItem resource.
        namespace (str): Namespace for the resource.

    Returns:
        dict: The CRD manifest as a Python dictionary.
    """
    return {
        "apiVersion": "example.com/v1",
        "kind": "PlaylistItem",
        "metadata": {
            "name": name,
            "namespace": namespace
        },
        "spec": {
            "num": num,
            "uri": uri,
            "player": player,
            "playtime_s": playtime_s
        }
    }

def draw_apod(output='terminal', center=False, img_bg=False):
    """
    Draws the Astronomy Picture of the Day (APOD) for the current day.
    Args:
        output (str): 'terminal' to print to terminal, 'wayland-view' to render via Wayland_view.
        center (bool): Center the APOD in the terminal (only for terminal output).
        img_bg (bool): Use an image background if available.
    """
    # Use configured API key if available
    key = globals().get('apod_api_key') or os.environ.get('APOD_API_KEY') or 'DEMO_KEY'
    apod = APOD(api_key=key)
    img_path, desc = apod.apod_data()
    if not img_path or not desc:
        logging.error("Failed to fetch APOD data.")
        metrics.inc("iss_display_fetch_failures_total", source="apod")
        return

    if output == 'terminal':
        print(desc)
    elif output == 'wayland-view':
        wv = Wayland_view(display.res_x, display.res_y, 1, theme)
        wv.s_objects[0]["font_size"] = 20
        wv.s_objects[0]["alignment"] = "center"
        wv.show_image(img_path)

def draw_calendar(output='terminal', view_x_res=None, center=False, img_bg=False):
    """
    Draws a calendar for the current month, highlighting the current day and day name.
    Args:
        output (str): 'terminal' to print to terminal, 'wayland-view' to render via Wayland_view.
        view_x_res (int): X resolution for wayland-view output (required if output='wayland-view').
        center (bool): Center the calendar in the terminal (only for terminal output).
    """
    import calendar
    import datetime
    import re

    today = datetime.date.today()
    cal = calendar.TextCalendar(calendar.MONDAY)
    month_str = cal.formatmonth(today.year, today.month)
    lines = month_str.split('\n')
    highlighted_lines = []
    day_str = str(today.day).rjust(2)
    day_name = today.strftime("%A")

    # Insert an empty line between the month name and the day names row
    if len(lines) > 1:
        lines = lines[:1] + [''] + lines[1:]

    def highlight(match):
        if output == 'terminal':
            return f"\033[1;7m{match.group(0)}\033[0m"
        elif output == 'wayland-view':
            font_face = "Monospace"
            font_size = 20
            font_face_hilight = "Monospace"
            font_size_hilight = 23
            markup = (
                f"</span><span foreground=\"orange\" font=\"{font_face_hilight} {font_size_hilight}\">{match.group(0)}</span>"
                f"<span foreground=\"white\" font=\"{font_face} {font_size}\">"
            )
            return markup

    def highlight_day_name(match):
        if output == 'terminal':
            return f"\033[1;7m{match.group(0)}\033[0m"
        elif output == 'wayland-view':
            return f"<span foreground=\"orange\" font=\"Monospace 23\">{match.group(0)}</span>"

    for line in lines:
        # Apply highlights if present
        line = re.sub(rf'(?<!\d){day_str}(?!\d)', highlight, line)
        line = re.sub(rf'\b{today.strftime("%a")}\b', highlight_day_name, line)
        highlighted_lines.append(line)

    if output == 'terminal':
        if center:
            # Get terminal width
            try:
                width = shutil.get_terminal_size((80, 20)).columns
            except Exception:
                width = 80

            centered_lines = []

            for line in highlighted_lines:
                # Remove ANSI codes for length calculation
                line_stripped = re.sub(r'\033\[[0-9;]*m', '', line)
                pad = max((width - len(line_stripped)) // 2, 0)
                centered_lines.append(' ' * pad + line)

            print("\n".join(centered_lines))
        else:
            print("\n".join(highlighted_lines))

    elif output == 'wayland-view':
        texts = []
        texts.append("\n".join(highlighted_lines))
        # Add next 3 bank holidays under the calendar
        holidays = Calendar.next_bank_holidays(location="Germany", count=3)
        if holidays:
            holiday_lines = []
            for h in holidays:
                # Format: "YYYY-MM-DD: Holiday Name (Local Name)"
                holiday_lines.append(f"{h['date']}: {h['name']} ({h['localName']})")
            texts.append("\n".join(holiday_lines))
        view = Wayland_view(display.res_x, display.res_y, len(texts), theme)
        view.s_objects[0]["font_size"] = 20
        view.s_objects[0]["alignment"] = "left"
        if len(texts) > 1:
            view.s_objects[1]["font_size"] = 16
            view.s_objects[1]["alignment"] = "left"
        view.show_content(texts, img_bg, html_escape=False)


class Zeroconf_service:

    def __init__(self,
                 name_prefix,
                 service_type,
                 hostname,
                 listen_address,
                 listen_port,
                 properties):

        # Support both IPv4/IPv6; pack address according to the provided address
        ip_version = IPVersion.All
        self.service_type = service_type
        self.service_name = name_prefix + "-" + \
            hostname + "." + \
            self.service_type

        # The zeroconf service data to publish on the network
        try:
            addr_obj = ipaddress.ip_address(listen_address)
            af = socket.AF_INET6 if addr_obj.version == 6 else socket.AF_INET
            packed = socket.inet_pton(af, listen_address)
            addrs = [packed]
        except Exception:
            logging.warning(f"zeroconf: invalid listen_address '{listen_address}', defaulting to 127.0.0.1")
            addrs = [socket.inet_pton(socket.AF_INET, '127.0.0.1')]

        self.zc_service = ServiceInfo(
            self.service_type,
            self.service_name,
            addresses=addrs,
            port=int(listen_port),
            properties=properties,
            server=hostname + ".local.",
        )

        self.zc = Zeroconf(ip_version=ip_version)

    def zc_register_service(self):
        self.zc.register_service(self.zc_service)

        return True

    def zc_unregister_service(self):
        self.zc.unregister_service(self.zc_service)
        self.zc.close()

        return True


class Theme:

    def __init__(self, name="default"):
        self.name = name
        self.font = ""
        self.font_face = "Monospace"
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
        self.playlist = list()
        self.playlist = self.create(uris)
        self.uris = self.get_uris()

    our_params = ("t", "enabled")

    @classmethod
    def split_params(cls, uri):
        parts = urlsplit(uri)
        if not parts.query:
            return uri, {}

        query = parse_qsl(parts.query, keep_blank_values=True)
        kept = [(k, v) for k, v in query if k not in cls.our_params]
        if len(kept) == len(query):
            return uri, {}

        params = {k: v for k, v in query if k in cls.our_params}

        return urlunsplit(parts._replace(query=urlencode(kept))), params

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

    def create(self, uris):
        n = 0
        playlist = list()

        for uri in uris:
            item = {}
            n += 1
            uri, params = self.split_params(uri)
            play_time_s = self.parse_play_time(params, uri)
            enabled = self.parse_enabled(params, uri)
            if uri.endswith(".m3u8"):
                item["num"] = n
                item["uri"] = uri
                item["player"] = "mediaplayer"
                item["play_time_s"] = self.default_play_time_s
            elif uri.startswith("https://") and any(uri.rstrip('/').lower().endswith(s) for s in ('/rss', '/feed', '/atom', '.rss', '.atom', '.xml')):
                item["num"] = n
                item["uri"] = uri
                item["player"] = "news"
                item["news"] = RSSFeed(uri)
                item["play_time_s"] = self.default_play_time_s
            elif uri.startswith("https://"):
                item["num"] = n
                item["uri"] = uri
                item["player"] = "browser"
                item["play_time_s"] = self.default_play_time_s
            elif uri.startswith("iss://apod"):
                item["num"] = n
                item["uri"] = uri
                item["player"] = "apod"
                item["play_time_s"] = self.default_play_time_s
            elif uri.startswith("iss://cal"):
                item["num"] = n
                item["uri"] = uri
                item["player"] = "calendar"
                item["play_time_s"] = self.default_play_time_s
            elif uri.startswith("iss://clock"):
                item["num"] = n
                item["uri"] = uri
                item["player"] = "clock"
                item["play_time_s"] = self.default_play_time_s
            elif uri.startswith("iss://mqtt"):
                item["num"] = n
                item["uri"] = uri
                item["player"] = "mqtt"
                item["play_time_s"] = self.default_play_time_s
            elif uri.startswith("iss://music"):
                item["num"] = n
                item["uri"] = uri
                item["player"] = "music"
                item["play_time_s"] = self.default_play_time_s
            elif uri.startswith("iss://network-sockets"):
                item["num"] = n
                item["uri"] = uri
                item["player"] = "sockets"
                item["play_time_s"] = self.default_play_time_s
            elif uri.startswith("iss://network"):
                item["num"] = n
                item["uri"] = uri
                item["player"] = "network"
                item["play_time_s"] = self.default_play_time_s
            elif uri.startswith("iss://news"):
                news_sources = {"hn" : "",
                                "db" : "/home/mue/.local/share/russ/feeds.db"}
                self.news = News(news_sources)
                item["num"] = n
                item["uri"] = uri
                item["player"] = "news"
                item["play_time_s"] = self.default_play_time_s
            elif uri.startswith("iss://otd"):
                otd_sources = {"wikipedia" : ""}
                self.otd = OTD(otd_sources)
                item["num"] = n
                item["uri"] = uri
                item["player"] = "onthisday"
                item["play_time_s"] = self.default_play_time_s
            elif uri.startswith("iss://proc"):
                item["num"] = n
                item["uri"] = uri
                item["player"] = "processes"
                item["play_time_s"] = self.default_play_time_s
            elif uri.startswith("iss://top"):
                item["num"] = n
                item["uri"] = uri
                item["player"] = "top"
                item["play_time_s"] = self.default_play_time_s
            elif uri.startswith("iss://system"):
                item["num"] = n
                item["uri"] = uri
                item["player"] = "system"
                item["play_time_s"] = self.default_play_time_s
            elif uri.startswith("iss://weather"):
                self.weather = Weather(self.location)
                item["num"] = n
                item["uri"] = uri
                item["player"] = "weather"
                item["play_time_s"] = self.default_play_time_s
            elif uri.startswith("iss://playlist"):
                item["num"] = n
                item["uri"] = uri
                item["player"] = "playlist"
                item["play_time_s"] = self.default_play_time_s
            elif uri.endswith(".svg"):
                download_file(uri.strip(), self.download_path)
                file = uri.split("/")
                item["num"] = n
                item["uri"] = "file:///" + self.download_path + file[-1]
                item["player"] = "imageviewer"
                item["play_time_s"] = self.default_play_time_s
            elif uri.endswith(".jpg"):
                item["num"] = n
                item["uri"] = uri
                item["player"] = "imageviewer"
                item["play_time_s"] = self.default_play_time_s

            # Append if we found a valid playlist item
            if "num" in item:
                item["play_time_explicit"] = bool(play_time_s)
                if play_time_s:
                    item["play_time_s"] = play_time_s
                item["enabled"] = enabled
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
        self.uris = self.get_uris()

        return item

    def get_uris(self):
        uris = []
        for item in self.playlist:
            uris.append(item["uri"])
        return uris

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

    def start_item(self, item, probe_ip_address=None):
        probe_ip_address = probe_ip_address or self.probe_ip_address
        x = None
        if item["player"] == "apod":
            x = threading.Thread(target=self.start_apod,
                                 args=())
        elif item["player"] == "browser":
            # start_browser() wants a list
            # but we want to start an instance for each URL
            urls = list()
            urls.append(item["uri"])
            x = threading.Thread(target=self.start_browser,
                                 args=(urls,))
        elif item["player"] == "calendar":
            x = threading.Thread(target=self.start_calendar,
                                 args=())
        elif item["player"] == "clock":
            x = threading.Thread(target=self.start_clock,
                                 args=())
        elif item["player"] == "imageviewer":
            x = threading.Thread(target=self.start_image_view,
                                 args=(item["uri"],))
        elif item["player"] == "mqtt":
            x = threading.Thread(target=self.start_mqtt_views,
                                 args=(self.topics,
                                       self.theme,))
        elif item["player"] == "music":
            x = threading.Thread(target=self.start_music_view,
                                 args=(self.theme.img_bg,))
        elif item["player"] == "network":
            x = threading.Thread(target=self.start_net_view,
                                 args=(self.theme.img_bg,
                                       probe_ip_address))
        elif item["player"] == "news":
            x = threading.Thread(target=self.start_news_view,
                                 args=(item.get("news") or self.news,
                                       self.theme.img_bg,))
        elif item["player"] == "onthisday":
            x = threading.Thread(target=self.start_onthisday_view,
                                 args=(self.otd,
                                       self.theme.img_bg,))
        elif item["player"] == "playlist":
            x = threading.Thread(target=self.start_playlist_view,
                                 args=(self.theme.img_bg,))
        elif item["player"] == "processes":
            x = threading.Thread(target=self.start_proc_view,
                                 args=(self.theme.img_bg,))
        elif item["player"] == "sockets":
            x = threading.Thread(target=self.start_sockets_view,
                                 args=(self.theme.img_bg,))
        elif item["player"] == "top":
            x = threading.Thread(target=self.start_top_view,
                                 args=(self.theme.img_bg,))
        elif item["player"] == "system":
            x = threading.Thread(target=self.start_sys_view,
                                 args=(self.theme.img_bg,
                                       probe_ip_address))
        elif item["player"] == "weather":
            x = threading.Thread(target=self.start_weather_view,
                                 args=(self.weather,
                                       self.theme.img_bg,))

        if x is None:
            logging.warning(f"No player for {item['player']}, item {item['num']}")

            return None

        x.playlist_item = item
        x.start()
        if not x.is_alive():
            logging.error(f"Failed to start {item['player']}")

            return None

        item["started"] = True

        return x

    def start_apod(self, center=False, img_bg=False):
        logging.info("Starting APOD")
        draw_apod('wayland-view', center=center, img_bg=img_bg)

    def start_browser(self, urls):
        cmd = [sys.executable, '-m', 'webdriver_util']
        for url in urls:
            cmd.append("--url")
            cmd.append(url)

        # Disable Selenium Manager and provide explicit driver/browser paths
        env_mod = env.copy()
        env_mod['SE_DISABLE_DRIVER_MANAGEMENT'] = '1'
        env_mod['GECKODRIVER'] = env_mod.get('GECKODRIVER', '/usr/bin/geckodriver')
        env_mod['FIREFOX_BIN'] = env_mod.get('FIREFOX_BIN', '/usr/bin/firefox')

        Popen(cmd,
              env=env_mod,
              shell=False,
              start_new_session=True,
              close_fds=True,
              encoding='utf8')

        return True

    def start_calendar(self, center=False, img_bg=False):
        logging.info("Starting calendar")
        draw_calendar('wayland-view', center=center, img_bg=img_bg)

    def start_clock(self):
        logging.info("Starting clock")
        cmd = [cmds["clock"]]

        try:
            Popen(cmd,
                  env=env,
                  start_new_session=True,
                  close_fds=True)
            return True
        except Exception as e:
            logging.error(f"Clock start failed: {e}")
            return False

    def start_image_viewer(self, file):
        logging.info(f"Starting image viewer with file: {file}")
        cmd = [cmds["image_viewer"],  file]

        Popen(cmd,
              env=env,
              start_new_session=True,
              close_fds=True)

        return True

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
            view = Wayland_view(display.res_x, display.res_y, len(texts), theme)
            for i in range(len(texts)):
                if i == 0:
                    view.s_objects[i]["font_size"] = 64
                    view.s_objects[i]["alignment"] = "center"
                else:
                    view.s_objects[i]["font_size"] = 48
                    view.s_objects[i]["alignment"] = "center"
            view.show_content(texts, theme.img_bg)

        for topic in topics:
            mqtt.subscribe(mqttc, topic, render_mqtt_view)
            logging.info(f"MQTT: Subscribed to {topic}")

        mqttc.loop_forever()

    def start_mediaplayer(self, url):
        logging.info(f"Starting media player with stream: {url}")
        cmd = [cmds["media_player"],  url]

        Popen(cmd,
              env=env,
              start_new_session=True,
              close_fds=True,
              encoding='utf8')

        return True

    def start_net_view(self, img_bg, probe_ip_address):
        logging.info("Starting network view")
        texts = list()
        net = System.net_data(probe_ip_address)
        texts.append("Network Address " + (net["address"] or ""))
        texts.append("Network Addresses " + (net["addresses"] or ""))
        texts.append("Public IP " + (net["public_ip"] or ""))
        texts.append("resolv.conf\n" + (net["resolvconf"] or ""))
        view = Wayland_view(display.res_x, display.res_y, len(texts), theme)
        for i in range(len(texts)):
            view.s_objects[i]["font_size"] = 20
            view.s_objects[i]["alignment"] = "left"
        view.show_content(texts, img_bg)

    def start_sockets_view(self, img_bg):
        logging.info("Starting sockets view")
        texts = [System.net_sockets(20) or "No sockets"]
        view = Wayland_view(display.res_x, display.res_y, len(texts), theme)
        view.s_objects[0]["font_size"] = 20
        view.s_objects[0]["alignment"] = "left"
        view.show_content(texts, img_bg)

    def start_top_view(self, img_bg):
        logging.info("Starting top view")
        texts = [System.top(20) or "No process data"]
        view = Wayland_view(display.res_x, display.res_y, len(texts), theme)
        view.s_objects[0]["font_size"] = 20
        view.s_objects[0]["alignment"] = "left"
        view.show_content(texts, img_bg)

    def start_playlist_view(self, img_bg):
        logging.info("Starting playlist view")
        items = get_playlist_items()
        if items:
            lines = [f"{item['num']:>2}  {item['uri']:<40} {item['player']}" for item in items]
            if self.name:
                lines.insert(0, f"{self.name}\n")
            text = "\n".join(lines)
        else:
            text = "Playlist unavailable"
        texts = [text]
        view = Wayland_view(display.res_x, display.res_y, len(texts), theme)
        view.s_objects[0]["font_size"] = 20
        view.s_objects[0]["alignment"] = "left"
        view.show_content(texts, img_bg)

    def start_proc_view(self, img_bg):
        texts = list()
        texts.append(System.list_processes(23) or "Process list unavailable")
        view = Wayland_view(display.res_x, display.res_y, len(texts), theme)
        view.s_objects[0]["font_size"] = 14
        view.s_objects[0]["alignment"] = "left"
        view.show_content(texts, img_bg)

    def start_sys_view(self, img_bg, probe_ip_address):
        logging.info("Starting sys view")
        net = System.net_data(probe_ip_address)
        sys = System.sys_data()
        texts = []
        texts.append(System.os_release())
        texts.append("")  # Insert empty line after the first line
        uptime_info = System.uptime(env)
        if isinstance(uptime_info, dict):
            texts.append(uptime_info.get("uptime", ""))
            texts.append(uptime_info.get("users", ""))
            texts.append(uptime_info.get("load", ""))
        else:
            texts.append(str(uptime_info))
        texts.append(f"Display started: {display.started}")
        texts.append(sys["uptime"] or "")
        texts.append(f"Display Resolution: {display.res_x}x{display.res_y}")
        texts.append(sys["data"] or "")
        texts.append(net["address"] or "")
        texts.append(net["addresses"] or "")
        texts.append((net["online_status"] or "") + " " + (net["public_ip"] or ""))
        texts.append(f"Listen address: {display.address}:{display.port}")
        view = Wayland_view(display.res_x, display.res_y, len(texts), theme)
        for i in range(len(texts)):
            if i == 0:
                view.s_objects[i]["font_size"] = 24
            else:
                view.s_objects[i]["font_size"] = 20
            view.s_objects[i]["alignment"] = "left"
        view.show_content(texts, img_bg)

    def start_weather_view(self, weather, img_bg):
        logging.info("Starting weather view")
        texts = list()
        data, icon = weather.current_weather()

        if icon:
            texts.append(icon)

        if not data:
            texts.append("Weather data unavailable")
        else:
            try:
                texts.append(data["current_condition"][0]["temp_C"] + "°C")
                texts.append(
                    data["current_condition"][0]["weatherDesc"][0]["value"]
                    + " "
                    + data["current_condition"][0]["windspeedKmph"]
                    + " km/h"
                )
                texts.append(data["nearest_area"][0]["areaName"][0]["value"])
                texts.append("")
            except Exception as e:
                logging.error(f"weather: Unexpected data format: {e}")
                texts.append("Weather data unavailable")

        # Add sunrise and sunset for today
        sunrise_sunset_times = Calendar.sunrise_sunset(location=weather.location)
        if not sunrise_sunset_times:
            logging.info(f"No sunrise/sunset data returned for location: {weather.location}")
        elif "today" not in sunrise_sunset_times:
            logging.info(f"'today' key missing in sunrise/sunset data for location: {weather.location}: {sunrise_sunset_times}")
        else:
            sunrise = sunrise_sunset_times["today"].get("sunrise")
            sunset = sunrise_sunset_times["today"].get("sunset")
            if not sunrise:
                logging.info(f"No 'sunrise' value in sunrise/sunset data for location: {weather.location}: {sunrise_sunset_times['today']}")
            if not sunset:
                logging.info(f"No 'sunset' value in sunrise/sunset data for location: {weather.location}: {sunrise_sunset_times['today']}")
            if sunrise and sunset:
                texts.append(f"Sunrise {sunrise}  Sunset {sunset}")

        # Add moon phase and icon
        moon_phase, moon_icon, days_until_full_moon = Calendar.moonphase(location=weather.location)
        moon_unicode_map = {
            "New Moon": "\U0001F311",
            "Waxing Crescent": "\U0001F312",
            "First Quarter": "\U0001F313",
            "Waxing Gibbous": "\U0001F314",
            "Full Moon": "\U0001F315",
            "Waning Gibbous": "\U0001F316",
            "Last Quarter": "\U0001F317",
            "Waning Crescent": "\U0001F318"
        }
        if moon_phase:
            moon_char = moon_unicode_map.get(moon_phase, "\U0001F319")
            moon_line = f"{moon_char} Moon {moon_phase}"
            if days_until_full_moon is not None:
                moon_line += f" ({days_until_full_moon} days to full)"
            texts.append(moon_line)

        # Fetch and display UV index
        uv_index = Weather.fetch_uv_index(location=weather.location)
        if uv_index:
            texts.append(f"UV Index {uv_index}")

        view = Wayland_view(display.res_x, display.res_y, len(texts), theme)
        view.s_objects[0]["font_size"] = 80
        view.s_objects[0]["alignment"] = "left"
        view.s_objects[1]["font_size"] = 40
        view.s_objects[1]["alignment"] = "left"
        view.s_objects[2]["font_size"] = 20
        view.s_objects[2]["alignment"] = "left"
        # Optionally set font size/alignment for extra lines
        for i in range(3, len(texts)):
            view.s_objects[i]["font_size"] = 20
            view.s_objects[i]["alignment"] = "left"

        view.show_content(texts, img_bg)
        if icon:
            view.show_image(icon)

    def start_music_view(self, img_bg):
        texts = list()
        music = Music()
        music_data = music.mpd()
        texts.append(music_data if music_data else "No music data available")
        view = Wayland_view(display.res_x, display.res_y, len(texts), theme)
        view.show_content(texts, img_bg)

    def start_news_view(self, news, img_bg, refresh_interval_s=10):
        def news_texts():
            item = news.news_item()
            if not item:
                metrics.inc("iss_display_fetch_failures_total", source="news")
                return ["No news available", "", ""]

            if hasattr(news, "item_count"):
                metrics.set("iss_display_rss_items", news.item_count(),
                            feed=item.get("feed", "") or "unknown")
            rank = item.get("rank")
            title = item.get("title", "")

            return [item.get("feed", ""),
                    f"#{rank} {title}" if rank else title,
                    item.get("url", "")]

        view = Wayland_view(display.res_x, display.res_y, 3, theme)
        view.s_objects[0]["font_size"] = 30
        view.s_objects[1]["font_size"] = 60
        view.s_objects[2]["font_size"] = 30
        view.show_content(news_texts(), img_bg,
                          refresh=news_texts,
                          refresh_interval_s=refresh_interval_s)

    def start_onthisday_view(self, otd, img_bg):
        texts = list()
        item = otd.otd_item()
        if item is None:
            texts.append("No 'On This Day' data available.")
        else:
            texts.append(item["year"])
            # Add a margin (empty line) between the year and the text
            texts.append("")
            texts.append(item["text"])
        view = Wayland_view(display.res_x, display.res_y, len(texts), theme)
        for i in range(len(texts)):
            if i == 0:
                view.s_objects[i]["font_size"] = 40
            else:
                view.s_objects[i]["font_size"] = 30
        view.show_content(texts, img_bg)

    def start_image_view(self, file):
        view = Wayland_view(display.res_x, display.res_y, 1, theme)
        view.show_image(file)


class Stream():

    def __init__(self, stream_source):
        self.streams = list()

        if stream_source == "v4l2":
            # Start streaming
            logging.info("Setting up source")
            self.stream_create_v4l2_src(stream_source_device)
            logging.info("Setting up stream")
            time.sleep(3)
            self.stream_v4l2_ffmpeg()
            #gst = self.stream_setup_gstreamer(stream_source,
            #                             stream_source_device,
            #                             local_ip,
            #                             listen_port)
            #gst.stdin.close()
            #gst.wait()

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
                '!', 'pngdec',
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

    def gst_stream_images(self, gstreamer, img_path, debug=False):
        t0 = int(round(time.time() * 1000))
        n = -1

        while True:
            #filename = img_path + '/image_' + str(0).zfill(4) + '.png'
            filename = '/tmp/screenshot.png'
            t1 = int(round(time.time() * 1000))

            logging.debug(f"{filename}: {t1 - t0} ms")

            t0 = t1

            f = Path(filename)

            if not f.is_file():
                logging.info("Startup: No file yet to stream")
                logging.info("Startup: Waiting..")
                time.sleep(3)
            else:
                if -1 == n:
                    logging.info("Found first file, starting stream")
                    n = 0
                with open(filename, 'rb') as f:
                    content = f.read()
                    gstreamer.stdin.write(content)
                    time.sleep(0.1)

            if 10 == n:
                n = 0
            else:
                n += 1

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
        ffmpeg = subprocess.Popen([
            'ffmpeg', '-f', 'v4l2', '-i', '/dev/video0',
            '-codec', 'copy',
            '-f', 'mpegts', 'udp:0.0.0.0:6000'
            ], env=env)


# Timer for the wayland eventlist, which expects a nexttime attribute
# and an alarm() method. Pulls the next set of texts and repaints,
# so a window shows fresh content instead of whatever it spawned with
class Content_refresh:

    def __init__(self, wayland_view, window, refresh, interval_s, html_escape):
        item = getattr(threading.current_thread(), "playlist_item", None)
        self.source = item["player"] if item else "unknown"
        self.wayland_view = wayland_view
        self.window = window
        self.refresh = refresh
        self.interval_s = interval_s
        self.html_escape = html_escape
        self.nexttime = time.time() + interval_s
        self.lock = threading.Lock()
        self.last_texts = None

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

        metrics.inc("iss_display_content_ticks_total", player=self.source)

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
        metrics.inc("iss_display_content_refreshes_total", player=self.source)
        with last_content_refresh_lock:
            last_content_refresh[self.source] = time.time()
        self.wayland_view.set_texts(texts, self.html_escape)
        if self.window.redraw_func:
            self.window.redraw_func(self.window)


class Wayland_view:

    def __init__(self, res_x, res_y, num_objects, theme):
        # Load the main Wayland protocol.
        if os.path.isfile("/usr/share/wayland/wayland.xml"):
            wp_base = wayland.protocol.Protocol("/usr/share/wayland/wayland.xml")
        elif os.path.isfile("/usr/local/share/wayland/wayland.xml"):
            wp_base = wayland.protocol.Protocol("/usr/local/share/wayland/wayland.xml")
        else:
            logging.error("wayland: Failed to find wayland protocol xml")
            sys.exit(1)

        if os.path.isfile("/usr/share/wayland-protocols/stable/xdg-shell/xdg-shell.xml"):
            wp_xdg_shell = wayland.protocol.Protocol(
                "/usr/share/wayland-protocols/stable/xdg-shell/xdg-shell.xml")
        elif os.path.isfile("/usr/local/share/wayland-protocols/stable/xdg-shell/xdg-shell.xml"):
            wp_xdg_shell = wayland.protocol.Protocol(
                "/usr/local/share/wayland-protocols/stable/xdg-shell/xdg-shell.xml")
        else:
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

        item = getattr(threading.current_thread(), "playlist_item", None)
        self.window = {}
        self.window["res_x"] = res_x
        self.window["res_y"] = res_y
        self.window["title"] = f"{item['player']}-{item['num']}" if item else "iss-view"

        s_object = {"alignment": "center",
                    "offset_x": 10,
                    "offset_y": 10,
                    "bg_alpha": 1,
                    #"bg_colour_r": 7,
                    #"bg_colour_g": 59,
                    #"bg_colour_b": 76,
                    "bg_colour_r": 40,
                    "bg_colour_g": 15,
                    "bg_colour_b": 40,
                    "font": theme.font,
                    "font_face": theme.font_face,
                    "font_size": 60,
                    "font_colour_r": 0,
                    "font_colour_g": 0,
                    "font_colour_b": 0,
                    "file": "",
                    "img_scale_up": True,
                    "img_scale_down": True,
                    "text": list()}

        self.s_objects = list()
        for n in range(0, num_objects):
            logging.info("view: Appending drawing object")
            self.s_objects.append(s_object.copy())

    def create_window(self, w):
        metrics.inc("iss_display_views_running")
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
        n = 0
        for text in texts:
            if n >= len(self.s_objects):
                logging.debug(f"view: Ignoring text block {n}, "
                              f"have {len(self.s_objects)} drawing object(s)")
                break
            logging.debug(f"Showing text: {text}")
            if html_escape:
                self.s_objects[n]["text"] = html.escape(str(text))
            else:
                self.s_objects[n]["text"] = str(text)
            n += 1

        # Clear any objects the new texts do not cover,
        # so nothing lingers from the previous content
        for i in range(n, len(self.s_objects)):
            self.s_objects[i]["text"] = ""

    def show_content(self, texts, img_bg=False, fullscreen=False, html_escape=True,
                     refresh=None, refresh_interval_s=None):
        logging.info(f"view: Have {len(texts)} text block(s)")

        self.set_texts(texts, html_escape)

        # Log the s_objects for debugging
        for idx, obj in enumerate(self.s_objects):
            if obj.get("file"):
                logging.info(f"s_objects[{idx}] has file: {obj['file']}")
            else:
                logging.debug(f"s_objects[{idx}] has no file field or is empty")

        # After all assignments, check for any file fields
        use_images = any(obj.get("file") for obj in self.s_objects)
        if use_images:
            if img_bg:
                self.s_objects[0]["file"] = img_bg
            logging.info("Using draw_images_with_text for rendering (at least one s_object has a file)")
            draw_function = view.draw_images_with_text
        else:
            logging.info("Using draw_text for rendering (no s_object has a file)")
            draw_function = view.draw_text

        w = view.Window(self.conn,
                        self.window,
                        self.s_objects,
                        redraw=draw_function,
                        fullscreen=fullscreen,
                        class_="iss-view")

        if refresh and refresh_interval_s:
            self.conn.eventlist.append(Content_refresh(self, w, refresh,
                                                       refresh_interval_s, html_escape))
            logging.info(f"view: Refreshing content every {refresh_interval_s}s")

        self.create_window(w)

    def show_image(self, img_file, fullscreen=False):
        self.s_objects[0]["text"] = list()
        self.s_objects[0]["file"] = img_file
        self.s_objects[0]["bg_alpha"] = 0
        self.s_objects[0]["offset_y"] = 0
        w = view.Window(self.conn,
                        self.window,
                        self.s_objects,
                        redraw=view.draw_image,
                        fullscreen=fullscreen,
                        class_="iss-view")

        self.create_window(w)


class Display:

    # The app_ids of the windows we spawn ourselves, the only ones we cycle
    window_app_ids = ("iss-view", "firefox")
    # Every window we cycle gets a workspace to itself, named with this prefix
    workspace_prefix = "iss-"
    # Where the state server binds and where its clients look for it
    default_state_udp_host = "127.0.0.1"
    default_state_udp_port = 7042

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
        self.skip_event = threading.Event()
        self.play_items = dict()
        self.playlist = None
        self.screenshot_path = "/tmp"
        self.screenshot_file = "screenshot.png"
        self.socket_path = self.get_socket_path()

        resolution = self.output_resolution()
        if resolution:
            self.res_x, self.res_y = resolution
        # UDP state server defaults
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
        self._state_server_stop.set()
        # Kick the socket to unblock if waiting
        try:
            target_host = self.state_udp_host
            target_port = self.state_udp_port
            try:
                ip_obj = ipaddress.ip_address(target_host)
                family = socket.AF_INET6 if ip_obj.version == 6 else socket.AF_INET
            except ValueError:
                try:
                    infos = socket.getaddrinfo(target_host, target_port, socket.AF_UNSPEC, socket.SOCK_DGRAM)
                    chosen = (
                        next((ai for ai in infos if ai[0] == socket.AF_INET6), None)
                        or next((ai for ai in infos if ai[0] == socket.AF_INET), None)
                    )
                    if chosen:
                        family = chosen[0]
                        target_host = chosen[4][0]
                        target_port = chosen[4][1]
                    else:
                        family = socket.AF_INET
                except Exception:
                    family = socket.AF_INET

            with socket.socket(family, socket.SOCK_DGRAM) as s:
                s.sendto(b"STOP", (target_host, target_port))
        except Exception:
            pass
        if self._state_server_thread:
            self._state_server_thread.join(timeout=1.0)
        if self._state_server_sock:
            try:
                self._state_server_sock.close()
            except Exception:
                pass
        logging.info("Stopped Display UDP state server")
        return True

    def _udp_state_server_loop(self):
        sock = None
        bind_host = self.state_udp_host
        bind_port = self.state_udp_port
        try:
            logging.info(f"Display UDP server binding to {bind_host}:{bind_port}")
            # Validate host and choose AF based on IPv4/IPv6
            try:
                ipaddress.ip_address(bind_host)
            except ValueError:
                logging.warning(f"Display UDP server: invalid bind host '{bind_host}', falling back to 0.0.0.0")
                bind_host = '0.0.0.0'

            family = socket.AF_INET6 if ':' in bind_host else socket.AF_INET
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

            while not self._state_server_stop.is_set():
                try:
                    data, addr = self._state_server_sock.recvfrom(4096)
                except socket.timeout:
                    continue
                except Exception as e:
                    logging.error(f"Display UDP server recv error: {e}")
                    continue

                if not data:
                    continue
                msg = data.decode('utf-8', errors='ignore').strip()
                if msg == 'GET_STATE':
                    metrics.inc("iss_display_state_commands_total", command="GET_STATE")
                    payload = {
                        'address': self.address,
                        'port': self.port,
                        'res_x': self.res_x,
                        'res_y': self.res_y,
                        'playlist_name': self.playlist.name if self.playlist else "",
                        'default_play_time_s': (self.playlist.default_play_time_s
                                                if self.playlist else 0),
                    }
                    self.send_json(addr, payload)
                elif msg == 'GET_METRICS':
                    metrics.inc("iss_display_state_commands_total", command="GET_METRICS")
                    self.send_json(addr, self.metrics_snapshot())
                elif msg == 'GET_PLAYLIST':
                    metrics.inc("iss_display_state_commands_total", command="GET_PLAYLIST")
                    payload = {'playlist': self.playlist_items()}
                    self.send_json(addr, payload)
                elif msg == 'NEXT':
                    metrics.inc("iss_display_state_commands_total", command="NEXT")
                    self.send_json(addr, self.step_rotation(1))
                elif msg == 'PREVIOUS':
                    metrics.inc("iss_display_state_commands_total", command="PREVIOUS")
                    self.send_json(addr, self.step_rotation(-1))
                elif msg.startswith('DEFAULT_TIME '):
                    metrics.inc("iss_display_state_commands_total", command="DEFAULT_TIME")
                    self.send_json(addr,
                                   self.set_default_play_time(msg.split(None, 1)[1].strip()))
                elif msg.startswith('NAME'):
                    metrics.inc("iss_display_state_commands_total", command="NAME")
                    parts = msg.split(None, 1)
                    payload = self.set_playlist_name(parts[1] if len(parts) > 1 else "")
                    self.send_json(addr, payload)
                elif msg.startswith('ADD '):
                    metrics.inc("iss_display_state_commands_total", command="ADD")
                    payload = self.add_playlist_item(msg.split(None, 1)[1].strip())
                    self.send_json(addr, payload)
                elif msg.startswith('RESOLUTION '):
                    metrics.inc("iss_display_state_commands_total", command="RESOLUTION")
                    payload = self.set_resolution(msg.split(None, 1)[1].strip())
                    self.send_json(addr, payload)
                elif msg.startswith('TOGGLE '):
                    metrics.inc("iss_display_state_commands_total", command="TOGGLE")
                    payload = self.toggle_playlist_item(msg.split(None, 1)[1].strip())
                    self.send_json(addr, payload)
                elif msg == 'STOP':
                    break
                else:
                    # Ignore unknown messages
                    pass
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
            try:
                ip_obj = ipaddress.ip_address(host)
                family = socket.AF_INET6 if ip_obj.version == 6 else socket.AF_INET
            except ValueError:
                family = socket.AF_INET

            with socket.socket(family, socket.SOCK_DGRAM) as s:
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
    def set_default_time(play_time_s, host=None, port=None, timeout=2.0):
        return Display.send_command(f'DEFAULT_TIME {play_time_s}', host, port, timeout)

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

        started = 0
        for item in self.playlist.playlist:
            if not item.get("enabled", True):
                continue
            item["started"] = False
            if self.playlist.start_item(item):
                started += 1

        logging.info(f"display: Restarted {started} view(s)")

        return started

    def set_resolution(self, mode):
        match = re.fullmatch(r"(\d{3,5})x(\d{3,5})", str(mode or "").strip())
        if not match:
            return {"error": f"bad resolution {mode!r}"}

        try:
            cmd = ['swaymsg', '-s', self.socket_path, '-t', 'get_outputs']
            outputs = json.loads(self.swaymsg_send_message(
                cmd, env=env, log_prefix="set_resolution"))
        except Exception as e:
            return {"error": f"could not read outputs: {e}"}

        if not outputs:
            return {"error": "no outputs"}

        name = outputs[0].get('name')
        was = (self.res_x, self.res_y)
        cmd = ['swaymsg', '-s', self.socket_path,
               'output', name, 'resolution', f"{match.group(1)}x{match.group(2)}"]
        self.swaymsg_send_message(cmd, env=env, log_prefix="set_resolution")

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
            cmd = ['swaymsg', '-s', self.socket_path, '-t', 'get_outputs']
            out = self.swaymsg_send_message(cmd, env=env, log_prefix="output_resolution")
            for output in json.loads(out):
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

        path = p.communicate()
        path = path[0].rstrip()
        #path = "/tmp/sway.sock"
        return path

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

    def get_windows_whitelist(self):
        windows = self.get_windows(self.window_blacklist)
        # The blacklist is a snapshot taken at startup, which races anything
        # the compositor is still mapping, so match on what we spawn instead.
        # Keeps the background and any foreign window out of the rotation
        windows = [w for w in windows
                   if w.get("app_id") in self.window_app_ids
                   and (self.window_item(w) or {}).get("enabled", True)]
        logging.debug(f"display: {len(windows)} windows in whitelist")

        return windows

    def get_windows(self, blacklist=None):
        cmd = ['swaymsg', '-s', self.socket_path, '-t', 'get_tree']
        windows = []

        p = subprocess.Popen(cmd,
                             shell=False,
                             stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE,
                             encoding='utf8',
                             env=env)

        out, err = p.communicate()

        if out:
            logging.debug(f"get_windows: swaymsg stdout: {out}")
        if err:
            logging.warning(f"get_windows: swaymsg stderr: {err}")
        data = json.loads(out)

        for output in data['nodes']:
            if output.get('type') == 'output':
                workspaces = output.get('nodes')
                for ws in workspaces:
                    if ws.get('type') == 'workspace':
                        windows += self.workspace_nodes(ws)

        if blacklist:
            return [w for w in windows if w.get("id") not in blacklist]

        return windows

    # Extracts all windows from sway workspace json
    def workspace_nodes(self, workspace):
        windows = []
        floating_nodes = workspace.get('floating_nodes')
        for floating_node in floating_nodes:
            windows.append(floating_node)
        nodes = workspace.get('nodes')
        stack = list(nodes)
        while stack:
            node = stack.pop()
            children = node.get('nodes')
            if not children:
                windows.append(node)
            else:
                for inner_node in children:
                    stack.append(inner_node)
        return windows

    def active_window(self):
        """Return the active window id or None if unavailable."""
        try:
            cmd = ['swaymsg', '-s', self.socket_path, '-t', 'get_tree']
            out = self.swaymsg_send_message(cmd, env=env, log_prefix="active_window")
            data = json.loads(out)

            def _find_focused(node):
                if not isinstance(node, dict):
                    return None
                if node.get('focused') is True and 'id' in node:
                    return node['id']
                for key in ('nodes', 'floating_nodes'):
                    for child in node.get(key, []) or []:
                        fid = _find_focused(child)
                        if fid is not None:
                            return fid
                return None

            return _find_focused(data)
        except Exception as e:
            logging.debug(f"active_window: failed to determine active window: {e}")
            return None

    # Float every window we spawn. Our views set a fixed size and ignore the
    # size the compositor asks them to take, so tiling them, which resizes
    # them to fill their workspace, leaves the buffer and the window disagreeing
    def set_window_rules(self):
        try:
            cmd = ['swaymsg', '-s', self.socket_path,
                   'for_window', '[app_id=".*"]', 'floating', 'enable']
            self.swaymsg_send_message(cmd, env=env, log_prefix="set_window_rules")
            logging.info("display: Set new windows to float")
        except Exception as e:
            logging.warning(f"display: Failed to set new windows to float: {e}")

    def window_workspace(self, win_id):
        return f"{self.workspace_prefix}{win_id}"

    # Views title their window after the playlist item they were spawned for,
    # which is how a window found in the tree is matched back to its item.
    # The browser titles its own window, so it is matched on app_id instead
    def set_playlist(self, playlist):
        self.playlist = playlist
        for item in playlist.playlist:
            if item["player"] == "browser":
                key = "firefox"
            else:
                key = f"{item['player']}-{item['num']}"
            self.play_items[key] = item

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
        metrics.set("iss_display_browser_up",
                    1 if any(w.get("app_id") == "firefox" for w in all_windows) else 0)

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
        for source, age in ages.items():
            metrics.set("iss_display_content_age_seconds", age, player=source)

        metrics.clear_gauge("iss_display_item_enabled")
        metrics.clear_gauge("iss_display_item_play_time_seconds")
        for item in (self.playlist.playlist if self.playlist else []):
            labels = {"player": item["player"], "num": item["num"]}
            metrics.set("iss_display_item_enabled",
                        1 if item.get("enabled", True) else 0, **labels)
            metrics.set("iss_display_item_play_time_seconds",
                        item.get("play_time_s", 0), **labels)

        return metrics.snapshot()

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

        return [{k: v for k, v in item.items() if k != "news"}
                for item in self.playlist.playlist]

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
            self.play_items["firefox"] = item
        else:
            self.play_items[f"{item['player']}-{item['num']}"] = item

        logging.info(f"display: Added item {item['num']}: {item['uri']}")
        if item.get("enabled", True):
            self.playlist.start_item(item)

        return {k: v for k, v in item.items() if k != "news"}

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

    def window_item(self, window):
        for key in (window.get("name"), window.get("app_id")):
            if key in self.play_items:
                return self.play_items[key]

        return None

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

    # Rebuilt every cycle rather than kept as a queue, so an item disabled or
    # added since the last switch is picked up at once, and next and previous
    # are a move of the index rather than surgery on a half consumed list
    def focus_next_window(self, t_focus_s):
        while True:
            windows = self.get_windows_whitelist()
            windows.sort(key=self.window_order)
            if not windows:
                logging.debug("display: Found no windows to switch to")
                time.sleep(t_focus_s)

                continue

            self.rotation_index %= len(windows)
            next_window = windows[self.rotation_index]
            win_id = next_window['id']
            play_time_s = self.window_play_time(next_window, t_focus_s)
            logging.info(f"display: Switching focus to: {win_id} "
                         f"({next_window.get('name')}) for {play_time_s}s")

            # One window to a workspace, so showing the next one never asks any
            # window to change size or state, only the compositor to show a
            # different workspace. A window already there is left alone, and
            # the browser needs no special case: its own fullscreen covers the
            # workspace it is alone on
            cmd = ['swaymsg', '-s', self.socket_path, f"[con_id={win_id}]",
                   'move', 'workspace', self.window_workspace(win_id)]
            self.swaymsg_send_message(cmd, env=env, log_prefix="focus_next_window")

            # Focus follows the window, so sway switches to its workspace
            cmd = ['swaymsg', '-s', self.socket_path, f"[con_id={win_id}]", 'focus']
            self.swaymsg_send_message(cmd, env=env, log_prefix="focus_next_window")

            item = self.window_item(next_window) or {}
            labels = {"player": item.get("player", "unknown"),
                      "num": item.get("num", 0)}
            metrics.inc("iss_display_window_switches_total", **labels)
            shown_from = time.time()
            self.skip_event.wait(play_time_s)
            self.skip_event.clear()
            metrics.inc("iss_display_item_shown_seconds_total",
                        time.time() - shown_from, **labels)

            step = self.rotation_step or 1
            self.rotation_step = 0
            self.rotation_index = (self.rotation_index + step) % len(windows)

    def switch_workspace(self, ws):
        cmd = ['swaymsg', '-s', self.socket_path, 'workspace', str(ws)]

        self.swaymsg_send_message(cmd, env=env, log_prefix="switch_workspace")

    def screenshot(self):
        fn = self.screenshot_path + "/" + self.screenshot_file
        return screenshot(fn)

def screenshot(path=None):
    if path is None:
        path = "/tmp/screenshot.png"
    logging.debug(f"Saving screenshot to {path}")
    cmd = [cmds["screenshot"], path]
    try:
        res = subprocess.run(cmd, env=os.environ.copy(), check=False)
    except Exception as e:
        logging.error(f"screenshot: Failed to capture: {e}")
        return None
    # Validate that the file exists and is non-empty
    try:
        if not os.path.exists(path) or os.path.getsize(path) == 0:
            logging.error("screenshot: Output file missing or empty")
            return None
    except Exception as e:
        logging.error(f"screenshot: Failed to stat output: {e}")
        return None
    return path


class Iss:

    def __init__(self, threads):
        self.threads = threads


class HtmlPage:

    resolutions = ("1024x768", "1280x800", "1366x768", "1600x900", "1920x1080")

    @staticmethod
    def get_css():
        return """
        <style>
        :root { --ambilight: #111; }
        body {
            background: var(--ambilight);
            transition: background 1.2s linear;
            margin: 0;
            padding: 1.5em;
        }
        #screenshot { max-width: 100%; display: block; }
        .player {
            position: relative;
            display: inline-block;
            line-height: 0;
        }
        .player:fullscreen {
            display: flex;
            align-items: center;
            justify-content: center;
            background: var(--ambilight);
            transition: background 1.2s linear;
        }
        .player:fullscreen #screenshot {
            max-width: 100vw;
            max-height: 100vh;
        }
        .overlay {
            position: absolute;
            inset: 0;
            display: none;
            overflow: auto;
            padding: 1em;
            box-sizing: border-box;
            background: rgba(0,0,0,0.7);
            color: #fff;
            line-height: normal;
        }
        .overlay th, .overlay td { color: #fff; }
        .controls {
            position: absolute;
            bottom: 0.75em;
            right: 0.75em;
            display: none;
            gap: 0.5em;
            z-index: 20;
        }
        .controls #pause-btn { margin-right: 1.5em; }
        .player:hover .overlay { display: flex; }
        .player:hover .controls { display: flex; }
        .overlay .tables-row {
            margin: auto;
            justify-content: center;
            align-items: flex-start;
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
        .menu button {
            background: rgba(0,0,0,0.7);
            color: #fff;
            border: 2px solid #fff;
            border-radius: 8px;
            padding: 0.25em 0.6em;
            font-weight: bold;
            cursor: pointer;
            opacity: 0.85;
        }
        .controls button {
            background: rgba(0,0,0,0.7);
            color: #fff;
            border: 2px solid #fff;
            border-radius: 8px;
            padding: 0.2em 0.5em;
            font-size: 1.8em;
            font-weight: bold;
            line-height: 1;
            cursor: pointer;
            opacity: 0.85;
        }
        .overlay button {
            background: rgba(0,0,0,0.7);
            color: #fff;
            border: 1px solid #fff;
            border-radius: 6px;
            padding: 0.1em 0.5em;
            cursor: pointer;
        }
        table td {
            padding: 0.5em 2em 0.5em 0.5em;
        }
        .tables-row {
            display: flex;
            flex-wrap: wrap;
            gap: 2em;
            margin-bottom: 1em;
        }
        .tables-row table {
            margin-bottom: 0;
        }
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
            "<thead><tr><th>#</th><th>URI</th><th>Player</th><th>Time (s)</th><th>State</th><th></th></tr></thead>",
            "<tbody>",
        ]
        for item in playlist_items:
            num = str(item.get('num', ''))
            uri = str(item.get('uri', ''))
            player = str(item.get('player', ''))
            play_time_s = str(item.get('play_time_s', ''))
            enabled = "enabled" if item.get('enabled', True) else "disabled"
            toggle = (f"<button onclick=\"toggleItem({html.escape(num)}, this)\">"
                      f"{'disable' if item.get('enabled', True) else 'enable'}</button>")
            table.append(
                f"<tr><td>{html.escape(num)}</td><td>{html.escape(uri)}</td><td>{html.escape(player)}</td><td>{html.escape(play_time_s)}</td><td class=\"state\">{html.escape(enabled)}</td><td>{toggle}</td></tr>"
            )
        table.append("</tbody>")
        table.append('<tfoot><tr><td colspan="5"></td>'
                     '<td><button onclick="addItem()" title="Add an item">+</button></td>'
                     '</tr></tfoot>')
        table.append("</table>")
        return "".join(table)

    @staticmethod
    def page_display():
        # screenshot image that refreshes via fetch, with pause button
        body = (
            '<div class="player" id="player">'
            '  <img id="screenshot" src="/api/v1/screenshot" alt="Screenshot" />'
            '  <div class="overlay"><div class="tables-row">'
            + HtmlPage.show_display_data()
            + HtmlPage.show_playlist_data()
            + '</div></div>'
            '  <div class="controls">'
            '    <button id="prev-btn" title="Previous">⏮</button>'
            '    <button id="pause-btn" title="Pause">⏸</button>'
            '    <button id="next-btn" title="Next">⏭</button>'
            '    <button id="shot-btn" title="Save screenshot">⤓</button>'
            '    <button id="fs-btn" title="Fullscreen">⛶</button>'
            '    <button id="res-btn" title="Resolution">⇲</button>'
            '    <button id="settings-btn" title="Settings">⚙</button>'
            '  </div>'
            '  <div class="menu" id="res-menu">'
            + "".join(f'<button onclick="setResolution(\'{mode}\')">{mode}</button>'
                      for mode in HtmlPage.resolutions) +
            '  </div>'
            '</div>'
            '<script>'
            'let paused = false;'
            'const player = document.getElementById("player");'
            'const btn = document.getElementById("pause-btn");'
            'const fsBtn = document.getElementById("fs-btn");'
            'const shotBtn = document.getElementById("shot-btn");'
            'const resBtn = document.getElementById("res-btn");'
            'const resMenu = document.getElementById("res-menu");'
            'resBtn.onclick = function(e) {'
            '  e.stopPropagation();'
            '  resMenu.classList.toggle("open");'
            '};'
            'async function setResolution(mode){'
            '  resMenu.classList.remove("open");'
            '  try {'
            '    await fetch("/api/v1/resolution/" + mode, {method: "POST"});'
            '  } catch(e) { }'
            '}'
            'const img = document.getElementById("screenshot");'
            'const sampler = document.createElement("canvas");'
            'const sctx = sampler.getContext("2d", {willReadFrequently: true});'
            'function ambilight(){'
            '  if(!img.naturalWidth || !img.naturalHeight) return;'
            '  const sw = 64, sh = 36, band = 2;'
            '  sampler.width = sw; sampler.height = sh;'
            '  sctx.drawImage(img, 0, 0, sw, sh);'
            '  let data;'
            '  try { data = sctx.getImageData(0, 0, sw, sh).data; } catch(e) { return; }'
            '  let r = 0, g = 0, b = 0, n = 0;'
            '  for(let y = 0; y < sh; y++){'
            '    for(let x = 0; x < sw; x++){'
            '      if(x >= band && x < sw - band && y >= band && y < sh - band) continue;'
            '      const i = (y * sw + x) * 4;'
            '      r += data[i]; g += data[i+1]; b += data[i+2]; n++;'
            '    }'
            '  }'
            '  if(!n) return;'
            '  const rgb = "rgb(" + Math.round(r/n) + "," + Math.round(g/n) + "," + Math.round(b/n) + ")";'
            '  document.documentElement.style.setProperty("--ambilight", rgb);'
            '}'
            'img.addEventListener("load", ambilight);'
            'if(img.complete) ambilight();'
            'btn.onclick = function(e) {'
            '  e.stopPropagation();'
            '  paused = !paused;'
            '  btn.innerText = paused ? "\u25B6" : "\u23F8";'
            '  btn.title = paused ? "Play" : "Pause";'
            '};'
            'shotBtn.onclick = async function(e) {'
            '  e.stopPropagation();'
            '  try {'
            '    const res = await fetch("/api/v1/screenshot", {cache: "no-store"});'
            '    if(!res.ok) return;'
            '    const blob = await res.blob();'
            '    if(!blob.size) return;'
            '    const a = document.createElement("a");'
            '    a.href = URL.createObjectURL(blob);'
            '    a.download = "iss-display.png";'
            '    a.click();'
            '    URL.revokeObjectURL(a.href);'
            '  } catch(e) { }'
            '};'
            'fsBtn.onclick = function(e) {'
            '  e.stopPropagation();'
            '  if (document.fullscreenElement) { document.exitFullscreen(); }'
            '  else { player.requestFullscreen(); }'
            '};'
            'document.addEventListener("fullscreenchange", function() {'
            '  fsBtn.title = document.fullscreenElement ? "Exit fullscreen" : "Fullscreen";'
            '});'
            'document.getElementById("prev-btn").onclick = function(e) {'
            '  e.stopPropagation();'
            '  fetch("/api/v1/previous", {method: "POST"});'
            '};'
            'document.getElementById("next-btn").onclick = function(e) {'
            '  e.stopPropagation();'
            '  fetch("/api/v1/next", {method: "POST"});'
            '};'
            'const settingsBtn = document.getElementById("settings-btn");'
            'settingsBtn.onclick = async function(e) {'
            '  e.stopPropagation();'
            '  const current = document.querySelector(".overlay td.playlist-name");'
            '  const name = prompt("Playlist name", current ? current.textContent : "");'
            '  if(name === null) return;'
            '  const shown = document.querySelector(".overlay td.default-time");'
            '  const t = prompt("Default display time in seconds",'
            '                   shown ? shown.textContent : "");'
            '  let url = "/api/v1/settings?name=" + encodeURIComponent(name);'
            '  if(t) url += "&default_time=" + encodeURIComponent(t);'
            '  try {'
            '    await fetch(url, {method: "POST"});'
            '    await refreshOverlay();'
            '  } catch(e) { }'
            '};'
            'async function refreshOverlay(){'
            '  try {'
            '    const res = await fetch(location.href, {cache: "no-store"});'
            '    const doc = new DOMParser().parseFromString(await res.text(), "text/html");'
            '    document.querySelector(".overlay").innerHTML ='
            '      doc.querySelector(".overlay").innerHTML;'
            '  } catch(e) { }'
            '}'
            'async function addItem(){'
            '  const uri = prompt("URI to add", "iss://apod");'
            '  if(!uri) return;'
            '  const t = prompt("Display time in seconds (blank for the default)", "");'
            '  let url = "/api/v1/playlist?uri=" + encodeURIComponent(uri);'
            '  if(t) url += "&t=" + encodeURIComponent(t);'
            '  try {'
            '    const res = await fetch(url, {method: "POST"});'
            '    const data = await res.json();'
            '    if(data.error) { alert(data.error); return; }'
            '    await refreshOverlay();'
            '  } catch(e) { }'
            '}'
            'async function toggleItem(num, el){'
            '  try {'
            '    const res = await fetch("/api/v1/playlist/" + num + "/toggle", {method: "POST"});'
            '    if(!res.ok) return;'
            '    const data = await res.json();'
            '    if(data.enabled === undefined) return;'
            '    const row = el.closest("tr");'
            '    row.querySelector(".state").textContent = data.enabled ? "enabled" : "disabled";'
            '    el.textContent = data.enabled ? "disable" : "enable";'
            '  } catch(e) { }'
            '}'
            'async function refreshScreenshot(){'
            '  if(paused) return;'
            '  try {'
            '    const res = await fetch("/api/v1/screenshot", {cache: "no-store"});'
            '    if(!res.ok) return;'
            '    const blob = await res.blob();'
            '    if(!blob.size) return;'
            '    const url = URL.createObjectURL(blob);'
            '    const old = img.src;'
            '    img.src = url;'
            '    if(old.startsWith("blob:")) { try { URL.revokeObjectURL(old); } catch(_){} }'
            '  } catch(e) { /* ignore */ }'
            '}'
            'setInterval(refreshScreenshot, 1000);'
            '</script>'
        )
        return "<html><head><title>ISS Display</title>" + HtmlPage.get_css() + "</head><body>" + body + "</body></html>"


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
                        action='append',
                        required=True)
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
                        default=6000)
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
    # Preferred flag/env
    parser.add_argument('--probe-ip-address',
                        dest='probe_ip_address',
                        env_var='PROBE_IP_ADDRESS',
                        help="The IP address to probe for",
                        type=str)
    parser.add_argument('--theme',
                        dest='theme_name',
                        env_var='THEME',
                        help="The theme to use",
                        type=str,
                        default="default")
    parser.add_argument('--update-controller',
                        dest='update_controller',
                        env_var='UPDATE_CONTROLLER',
                        help="Fetch latest controller.py on startup",
                        action='store_true')
    parser.add_argument('--controller-update-url',
                        dest='controller_update_url',
                        env_var='CONTROLLER_UPDATE_URL',
                        help="The URL to controller.py to update with",
                        type=str,
                        default="https://github.com/opsboost/iss-display-controller/blob/dev/controller.py")
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

    debug = args.debug
    uris = args.uris
    stream_source = args.stream_source
    stream_source_device = args.stream_source_device
    img_path = args.img_path
    listen_address = args.listen_address
    listen_port = args.listen_port
    location = args.location
    mqtt_broker = args.mqtt_broker
    mqtt_port = args.mqtt_port
    mqtt_user = args.mqtt_user
    mqtt_pw = args.mqtt_pw
    mqtt_topics = args.mqtt_topics
    probe_ip_address = args.probe_ip_address
    theme_name = args.theme_name
    apod_api_key = args.apod_api_key
    update_controller = args.update_controller
    controller_update_url = args.controller_update_url
    zeroconf_publish_service = args.zeroconf_publish_service
    zc_service_name_prefix = args.zeroconf_service_name_prefix
    zc_service_type = args.zeroconf_service_type
    logfile = args.logfile
    loglevel = args.loglevel
    log_format = '[%(asctime)s] \
    {%(filename)s:%(lineno)d} %(levelname)s - %(message)s'
    del locals()['args']

    # Optional File Logging
    if logfile:
        tlog = logfile.rsplit('/', 1)
        logpath = tlog[0]
        logfile = tlog[1]
        if not os.access(logpath, os.W_OK):
            # Our logger is not set up yet, so we use print here
            print("Logging: Can not write to directory. Skipping file handler")
        else:
            fn = logpath + '/' + logfile
            file_handler = logging.FileHandler(filename=fn)
            # Our logger is not set up yet, so we use print here
            print("Logging: Logging to " + fn)

    stdout_handler = logging.StreamHandler(sys.stdout)

    if 'file_handler' in locals():
        handlers = [file_handler, stdout_handler]
    else:
        handlers = [stdout_handler]

    logging.basicConfig(
        level=logging.INFO,
        format=log_format,
        handlers=handlers
    )

    logger = logging.getLogger(__name__)
    level = logging.getLevelName(loglevel)
    logger.setLevel(level)

    for dep in dependencies:
        if which(dep) is None:
            logging.error(f"Could not find dependency: {dep}, aborting..")
            sys.exit(1)

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
    local_ip = ""

    local_ip = System.net_iface_address(probe_ip_address)

    path_update = "/tmp/controller-updated"
    if update_controller and not os.path.exists(path_update):
        logging.info("Updating self..")
        #download_file(controller_update_url, os.path.abspath("/tmp/" + os.path.basename(__file__)), False)
        with open(path_update, "w") as file:
            file.write("Update done\n")
        reexec_self()

    display = Display(local_ip, listen_port)

    # Start UDP state server so external processes (e.g., Daphne) can query Display
    display.start_state_server()

    nwins = len(display.get_windows())
    if nwins > 0:
        logging.warning(f"Expected no windows but found {nwins}")

    theme = Theme(theme_name)
    logging.info(f"PATH: {env.get('PATH', '')}")
    logging.info(f"Using theme: {theme_name}")
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
    iss = Iss(threads)

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

        r = zc.zc_register_service()

        if not r:
            logging.error("zeroconf: Failed to publish service")

    # Set up signal handler
    def signal_handler(number, *args):
        logging.info(f'Signal received: {number}')

        # Unpublish service
        if zeroconf_publish_service and zc:
            zc.zc_unregister_service()

        for thread in threads:
            logging.info("Stopping thread")
            thread.join()

        # Stop UDP state server
        try:
            display.stop_state_server()
        except Exception:
            pass

        logging.shutdown()

        sys.exit(0)

    # Register signal handler
    signal.signal(signal.SIGINT, signal_handler)

    # Change view regularly
    display.start_time = time.time()

    # Start ASGI server via uvicorn
    try:
        cmd = ['uvicorn', 'controller:asgi_app', '--host', listen_address, '--port', str(listen_port)]
        if str(loglevel).upper() != 'DEBUG':
            cmd.append('--no-access-log')
        logging.info(f"Starting uvicorn ASGI server on {listen_address}:{listen_port}")
        # Ensure the controller directory is importable so uvicorn can import 'controller:asgi_app'
        module_dir = os.path.dirname(os.path.abspath(__file__))
        env_mod = os.environ.copy()
        env_mod['PYTHONPATH'] = module_dir + (os.pathsep + env_mod['PYTHONPATH'] if 'PYTHONPATH' in env_mod else '')
        subprocess.run(cmd, env=env_mod, check=False)
    except FileNotFoundError:
        logging.error("uvicorn not found. Install 'uvicorn' to run the ASGI server.")
        sys.exit(1)
