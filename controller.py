#!/usr/bin/env python3
import requests
import asyncio
import configargparse
from starlette.applications import Starlette
from starlette.responses import JSONResponse, PlainTextResponse, HTMLResponse, FileResponse
from hnapi import HnApi
import html
import json
import logging
from logging import DEBUG
import os
from paho.mqtt import client as mqtt_client
from pathlib import Path
import random
import socket
import stat
import subprocess
from subprocess import Popen, PIPE
import signal
import sqlite3
import sys
import tempfile
import time
import threading
from zeroconf import IPVersion, ServiceInfo, Zeroconf
sys.path.append(os.path.abspath("/usr/local/src/python-wayland"))
import draw as view
import wayland.protocol

# Ensure child processes inherit the runtime environment (including PATH)
env = os.environ.copy()

# Prepare ASGI app for API and web ui
app = Starlette()
asgi_app = app
dependencies = []
stream_sources = ["static-images", "v4l2", "vnc-browser"]
cmds = {"clock":        "humanbeans_clock",
        "image_viewer": "imv",
        "media_player": "mpv",
        "screenshot":   "grim"}


@app.route("/", methods=["GET"])
def web_main(request):
    return HTMLResponse(HtmlPage.page_display())

@app.route("/display", methods=["GET"])
def web_display(request):
    return HTMLResponse(HtmlPage.page_display())


# Readiness
@app.route('/healthy', methods=["GET"])
def healthy(request):
    return PlainTextResponse("OK")


# Liveness
@app.route('/healthz', methods=["GET"])
def healthz(request):
    return PlainTextResponse(probe_liveness())


@app.route("/api/v1/display", methods=["GET"])
def screen(request):
    state = Display.query_state()
    if not state:
        return PlainTextResponse("Service Unavailable", status_code=503)
    data = {
        "name": "display-0",
        "os_release": "iss-display",
        "listen_address": state.get('address'),
        "listen_port": state.get('port'),
        "res_x": state.get('res_x'),
        "res_y": state.get('res_y'),
        "playlist": globals().get('playlist').playlist if 'playlist' in globals() else [],
        "uris": globals().get('playlist').uris if 'playlist' in globals() else [],
        "streams": globals().get('stream').streams if 'stream' in globals() else [],
    }
    return JSONResponse(data)

@app.route("/screenshot", methods=["GET"])
def display_screenshot(request):
    fn = screenshot()
    if not os.path.exists(fn):
        logging.error("screenshot: File {} does not exist".format(fn))
        return PlainTextResponse("Not Found", status_code=404)
    return FileResponse(fn, media_type='image/png')

@app.route("/api/v1/screenshot", methods=["GET"])
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

@app.route("/api/v1/routes", methods=["GET"])
def api_routes(request):
    return JSONResponse(list_routes())

def probe_liveness():
    return "OK"


def which(cmd):
    def is_exe(fpath):
        return os.path.isfile(fpath) and os.access(fpath, os.X_OK)

def download_file(url, path, use_curl=True):
    logging.info("Downloading: " + url)

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
              close_fds=True,
              encoding='utf8')

    return True


def reexec_self():
    logging.info("Restarting after update..")
    # Re-execute the current script
    os.execv(sys.executable, [sys.executable] + sys.argv)


def skip_comments(file):
    for line in file:
        if not line.strip().startswith('#'):
            yield line.strip()


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
    apod = APOD()
    img_path, desc = apod.apod_data()
    if not img_path or not desc:
        logging.error("Failed to fetch APOD data.")
        return

    if output == 'terminal':
        print(desc)
    elif output == 'wayland-view':
        view = Wayland_view(display.res_x, display.res_y, 1, theme)
        view.s_objects[0]["font_size"] = 20
        view.s_objects[0]["alignment"] = "left"
        view.show_image(img_path)

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
    import shutil

    today = datetime.date.today()
    cal = calendar.TextCalendar(calendar.MONDAY)
    month_str = cal.formatmonth(today.year, today.month)
    lines = month_str.split('\n')
    highlighted_lines = []
    day_str = str(today.day).rjust(2)
    day_name = today.strftime("%A")

    for line in lines:
        # Highlight the current day
        def highlight(match):
            if output == 'terminal':
                return f"\033[1;7m{match.group(0)}\033[0m"
            elif output == 'wayland-view':
                font_face = "Monospace"
                font_size = 20
                font_face_hilight = "Monospace"
                font_size_hilight = 23
                markup = "</span><span foreground=\"orange\" font=\"{} {}\">{}</span><span foreground=\"white\" font=\"{} {}\">".format(
                    font_face_hilight,
                    font_size_hilight,
                    match.group(0),
                    font_face,
                    font_size
                )
                return markup

        # Highlight the current day
        def highlight_day_name(match):
            if output == 'terminal':
                return f"\033[1;7m{match.group(0)}\033[0m"
            elif output == 'wayland-view':
                return f"<span foreground=\"orange\" font=\"Monospace 23\">{match.group(0)}</span>"

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
        holidays = next_bank_holidays(location="Germany", count=3)
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
        view.show_text(texts, img_bg, html_escape=False)


class System:

    def list_processes(limit=0):
        # Processes without kernel threads
        cmd = ['ps', 'wux', '--ppid', '2', '-p', '2', '--deselect']

        # FreeBSD
        cmd = ['ps', 'ux']

        ps = subprocess.Popen(cmd,
                      stdout=subprocess.PIPE,
                      shell=False,
                      encoding="utf8",
                      env=env).communicate()[0]
        return ps

    def os_release():
        data = ""
        f = open('/etc/os-release')
        for line in skip_comments(f):
            if line.startswith("PRETTY_NAME"):
                data += line.split("=")[1].strip('"')

        return data

    def sys_data():
        sys = {"data" : None,
               "uptime" : ""}
        sysdata = Py3status("sysdata")
        sys["data"] = sysdata.run_module()
        uptime = Py3status("uptime")
        sys["uptime"] = uptime.run_module()

        return sys

    def net_data(probe_ip):
        net = {"address" : None,
               "addresses" : "",
               "public_ip" : "",
               "online_status" : "",
               "resolvconf" : ""}
        net["address"] = System.net_iface_address(probe_ip)
        net_iplist = Py3status("net_iplist")
        net["addresses"] = net_iplist.run_module()
        whatismyip = Py3status("whatismyip")
        net["public_ip"] = whatismyip.run_module()
        online_status = Py3status("online_status")
        net["online_status"] = online_status.run_module()
        net["resolvconf"] = System.net_resolvconf()

        return net

    def net_resolvconf():
        data = ""
        f = open('/etc/resolv.conf')
        for line in skip_comments(f):
            data += line + "\n"
        return data

    def net_valid_ip_address(ip_address):
        try:
            socket.inet_pton(socket.AF_INET, ip_address)
        except:
            try:
                socket.inet_pton(socket.AF_INET6, address)
            except:
                logging.warning('%s is an invalid IP address' % (ip_address))
                return False

        return True

    def net_iface_address(ip_address):
        ip_address = "9.9.9.9"

        try:
            socket.inet_pton(socket.AF_INET6, ip_address)
            s = socket.socket(socket.AF_INET6, socket.SOCK_DGRAM)
        except:
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                socket.inet_pton(socket.AF_INET, ip_address)
            except:
                logging.warning('%s is an invalid IP address' % (ip_address))
                return False

        try:
            s.connect((ip_address, 80))
        except OSError as e:
            if e.errno == 51:
                logging.info("%s is unreachable", ip_address)
                return False
            else:
                raise

        return s.getsockname()[0]

    def uptime():
        p = subprocess.Popen(['uptime'], shell=True,
                         stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT,
                         encoding="utf8",
                         env=env)
        output, error = p.communicate()
        return output.strip()


