#!/usr/bin/env python3
import requests
import asyncio
import configargparse
from flask import Flask, send_file
from hnapi import HnApi
import html
import json
import logging
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

wserver = Flask(__name__)
dependencies = []
stream_sources = ["static-images", "v4l2", "vnc-browser"]
cmds = {"clock":        "humanbeans_clock",
        "image_viewer": "imv",
        "media_player": "mpv",
        "screenshot":   "grim"}


@wserver.route("/")
def info():
    return True


# Readiness
@wserver.route('/healthy')
def healthy():
    return "OK"


# Liveness
@wserver.route('/healthz')
def healthz():
    return probe_liveness()


@wserver.route("/screen")
def screen():
    data = {}
    data["name"] = "display-0"
    data["os_release"] = "iss-display"
    data['listen_address'] = display.address
    data['listen_port'] = display.port
    data['res_x'] = display.res_x
    data['res_y'] = display.res_y
    data['playlist'] = playlist.playlist
    data['uris'] = playlist.uris
    data['streams'] = stream.streams
    json_data = json.dumps(data)
    return json_data


def probe_liveness():
    return "OK"


@wserver.route("/screenshot")
def display_screenshot():
    fn = display.screenshot()
    if not os.path.isfile(fn):
        logging.error("screenshot: File {} does not exist".format(fn))
        #return False
    return send_file(fn, mimetype='image/png')


def which(cmd):
    def is_exe(fpath):
        return os.path.isfile(fpath) and os.access(fpath, os.X_OK)

    fpath, fname = os.path.split(cmd)
    if fpath:
        if is_exe(cmd):
            return cmd
    else:
        for path in os.environ["PATH"].split(os.pathsep):
            exe_file = os.path.join(path, cmd)
            if is_exe(exe_file):
                return exe_file

    return None


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


