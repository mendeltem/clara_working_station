"""The in-process guard refuses every non-allowlisted connection before a packet leaves."""
import json
import socket
import threading

import pytest

from clara import netguard
from clara.netguard import NetworkBlocked


@pytest.fixture
def audit(tmp_path):
    return tmp_path / "netaudit.log"


def _listener():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen(1)
    threading.Thread(target=lambda: (s.accept()[0].close() if True else None), daemon=True).start()
    return s, s.getsockname()[1]


def test_external_connect_is_refused_and_logged(audit):
    netguard.install({"127.0.0.1", "::1"}, audit)
    s = socket.socket()
    s.settimeout(2)
    with pytest.raises(NetworkBlocked):
        s.connect(("1.1.1.1", 443))
    s.close()
    entries = [json.loads(l) for l in audit.read_text().splitlines()]
    assert entries[-1]["decision"] == "deny" and entries[-1]["host"] == "1.1.1.1" and entries[-1]["port"] == 443
    assert netguard.counts()["blocked"] >= 1


def test_name_resolution_of_external_host_is_refused(audit):
    netguard.install({"127.0.0.1"}, audit)
    with pytest.raises(NetworkBlocked):
        socket.getaddrinfo("example.com", 443)
    assert any(json.loads(l)["event"] == "resolve" for l in audit.read_text().splitlines())


def test_create_connection_to_external_is_refused(audit):
    netguard.install({"127.0.0.1"}, audit)
    with pytest.raises(NetworkBlocked):
        socket.create_connection(("8.8.8.8", 53), timeout=2)


def test_udp_sendto_external_is_refused(audit):
    netguard.install({"127.0.0.1"}, audit)
    u = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    with pytest.raises(NetworkBlocked):
        u.sendto(b"x", ("8.8.8.8", 53))
    u.close()


def test_loopback_is_allowed(audit):
    netguard.install({"127.0.0.1"}, audit)
    srv, port = _listener()
    c = socket.create_connection(("127.0.0.1", port), timeout=2)
    c.close()
    srv.close()
    assert any(json.loads(l)["decision"] == "allow" for l in audit.read_text().splitlines())


def test_allowlisted_lan_host_is_allowed_only_when_listed(audit):
    netguard.install({"127.0.0.1", "192.168.1.50"}, audit)
    assert netguard.is_allowed("192.168.1.50")
    assert not netguard.is_allowed("192.168.1.51")
    assert netguard.is_allowed("localhost")
    assert netguard.is_allowed("::1")


def test_ask_mode_consults_the_user_and_denies_by_default(audit):
    asked = []

    def asker(host, port, kind):
        asked.append((host, port, kind))
        return False

    netguard.install({"127.0.0.1"}, audit, mode="ask", asker=asker)
    with pytest.raises(NetworkBlocked):
        socket.create_connection(("1.1.1.1", 443), timeout=2)
    assert asked == [("1.1.1.1", 443, "resolve")] or asked[0][0] == "1.1.1.1"
    entries = [json.loads(l) for l in audit.read_text().splitlines()]
    assert entries[-1]["decision"] == "ask-deny"


def test_guard_survives_reinstall_with_new_policy(audit):
    netguard.install({"127.0.0.1"}, audit)
    netguard.install({"127.0.0.1", "10.0.0.9"}, audit)
    assert netguard.is_installed() and netguard.is_allowed("10.0.0.9")


def test_legacy_resolvers_and_sendmsg_are_guarded(audit):
    netguard.install({"127.0.0.1"}, audit)
    with pytest.raises(NetworkBlocked):
        socket.gethostbyname("example.com")
    with pytest.raises(NetworkBlocked):
        socket.gethostbyname_ex("example.com")
    assert socket.gethostbyname("localhost") in ("127.0.0.1", "::1")
    u = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    with pytest.raises(NetworkBlocked):
        u.sendmsg([b"x"], [], 0, ("8.8.8.8", 53))
    u.close()
