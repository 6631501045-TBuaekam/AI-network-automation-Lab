"""Paramiko-based interactive session to Cisco IOS routers."""
import os
import re
import socket
import time

import paramiko

PROMPT_RE = re.compile(r"(?:^|[\r\n])([A-Za-z0-9_.\-]+(?:\([a-z0-9\-]+\))?)([#>])\s*$")
ERROR_RE = re.compile(r"^\s*% (?!OSPF: Reload or use)(.+)$", re.M)


class DeviceError(Exception):
    """Base class; .kind is shown in the UI (auth, timeout, unreachable, ssh, cli)."""
    kind = "error"


class AuthError(DeviceError):
    kind = "auth"


class TimeoutError_(DeviceError):
    kind = "timeout"


class UnreachableError(DeviceError):
    kind = "unreachable"


class CLIError(DeviceError):
    kind = "cli"


class IOSSession:
    def __init__(self, host: str, port: int, username: str, password: str,
                 enable_secret: str | None = None, timeout: float = 15.0,
                 host_key_policy: str = "accept-new", known_hosts: str | None = None) -> None:
        self.host, self.port = host, port
        self.username, self.password, self.enable_secret = username, password, enable_secret
        self.timeout = timeout
        self.host_key_policy = host_key_policy
        self.known_hosts = known_hosts
        self.client: paramiko.SSHClient | None = None
        self.chan = None
        self.transcript: list[str] = []
        self.last_prompt: str | None = None

    # ---------------------------------------------------------------- connection
    def connect(self) -> "IOSSession":
        c = paramiko.SSHClient()
        if self.known_hosts:
            os.makedirs(os.path.dirname(self.known_hosts) or ".", exist_ok=True)
            if not os.path.exists(self.known_hosts):
                open(self.known_hosts, "a").close()
            c.load_host_keys(self.known_hosts)  # mismatch => BadHostKeyException
        c.set_missing_host_key_policy(paramiko.RejectPolicy() if self.host_key_policy == "strict"
                                      else paramiko.AutoAddPolicy())
        try:
            c.connect(self.host, port=self.port, username=self.username, password=self.password,
                      timeout=self.timeout, banner_timeout=self.timeout, auth_timeout=self.timeout,
                      look_for_keys=False, allow_agent=False)
        except paramiko.AuthenticationException as exc:
            raise AuthError(f"SSH authentication failed for {self.username}@{self.host}: {exc}") from exc
        except paramiko.BadHostKeyException as exc:
            raise DeviceError(f"host key for {self.host} changed - possible MITM or router re-keyed "
                              f"(remove it from {self.known_hosts} if expected)") from exc
        except (socket.timeout, TimeoutError) as exc:
            raise TimeoutError_(f"SSH connection to {self.host}:{self.port} timed out") from exc
        except (paramiko.ssh_exception.NoValidConnectionsError, ConnectionRefusedError, OSError) as exc:
            raise UnreachableError(f"cannot reach {self.host}:{self.port}: {exc}") from exc
        except paramiko.SSHException as exc:
            raise DeviceError(f"SSH negotiation with {self.host} failed: {exc}") from exc
        self.client = c
        self.chan = c.invoke_shell(width=511, height=1000)
        self.chan.settimeout(self.timeout)
        out = self._read_until_prompt(self.timeout)
        if self._prompt(out)[1] == ">":
            self._enable()
        self.send("terminal length 0")
        self.send("terminal width 511")
        return self

    def _enable(self) -> None:
        if not self.enable_secret:
            raise AuthError("router is in user EXEC mode and ROUTER_ENABLE_SECRET is not set")
        self.chan.send("enable\n")
        buf = self._read_until(re.compile(r"[Pp]assword:\s*$"), 10)
        if "assword" in buf:
            self.chan.send(self.enable_secret + "\n")
        out = self._read_until_prompt(10)
        if self._prompt(out)[1] != "#":
            raise AuthError("enable failed (wrong enable secret?)")

    def close(self) -> None:
        try:
            if self.client:
                self.client.close()
        finally:
            self.client = None

    def __enter__(self):
        # open_session() hands over an already-connected session: connecting again here
        # would leak the first SSH session (exhausting the router's VTY lines) and bypass
        # the retry loop. Only connect when not connected yet.
        return self if self.client is not None else self.connect()

    def __exit__(self, *exc):
        self.close()

    # ---------------------------------------------------------------- I/O
    @staticmethod
    def _prompt(buf: str):
        m = PROMPT_RE.search(buf.rstrip("\x00"))
        return (m.group(1), m.group(2)) if m else (None, None)

    def _read_until(self, rx: re.Pattern, timeout: float) -> str:
        buf, end = "", time.time() + timeout
        while time.time() < end:
            if self.chan.recv_ready():
                buf += self.chan.recv(65535).decode("utf-8", "replace")
                if rx.search(buf):
                    break
            else:
                time.sleep(0.05)
        self.transcript.append(buf)
        return buf

    def _read_until_prompt(self, timeout: float, after: str | None = None) -> str:
        """Read until a CLI prompt ends the buffer. Asynchronous syslog lines
        (e.g. %LINK-3-UPDOWN) can arrive after the prompt, so on silence we send
        a bare newline to re-draw the prompt."""
        buf, end, last_rx, nudges = "", time.time() + timeout, time.time(), 0
        while time.time() < end:
            if self.chan.recv_ready():
                chunk = self.chan.recv(65535).decode("utf-8", "replace")
                buf += chunk
                last_rx = time.time()
                # A prompt only ends the read once the router has echoed our command:
                # a late prompt left over from the previous command (or a newline nudge)
                # would otherwise end this read early with empty output.
                seen = buf if after is None else buf[buf.find(after) + len(after):] if after in buf else ""
                if seen and self._prompt(seen)[0]:
                    time.sleep(0.05)
                    if not self.chan.recv_ready():
                        break
            else:
                if self.chan.closed or self.chan.exit_status_ready():
                    raise UnreachableError(f"{self.host} closed the SSH session")
                if time.time() - last_rx > 2.0 and nudges < 3 and buf:
                    self.chan.send("\n")
                    nudges += 1
                    last_rx = time.time()
                time.sleep(0.05)
        else:
            self.transcript.append(buf)
            raise TimeoutError_(f"timed out waiting for prompt from {self.host}")
        self.transcript.append(buf)
        self.last_prompt = self._prompt(buf)[0]
        return buf

    def send(self, command: str, timeout: float | None = None) -> str:
        self.chan.send(command + "\n")
        cmd = command.strip()
        # wait for the echo of the first part of the command (IOS may wrap a long line)
        raw = self._read_until_prompt(timeout or max(self.timeout, 30), after=cmd[:30] or None)
        lines = raw.replace("\r", "").split("\n")
        echo = next((i for i, ln in enumerate(lines) if cmd and cmd[:30] in ln), None)
        if echo is not None:
            lines = lines[echo + 1:]  # drop the echo and anything before it (a stale prompt)
        if lines and self._prompt(lines[-1])[0]:
            lines = lines[:-1]  # drop trailing prompt
        return "\n".join(lines).strip("\n")

    # ---------------------------------------------------------------- high level
    def configure(self, lines: list[str]) -> tuple[str, list[str]]:
        """Send config lines one at a time; stop on the first IOS error."""
        log, errors = [], []
        out = self.send("configure terminal")
        log.append(f"configure terminal\n{out}")
        try:
            for ln in lines:
                out = self.send(ln)
                log.append(f"{ln}\n{out}" if out else ln)
                errs = ERROR_RE.findall(out)
                if errs:
                    errors.append(f"'{ln.strip()}': % {errs[0].strip()}")
                    break
        finally:
            # always leave configuration mode, even after an error
            if "(config" in (self.last_prompt or ""):
                self.send("end")
        return "\n".join(log), errors

    def save(self) -> str:
        return self.send("write memory", timeout=90)