class Zeroconf_service:

    def __init__(self,
                 name_prefix,
                 service_type,
                 hostname,
                 listen_address,
                 listen_port,
                 properties):

        ip_version = IPVersion.V6Only
        self.service_type = service_type
        self.service_name = name_prefix + "-" + \
            hostname + "." + \
            self.service_type

        # The zeroconf service data to publish on the network
        self.zc_service = ServiceInfo(
            self.service_type,
            self.service_name,
            addresses=[socket.inet_pton(socket.AF_INET,
                                        listen_address)],
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
                 location=None):

        self.download_path = "/tmp/"
        self.location = location
        self.default_play_time_s = default_play_time_s
        self.news = None
        self.theme = theme
        self.topics = topics

        self.playlist = list()
        self.playlist = self.create(uris)
        self.uris = self.get_uris()

    def create(self, uris):
        n = 0
        playlist = list()

        for uri in uris:
            item = {}
            n += 1
            if uri.endswith(".m3u8"):
                item["num"] = n
                item["uri"] = uri
                item["player"] = "mediaplayer"
                item["play_time_s"] = self.default_play_time_s
            elif uri.startswith("https://"):
                item["num"] = n
                item["uri"] = uri
                item["player"] = "browser"
                item["play_time_s"] = self.default_play_time_s
            elif uri.startswith("iss-apod://"):
                item["num"] = n
                item["uri"] = uri
                item["player"] = "apod"
                item["play_time_s"] = self.default_play_time_s
            elif uri.startswith("iss-cal://"):
                item["num"] = n
                item["uri"] = uri
                item["player"] = "calendar"
                item["play_time_s"] = self.default_play_time_s
            elif uri.startswith("iss-clock://"):
                item["num"] = n
                item["uri"] = uri
                item["player"] = "clock"
                item["play_time_s"] = self.default_play_time_s
            elif uri.startswith("iss-mqtt://"):
                item["num"] = n
                item["uri"] = uri
                item["player"] = "mqtt"
                item["play_time_s"] = self.default_play_time_s
            elif uri.startswith("iss-music://"):
                item["num"] = n
                item["uri"] = uri
                item["player"] = "music"
                item["play_time_s"] = self.default_play_time_s
            elif uri.startswith("iss-network://"):
                item["num"] = n
                item["uri"] = uri
                item["player"] = "network"
                item["play_time_s"] = self.default_play_time_s
            elif uri.startswith("iss-news://"):
                news_sources = {"hn" : "",
                                "db" : "/home/mue/.local/share/russ/feeds.db"}
                self.news = News(news_sources)
                item["num"] = n
                item["uri"] = uri
                item["player"] = "news"
                item["play_time_s"] = self.default_play_time_s
            elif uri.startswith("iss-otd://"):
                otd_sources = {"wikipedia" : ""},
                self.otd = OTD(otd_sources)
                item["num"] = n
                item["uri"] = uri
                item["player"] = "onthisday"
                item["play_time_s"] = self.default_play_time_s
            elif uri.startswith("iss-proc://"):
                item["num"] = n
                item["uri"] = uri
                item["player"] = "processes"
                item["play_time_s"] = self.default_play_time_s
            elif uri.startswith("iss-system://"):
                item["num"] = n
                item["uri"] = uri
                item["player"] = "system"
                item["play_time_s"] = self.default_play_time_s
            elif uri.startswith("iss-weather://"):
                self.weather = Weather(self.location)
                item["num"] = n
                item["uri"] = uri
                item["player"] = "weather"
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
                playlist.append(item)

        return playlist

    def get_uris(self):
        for item in self.playlist:
            uris.append(item["uri"])

        return uris

    def start_player(self, probe_ip):
        threads = list()
        for item in self.playlist:
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
                                           probe_ip))
            elif item["player"] == "news":
                x = threading.Thread(target=self.start_news_view,
                                     args=(self.news,
                                           self.theme.img_bg,))
            elif item["player"] == "onthisday":
                x = threading.Thread(target=self.start_onthisday_view,
                                     args=(self.otd,
                                           self.theme.img_bg,))
            elif item["player"] == "processes":
                x = threading.Thread(target=self.start_proc_view,
                                     args=(self.theme.img_bg,))
            elif item["player"] == "system":
                x = threading.Thread(target=self.start_sys_view,
                                     args=(self.theme.img_bg,
                                           probe_ip))
            elif item["player"] == "weather":
                x = threading.Thread(target=self.start_weather_view,
                                     args=(self.weather,
                                           self.theme.img_bg,))

            x.start()
            if not x.is_alive():
                logging.error("Failed to start {}".format(item["player"]))
            else:
                threads.append(x)

        return threads

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

        p = Popen(cmd,
                  env=env,
                  start_new_session=True,
                  close_fds=True)

        if p.communicate()[0] != 0:
            return False

        return True

    def start_image_viewer(self, file):
        logging.info("Starting image viewer with file: " + file)
        cmd = [cmds["image_viewer"],  file]

        Popen(cmd,
              env=env,
              start_new_session=True,
              close_fds=True,
              encoding='utf8')

        return True

    def start_mqtt_views(self, topics, theme):
        mqtt_client_id = "iss-display-42"
        mqtt = MQTT(mqtt_broker,
                    mqtt_client_id,
                    mqtt_port,
                    mqtt_user,
                    mqtt_pw)
        try:
            mqttc = mqtt.connect()
        except Exception:
            logging.info("MQTT: Failed to connect")
            return False

        for topic in topics:
            mqtt.subscribe(mqttc, topic, theme)
            logging.info("MQTT: Subscribed to {}".format(topic))

        mqttc.loop_forever()

    def start_mediaplayer(self, url):
        logging.info("Starting media player with stream: " + url)
        cmd = [cmds["media_player"],  url]

        Popen(cmd,
              env=env,
              start_new_session=True,
              close_fds=True,
              encoding='utf8')

        return True

    def start_net_view(self, img_bg, probe_ip):
        logging.info("Starting network view")
        texts = list()
        net = System.net_data(probe_ip)
        texts.append(net["address"])
        texts.append(net["addresses"])
        texts.append(net["public_ip"])
        texts.append(net["resolvconf"])
        view = Wayland_view(display.res_x, display.res_y, len(texts), theme)
        view.s_objects[0]["font_size"] = 20
        view.s_objects[0]["alignment"] = "left"
        view.s_objects[1]["font_size"] = 20
        view.s_objects[1]["alignment"] = "left"
        view.s_objects[2]["font_size"] = 20
        view.s_objects[2]["alignment"] = "left"
        view.s_objects[3]["font_size"] = 20
        view.s_objects[3]["alignment"] = "left"
        view.show_text(texts, img_bg)

    def start_proc_view(self, img_bg):
        texts = list()
        texts.append(System.list_processes(23))
        view = Wayland_view(display.res_x, display.res_y, len(texts), theme)
        view.s_objects[0]["font_size"] = 14
        view.s_objects[0]["alignment"] = "left"
        view.show_text(texts, img_bg)

    def start_sys_view(self, img_bg, probe_ip):
        logging.info("Starting sys view")
        net = System.net_data(probe_ip)
        sys = System.sys_data()
        texts = list()
        texts.append(System.os_release())
        texts.append(System.uptime())
        texts.append(f"Display started: {display.started}")
        texts.append(sys["uptime"])
        texts.append(f"Display Resolution: {display.res_x}x{display.res_y}")
        texts.append(sys["data"])
        texts.append(net["address"])
        texts.append(net["addresses"])
        texts.append(str(net["online_status"]) + " " + str(net["public_ip"]))
        texts.append(f"Listen address: {display.address}:{display.port}")
        view = Wayland_view(display.res_x, display.res_y, len(texts), theme)
        view.s_objects[0]["font_size"] = 24
        view.s_objects[0]["alignment"] = "left"
        view.s_objects[1]["font_size"] = 20
        view.s_objects[1]["alignment"] = "left"
        view.s_objects[2]["font_size"] = 20
        view.s_objects[2]["alignment"] = "left"
        view.s_objects[3]["font_size"] = 20
        view.s_objects[3]["alignment"] = "left"
        view.s_objects[4]["font_size"] = 20
        view.s_objects[4]["alignment"] = "left"
        view.s_objects[5]["font_size"] = 20
        view.s_objects[5]["alignment"] = "left"
        view.s_objects[6]["font_size"] = 20
        view.s_objects[6]["alignment"] = "left"
        view.s_objects[7]["font_size"] = 20
        view.s_objects[7]["alignment"] = "left"
        view.s_objects[8]["font_size"] = 20
        view.s_objects[8]["alignment"] = "left"
        view.show_text(texts, img_bg)

    def start_weather_view(self, weather, img_bg):
        logging.info("Starting weather view")
        texts = list()
        data, icon = weather.current_weather()

        if not icon:
            logging.debug(f"weather: No icon found for current condition")
        else:
            texts.append(icon)

        texts.append(data["current_condition"][0]["temp_C"] + "°C")
        texts.append(data["current_condition"][0]["weatherDesc"][0]["value"]
                     + " " +
                     data["current_condition"][0]["windspeedKmph"] + " km/h")
        texts.append(data["nearest_area"][0]["areaName"][0]["value"])

        # Add dawn and sunset for today
        dawn_sunset_times = dawn_sunset(location=weather.location)
        if not dawn_sunset_times:
            logging.info(f"No dawn/sunset data returned for location: {weather.location}")
        elif "today" not in dawn_sunset_times:
            logging.info(f"'today' key missing in dawn/sunset data for location: {weather.location}: {dawn_sunset_times}")
        else:
            dawn = dawn_sunset_times["today"].get("dawn")
            sunset = dawn_sunset_times["today"].get("sunset")
            if not dawn:
                logging.info(f"No 'dawn' value in dawn/sunset data for location: {weather.location}: {dawn_sunset_times['today']}")
            if not sunset:
                logging.info(f"No 'sunset' value in dawn/sunset data for location: {weather.location}: {dawn_sunset_times['today']}")
            if dawn and sunset:
                texts.append(f"Dawn: {dawn}  Sunset: {sunset}")

        # Add a margin (empty line) between location and moon data
        texts.append("")

        # Add moon phase and icon
        moon_phase, moon_icon, days_until_full_moon = moonphase(location=weather.location)
        if moon_phase:
            moon_line = f"Moon: {moon_phase}"
            if days_until_full_moon is not None:
                moon_line += f" ({days_until_full_moon} days to full)"
            texts.append(moon_line)
            if moon_icon:
                texts.append(moon_icon)

        # Fetch and display UV index
        uv_index = fetch_uv_index(location=weather.location)
        if uv_index:
            texts.append(f"UV Index: {uv_index}")

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
        view.show_text(texts, img_bg)
        # Show weather icon (already shown as first text if present)
        # Show moon icon if present and not already shown
        # (If you want to show as image, uncomment below)
        # if moon_icon:
        #     view.show_image(moon_icon)
        # else:
        view.show_image(icon)

    def start_music_view(self, img_bg):
        texts = list()
        music = Music()
        music_data = music.mpd()
        texts.append(music_data)
        view = Wayland_view(display.res_x, display.res_y, len(texts), theme)
        view.show_text(texts, img_bg)

    def start_news_view(self, news, img_bg):
        texts = list()
        item = news.news_item()
        texts.append(item["feed"])
        texts.append(item["title"])
        texts.append(item["url"])
        view = Wayland_view(display.res_x, display.res_y, len(texts), theme)
        view.s_objects[0]["font_size"] = 30
        view.s_objects[1]["font_size"] = 60
        view.s_objects[2]["font_size"] = 30
        view.show_text(texts, img_bg)

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
        view.show_text(texts, img_bg)

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

            logging.debug(filename + ": " + str(t1 - t0) + " ms")

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
            logging.error(device + " does not exist, aborting..")
            sys.exit(1)

        # Create v4l2 recording of screen
        logging.info("Creating v4l2 stream with device: " + device)
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


