"""Shared helpers for the lab tools. Reuses the hardened ESL client in `esl-check`."""
import importlib.machinery
import importlib.util
import os
import socket
import time

_here = os.path.dirname(os.path.abspath(__file__))
_loader = importlib.machinery.SourceFileLoader("esl_check", os.path.join(_here, "esl-check"))
_spec = importlib.util.spec_from_loader("esl_check", _loader)
chk = importlib.util.module_from_spec(_spec)
_loader.exec_module(chk)

Fail = chk.Fail


class Conn:
    """One authenticated ESL connection. The password comes from $ESL_PASSWORD_FILE only."""

    def __init__(self, host="127.0.0.1", port=8021, timeout=5.0):
        password = chk.load_password({"password-file": None, "password-env": None})
        try:
            self.sock = socket.create_connection((host, port), timeout=timeout)
        except OSError:
            raise Fail(3, "cannot connect to %s:%d" % (host, port))
        try:
            self.esl = chk.Esl(self.sock, time.monotonic() + timeout)
            headers, _ = self.esl.read_message()
            if headers.get("content-type") != "auth/request":
                raise Fail(1, "unexpected greeting from server")
            self.esl.send("auth " + password)
            headers, _ = self.esl.read_message()
            if not headers.get("reply-text", "").startswith("+OK"):
                raise Fail(2, "authentication rejected")
        except BaseException:
            self.sock.close()
            raise

    def api(self, command, timeout=10.0):
        if "\n" in command or "\r" in command:
            raise Fail(4, "command must be a single line")
        self.esl.deadline = time.monotonic() + timeout
        self.esl.send("api " + command)
        headers, body = self.esl.read_message()
        if headers.get("content-type") != "api/response":
            raise Fail(1, "unexpected response type")
        return body.decode("utf-8", "replace")

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


def one_shot(command, timeout=10.0):
    conn = Conn(timeout=timeout)
    try:
        return conn.api(command, timeout)
    finally:
        conn.close()
