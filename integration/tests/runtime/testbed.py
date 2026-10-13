# (C) Datadog, Inc. 2026-present
# All rights reserved
# Licensed under a 3-clause BSD style license (see LICENSE)
"""A disposable, loopback-only FreeSWITCH test environment (Docker).

It is separate from the lab (``docker-compose.yml``): a new container with a new name, its own generated
configuration and tmpfs state, and no volume or network shared with the lab. SIP and RTP are bound to the
container's loopback address, nothing is published except the Event Socket on the host's 127.0.0.1, and the only
gateways point to the container's own loopback. All credentials are generated at start-up, written only to a
temporary directory that is removed on teardown, and never printed.

The image is the lab's already-built FreeSWITCH 1.10.12 image (``FS_TESTBED_IMAGE`` to override).
"""

from __future__ import annotations

import os
import secrets
import shutil
import socket
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

IMAGE = os.environ.get('FS_TESTBED_IMAGE', 'datadog-freeswitch-lab/freeswitch:1.10.12')
LABEL = 'datadog-freeswitch.testbed=1'
DOMAIN = 'test.local'
RESTRICTED_USER = 'esl_ro'
EVENT_USER = 'esl_ev'  # research only: may subscribe to CHANNEL_HANGUP_COMPLETE and run no API command
UNRESTRICTED_USER = 'esl_open'  # same test password, but no esl-allowed-api: the check must refuse it
REG_USER = '1000'

_ROOT = """<?xml version="1.0"?>
<document type="freeswitch/xml">
  <X-PRE-PROCESS cmd="include" data="vars.xml"/>
  <section name="configuration" description="Modules and settings">
    <X-PRE-PROCESS cmd="include" data="autoload_configs/*.xml"/>
  </section>
  <section name="dialplan" description="Regex/XML Dialplan">
    <X-PRE-PROCESS cmd="include" data="dialplan/*.xml"/>
  </section>
  <section name="directory" description="User Directory">
    <X-PRE-PROCESS cmd="include" data="directory/*.xml"/>
  </section>
</document>
"""

_CONSOLE = """<configuration name="console.conf" description="Console Logger"><settings>
<param name="colorize" value="false"/><param name="loglevel" value="info"/></settings></configuration>"""

_LOGFILE = """<configuration name="logfile.conf" description="File Logging">
  <settings>
    <param name="rotate-on-hup" value="false"/>
  </settings>
  <profiles>
    <profile name="default">
      <settings>
        <param name="logfile" value="/usr/local/freeswitch/log/freeswitch.log"/>
      </settings>
      <mappings>
        <map name="all" value="debug,info,notice,warning,err,crit,alert"/>
      </mappings>
    </profile>
  </profiles>
</configuration>"""

_MODULES = """<configuration name="modules.conf" description="Modules"><modules>
<load module="mod_console"/><load module="mod_logfile"/><load module="mod_event_socket"/>
<load module="mod_sofia"/><load module="mod_loopback"/>
<load module="mod_commands"/><load module="mod_dptools"/>
<load module="mod_dialplan_xml"/><load module="mod_tone_stream"/>
</modules></configuration>"""

_SWITCH = """<configuration name="switch.conf" description="Core"><settings>
<param name="colorize-console" value="false"/><param name="max-sessions" value="{max_sessions}"/>
<param name="sessions-per-second" value="{sps}"/><param name="loglevel" value="info"/>
<param name="rtp-start-port" value="16384"/><param name="rtp-end-port" value="{rtp_end}"/>
</settings></configuration>"""

_EVENT_SOCKET = """<configuration name="event_socket.conf" description="ESL"><settings>
<param name="listen-ip" value="0.0.0.0"/><param name="listen-port" value="8021"/>
<param name="password" value="{password}"/><param name="apply-inbound-acl" value="esl_test"/>
</settings></configuration>"""

