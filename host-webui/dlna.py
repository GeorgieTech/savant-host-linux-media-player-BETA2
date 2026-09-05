#!/usr/bin/env python3
"""UPnP/DLNA MediaRenderer. SSDP + SOAP, ffmpeg|paplay via HostPlayer."""
import hashlib
import os
import re
import socket
import struct
import threading
import time
import uuid
import xml.etree.ElementTree as ET
from email.utils import formatdate
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse
from xml.sax.saxutils import escape

from airplay import DEFAULT_NAME, sanitize_name

SSDP_ADDR = "239.255.255.250"
SSDP_PORT = 1900
HTTP_PORT = 49494
NS_SOAP = "http://schemas.xmlsoap.org/soap/envelope/"
NS_UPNP_ERR = "urn:schemas-upnp-org:control-1-0"
UPNP_SERVER = "Linux/4.14 UPnP/1.0 Gigawatt/0.10"
TRANSPORT_ACTIONS = "Play,Pause,Stop,Seek,Next,Previous"
SINK = ",".join(
    [
        "http-get:*:audio/mpeg:*",
        "http-get:*:audio/mp3:*",
        "http-get:*:audio/flac:*",
        "http-get:*:audio/x-flac:*",
        "http-get:*:audio/ogg:*",
        "http-get:*:audio/opus:*",
        "http-get:*:audio/wav:*",
        "http-get:*:audio/x-wav:*",
        "http-get:*:audio/L16:*",
        "http-get:*:audio/mp4:*",
        "http-get:*:audio/aac:*",
        "http-get:*:audio/*:*",
        "http-get:*:*:*",
    ]
)


def _localname(tag):
    if tag and tag.startswith("{") and "}" in tag:
        return tag.split("}", 1)[1]
    return tag or ""


def _xml_text(parent, names):
    wanted = set(names)
    if parent is None:
        return ""
    for child in parent.iter():
        if _localname(child.tag) in wanted and (child.text or "").strip():
            return child.text.strip()
    return ""


def _parse_hms(value):
    value = (value or "").strip()
    if not value or value in (".", "NOT_IMPLEMENTED"):
        return 0.0
    parts = value.split(":")
    try:
        parts = [float(p) for p in parts]
    except ValueError:
        return 0.0
    if len(parts) == 3:
        return parts[0] * 3600 + parts[1] * 60 + parts[2]
    if len(parts) == 2:
        return parts[0] * 60 + parts[1]
    return parts[0] if parts else 0.0