class Music:

    def __init__(self):
        self.music = {'mpd_data'   : False,
                      'mpd_state'  : False,
                      'mpd_artist' : "",
                      'mpd_title'  : "",
                      'mpd_album'  : ""}

    def mpd(self):
        mpd = Py3status("mpd")
        data = mpd.run_module()
        print(data)


class MQTT:

    def __init__(self, broker, client_id, port=1883, user="", pw=""):
        self.client_id = client_id
        self.broker = broker
        self.port = port
        self.user = user
        self.pw = pw
        self.mqtt_views = list()

    def connect(self):
        def on_connect(client, userdata, flags, r):
            if r == 0:
                logging.info("MQTT: Connected")
            else:
                logging.error("MQTT: Failed to connect: %d\n", r)

        # Set client ID
        client = mqtt_client.Client(self.client_id)

        if self.user != "" and self.pw != "":
            client.username_pw_set(self.user, self.pw)

        client.on_connect = on_connect
        client.connect(self.broker, self.port)
        return client

    def subscribe(self, client: mqtt_client, topic, theme):
        self.theme = theme

        def on_message(client, userdata, msg):
            self.active_msg = ""
            jm = msg.payload.decode()
            m = json.loads(jm)
            logging.info(f"Received `{m}` in  `{msg.topic}`")
            texts = list()

            if msg.topic == "hyperblast/current_song":
                texts.append(m["title"])
                texts.append("[" + m["file"] + "]")
            elif msg.topic == "sensor/mainhallsensor/temperature":
                texts.append("Mainhall")
                texts.append(str(m) + " °C")

            view = Wayland_view(display.res_x,
                                display.res_y,
                                len(texts),
                                self.theme)

            view.s_objects[0]["font_size"] = 64
            view.s_objects[0]["alignment"] = "center"
            view.s_objects[1]["font_size"] = 48
            view.s_objects[1]["alignment"] = "center"
            view.show_text(texts, theme["img_bg"])
            self.mqtt_views.append(msg.topic)

        client.subscribe(topic)
        client.on_message = on_message