class System:

    def list_processes(limit=0):
        # Processes without kernel threads
        cmd = ['ps', 'ux', '--ppid', '2', '-p', '2', '--deselect']

        # FreeBSD
        cmd = ['ps', 'ux']

        ps = subprocess.Popen(cmd,
                              stdout=subprocess.PIPE,
                              shell=False,
                              encoding="utf8").communicate()[0]
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
            data += line
        return data

    def net_valid_ip_address(ip_address):
        try:
            socket.inet_pton(socket.AF_INET, ip_address)
        except:
            try:
                socket.inet_pton(socket.AF_INET6, address)
            except:
                logging.warning('%s is an invalid IP address' % (ip_addr))
                return False

        return True

    def net_iface_address(ip_address):

        try:
            socket.inet_pton(socket.AF_INET6, ip_address)
            s = socket.socket(socket.AF_INET6, socket.SOCK_DGRAM)
        except:
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                socket.inet_pton(socket.AF_INET, ip_address)
            except:
                logging.warning('%s is an invalid IP address' % (ip_addr))
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
                                         encoding="utf8")
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
            elif uri.startswith("iss-net://"):
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
            if item["player"] == "browser":
                # start_browser() wants a list
                # but we want to start an instance for each URL
                urls = list()
                urls.append(item["uri"])
                x = threading.Thread(target=self.start_browser,
                                     args=(urls,))
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
                                     args=(self.theme))
            elif item["player"] == "news":
                x = threading.Thread(target=self.start_news_view,
                                     args=(self.news,
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

    def start_browser(self, urls):
        cmd = ['webdriver_util.py']
        for url in urls:
            cmd.append("--url")
            cmd.append(url)

        Popen(cmd,
              env=env,
              start_new_session=True,
              close_fds=True,
              encoding='utf8')

        return True

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

    def start_net_view(self, probe_ip, img_bg):
        texts = list()
        net = System.net_data(probe_ip)
        texts.append(net["address"])
        texts.append(net["addresses"])
        texts.append(net["public_ip"])
        texts.append(net["resolvconf"])
        view = Wayland_view(display.res_x, display.res_y, len(texts), theme)
        view.s_objects[0]["font_size"] = 14
        view.s_objects[0]["alignment"] = "left"
        view.s_objects[1]["font_size"] = 14
        view.s_objects[1]["alignment"] = "left"
        view.show_text(texts, img_bg)

    def start_proc_view(self, img_bg):
        texts = list()
        texts.append(System.list_processes(23))
        view = Wayland_view(display.res_x, display.res_y, len(texts), theme)
        view.s_objects[0]["font_size"] = 14
        view.s_objects[0]["alignment"] = "left"
        view.show_text(texts, img_bg)

    def start_sys_view(self, img_bg, probe_ip):
        net = System.net_data(probe_ip)
        sys = System.sys_data()
        texts = list()
        texts.append(System.os_release())
        texts.append(System.uptime())
        texts.append(sys["uptime"])
        texts.append(sys["data"])
        texts.append(net["address"])
        texts.append(net["addresses"])
        texts.append(net["online_status"] + " " + net["public_ip"])
        texts.append(f"Listen address: {display.address}:{display.port}")
        texts.append(f"Display Resolution: {display.res_x}x{display.res_y}")
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
        view = Wayland_view(display.res_x, display.res_y, len(texts), theme)
        view.s_objects[0]["font_size"] = 80
        view.s_objects[0]["alignment"] = "left"
        view.s_objects[1]["font_size"] = 40
        view.s_objects[1]["alignment"] = "left"
        view.s_objects[2]["font_size"] = 20
        view.s_objects[2]["alignment"] = "left"
        view.show_text(texts, img_bg)
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
                ], stdin=subprocess.PIPE)

        elif stream_source == "v4l2":
            gstreamer = subprocess.Popen([
                'gst-launch-1.0', '-v', '-e',
                'v4l2src', 'device=' + source_device,
                '!', 'videorate',
                '!', 'video/x-raw,framerate=25/2',
                '!', 'queue',
                '!', 'tcpserversink', 'host=' + ip + '',
                'port=' + str(port) + ''
                ], stdin=subprocess.PIPE)

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
                    encoding='utf8')

        # Handle wf-recorder prompt for overwriting the file
        p.stdin.write('Y\n')
        p.stdin.flush()

        return True

    def stream_v4l2_ffmpeg(self):
        ffmpeg = subprocess.Popen([
            'ffmpeg', '-f', 'v4l2', '-i', '/dev/video0',
            '-codec', 'copy',
            '-f', 'mpegts', 'udp:0.0.0.0:6000'
            ])


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
    colors = true
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
        self.module_config["sysdata"] = self.config_common + """
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
        self.module_config["uptime"] = self.config_common + """
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

    def run_module(self):
        config_file = self.write_config()
        cmd = 'py3status -c ' + config_file.name
        logging.info(f"Py3status: Running module {self.module_name} {cmd}")
        p = subprocess.Popen(cmd, shell=True,
                                  stdout=subprocess.PIPE,
                                  stderr=subprocess.STDOUT,
                                  start_new_session=True,
                                  close_fds=True,
                                  encoding="utf8")
        output, error = p.communicate()
        output = output.replace("\n", "")
        output = output.replace("[", "")
        output = output.replace("]", "")
        output = "[" + output + "]"
        config_file.close()

        try:
            module_data = json.loads(output)
            module_data = module_data[1]["full_text"]
        except ValueError as e:
            logging.error(f"Py3status: Failed to parse output as JSON: {output}")
            return False

        return module_data

    def write_config(self):
        tmp = tempfile.NamedTemporaryFile()
        tmp.write(self.module_config[self.module_name].encode())
        tmp.seek(0)

        return tmp