def _fmt_hms(sec):
    sec = max(0, int(round(float(sec or 0))))
    return "%d:%02d:%02d" % (sec // 3600, (sec % 3600) // 60, sec % 60)


def _parse_didl(meta):
    info = {"title": "", "artist": "", "album": "", "duration": 0.0}
    raw = (meta or "").strip()
    if not raw:
        return info
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        return info
    info["title"] = _xml_text(root, ("title",))
    info["artist"] = _xml_text(root, ("artist", "creator"))
    info["album"] = _xml_text(root, ("album",))
    for child in root.iter():
        if _localname(child.tag) == "res":
            info["duration"] = _parse_hms(child.attrib.get("duration") or "")
            break
    return info


def _uuid_from_name(name):
    digest = hashlib.md5(("gigawatt-dlna:" + (name or "")).encode("utf-8")).hexdigest()
    return str(uuid.UUID(digest))


AVT_SCPD = """<?xml version="1.0"?>
<scpd xmlns="urn:schemas-upnp-org:service-1-0">
  <specVersion><major>1</major><minor>0</minor></specVersion>
  <actionList>
    <action><name>SetAVTransportURI</name><argumentList>
      <argument><name>InstanceID</name><direction>in</direction><relatedStateVariable>A_ARG_TYPE_InstanceID</relatedStateVariable></argument>
      <argument><name>CurrentURI</name><direction>in</direction><relatedStateVariable>AVTransportURI</relatedStateVariable></argument>
      <argument><name>CurrentURIMetaData</name><direction>in</direction><relatedStateVariable>AVTransportURIMetaData</relatedStateVariable></argument>
    </argumentList></action>
    <action><name>GetMediaInfo</name><argumentList>
      <argument><name>InstanceID</name><direction>in</direction><relatedStateVariable>A_ARG_TYPE_InstanceID</relatedStateVariable></argument>
      <argument><name>NrTracks</name><direction>out</direction><relatedStateVariable>NumberOfTracks</relatedStateVariable></argument>
      <argument><name>MediaDuration</name><direction>out</direction><relatedStateVariable>CurrentTrackDuration</relatedStateVariable></argument>
      <argument><name>CurrentURI</name><direction>out</direction><relatedStateVariable>AVTransportURI</relatedStateVariable></argument>
      <argument><name>CurrentURIMetaData</name><direction>out</direction><relatedStateVariable>AVTransportURIMetaData</relatedStateVariable></argument>
      <argument><name>NextURI</name><direction>out</direction><relatedStateVariable>NextAVTransportURI</relatedStateVariable></argument>
      <argument><name>NextURIMetaData</name><direction>out</direction><relatedStateVariable>NextAVTransportURIMetaData</relatedStateVariable></argument>
      <argument><name>PlayMedium</name><direction>out</direction><relatedStateVariable>PlaybackStorageMedium</relatedStateVariable></argument>
      <argument><name>RecordMedium</name><direction>out</direction><relatedStateVariable>RecordStorageMedium</relatedStateVariable></argument>
      <argument><name>WriteStatus</name><direction>out</direction><relatedStateVariable>RecordMediumWriteStatus</relatedStateVariable></argument>
    </argumentList></action>
    <action><name>GetTransportInfo</name><argumentList>
      <argument><name>InstanceID</name><direction>in</direction><relatedStateVariable>A_ARG_TYPE_InstanceID</relatedStateVariable></argument>
      <argument><name>CurrentTransportState</name><direction>out</direction><relatedStateVariable>TransportState</relatedStateVariable></argument>
      <argument><name>CurrentTransportStatus</name><direction>out</direction><relatedStateVariable>TransportStatus</relatedStateVariable></argument>
      <argument><name>CurrentSpeed</name><direction>out</direction><relatedStateVariable>TransportPlaySpeed</relatedStateVariable></argument>
    </argumentList></action>
    <action><name>GetPositionInfo</name><argumentList>
      <argument><name>InstanceID</name><direction>in</direction><relatedStateVariable>A_ARG_TYPE_InstanceID</relatedStateVariable></argument>
      <argument><name>Track</name><direction>out</direction><relatedStateVariable>CurrentTrack</relatedStateVariable></argument>
      <argument><name>TrackDuration</name><direction>out</direction><relatedStateVariable>CurrentTrackDuration</relatedStateVariable></argument>
      <argument><name>TrackMetaData</name><direction>out</direction><relatedStateVariable>CurrentTrackMetaData</relatedStateVariable></argument>
      <argument><name>TrackURI</name><direction>out</direction><relatedStateVariable>CurrentTrackURI</relatedStateVariable></argument>
      <argument><name>RelTime</name><direction>out</direction><relatedStateVariable>RelativeTimePosition</relatedStateVariable></argument>
      <argument><name>AbsTime</name><direction>out</direction><relatedStateVariable>AbsoluteTimePosition</relatedStateVariable></argument>
      <argument><name>RelCount</name><direction>out</direction><relatedStateVariable>RelativeCounterPosition</relatedStateVariable></argument>
      <argument><name>AbsCount</name><direction>out</direction><relatedStateVariable>AbsoluteCounterPosition</relatedStateVariable></argument>
    </argumentList></action>
    <action><name>GetDeviceCapabilities</name><argumentList>
      <argument><name>InstanceID</name><direction>in</direction><relatedStateVariable>A_ARG_TYPE_InstanceID</relatedStateVariable></argument>
      <argument><name>PlayMedia</name><direction>out</direction><relatedStateVariable>PossiblePlaybackStorageMedia</relatedStateVariable></argument>
      <argument><name>RecMedia</name><direction>out</direction><relatedStateVariable>PossibleRecordStorageMedia</relatedStateVariable></argument>
      <argument><name>RecQualityModes</name><direction>out</direction><relatedStateVariable>PossibleRecordQualityModes</relatedStateVariable></argument>
    </argumentList></action>
    <action><name>GetTransportSettings</name><argumentList>
      <argument><name>InstanceID</name><direction>in</direction><relatedStateVariable>A_ARG_TYPE_InstanceID</relatedStateVariable></argument>
      <argument><name>PlayMode</name><direction>out</direction><relatedStateVariable>CurrentPlayMode</relatedStateVariable></argument>
      <argument><name>RecQualityMode</name><direction>out</direction><relatedStateVariable>CurrentRecordQualityMode</relatedStateVariable></argument>
    </argumentList></action>
    <action><name>Stop</name><argumentList>
      <argument><name>InstanceID</name><direction>in</direction><relatedStateVariable>A_ARG_TYPE_InstanceID</relatedStateVariable></argument>
    </argumentList></action>
    <action><name>Play</name><argumentList>
      <argument><name>InstanceID</name><direction>in</direction><relatedStateVariable>A_ARG_TYPE_InstanceID</relatedStateVariable></argument>
      <argument><name>Speed</name><direction>in</direction><relatedStateVariable>TransportPlaySpeed</relatedStateVariable></argument>
    </argumentList></action>
    <action><name>Pause</name><argumentList>
      <argument><name>InstanceID</name><direction>in</direction><relatedStateVariable>A_ARG_TYPE_InstanceID</relatedStateVariable></argument>
    </argumentList></action>
    <action><name>Seek</name><argumentList>
      <argument><name>InstanceID</name><direction>in</direction><relatedStateVariable>A_ARG_TYPE_InstanceID</relatedStateVariable></argument>
      <argument><name>Unit</name><direction>in</direction><relatedStateVariable>A_ARG_TYPE_SeekMode</relatedStateVariable></argument>
      <argument><name>Target</name><direction>in</direction><relatedStateVariable>A_ARG_TYPE_SeekTarget</relatedStateVariable></argument>
    </argumentList></action>
    <action><name>Next</name><argumentList>
      <argument><name>InstanceID</name><direction>in</direction><relatedStateVariable>A_ARG_TYPE_InstanceID</relatedStateVariable></argument>
    </argumentList></action>
    <action><name>Previous</name><argumentList>
      <argument><name>InstanceID</name><direction>in</direction><relatedStateVariable>A_ARG_TYPE_InstanceID</relatedStateVariable></argument>
    </argumentList></action>
    <action><name>GetCurrentTransportActions</name><argumentList>
      <argument><name>InstanceID</name><direction>in</direction><relatedStateVariable>A_ARG_TYPE_InstanceID</relatedStateVariable></argument>
      <argument><name>Actions</name><direction>out</direction><relatedStateVariable>CurrentTransportActions</relatedStateVariable></argument>
    </argumentList></action>
    <action><name>SetNextAVTransportURI</name><argumentList>
      <argument><name>InstanceID</name><direction>in</direction><relatedStateVariable>A_ARG_TYPE_InstanceID</relatedStateVariable></argument>
      <argument><name>NextURI</name><direction>in</direction><relatedStateVariable>NextAVTransportURI</relatedStateVariable></argument>
      <argument><name>NextURIMetaData</name><direction>in</direction><relatedStateVariable>NextAVTransportURIMetaData</relatedStateVariable></argument>
    </argumentList></action>
  </actionList>
  <serviceStateTable>
    <stateVariable sendEvents="no"><name>TransportState</name><dataType>string</dataType></stateVariable>
    <stateVariable sendEvents="no"><name>TransportStatus</name><dataType>string</dataType></stateVariable>
    <stateVariable sendEvents="no"><name>TransportPlaySpeed</name><dataType>string</dataType></stateVariable>
    <stateVariable sendEvents="no"><name>NumberOfTracks</name><dataType>ui4</dataType></stateVariable>
    <stateVariable sendEvents="no"><name>CurrentTrack</name><dataType>ui4</dataType></stateVariable>
    <stateVariable sendEvents="no"><name>CurrentTrackDuration</name><dataType>string</dataType></stateVariable>
    <stateVariable sendEvents="no"><name>CurrentMediaDuration</name><dataType>string</dataType></stateVariable>
    <stateVariable sendEvents="no"><name>CurrentTrackMetaData</name><dataType>string</dataType></stateVariable>
    <stateVariable sendEvents="no"><name>CurrentTrackURI</name><dataType>string</dataType></stateVariable>
    <stateVariable sendEvents="no"><name>AVTransportURI</name><dataType>string</dataType></stateVariable>
    <stateVariable sendEvents="no"><name>AVTransportURIMetaData</name><dataType>string</dataType></stateVariable>
    <stateVariable sendEvents="no"><name>NextAVTransportURI</name><dataType>string</dataType></stateVariable>
    <stateVariable sendEvents="no"><name>NextAVTransportURIMetaData</name><dataType>string</dataType></stateVariable>
    <stateVariable sendEvents="no"><name>PlaybackStorageMedium</name><dataType>string</dataType></stateVariable>
    <stateVariable sendEvents="no"><name>RecordStorageMedium</name><dataType>string</dataType></stateVariable>
    <stateVariable sendEvents="no"><name>RecordMediumWriteStatus</name><dataType>string</dataType></stateVariable>
    <stateVariable sendEvents="no"><name>CurrentPlayMode</name><dataType>string</dataType></stateVariable>
    <stateVariable sendEvents="no"><name>CurrentRecordQualityMode</name><dataType>string</dataType></stateVariable>
    <stateVariable sendEvents="no"><name>PossiblePlaybackStorageMedia</name><dataType>string</dataType></stateVariable>
    <stateVariable sendEvents="no"><name>PossibleRecordStorageMedia</name><dataType>string</dataType></stateVariable>
    <stateVariable sendEvents="no"><name>PossibleRecordQualityModes</name><dataType>string</dataType></stateVariable>
    <stateVariable sendEvents="no"><name>RelativeTimePosition</name><dataType>string</dataType></stateVariable>
    <stateVariable sendEvents="no"><name>AbsoluteTimePosition</name><dataType>string</dataType></stateVariable>
    <stateVariable sendEvents="no"><name>RelativeCounterPosition</name><dataType>i4</dataType></stateVariable>
    <stateVariable sendEvents="no"><name>AbsoluteCounterPosition</name><dataType>i4</dataType></stateVariable>
    <stateVariable sendEvents="no"><name>A_ARG_TYPE_SeekMode</name><dataType>string</dataType>
      <allowedValueList>
        <allowedValue>REL_TIME</allowedValue>
        <allowedValue>ABS_TIME</allowedValue>
        <allowedValue>TRACK_NR</allowedValue>
      </allowedValueList>
    </stateVariable>
    <stateVariable sendEvents="no"><name>A_ARG_TYPE_SeekTarget</name><dataType>string</dataType></stateVariable>
    <stateVariable sendEvents="no"><name>A_ARG_TYPE_InstanceID</name><dataType>ui4</dataType></stateVariable>
    <stateVariable sendEvents="no"><name>CurrentTransportActions</name><dataType>string</dataType></stateVariable>
    <stateVariable sendEvents="yes"><name>LastChange</name><dataType>string</dataType></stateVariable>
  </serviceStateTable>
</scpd>
"""

RCS_SCPD = """<?xml version="1.0"?>
<scpd xmlns="urn:schemas-upnp-org:service-1-0">
  <specVersion><major>1</major><minor>0</minor></specVersion>
  <actionList>
    <action><name>SetMute</name><argumentList>
      <argument><name>InstanceID</name><direction>in</direction><relatedStateVariable>A_ARG_TYPE_InstanceID</relatedStateVariable></argument>
      <argument><name>Channel</name><direction>in</direction><relatedStateVariable>A_ARG_TYPE_Channel</relatedStateVariable></argument>
      <argument><name>DesiredMute</name><direction>in</direction><relatedStateVariable>Mute</relatedStateVariable></argument>
    </argumentList></action>
    <action><name>GetMute</name><argumentList>
      <argument><name>InstanceID</name><direction>in</direction><relatedStateVariable>A_ARG_TYPE_InstanceID</relatedStateVariable></argument>
      <argument><name>Channel</name><direction>in</direction><relatedStateVariable>A_ARG_TYPE_Channel</relatedStateVariable></argument>
      <argument><name>CurrentMute</name><direction>out</direction><relatedStateVariable>Mute</relatedStateVariable></argument>
    </argumentList></action>
    <action><name>SetVolume</name><argumentList>
      <argument><name>InstanceID</name><direction>in</direction><relatedStateVariable>A_ARG_TYPE_InstanceID</relatedStateVariable></argument>
      <argument><name>Channel</name><direction>in</direction><relatedStateVariable>A_ARG_TYPE_Channel</relatedStateVariable></argument>
      <argument><name>DesiredVolume</name><direction>in</direction><relatedStateVariable>Volume</relatedStateVariable></argument>
    </argumentList></action>
    <action><name>GetVolume</name><argumentList>
      <argument><name>InstanceID</name><direction>in</direction><relatedStateVariable>A_ARG_TYPE_InstanceID</relatedStateVariable></argument>
      <argument><name>Channel</name><direction>in</direction><relatedStateVariable>A_ARG_TYPE_Channel</relatedStateVariable></argument>
      <argument><name>CurrentVolume</name><direction>out</direction><relatedStateVariable>Volume</relatedStateVariable></argument>
    </argumentList></action>
  </actionList>
  <serviceStateTable>
    <stateVariable sendEvents="yes"><name>LastChange</name><dataType>string</dataType></stateVariable>
    <stateVariable sendEvents="no"><name>Mute</name><dataType>boolean</dataType></stateVariable>
    <stateVariable sendEvents="no"><name>Volume</name><dataType>ui2</dataType>
      <allowedValueRange><minimum>0</minimum><maximum>100</maximum><step>1</step></allowedValueRange>
    </stateVariable>
    <stateVariable sendEvents="no"><name>A_ARG_TYPE_Channel</name><dataType>string</dataType></stateVariable>
    <stateVariable sendEvents="no"><name>A_ARG_TYPE_InstanceID</name><dataType>ui4</dataType></stateVariable>
  </serviceStateTable>
</scpd>
"""

CM_SCPD = """<?xml version="1.0"?>
<scpd xmlns="urn:schemas-upnp-org:service-1-0">
  <specVersion><major>1</major><minor>0</minor></specVersion>
  <actionList>
    <action><name>GetProtocolInfo</name><argumentList>
      <argument><name>Source</name><direction>out</direction><relatedStateVariable>SourceProtocolInfo</relatedStateVariable></argument>
      <argument><name>Sink</name><direction>out</direction><relatedStateVariable>SinkProtocolInfo</relatedStateVariable></argument>
    </argumentList></action>
    <action><name>GetCurrentConnectionIDs</name><argumentList>
      <argument><name>ConnectionIDs</name><direction>out</direction><relatedStateVariable>CurrentConnectionIDs</relatedStateVariable></argument>
    </argumentList></action>
    <action><name>GetCurrentConnectionInfo</name><argumentList>
      <argument><name>ConnectionID</name><direction>in</direction><relatedStateVariable>A_ARG_TYPE_ConnectionID</relatedStateVariable></argument>
      <argument><name>RcsID</name><direction>out</direction><relatedStateVariable>A_ARG_TYPE_RcsID</relatedStateVariable></argument>
      <argument><name>AVTransportID</name><direction>out</direction><relatedStateVariable>A_ARG_TYPE_AVTransportID</relatedStateVariable></argument>
      <argument><name>ProtocolInfo</name><direction>out</direction><relatedStateVariable>A_ARG_TYPE_ProtocolInfo</relatedStateVariable></argument>
      <argument><name>PeerConnectionManager</name><direction>out</direction><relatedStateVariable>A_ARG_TYPE_ConnectionManager</relatedStateVariable></argument>
      <argument><name>PeerConnectionID</name><direction>out</direction><relatedStateVariable>A_ARG_TYPE_ConnectionID</relatedStateVariable></argument>
      <argument><name>Direction</name><direction>out</direction><relatedStateVariable>A_ARG_TYPE_Direction</relatedStateVariable></argument>
      <argument><name>Status</name><direction>out</direction><relatedStateVariable>A_ARG_TYPE_ConnectionStatus</relatedStateVariable></argument>
    </argumentList></action>
  </actionList>
  <serviceStateTable>
    <stateVariable sendEvents="yes"><name>SourceProtocolInfo</name><dataType>string</dataType></stateVariable>
    <stateVariable sendEvents="yes"><name>SinkProtocolInfo</name><dataType>string</dataType></stateVariable>
    <stateVariable sendEvents="yes"><name>CurrentConnectionIDs</name><dataType>string</dataType></stateVariable>
    <stateVariable sendEvents="no"><name>A_ARG_TYPE_ConnectionStatus</name><dataType>string</dataType></stateVariable>
    <stateVariable sendEvents="no"><name>A_ARG_TYPE_ConnectionID</name><dataType>i4</dataType></stateVariable>
    <stateVariable sendEvents="no"><name>A_ARG_TYPE_AVTransportID</name><dataType>i4</dataType></stateVariable>
    <stateVariable sendEvents="no"><name>A_ARG_TYPE_RcsID</name><dataType>i4</dataType></stateVariable>
    <stateVariable sendEvents="no"><name>A_ARG_TYPE_ProtocolInfo</name><dataType>string</dataType></stateVariable>
    <stateVariable sendEvents="no"><name>A_ARG_TYPE_ConnectionManager</name><dataType>string</dataType></stateVariable>
    <stateVariable sendEvents="no"><name>A_ARG_TYPE_Direction</name><dataType>string</dataType></stateVariable>
  </serviceStateTable>
</scpd>
"""


def _soap_fault(code, description):
    return (
        '<?xml version="1.0"?>'
        '<s:Envelope xmlns:s="%s" s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
        "<s:Body><s:Fault><faultcode>s:Client</faultcode><faultstring>UPnPError</faultstring>"
        "<detail><u:UPnPError xmlns:u=\"%s\"><errorCode>%s</errorCode>"
        "<errorDescription>%s</errorDescription></u:UPnPError></detail>"
        "</s:Fault></s:Body></s:Envelope>"
    ) % (NS_SOAP, NS_UPNP_ERR, int(code), escape(str(description or "Action Failed")))


class _DlnaHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = UPNP_SERVER
    sys_version = ""

    def version_string(self):
        return UPNP_SERVER

    def log_message(self, fmt, *args):
        return

    def do_GET(self):
        renderer = self.server.renderer
        path = urlparse(self.path).path
        pages = {
            "/device.xml": renderer.device_xml(),
            "/AVTransport.xml": AVT_SCPD,
            "/RenderingControl.xml": RCS_SCPD,
            "/ConnectionManager.xml": CM_SCPD,
        }
        body = pages.get(path)
        if body is None:
            self.send_error(404)
            return
        raw = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/xml; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("SERVER", UPNP_SERVER)
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(raw)

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            length = 0
        payload = self.rfile.read(length) if length else b""
        soap = self.headers.get("SOAPAction") or self.headers.get("Soapaction") or ""
        action = soap.strip().strip('"').split("#")[-1]
        args = {}
        try:
            root = ET.fromstring(payload)
            body = None
            for child in root:
                if _localname(child.tag) == "Body":
                    body = child
                    break
            if body is not None:
                for child in body:
                    for arg in child:
                        args[_localname(arg.tag)] = arg.text or ""
                    if not action:
                        action = _localname(child.tag)
        except ET.ParseError:
            pass
        path = urlparse(self.path).path
        service = "AVTransport"
        if "RenderingControl" in path:
            service = "RenderingControl"
        elif "ConnectionManager" in path:
            service = "ConnectionManager"
        ok, out = self.server.renderer.handle_action(service, action, args)
        out = out or {}
        if not ok:
            fault = out.get("_fault") or (501, "Action Failed")
            raw = _soap_fault(fault[0], fault[1]).encode("utf-8")
            self.send_response(500)
        else:
            inner = "".join(
                "<%s>%s</%s>" % (k, escape(str(v)), k) for k, v in out.items() if not str(k).startswith("_")
            )
            xml = (
                '<?xml version="1.0"?>'
                '<s:Envelope xmlns:s="%s" s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
                "<s:Body><u:%sResponse xmlns:u=\"urn:schemas-upnp-org:service:%s:1\">%s</u:%sResponse></s:Body>"
                "</s:Envelope>"
            ) % (NS_SOAP, action or "Unknown", service, inner, action or "Unknown")
            raw = xml.encode("utf-8")
            self.send_response(200)
        self.send_header("Content-Type", 'text/xml; charset="utf-8"')
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("EXT", "")
        self.send_header("SERVER", UPNP_SERVER)
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(raw)

    def do_SUBSCRIBE(self):
        renderer = self.server.renderer
        path = urlparse(self.path).path
        service = "AVTransport"
        if "RenderingControl" in path:
            service = "RenderingControl"
        elif "ConnectionManager" in path:
            service = "ConnectionManager"
        callback = self.headers.get("CALLBACK") or self.headers.get("Callback") or ""
        sid = self.headers.get("SID") or self.headers.get("Sid") or ""
        timeout = self.headers.get("TIMEOUT") or self.headers.get("Timeout") or "Second-1800"
        new_sid, seconds = renderer.subscribe(service, callback, sid, timeout)
        self.send_response(200)
        self.send_header("SID", new_sid)
        self.send_header("TIMEOUT", "Second-%s" % seconds)
        self.send_header("Content-Length", "0")
        self.send_header("Connection", "close")
        self.end_headers()

    def do_UNSUBSCRIBE(self):
        sid = self.headers.get("SID") or self.headers.get("Sid") or ""
        self.server.renderer.unsubscribe(sid)
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()


class DlnaRenderer:
    def __init__(self, player, on_begin=None, name=None, volume_getter=None, uuid_path=None):
        self.player = player
        self.on_begin = on_begin
        self.volume_getter = volume_getter
        self.lock = threading.Lock()
        self.enabled = False
        self.error = ""
        self.name = sanitize_name(name) or DEFAULT_NAME
        self.uuid_path = uuid_path
        self.uuid = self._load_uuid()
        self.uri = ""
        self.meta = ""
        self.title = ""
        self.artist = ""
        self.album = ""
        self.duration = 0.0
        self.state = "STOPPED"
        self.muted = False
        self.http = None
        self.ssdp_sock = None
        self.stop_event = threading.Event()
        self.subs = []
        self.bootid = str(int(time.time()))
        self.next_uri = ""
        self.next_meta = ""

    def available(self):
        return True

    def snapshot(self):
        with self.lock:
            self._sync_state_locked()
            return {
                "available": True,
                "enabled": bool(self.enabled),
                "active": bool(self.enabled and self.state in ("PLAYING", "PAUSED_PLAYBACK")),
                "name": self.name,
                "title": self.title if self.state != "STOPPED" else "",
                "artist": self.artist if self.state != "STOPPED" else "",
                "album": self.album if self.state != "STOPPED" else "",
                "client": "",
                "error": self.error,
            }

    def set_name(self, name):
        clean = sanitize_name(name)
        if not clean:
            self.error = "name must be 1–50 letters, numbers, space, dot, underscore, or dash"
            return False
        with self.lock:
            self.name = clean
            self.error = ""
            running = self.enabled
        if running:
            self.set_enabled(False)
            return self.set_enabled(True)
        return True

    def set_enabled(self, value):
        want = bool(value)
        with self.lock:
            if want == self.enabled and (self.http is not None) == want:
                self.enabled = want
                return True
            self.enabled = want
        if want:
            return self._start()
        self._stop()
        return True

    def stop_playback(self):
        with self.lock:
            if self.player is not None:
                snap = self.player.snapshot()
                if snap.get("source") == "url":
                    self.player.stop()
            self.state = "STOPPED"
        self._notify("AVTransport")

    def on_stream_ended(self):
        with self.lock:
            self.state = "STOPPED"
        self._notify("AVTransport")

    def _load_uuid(self):
        path = self.uuid_path
        if path:
            try:
                with open(path) as fh:
                    value = fh.read().strip()
                uuid.UUID(value)
                return value
            except Exception:
                pass
        value = _uuid_from_name(socket.gethostname())
        if path:
            try:
                os.makedirs(os.path.dirname(path), exist_ok=True)
                tmp = path + ".tmp"
                with open(tmp, "w") as fh:
                    fh.write(value + "\n")
                os.replace(tmp, path)
            except Exception:
                pass
        return value

    def _local_ip(self):
        ip = "127.0.0.1"
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.settimeout(0.2)
            sock.connect(("192.168.1.1", 80))
            ip = sock.getsockname()[0]
            sock.close()
        except Exception:
            pass
        return ip

    def device_xml(self):
        ip = self._local_ip()
        name = escape(self.name)
        udn = "uuid:" + self.uuid
        loc = "http://%s:%s" % (ip, HTTP_PORT)
        return """<?xml version="1.0"?>
<root xmlns="urn:schemas-upnp-org:device-1-0" xmlns:dlna="urn:schemas-dlna-org:device-1-0">
  <specVersion><major>1</major><minor>0</minor></specVersion>
  <device>
    <deviceType>urn:schemas-upnp-org:device:MediaRenderer:1</deviceType>
    <friendlyName>%s</friendlyName>
    <manufacturer>Gigawatt</manufacturer>
    <modelName>Gigawatt</modelName>
    <modelNumber>0.10</modelNumber>
    <UDN>%s</UDN>
    <dlna:X_DLNADOC>DMR-1.50</dlna:X_DLNADOC>
    <serviceList>
      <service>
        <serviceType>urn:schemas-upnp-org:service:AVTransport:1</serviceType>
        <serviceId>urn:upnp-org:serviceId:AVTransport</serviceId>
        <SCPDURL>/AVTransport.xml</SCPDURL>
        <controlURL>/AVTransport/control</controlURL>
        <eventSubURL>/AVTransport/event</eventSubURL>
      </service>
      <service>
        <serviceType>urn:schemas-upnp-org:service:RenderingControl:1</serviceType>
        <serviceId>urn:upnp-org:serviceId:RenderingControl</serviceId>
        <SCPDURL>/RenderingControl.xml</SCPDURL>
        <controlURL>/RenderingControl/control</controlURL>
        <eventSubURL>/RenderingControl/event</eventSubURL>
      </service>
      <service>
        <serviceType>urn:schemas-upnp-org:service:ConnectionManager:1</serviceType>
        <serviceId>urn:upnp-org:serviceId:ConnectionManager</serviceId>
        <SCPDURL>/ConnectionManager.xml</SCPDURL>
        <controlURL>/ConnectionManager/control</controlURL>
        <eventSubURL>/ConnectionManager/event</eventSubURL>
      </service>
    </serviceList>
  </device>
</root>
""" % (name, udn)

    def _start(self):
        self.stop_event.clear()
        self.bootid = str(int(time.time()))
        try:
            http = ThreadingHTTPServer(("0.0.0.0", HTTP_PORT), _DlnaHandler)
            http.renderer = self
            self.http = http
        except Exception as exc:
            self.error = str(exc)
            return False
        threading.Thread(target=http.serve_forever, daemon=True).start()
        try:
            self._open_ssdp()
        except Exception as exc:
            self.error = "SSDP: %s" % exc
            return False
        threading.Thread(target=self._ssdp_loop, daemon=True).start()
        threading.Thread(target=self._alive_loop, daemon=True).start()
        self._ssdp_notify("ssdp:alive")
        self.error = ""
        return True

    def _stop(self):
        self.stop_event.set()
        try:
            self._ssdp_notify("ssdp:byebye")
        except Exception:
            pass
        http = self.http
        self.http = None
        if http is not None:
            try:
                http.shutdown()
            except Exception:
                pass
        sock = self.ssdp_sock
        self.ssdp_sock = None
        if sock is not None:
            try:
                sock.close()
            except Exception:
                pass
        self.stop_playback()
        self.error = ""

    def _open_ssdp(self):
        ip = self._local_ip()
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        except Exception:
            pass
        sock.bind(("", SSDP_PORT))
        mreq = struct.pack("4s4s", socket.inet_aton(SSDP_ADDR), socket.inet_aton(ip))
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(ip))
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
        sock.settimeout(1.0)
        self.ssdp_sock = sock

    def _nt_list(self):
        udn = "uuid:" + self.uuid
        return [
            ("upnp:rootdevice", udn + "::upnp:rootdevice"),
            (udn, udn),
            ("urn:schemas-upnp-org:device:MediaRenderer:1", udn + "::urn:schemas-upnp-org:device:MediaRenderer:1"),
            ("urn:schemas-upnp-org:service:AVTransport:1", udn + "::urn:schemas-upnp-org:service:AVTransport:1"),
            ("urn:schemas-upnp-org:service:RenderingControl:1", udn + "::urn:schemas-upnp-org:service:RenderingControl:1"),
            ("urn:schemas-upnp-org:service:ConnectionManager:1", udn + "::urn:schemas-upnp-org:service:ConnectionManager:1"),
        ]

    def _ssdp_notify(self, nts):
        sock = self.ssdp_sock
        if sock is None:
            return
        ip = self._local_ip()
        loc = "http://%s:%s/device.xml" % (ip, HTTP_PORT)
        for nt, usn in self._nt_list():
            msg = (
                "NOTIFY * HTTP/1.1\r\n"
                "HOST: %s:%s\r\n"
                "CACHE-CONTROL: max-age=1800\r\n"
                "LOCATION: %s\r\n"
                "NT: %s\r\n"
                "NTS: %s\r\n"
                "SERVER: %s\r\n"
                "USN: %s\r\n"
                "BOOTID.UPNP.ORG: %s\r\n"
                "CONFIGID.UPNP.ORG: 1\r\n"
                "\r\n"
            ) % (SSDP_ADDR, SSDP_PORT, loc, nt, nts, UPNP_SERVER, usn, self.bootid)
            try:
                sock.sendto(msg.encode("utf-8"), (SSDP_ADDR, SSDP_PORT))
            except Exception:
                pass

    def _ssdp_loop(self):
        while not self.stop_event.is_set():
            sock = self.ssdp_sock
            if sock is None:
                return
            try:
                data, addr = sock.recvfrom(2048)
            except socket.timeout:
                continue
            except Exception:
                return
            text = data.decode("utf-8", "replace")
            if not text.upper().startswith("M-SEARCH"):
                continue
            st = ""
            for line in text.splitlines():
                if line.upper().startswith("ST:"):
                    st = line.split(":", 1)[1].strip()
            self._ssdp_reply(addr, st)

    def _ssdp_reply(self, addr, st):
        sock = self.ssdp_sock
        if sock is None:
            return
        ip = self._local_ip()
        loc = "http://%s:%s/device.xml" % (ip, HTTP_PORT)
        date = formatdate(usegmt=True)
        wanted = (st or "").lower()
        for nt, usn in self._nt_list():
            if wanted in ("", "ssdp:all", nt.lower(), "upnp:rootdevice", "uuid:" + self.uuid):
                msg = (
                    "HTTP/1.1 200 OK\r\n"
                    "CACHE-CONTROL: max-age=1800\r\n"
                    "DATE: %s\r\n"
                    "EXT:\r\n"
                    "LOCATION: %s\r\n"
                    "SERVER: %s\r\n"
                    "ST: %s\r\n"
                    "USN: %s\r\n"
                    "BOOTID.UPNP.ORG: %s\r\n"
                    "CONFIGID.UPNP.ORG: 1\r\n"
                    "\r\n"
                ) % (date, loc, UPNP_SERVER, nt, usn, self.bootid)
                try:
                    sock.sendto(msg.encode("utf-8"), addr)
                except Exception:
                    pass

    def _alive_loop(self):
        while not self.stop_event.wait(300):
            self._ssdp_notify("ssdp:alive")

    def _volume(self):
        if self.volume_getter:
            try:
                return int(self.volume_getter())
            except Exception:
                pass
        return 80

    def _sync_state_locked(self):
        if self.player is None:
            return
        snap = self.player.snapshot()
        if snap.get("source") != "url":
            if self.state != "STOPPED":
                self.state = "STOPPED"
            return
        if snap.get("paused"):
            self.state = "PAUSED_PLAYBACK"
        elif snap.get("playing"):
            self.state = "PLAYING"
        else:
            self.state = "STOPPED"

    def handle_action(self, service, action, args):
        if service == "ConnectionManager":
            if action == "GetProtocolInfo":
                return True, {"Source": "", "Sink": SINK}
            if action == "GetCurrentConnectionIDs":
                return True, {"ConnectionIDs": "0"}
            if action == "GetCurrentConnectionInfo":
                return True, {
                    "RcsID": "0",
                    "AVTransportID": "0",
                    "ProtocolInfo": "",
                    "PeerConnectionManager": "",
                    "PeerConnectionID": "-1",
                    "Direction": "Input",
                    "Status": "OK",
                }
            return True, {}
        if service == "RenderingControl":
            if action == "GetVolume":
                return True, {"CurrentVolume": str(self._volume())}
            if action == "SetVolume":
                try:
                    n = int(float(args.get("DesiredVolume") or 0))
                except ValueError:
                    n = 80
                if self.player is not None:
                    self.player.set_volume(n)
                self._notify("RenderingControl")
                return True, {}
            if action == "GetMute":
                return True, {"CurrentMute": "1" if self.muted else "0"}
            if action == "SetMute":
                self.muted = str(args.get("DesiredMute") or "").lower() in ("1", "true")
                if self.player is not None:
                    self.player.set_volume(0 if self.muted else self._volume())
                self._notify("RenderingControl")
                return True, {}
            return True, {}
        return self._avt(action, args)

    def _avt(self, action, args):
        begin = False
        with self.lock:
            if action == "SetAVTransportURI":
                self.uri = (args.get("CurrentURI") or "").strip()
                self.meta = args.get("CurrentURIMetaData") or ""
                info = _parse_didl(self.meta)
                self.title = info["title"] or self.uri.rsplit("/", 1)[-1]
                self.artist = info["artist"]
                self.album = info["album"]
                self.duration = info["duration"]
                self.state = "STOPPED"
                if self.uri.lower().startswith("https://"):
                    self.error = "DLNA HTTPS is not supported on this host (HTTP only)"
                    return False, {"_fault": (714, "HTTPS is not supported")}
                self.error = ""
            elif action == "Play":
                if not self.uri:
                    self.error = "no stream URI"
                    return False, {"_fault": (402, "No AVTransport URI")}
                if self.uri.lower().startswith("https://"):
                    self.error = "DLNA HTTPS is not supported on this host (HTTP only)"
                    self.state = "STOPPED"
                    return False, {"_fault": (714, "HTTPS is not supported")}
                ok = True
                if self.player is not None:
                    ok = self.player.play_url(self.uri, title=self.title, duration=self.duration)
                    if ok:
                        self.player.set_volume(0 if self.muted else self._volume())
                self.state = "PLAYING" if ok else "STOPPED"
                if not ok:
                    self.error = (self.player.snapshot().get("error") if self.player else "play failed") or "play failed"
                    return False, {"_fault": (701, self.error)}
                self.error = ""
                begin = True
            elif action == "Pause":
                if self.player is not None:
                    self.player.pause()
                self.state = "PAUSED_PLAYBACK"
            elif action == "Stop":
                if self.player is not None:
                    snap = self.player.snapshot()
                    if snap.get("source") == "url":
                        self.player.stop()
                self.state = "STOPPED"
            elif action == "Seek":
                unit = (args.get("Unit") or "").upper()
                target = args.get("Target") or "0"
                if self.player is not None and unit in ("REL_TIME", "ABS_TIME"):
                    self.player.seek(_parse_hms(target))
            elif action == "GetTransportInfo":
                self._sync_state_locked()
                return True, {
                    "CurrentTransportState": self.state,
                    "CurrentTransportStatus": "OK",
                    "CurrentSpeed": "1",
                }
            elif action == "GetMediaInfo":
                dur = _fmt_hms(self.duration)
                return True, {
                    "NrTracks": "1" if self.uri else "0",
                    "MediaDuration": dur,
                    "CurrentURI": self.uri,
                    "CurrentURIMetaData": self.meta,
                    "NextURI": self.next_uri,
                    "NextURIMetaData": self.next_meta,
                    "PlayMedium": "NETWORK",
                    "RecordMedium": "NOT_IMPLEMENTED",
                    "WriteStatus": "NOT_IMPLEMENTED",
                }
            elif action == "GetPositionInfo":
                self._sync_state_locked()
                pos = 0.0
                dur = self.duration
                if self.player is not None:
                    snap = self.player.snapshot()
                    if snap.get("source") == "url":
                        pos = snap.get("position") or 0.0
                        dur = snap.get("duration") or dur
                t = _fmt_hms(pos)
                d = _fmt_hms(dur)
                return True, {
                    "Track": "1" if self.uri else "0",
                    "TrackDuration": d,
                    "TrackMetaData": self.meta,
                    "TrackURI": self.uri,
                    "RelTime": t,
                    "AbsTime": t,
                    "RelCount": "2147483647",
                    "AbsCount": "2147483647",
                }
            elif action == "GetDeviceCapabilities":
                return True, {
                    "PlayMedia": "NETWORK",
                    "RecMedia": "NOT_IMPLEMENTED",
                    "RecQualityModes": "NOT_IMPLEMENTED",
                }
            elif action == "GetTransportSettings":
                return True, {"PlayMode": "NORMAL", "RecQualityMode": "NOT_IMPLEMENTED"}
            elif action in ("Next", "Previous"):
                return True, {}
            elif action == "GetCurrentTransportActions":
                if not self.uri:
                    actions = ""
                elif self.state == "PLAYING":
                    actions = "Pause,Stop,Seek,Next,Previous"
                elif self.state == "PAUSED_PLAYBACK":
                    actions = "Play,Stop,Seek,Next,Previous"
                else:
                    actions = TRANSPORT_ACTIONS
                return True, {"Actions": actions, "CurrentTransportActions": actions}
            elif action == "SetNextAVTransportURI":
                self.next_uri = (args.get("NextURI") or "").strip()
                self.next_meta = args.get("NextURIMetaData") or ""
                return True, {}
        if action in ("SetAVTransportURI", "Play", "Pause", "Stop", "Seek"):
            self._notify("AVTransport")
        if begin and self.on_begin:
            try:
                self.on_begin()
            except Exception:
                pass
        return True, {}

    def subscribe(self, service, callback, sid, timeout):
        seconds = 1800
        match = re.search(r"(\d+)", timeout or "")
        if match:
            seconds = max(30, min(1800, int(match.group(1))))
        urls = re.findall(r"<([^>]+)>", callback or "")
        with self.lock:
            if sid:
                for sub in self.subs:
                    if sub["sid"] == sid:
                        sub["until"] = time.time() + seconds
                        return sid, seconds
            sid = "uuid:" + str(uuid.uuid4())
            self.subs.append(
                {
                    "sid": sid,
                    "service": service,
                    "urls": urls,
                    "until": time.time() + seconds,
                    "seq": 0,
                }
            )
        self._notify(service, sid=sid)
        return sid, seconds

    def unsubscribe(self, sid):
        with self.lock:
            self.subs = [s for s in self.subs if s["sid"] != sid]

    def _last_change(self, service):
        if service == "RenderingControl":
            inner = (
                '<Event xmlns="urn:schemas-upnp-org:metadata-1-0/RCS/">'
                '<InstanceID val="0">'
                '<Mute channel="Master" val="%s"/>'
                '<Volume channel="Master" val="%s"/>'
                "</InstanceID></Event>"
            ) % ("1" if self.muted else "0", self._volume())
            return inner
        if service == "ConnectionManager":
            return ""
        with self.lock:
            self._sync_state_locked()
            inner = (
                '<Event xmlns="urn:schemas-upnp-org:metadata-1-0/AVT/">'
                '<InstanceID val="0">'
                '<TransportState val="%s"/>'
                '<TransportStatus val="OK"/>'
                '<CurrentTrackURI val="%s"/>'
                '<AVTransportURI val="%s"/>'
                '<CurrentTrackMetaData val="%s"/>'
                '<CurrentTrackDuration val="%s"/>'
                '<CurrentTrack val="%s"/>'
                "</InstanceID></Event>"
            ) % (
                escape(self.state, {'"': "&quot;"}),
                escape(self.uri, {'"': "&quot;"}),
                escape(self.uri, {'"': "&quot;"}),
                escape(self.meta, {'"': "&quot;"}),
                _fmt_hms(self.duration),
                "1" if self.uri else "0",
            )
            return inner

    def _notify(self, service, sid=None):
        body_inner = self._last_change(service)
        if not body_inner:
            return
        now = time.time()
        with self.lock:
            live = []
            for sub in self.subs:
                if sub["until"] < now:
                    continue
                if sub["service"] != service:
                    live.append(sub)
                    continue
                if sid and sub["sid"] != sid:
                    live.append(sub)
                    continue
                xml = (
                    '<?xml version="1.0"?>'
                    '<e:propertyset xmlns:e="urn:schemas-upnp-org:event-1-0">'
                    "<e:property><LastChange>%s</LastChange></e:property>"
                    "</e:propertyset>"
                ) % escape(body_inner)
                raw = xml.encode("utf-8")
                for url in sub["urls"]:
                    self._post_event(url, sub["sid"], sub["seq"], raw)
                sub["seq"] += 1
                live.append(sub)
            self.subs = live

    def _post_event(self, url, sid, seq, body):
        try:
            parsed = urlparse(url)
            host = parsed.hostname
            port = parsed.port or 80
            path = parsed.path or "/"
            if parsed.query:
                path += "?" + parsed.query
            header = (
                "NOTIFY %s HTTP/1.1\r\n"
                "HOST: %s:%s\r\n"
                'CONTENT-TYPE: text/xml; charset="utf-8"\r\n'
                "NT: upnp:event\r\n"
                "NTS: upnp:propchange\r\n"
                "SID: %s\r\n"
                "SEQ: %s\r\n"
                "CONTENT-LENGTH: %s\r\n"
                "CONNECTION: close\r\n"
                "\r\n"
            ) % (path, host, port, sid, seq, len(body))
            sock = socket.create_connection((host, port), timeout=2)
            sock.sendall(header.encode("utf-8") + body)
            sock.close()
        except Exception:
            pass