class OTD:

    def __init__(self, sources):
        import datetime
        today = datetime.date.today()
        month = today.month
        day = today.day
        url = f"https://en.wikipedia.org/api/rest_v1/feed/onthisday/events/{month}/{day}"
        self.events = []

        response = requests.get(url)

        if response.status_code == 200:
            data = response.json()
            for event in data.get("events", []):
                logging.info(f"{event['year']}: {event['text']}")
                self.events.append(event)
        else:
            logging.error("Failed to fetch otd data")

    def otd_item(self):
        n = {"year": "",
             "text": ""}

        if not self.events:
            return None

        n["year"] = self.events[0].get('year')
        n["text"] = self.events[0].get('text')
        return n


class News:

    def __init__(self, sources):
        self.news = []
        self.sqlite_select(sources["db"], "SELECT feeds.title, entries.title, entries.link, entries.pub_date FROM entries INNER JOIN feeds ON entries.feed_id = feeds.id ORDER BY pub_date DESC LIMIT 9")
        #self.hn_fetch_top_news(10)

    # news_item returns a single news text
    # from the previously fetched ones
    def news_item(self):
        n = {"feed": "",
             "title": "",
             "url": ""}

        n["feed"] = self.news[0].get('feed')
        n["title"] = self.news[0].get('title')
        n["url"] = self.news[0].get('url')
        self.news.pop(0)

        return n

    def sqlite_select(self, db, query):
        n = {"title": "",
             "url": ""}

        if not os.path.exists(db):
            logging.error("News: Database does not exist %s", db)
            return False

        con = sqlite3.connect(db)
        cur = con.cursor()
        res = cur.execute(query)
        r = res.fetchall()
        for news in r:
            logging.info("News: Appending news")
            self.news.append({"feed"  : news[0],
                              "title" : news[1],
                              "url"   : news[2]})

    def hn_fetch_top_news(self, nitems):
        n = 0

        logging.info("Fetching News")
        con = HnApi()
        top = con.get_top()

        for tnews in top:
            if n == nitems:
                break

            self.news.append(con.get_item(tnews))
            n += 1


class Py3status:

    def __init__(self, module_name):
        self.module_name = module_name
        self.config_common = """
general {
    colors = false
    interval = 5
    color_good = "#96b5b4"
}
"""
        self.module_config = {"mpd" : ""}
        self.module_config["mpd"] = self.config_common + """
order = "mpd"

"""
        self.module_config["net_iplist"] = self.config_common + """
order = "net_iplist"

net_iplist {
    iface_blacklist = ['lo0']
    ip_blacklist = ['127.*', '::1']
    format = "{format_iface}"
}
"""
        self.module_config["sysdata"] = self.config_common + r"""
order = "sysdata"

sysdata {
    format = "CPU Histogram [\?color=cpu_used_percent {format_cpu}]"
    format_cpu = "[\?if=used_percent>80 ⡇|[\?if=used_percent>60 ⡆"
    format_cpu += "|[\?if=used_percent>40 ⡄|[\?if=used_percent>20 ⡀"
    format_cpu += "|⠀]]]]"
    format_cpu_separator = ""
    thresholds = [(0, "good"), (60, "degraded"), (80, "bad")]
    cache_timeout = 1
}
"""
        self.module_config["online_status"] = self.config_common + """
order = "online_status"
"""
        self.module_config["uptime"] = self.config_common + r"""
order = "uptime"

uptime {
        format = 'up [\?if=weeks {weeks} weeks ][\?if=days {days} days ]
        [\?if=hours {hours} hours ][\?if=minutes {minutes} minutes ]'
}
"""
        self.module_config["whatismyip"] = self.config_common + """
order = "whatismyip"

whatismyip {
        format = '{icon} {ip} {country} {city}'
}
"""
        self.config_path = self.write_config()
        self.output = {}  # Store latest output per module

    def run_module(self):
        cmd = ['py3status', '-c', self.config_path, '-o']
        exists = os.path.exists(self.config_path)
        readable = os.access(self.config_path, os.R_OK)

        logging.info(f"py3status config path: {self.config_path}, exists={exists}, readable={readable}")

        if readable:
            try:
                with open(self.config_path, 'r', encoding='utf8') as cf:
                    logging.info(f"py3status config:\n{cf.read()}")
            except Exception as e:
                logging.warning(f"py3status: failed reading config content: {e}")

        p = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            encoding='utf8',
            env = {'PATH': '/venv/bin:/usr/local/bin:/usr/bin:/bin'}
        )
        stdout, stderr = p.communicate()
        logging.info(f"py3status ({self.module_name}):\n{stdout}")

        if stderr:
            logging.debug(f"py3status stderr ({self.module_name}): {stderr.strip()}")

        data = None
        if stdout:
            lines = [l.strip() for l in stdout.splitlines() if l.strip()]
            tail = []
            for i in range(len(lines) - 1, -1, -1):
                tail.insert(0, lines[i])
                try:
                    data = json.loads("\n".join(tail))
                    break
                except Exception:
                    continue

        # Extract full_text
        result = data
        try:
            if isinstance(data, dict) and 'full_text' in data:
                result = data['full_text']
            elif isinstance(data, list):
                blocks = data[-1] if (data and isinstance(data[-1], list)) else data
                if isinstance(blocks, list):
                    for blk in reversed(blocks):
                        if isinstance(blk, dict) and 'full_text' in blk:
                            result = blk['full_text']
                            break
        except Exception:
            pass

        # Fallback: regex the last full_text from raw stdout
        if result is None:
            import re
            matches = re.findall(r'"full_text"\s*:\s*"([^"]+)"', stdout or '')
            if matches:
                result = matches[-1]

        logging.info(f"py3status ({self.module_name}) parsed: {result}")
        return result

    def write_config(self):
        tmp = tempfile.NamedTemporaryFile(delete=False, mode='w+', encoding='utf-8')
        # Always write only the config for this module
        tmp.write(self.module_config[self.module_name])
        tmp.flush()
        tmp.close()
        return tmp.name