class Weather:

    def __init__(self, location):
        self.location = location
        self.weather = self.fetch_weather()

    def fetch_weather(self):
        url = "https://wttr.in/{}?format=j1".format(self.location)
        logging.info("iss-weather: Fetching weather for {} at {}"
                     .format(self.location, url))

        try:
            data = requests.get(url, timeout=10)
        except requests.ReadTimeout as e:
            logging.error("weather: Timeout for request {}"
                          .format(e))
        except Exception:
            logging.error("weather: Error requesting weather")

        if not data:
            logging.error("Failed to fetch weather data")

        try:
            data = data.content
            data = json.loads(data)
        except ValueError as e:
            logging.error("weather: Failed to decode data")
            logging.error(e)
            data = {}
            sys.exit(1)

        icon = self.icon(data["current_condition"][0]["weatherDesc"][0]["value"])

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
                    "bg_colour_r": 7,
                    "bg_colour_g": 59,
                    "bg_colour_b": 76,
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

    def show_text(self, texts, img_bg=False, fullscreen=False):
        logging.info("view: Have {} text block(s)".format(len(texts)))

        n = 0
        for text in texts:
            logging.debug(f"Showing text: {text}")
            self.s_objects[n]["text"] = html.escape(str(text).replace("&", "&amp;"))
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
        w = view.Window(self.conn,
                        self.window,
                        self.s_objects,
                        redraw=view.draw_images_with_text,
                        fullscreen=fullscreen,
                        class_="iss-view")

        self.create_window(w)


class Display:

    def __init__(self, address, port, res_x=1366, res_y=768):
        self.address = address
        self.port = port
        self.res_x = res_x
        self.res_y = res_y
        self.start_time = time.time()
        self.switching_windows = list()
        self.window_blacklist = list()
        self.screenshot_path = "/tmp"
        self.screenshot_file = "screenshot.png"
        self.socket_path = self.get_socket_path()

        logging.info("Resolution: {} x {}"
                     .format(self.res_x, self.res_y))

        # We do not want to handle existing windows,
        # so we put their IDs on a blacklist
        existing_windows = self.get_windows()
        for win in existing_windows:
            self.window_blacklist.append(win["id"])

        logging.info("Blacklisted {} windows"
                     .format(len(self.window_blacklist)))

        self.x = threading.Thread(target=self.focus_next_window, args=(3,))
        self.x.start()

    def get_socket_path(self):
        cmd = ['sway', '--get-socketpath']
        p = subprocess.Popen(cmd,
                             shell=False,
                             stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT,
                             encoding="utf8")

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
                             stderr=subprocess.PIPE)

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
                    logging.debug("display: Expected windows to display but there is none")

                    continue

            next_window = self.switching_windows.pop()
            logging.info("display: Switching focus to: {}"
                         .format(next_window["id"]))
            cmd = "swaymsg -s {} [con_id={}] focus"\
                  .format(self.socket_path, next_window["id"])
            p = subprocess.Popen(cmd, shell=True)
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
                             shell=True)

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
                             encoding="utf8")

        res = p.communicate()[0]

    def screenshot(self):
        fn = self.screenshot_path + "/" + self.screenshot_file

        logging.info("Saving screenshot to {}".format(fn))
        cmd = [cmds["screenshot"],
               fn]

        p = Popen(cmd,
                  env=env,
                  start_new_session=True,
                  close_fds=True)

        return fn


class Iss:

    def __init__(self, threads):
        self.threads = threads


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
    # workaround: Make url a new list if first element contains a '|'
    # since we can specify --uri multiple times but not URI as
    # an env var. We use pipe: | as seperator since it is not allowed in URIs
    if args.uris[0].find("|") != -1:
        args.uris = args.uris[0].split("|")
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
        #download_file(controller_update_url, os.path.abspath(__file__), False)
        download_file(controller_update_url, os.path.abspath("/tmp/" + os.path.basename(__file__)), False)
        with open(path_update, "w") as file:
            file.write("Update done\n")
        reexec_self()

    display = Display(local_ip, listen_port)

    nwins = len(display.get_windows())
    if nwins > 0:
        logging.warning("Expected no windows but found {}"
                        .format(nwins))

    theme = Theme(theme_name)
    logging.info("Using theme: {}".format(theme_name))
    logging.info("URIs: {}".format(uris))
    playlist = Playlist(uris, 5, theme, mqtt_topics, location)
    logging.info("Playlist: {}".format(playlist))
    threads = playlist.start_player(probe_ip)
    logging.info("Started {} player".format(len(threads)))
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

        logging.shutdown()

        sys.exit(0)

    # Register signal handler
    signal.signal(signal.SIGINT, signal_handler)

    # Change view regularly
    display.start_time = time.time()

    # Start webserver
    wserver.run(host=listen_address)