_ACL = """<configuration name="acl.conf" description="ACL"><network-lists>
<list name="esl_test" default="deny"><node type="allow" cidr="127.0.0.0/8"/><node type="allow" cidr="172.16.0.0/12"/>
<node type="allow" cidr="192.168.0.0/16"/><node type="allow" cidr="10.0.0.0/8"/></list>
</network-lists></configuration>"""

_SOFIA = """<configuration name="sofia.conf" description="sofia">
  <global_settings>
    <param name="log-level" value="0"/>
  </global_settings>
  <profiles>
    <X-PRE-PROCESS cmd="include" data="../sip_profiles/*.xml"/>
  </profiles>
</configuration>"""

# ``internal`` places the calls (outbound through gateways); ``edge`` is the SIP server those gateways talk to.
_PROFILE_INTERNAL = """<profile name="internal"><gateways>
<gateway name="loopreg"><param name="proxy" value="127.0.0.1:5080"/><param name="realm" value="{domain}"/>
<param name="from-domain" value="{domain}"/>
<param name="username" value="{reg_user}"/><param name="password" value="{reg_password}"/>
<param name="register" value="true"/><param name="expire-seconds" value="3600"/>
<param name="retry-seconds" value="5"/></gateway>
<gateway name="loopnoreg"><param name="proxy" value="127.0.0.1:5080"/><param name="register" value="false"/>
<param name="ping" value="10"/></gateway>
<gateway name="deadreg"><param name="proxy" value="127.0.0.1:5099"/><param name="realm" value="{domain}"/>
<param name="username" value="nobody"/><param name="password" value="x"/><param name="register" value="true"/>
<param name="retry-seconds" value="300"/></gateway>
</gateways><settings>
<param name="sip-ip" value="127.0.0.1"/><param name="rtp-ip" value="127.0.0.1"/><param name="sip-port" value="5060"/>
<param name="context" value="default"/><param name="dialplan" value="XML"/><param name="auth-calls" value="true"/>
<param name="inbound-codec-prefs" value="{codecs}"/><param name="outbound-codec-prefs" value="{codecs}"/>
<param name="manage-presence" value="false"/><param name="disable-register" value="true"/>
{common_extra}
</settings></profile>"""

_PROFILE_EDGE = """<profile name="edge"><settings>
<param name="sip-ip" value="127.0.0.1"/><param name="rtp-ip" value="127.0.0.1"/><param name="sip-port" value="5080"/>
<param name="context" value="edge"/><param name="dialplan" value="XML"/><param name="auth-calls" value="false"/>
<param name="inbound-codec-prefs" value="{codecs}"/><param name="outbound-codec-prefs" value="{codecs}"/>
<param name="manage-presence" value="false"/><param name="challenge-realm" value="auto_from"/>
{edge_extra}
</settings></profile>"""

_DIRECTORY = """<include>
<domain name="{domain}">
<users>
<user id="{reg_user}"><params><param name="password" value="{reg_password}"/></params></user>
<user id="{restricted}">
<params>
<param name="esl-password" value="{restricted_password}"/>
<param name="esl-allowed-api" value="uptime,show"/>
</params>
</user>
<user id="{event_user}">
<params>
<param name="esl-password" value="{restricted_password}"/>
<param name="esl-allowed-api" value="none"/>
<param name="esl-allowed-events" value="CHANNEL_HANGUP_COMPLETE"/>
</params>
</user>
<user id="{unrestricted}">
<params>
<param name="esl-password" value="{restricted_password}"/>
</params>
</user>
</users>
</domain>
</include>"""