class Weather:

    def __init__(self, location):
        self.location = location
        self.weather = self.fetch_weather()

    def fetch_weather(self):
        url = "https://wttr.in/{}?format=j1".format(self.location)
        logging.info("iss-weather: Fetching weather for {} at {}"
                     .format(self.location, url))
        data = None
        icon = None
        try:
            response = requests.get(url, timeout=10)
            if response and response.content:
                try:
                    data = json.loads(response.content)
                    icon = self.icon(data["current_condition"][0]["weatherDesc"][0]["value"])
                except (ValueError, KeyError, IndexError) as e:
                    logging.error("weather: Failed to decode or parse data")
                    logging.error(e)
                    data = None
                    icon = None
            else:
                logging.error("Failed to fetch weather data: empty response")
        except requests.ReadTimeout as e:
            logging.error("weather: Timeout for request {}".format(e))
        except Exception as e:
            logging.error("weather: Error requesting weather: {}".format(e))
        return data, icon

    def icon(self, condition):
        icon = False

        if "Sunny" == condition:
            condition = "clear"
        elif "Clear" == condition:
            condition = "clear"

        fn = f"themes/default/weather/{condition}.svg"

        if not os.path.exists(fn):
            return False

        return fn

    def current_weather(self):
        # Always return a tuple for unpacking
        if self.weather is None:
            return None, None
        return self.weather


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

        self.window = {}
        self.window["res_x"] = res_x
        self.window["res_y"] = res_y
        self.window["title"] = "News"

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
                    "text": list()}

        self.s_objects = list()
        for n in range(0, num_objects):
            logging.info("view: Appending drawing object")
            self.s_objects.append(s_object.copy())

    def create_window(self, w):
        view.eventloop()

        w.close()
        self.conn.display.roundtrip()
        self.conn.disconnect()
        logging.info("Exiting wayland view: {}".format(view.shutdowncode))

    def show_text(self, texts, img_bg=False, fullscreen=False, html_escape=True):
        logging.info("view: Have {} text block(s)".format(len(texts)))

        n = 0
        for text in texts:
            logging.debug(f"Showing text: {text}")
            if html_escape:
                self.s_objects[n]["text"] = html.escape(str(text).replace("&", "&amp;"))
            else:
                self.s_objects[n]["text"] = str(text).replace("&", "&amp;")
            n += 1

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

        self.create_window(w)

    def show_image(self, img_file, fullscreen=False):
        self.s_objects[0]["texts"] = list()
        self.s_objects[0]["file"] = img_file
        self.s_objects[0]["bg_alpha"] = 0
        self.s_objects[0]["offset_y"] = 0
        w = view.Window(self.conn,
                        self.window,
                        self.s_objects,
                        redraw=draw_function,
                        fullscreen=fullscreen,
                        class_="iss-view")

        self.create_window(w)


class Display:

    def __init__(self, address, port, res_x=1366, res_y=768):
        self.address = address
        self.port = port
        self.res_x = res_x
        self.res_y = res_y
        self.started = time.strftime("%Y-%m-%d %H:%M:%S %Z", time.localtime())
        self.start_time = time.time()
        self.switching_windows = list()
        self.window_blacklist = list()
        self.screenshot_path = "/tmp"
        self.screenshot_file = "screenshot.png"
        self.socket_path = self.get_socket_path()
        # UDP state server defaults
        self.state_udp_host = os.environ.get('DISPLAY_STATE_UDP_HOST', '127.0.0.1')
        self.state_udp_port = int(os.environ.get('DISPLAY_STATE_UDP_PORT', '6100'))
        self._state_server_thread = None
        self._state_server_stop = threading.Event()
        self._state_server_sock = None

        logging.info(f"Python executable: {sys.executable}")
        logging.info(f"Resolution: {self.res_x} x {self.res_y}")

        # We do not want to handle existing windows,
        # so we put their IDs on a blacklist
        # This is usually only useful in development/testing scenarios
        # e.g. when run locally with an existing sway session
        existing_windows = self.get_windows()
        for win in existing_windows:
            self.window_blacklist.append(win["id"])

        logging.info("Blacklisted {} windows"
                     .format(len(self.window_blacklist)))

        self.x = threading.Thread(target=self.focus_next_window, args=(3,))
        self.x.start()

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
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.sendto(b"STOP", (self.state_udp_host, self.state_udp_port))
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
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.settimeout(0.5)
            sock.bind((self.state_udp_host, self.state_udp_port))
            self._state_server_sock = sock
        except Exception as e:
            logging.error(f"Display UDP server bind failed: {e}")
            return

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
                payload = {
                    'address': self.address,
                    'port': self.port,
                    'res_x': self.res_x,
                    'res_y': self.res_y,
                }
                try:
                    self._state_server_sock.sendto(json.dumps(payload).encode('utf-8'), addr)
                except Exception as e:
                    logging.error(f"Display UDP server send error: {e}")
            elif msg == 'STOP':
                break
            else:
                # Ignore unknown messages
                pass

    @staticmethod
    def query_state(host=None, port=None, timeout=0.5):
        host = host or os.environ.get('DISPLAY_STATE_UDP_HOST', '127.0.0.1')
        port = int(port or os.environ.get('DISPLAY_STATE_UDP_PORT', '6100'))
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.settimeout(timeout)
                s.sendto(b'GET_STATE', (host, port))
                data, _ = s.recvfrom(4096)
                return json.loads(data.decode('utf-8'))
        except socket.timeout:
            logging.warning("Display UDP client: timeout querying state")
            return None
        except Exception as e:
            logging.error(f"Display UDP client error: {e}")
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

    def get_windows_whitelist(self):
        windows = self.get_windows(self.window_blacklist)
        logging.debug("display: {} windows in whitelist".format(len(windows)))

        return windows

    def get_windows(self, blacklist=None):
        cmd = "swaymsg -s {} -t get_tree".format(self.socket_path)
        windows = []

        p = subprocess.Popen(cmd,
                     shell=True,
                     stdout=subprocess.PIPE,
                     stderr=subprocess.PIPE,
                     env=env)

        data = json.loads(p.communicate()[0])

        for output in data['nodes']:
            if output.get('type') == 'output':
                workspaces = output.get('nodes')
                for ws in workspaces:
                    if ws.get('type') == 'workspace':
                        windows += self.workspace_nodes(ws)

        if blacklist:
            windows_whitelist = windows.copy()
            for win in windows:
                if win["id"] in blacklist:
                    windows_whitelist.pop()

            return windows_whitelist

        return windows

    # Extracts all windows from sway workspace json
    def workspace_nodes(self, workspace):
        windows = []

        floating_nodes = workspace.get('floating_nodes')

        for floating_node in floating_nodes:
            windows.append(floating_node)

        nodes = workspace.get('nodes')

        for node in nodes:
            # Leaf node
            if len(node.get('nodes')) == 0:
                windows.append(node)
            # Nested node
            else:
                for inner_node in node.get('nodes'):
                    nodes.append(inner_node)

        return windows

    def active_window(self):
        cmd = 'swaymsg -s {} -t get_tree) | jq ".. | select(.type?) | \
               select(.focused==true).id"'\
               .format(self.socket_path)

    def focus_next_window(self, t_focus_s):
        while True:
            time.sleep(t_focus_s)
            if len(self.switching_windows) == 0:
                self.switching_windows = self.get_windows_whitelist()
                if len(self.switching_windows) == 0:
                    logging.debug("display: Expected no windows but found {}"
                                  .format(nwins))

                    continue

            next_window = self.switching_windows.pop()
            logging.info("display: Switching focus to: {}"
                         .format(next_window["id"]))
            cmd = "swaymsg -s {} [con_id={}] focus"\
                  .format(self.socket_path, next_window["id"])
            p = subprocess.Popen(cmd, shell=True, env=env)
            p.communicate()[0]

    async def fullscreen_next_window(self):
        await asyncio.sleep(random.random() * 3)
        t = round(time.time() - self.start_time, 1)
        logging.info("Finished task: {}".format(t))

        if len(self.switching_windows) == 0:
            self.switching_windows = self.get_windows_debugwhitelist()
            if len(self.switching_windows) == 0:
                logging.debug("display: Expected windows to display but there is none")

                return

        next_window = self.switching_windows.pop()
        logging.info("display: Switching focus to: {}"
                     .format(next_window["id"]))

        cmd = "swaymsg -s {} [con_id={}] fullscreen"\
              .format(self.socket_path, next_window["id"])

        p = subprocess.Popen(cmd,
                     shell=True,
                     env=env)

        p.communicate()[0]

    async def task_scheduler(self, interval_s, interval_function):
        while True:
            logging.info("Starting periodic function: {}"
                         .format(round(time.time() - self.start_time, 1)))
            await asyncio.gather(
                asyncio.sleep(interval_s),
                interval_function(),
            )

    def switch_workspace(self, ws):
        cmd = "swaymsg -s {} workspace {}".format(self.socket_path, ws)

        p = subprocess.Popen(cmd,
                     shell=True,
                     encoding="utf8",
                     env=env)

        res = p.communicate()[0]

    def screenshot(self):
        fn = self.screenshot_path + "/" + self.screenshot_file
        return screenshot(fn)

def screenshot(path=None):
    if path is None:
        path = "/tmp/screenshot.png"
    logging.info("Saving screenshot to {}".format(path))
    cmd = [cmds["screenshot"], path]
    try:
        subprocess.run(cmd, env=os.environ.copy(), check=False)
    except Exception as e:
        logging.error("screenshot: Failed to capture: {}".format(e))
    return path


class Iss:

    def __init__(self, threads):
        self.threads = threads


def moonphase(location="Berlin"):
    """
    Fetches the current moon phase and icon from wttr.in for the given location.
    Returns:
        tuple: (moon_phase_text, moon_icon_url, days_until_full_moon) or (None, None, None) on failure.
    """
    import datetime

    url = f"https://wttr.in/{location}?format=j1"
    try:
        resp = requests.get(url, timeout=10)
        data = resp.json()
        # The moon phase is in the first day's astronomy section
        moon_phase = data["weather"][0]["astronomy"][0]["moon_phase"]
        moon_icon = data["weather"][0]["astronomy"][0].get("moon_icon", None)
        # wttr.in does not always provide a direct icon URL, so we map phase to icon if needed
        moon_icon_map = {
            "New Moon": "new-moon",
            "Waxing Crescent": "waxing-crescent",
            "First Quarter": "first-quarter",
            "Waxing Gibbous": "waxing-gibbous",
            "Full Moon": "full-moon",
            "Waning Gibbous": "waning-gibbous",
            "Last Quarter": "last-quarter",
            "Waning Crescent": "waning-crescent"
        }
        if not moon_icon:
            icon_name = moon_icon_map.get(moon_phase, "moon")
            moon_icon = f"https://wttr.in/files/{icon_name}.png"

        # Calculate days until next full moon
        today = datetime.date.today()
        days_until_full_moon = None
        # Look ahead in the weather forecast for the next full moon
        for day in data.get("weather", []):
            astronomy = day.get("astronomy", [])
            if astronomy and astronomy[0].get("moon_phase") == "Full Moon":
                date_str = day.get("date")
                if date_str:
                    date_obj = datetime.datetime.strptime(date_str, "%Y-%m-%d").date()
                    delta = (date_obj - today).days
                    if delta >= 0:
                        days_until_full_moon = delta
                        break
        return moon_phase, moon_icon, days_until_full_moon
    except Exception as e:
        logging.error(f"Failed to fetch moonphase: {e}")
        return None, None, None