_DIALPLAN = """<include>
<context name="edge">
 <extension name="echo"><condition field="destination_number" expression="^5000$">
  <action application="answer"/><action application="echo"/></condition></extension>
 <extension name="hold"><condition field="destination_number" expression="^5001$">
  <action application="answer"/><action application="sleep" data="120000"/></condition></extension>
 <extension name="tone"><condition field="destination_number" expression="^5003$">
  <action application="answer"/><action application="playback" data="tone_stream://L=2000;%(1000,0,440)"/></condition></extension>
 <extension name="busy"><condition field="destination_number" expression="^5002$">
  <action application="respond" data="486"/></condition></extension>
</context>
<context name="default">
 <extension name="none"><condition field="destination_number" expression="^never$">
  <action application="hangup"/></condition></extension>
</context>
</include>"""


class Testbed:
    __test__ = False  # not a pytest test class

    def __init__(
        self,
        max_sessions: int = 100,
        sessions_per_second: int = 40,
        rtp_end: int = 16483,
        network: str | None = None,
        edge_extra: str = '<param name="rtcp-audio-interval-msec" value="1000"/>',
        common_extra: str = '<param name="rtp-timer-name" value="soft"/>',
        cap_net_admin: bool = False,
        codecs: str = 'PCMU',
        esl_host_port: int | None = None,
    ) -> None:
        self.name = 'fs-metrics-testbed-' + uuid.uuid4().hex[:10]
        self.max_sessions = max_sessions
        self.rtp_end = rtp_end
        # Extra <param> lines for both sofia profiles. Without `rtp-timer-name` the minimal profiles pace no RTP at all
        # (about one datagram per second was measured), which made every earlier media statistic meaningless.
        self.common_extra = common_extra
        self.esl_host_port = esl_host_port  # a stable host port survives a restart of the container
        self.codecs = codecs  # codec preference list of both sofia profiles
        self.cap_net_admin = cap_net_admin  # only for the impairment experiment (tc netem inside the container)
        self.edge_extra = edge_extra  # extra <param> lines for the `edge` sofia profile
        self.network = network  # optional user-defined Docker network, reachable by the alias `fs-testbed`
        self.sessions_per_second = sessions_per_second
        self.password = secrets.token_hex(12)
        self.restricted_password = secrets.token_hex(12)
        self._reg_password = secrets.token_hex(12)
        self.port = 0
        self._dir: Path | None = None

    # -- lifecycle --------------------------------------------------------------------------------------------

    def _write_config(self, root: Path) -> None:
        values = {
            'domain': DOMAIN,
            'reg_user': REG_USER,
            'reg_password': self._reg_password,
            'restricted': RESTRICTED_USER,
            'unrestricted': UNRESTRICTED_USER,
            'event_user': EVENT_USER,
            'restricted_password': self.restricted_password,
            'password': self.password,
            'max_sessions': self.max_sessions,
            'sps': self.sessions_per_second,
            'rtp_end': self.rtp_end,
            'edge_extra': self.edge_extra + self.common_extra,
            'common_extra': self.common_extra,
            'codecs': self.codecs,
        }
        files = {
            'freeswitch.xml': _ROOT,
            'vars.xml': '<include><X-PRE-PROCESS cmd="set" data="domain={}"/></include>'.format(DOMAIN),
            'autoload_configs/console.conf.xml': _CONSOLE,
            'autoload_configs/logfile.conf.xml': _LOGFILE,
            'autoload_configs/modules.conf.xml': _MODULES,
            'autoload_configs/switch.conf.xml': _SWITCH.format(**values),
            'autoload_configs/event_socket.conf.xml': _EVENT_SOCKET.format(**values),
            'autoload_configs/acl.conf.xml': _ACL,
            'autoload_configs/sofia.conf.xml': _SOFIA,
            'sip_profiles/internal.xml': _PROFILE_INTERNAL.format(**values),
            'sip_profiles/edge.xml': _PROFILE_EDGE.format(**values),
            'directory/default.xml': _DIRECTORY.format(**values),
            'dialplan/default.xml': _DIALPLAN,
        }
        for rel, content in files.items():
            path = root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
        for path in [root, *root.rglob('*')]:
            path.chmod(0o755 if path.is_dir() else 0o644)  # readable by the container's freeswitch user

    def start(self, wait: float = 60.0) -> None:
        self._dir = Path(tempfile.mkdtemp(prefix='fs-testbed-'))
        self._write_config(self._dir)
        cmd = [
            'docker', 'run', '-d', '--init', '--name', self.name, '--label', LABEL,
            '-v', '{}:/usr/local/freeswitch/conf:ro'.format(self._dir),
            '--tmpfs', '/usr/local/freeswitch/log:uid=999,gid=999',
            '--tmpfs', '/usr/local/freeswitch/db:uid=999,gid=999',
            '--tmpfs', '/usr/local/freeswitch/run:uid=999,gid=999',
            '-p', '127.0.0.1:{}:8021/tcp'.format(self.esl_host_port or ''),
            *(['--network', self.network, '--network-alias', 'fs-testbed'] if self.network else []),
            *(['--cap-add', 'NET_ADMIN'] if self.cap_net_admin else []),
            IMAGE,
        ]  # fmt: skip
        subprocess.run(cmd, check=True, capture_output=True)
        self._wait_ready(wait)

    def _wait_ready(self, wait: float) -> None:
        out = subprocess.run(['docker', 'port', self.name, '8021/tcp'], check=True, capture_output=True, text=True)
        self.port = int(out.stdout.strip().splitlines()[0].rsplit(':', 1)[1])
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline:
            try:
                with socket.create_connection(('127.0.0.1', self.port), timeout=1) as s:
                    s.settimeout(1)
                    if b'auth/request' in s.recv(256):
                        time.sleep(3)  # let sofia profiles and gateways finish starting
                        return
            except OSError:
                pass
            time.sleep(0.5)
        raise RuntimeError('test FreeSWITCH did not become ready')

    def restart(self, wait: float = 60.0) -> None:
        """Restart only this container (a new FreeSWITCH process, same generated configuration)."""
        subprocess.run(['docker', 'restart', '-t', '2', self.name], check=True, capture_output=True)
        self._wait_ready(wait)

    def stop(self) -> None:
        # Only this testbed's own, uniquely named container and its own temporary directory are touched.
        subprocess.run(['docker', 'rm', '-f', self.name], capture_output=True)
        if self._dir is not None:
            shutil.rmtree(self._dir, ignore_errors=True)
            self._dir = None

    def __enter__(self) -> Testbed:
        try:
            self.start()
        except BaseException:
            self.stop()
            raise
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()

    # -- test-only control channel (full-control account; used only against this disposable instance) -----------

    def raw(self, command: str, timeout: float = 15.0) -> str:
        """Run one API command with the testbed's full-control password. For setup and ground truth only."""
        with socket.create_connection(('127.0.0.1', self.port), timeout=timeout) as s:
            s.settimeout(timeout)
            buf = b''

            def read_msg() -> tuple[dict[str, str], bytes]:
                nonlocal buf
                while b'\n\n' not in buf:
                    chunk = s.recv(65536)
                    if not chunk:
                        raise RuntimeError('closed')
                    buf += chunk
                head, buf = buf.split(b'\n\n', 1)
                headers = dict(line.split(': ', 1) for line in head.decode().split('\n') if ': ' in line)
                n = int(headers.get('Content-Length', 0))
                while len(buf) < n:
                    buf += s.recv(65536)
                body, buf = buf[:n], buf[n:]
                return headers, body

            read_msg()
            s.sendall('auth {}\n\n'.format(self.password).encode())
            read_msg()
            s.sendall('api {}\n\n'.format(command).encode())
            return read_msg()[1].decode('utf-8', 'replace')

    def logs(self) -> str:
        """The FreeSWITCH log file of this testbed (debug level)."""
        return subprocess.run(
            ['docker', 'exec', self.name, 'cat', '/usr/local/freeswitch/log/freeswitch.log'],
            capture_output=True,
            text=True,
        ).stdout