def dawn_sunset(location="Berlin"):
    """
    Fetches dawn and sunset times for today and tomorrow from wttr.in for the given location.
    Returns:
        dict: {
            "today": {"dawn": str, "sunset": str},
            "tomorrow": {"dawn": str, "sunset": str}
        }
        or None on failure.
    """
    url = f"https://wttr.in/{location}?format=j1"
    try:
        resp = requests.get(url, timeout=10)
        data = resp.json()
        result = {}
        weather = data.get("weather", [])
        for idx, key in zip([0, 1], ["today", "tomorrow"]):
            if idx < len(weather):
                astronomy = weather[idx].get("astronomy", [{}])[0]
                dawn = astronomy.get("dawn", None)
                sunset = astronomy.get("sunset", None)
                result[key] = {"dawn": dawn, "sunset": sunset}
        return result
    except Exception as e:
        logging.error(f"Failed to fetch dawn/sunset: {e}")
        return None


def next_bank_holidays(location="Germany", count=3):
    """
    Fetches the next `count` bank holidays for the given location using the Nager.Date API.
    Returns:
        list of dicts: [{ "date": "YYYY-MM-DD", "localName": "Holiday Name", "name": "English Name" }, ...]
        or None on failure.
    """
    # Map some common location names to country codes for Nager.Date API
    country_map = {
        "Germany": "DE",
        "DE": "DE",
        "United Kingdom": "GB",
        "UK": "GB",
        "Great Britain": "GB",
        "France": "FR",
        "FR": "FR",
        "United States": "US",
        "USA": "US",
        "US": "US",
        "Austria": "AT",
        "AT": "AT",
        "Switzerland": "CH",
        "CH": "CH",
    }
    import datetime
    today = datetime.date.today()
    year = today.year
    country_code = country_map.get(location, location)
    url = f"https://date.nager.at/api/v3/PublicHolidays/{year}/{country_code}"
    try:
        resp = requests.get(url, timeout=10)
        holidays = resp.json()
        # Filter for holidays after today
        upcoming = [
            h for h in holidays
            if datetime.datetime.strptime(h["date"], "%Y-%m-%d").date() >= today
        ]
        # If not enough holidays left this year, fetch next year as well
        if len(upcoming) < count:
            url_next = f"https://date.nager.at/api/v3/PublicHolidays/{year+1}/{country_code}"
            resp_next = requests.get(url_next, timeout=10)
            holidays_next = resp_next.json()
            upcoming += holidays_next
            # Filter again for only future holidays
            upcoming = [
                h for h in upcoming
                if datetime.datetime.strptime(h["date"], "%Y-%m-%d").date() >= today
            ]
        # Return the next `count` holidays
        return [
            {
                "date": h["date"],
                "localName": h["localName"],
                "name": h["name"]
            }
            for h in upcoming[:count]
        ]
    except Exception as e:
        logging.error(f"Failed to fetch bank holidays: {e}")
        return None


def fetch_uv_index(location="Berlin"):
    """
    Fetches the UV index for the given location from wttr.in.
    Returns:
        str: The UV index as a string, or None on failure.
    """
    url = f"https://wttr.in/{location}?format=%u"
    try:
        response = requests.get(url, timeout=10)
        if response.status_code == 200:
            return response.text.strip()
        else:
            logging.error(f"Failed to fetch UV index: HTTP {response.status_code}")
    except Exception as e:
        logging.error(f"Error fetching UV index: {e}")
    return None


class APOD:
    def __init__(self, api_key="DEMO_KEY", save_dir="/tmp"):
        self.api_key = api_key
        self.save_dir = save_dir

    def apod_data(self):
        """
        Downloads NASA's Astronomy Picture of the Day (APOD) and returns its description text.
        Returns:
            tuple: (image_path, description_string) or (None, None) on failure.
        """
        apod_url = f"https://api.nasa.gov/planetary/apod?api_key={self.api_key}"
        try:
            resp = requests.get(apod_url, timeout=10)
            if resp.status_code != 200:
                logging.error(f"APOD: Failed to fetch metadata: {resp.status_code}")
                return None, None
            data = resp.json()
            img_url = data.get("hdurl") or data.get("url")
            desc = data.get("explanation", "")
            if not img_url:
                logging.error("APOD: No image URL found in response")
                return None, None

            # Download image
            img_resp = requests.get(img_url, timeout=10)
            if img_resp.status_code != 200:
                logging.error(f"APOD: Failed to download image: {img_resp.status_code}")
                return None, None

            img_ext = os.path.splitext(img_url)[-1]
            img_path = os.path.join(self.save_dir, f"apod{img_ext}")
            with open(img_path, "wb") as f:
                f.write(img_resp.content)

            # Return image path and description string
            return img_path, desc
        except Exception as e:
            logging.error(f"APOD: Error fetching APOD: {e}")
            return None, None

class HtmlPage:
    @staticmethod
    def show_display_data():
        state = Display.query_state()
        if not state:
            return "<p>Display state unavailable</p>"

        name = "display-0"
        address = state.get('address')
        port = state.get('port')
        res_x = state.get('res_x')
        res_y = state.get('res_y')

        items = [
            f"<li>Name: {html.escape(name)}</li>",
            f"<li>Address: {html.escape(str(address))}</li>",
            f"<li>Port: {html.escape(str(port))}</li>",
            f"<li>Resolution: {html.escape(str(res_x))} x {html.escape(str(res_y))}</li>",
        ]
        return "<ul>" + "".join(items) + "</ul>"

    @staticmethod
    def show_playlist_data():
        # Prefer global playlist created in __main__
        pl_global = globals().get('playlist') if 'playlist' in globals() else None
        playlist_items = pl_global.playlist if pl_global else None

        # Fallback: build playlist via Playlist.create() using env URIs
        if not playlist_items:
            uris_env = os.environ.get('URI') or os.environ.get('URIS')
            if uris_env:
                # Split by common separators
                for sep in ["\n", ",", ";"]:
                    uris_env = uris_env.replace(sep, " ")
                uris_list = [u.strip() for u in uris_env.split() if u.strip()]
                try:
                    tmp_pl = Playlist(uris_list, 5, Theme('default'), [], None)
                    playlist_items = tmp_pl.create(uris_list)
                except Exception:
                    playlist_items = None

        if not playlist_items:
            return "<p>No playlist items available</p>"

        table = [
            "<table>",
            "<thead><tr><th>#</th><th>URI</th><th>Player</th><th>Time (s)</th></tr></thead>",
            "<tbody>",
        ]
        for item in playlist_items:
            num = str(item.get('num', ''))
            uri = str(item.get('uri', ''))
            player = str(item.get('player', ''))
            play_time_s = str(item.get('play_time_s', ''))
            table.append(
                f"<tr><td>{html.escape(num)}</td><td>{html.escape(uri)}</td><td>{html.escape(player)}</td><td>{html.escape(play_time_s)}</td></tr>"
            )
        table.append("</tbody>")
        table.append("</table>")
        return "".join(table)

    @staticmethod
    def page_display():
        # Top screenshot image that refreshes via fetch
        body = (
            '<img id="screenshot" src="/api/v1/screenshot" alt="Screenshot" style="max-width:100%;" />'
            '<script>'
            'async function refreshScreenshot(){'
            '  try {'
            '    const res = await fetch("/api/v1/screenshot", {cache: "no-store"});'
            '    if(!res.ok) return;'
            '    const blob = await res.blob();'
            '    const url = URL.createObjectURL(blob);'
            '    const img = document.getElementById("screenshot");'
            '    const old = img.src;'
            '    img.src = url;'
            '    if(old.startsWith("blob:")) { try { URL.revokeObjectURL(old); } catch(_){} }'
            '  } catch(e) { /* ignore */ }'
            '}'
            'setInterval(refreshScreenshot, 1000);'
            '</script>'
        )
        body += HtmlPage.show_display_data()
        body += HtmlPage.show_playlist_data()
        return "<html><head><title>ISS Display</title></head><body>" + body + "</body></html>"

if __name__ == "__main__":

    parser = configargparse.ArgParser(description="")
    parser.add_argument('--debug',
                        dest='debug',
                        env_var='DEBUG',
                        help="Show debug output",
                        type=bool,
                        default=False)
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
    parser.add_argument('--probe-ip',
                        dest='probe_ip',
                        env_var='PROBE_IP',
                        help="The address to probe for",
                        type=str,
                        default="9.9.9.9")
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
                        type=bool,
                        default=False)
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
                        type=bool,
                        default=False)
    parser.add_argument('--zeroconf-service-name-prefix',
                        dest='zeroconf_service_name_prefix',
                        env_var='ZEROCONF_PREFIX',
                        help="The name prefix of the service",
                        type=str,
                        default="controller")
    parser.add_argument('--zeroconf-service-type',
                        dest='zeroconf_service_type',
                        env_var='ZEROCONF_TYPE',
                        help="The type of service",
                        type=str,
                        default="_http._tcp.local.")

    args = parser.parse_args()
    if isinstance(args.uris, str):
        uris_list = [args.uris]
    else:
        uris_list = args.uris
    if uris_list and isinstance(uris_list[0], str) and "|" in uris_list[0]:
        uris_list = uris_list[0].split("|")
    args.uris = uris_list
    # Same for MQTT topics
    if args.mqtt_topics and gs.mqtt_topics[0].find("|") != -1:
        args.mqtt_topics[0] = args.mqtt_topics[0].split("|")

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
    probe_ip = args.probe_ip
    theme_name = args.theme_name
    update_controller = args.update_controller
    controller_update_url = args.controller_update_url
    zeroconf_publish_service = args.zeroconf_publish_service
    zc_service_name_prefix = args.zeroconf_service_name_prefix
    zc_service_type = args.zeroconf_service_type
    logfile = args.logfile
    loglevel = args.loglevel
    loglevel = DEBUG
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
            logging.error("Could not find dependency: " + dep + ", aborting..")
            sys.exit(1)

    env = os.environ.copy()

    if debug:
        for k, v in env.items():
            logging.debug(k + '=' + v)
            logging.debug(System.list_processes())

    if listen_port < 1025 or listen_port > 65535:
        logging.error("Invalid port, aborting..")
        sys.exit(1)

    if stream_source not in stream_sources:
        sources_str = " ".join(str(x) for x in stream_sources)
        logging.error("Invalid source: {}, aborting..".format(stream_source))
        logging.info("Possible choices are: " + sources_str)
        #sys.exit(1)

    hostname = socket.gethostname()
    local_ip = ""

    local_ip = System.net_iface_address(probe_ip)
    if not local_ip:
        local_ip = System.net_iface_address(probe_ip)

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
        logging.warning("Expected no windows but found {}"
                        .format(nwins))

    theme = Theme(theme_name)
    logging.info(f"PATH: {env.get('PATH', '')}")
    logging.info("Using theme: {}".format(theme_name))
    logging.info("URIs: {}".format(uris))
    playlist = Playlist(uris, 5, theme, mqtt_topics, location)
    logging.info("Playlist: {}".format(playlist))
    threads = playlist.start_player(probe_ip)
    started = len(threads)
    expected = len(playlist.playlist)
    logging.info("Started {} {}".format(started, "player" if started == 1 else "players"))
    if expected != started:
        logging.info("Player mismatch: expected {} from URIs, started {}".format(expected, started))
    iss = Iss(threads)

    stream = Stream(stream_source)

    # Publish service on the network via mDNS
    if zeroconf_publish_service:
        zc_listen_address = listen_address

        if "0.0.0.0" == zc_listen_address:
            zc_listen_address = System.net_iface_address(probe_ip)

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

    # Start ASGI server via Daphne
    try:
        cmd = ['daphne', '-b', listen_address, '-p', str(listen_port), 'controller:asgi_app']
        logging.info("Starting Daphne ASGI server on %s:%s" % (listen_address, listen_port))
        # Ensure the controller directory is importable so Daphne can import 'controller:asgi_app'
        module_dir = os.path.dirname(os.path.abspath(__file__))
        env_mod = os.environ.copy()
        env_mod['PYTHONPATH'] = module_dir + (os.pathsep + env_mod['PYTHONPATH'] if 'PYTHONPATH' in env_mod else '')
        subprocess.run(cmd, env=env_mod)
        module_dir = os.path.dirname(os.path.abspath(__file__))
        subprocess.run(cmd, env=env, shell=True)
    except FileNotFoundError:
        logging.error("Daphne not found. Install 'daphne' to run the ASGI server.")
        sys.exit(1)
