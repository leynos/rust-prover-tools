"""A real TLS loopback server for end-to-end download tests.

The downloader's connection boundary is `http.client.HTTPSConnection`. Faking
that boundary, or the downloader above it, would leave the TLS handshake, the
redirect hop and the streamed body untested. This module therefore serves
scripted responses from a genuine HTTPS server on the loopback interface and
trusts its certificate through `SSL_CERT_FILE`.

Only the URL policy is test-local. The production policy pins the exact
`github.com` release URL on the default HTTPS port, which cannot be served from
an unprivileged loopback port, so `LoopbackReleasePolicy` keeps the exact
initial-URL and redirect-host-list semantics while naming the loopback origin.
"""

from __future__ import annotations

import contextlib
import dataclasses as dc
import http.server
import ssl
import threading
import typing as typ
import urllib.parse

from rust_prover_tools.errors import ProverToolError
from rust_prover_tools.kani_snapshot.download import ReleaseUrlPolicy
from rust_prover_tools.kani_snapshot.models import REPOSITORY

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from pathlib import Path

    import pytest

__all__ = (
    "CERTIFICATE_PEM",
    "PRIVATE_KEY_PEM",
    "LoopbackReleasePolicy",
    "Reply",
    "loopback_release_policy",
    "tls_server",
    "trust_loopback_certificate",
)

#: Self-signed certificate for `127.0.0.1`, generated with
#: `openssl req -x509 -newkey rsa:2048 -nodes -days 36500 -subj /CN=127.0.0.1
#: -addext subjectAltName=IP:127.0.0.1`. It authenticates nothing beyond this
#: loopback test server and is trusted only through `SSL_CERT_FILE`.
CERTIFICATE_PEM: typ.Final[str] = """\
-----BEGIN CERTIFICATE-----
MIIDHDCCAgSgAwIBAgIUcip6kQRaCT/JtqbYAEJKnLyadtEwDQYJKoZIhvcNAQEL
BQAwFDESMBAGA1UEAwwJMTI3LjAuMC4xMCAXDTI2MTExMTAwMzIzNloYDzIxMjYx
MDE4MDAzMjM2WjAUMRIwEAYDVQQDDAkxMjcuMC4wLjEwggEiMA0GCSqGSIb3DQEB
AQUAA4IBDwAwggEKAoIBAQCr5zP2EEisSiJ+SLbJ7hMYY9MsPRWww+eYdI06r1Le
HL0w6odFEPJUoV4FEsOKj6Z+++rhVuqwiZPIzkK4poe5Qpm1q7Qku1AF73gF8eom
Utrt8RXL1fteXbGUwhOJekcfc9xdh4iQJyLiSB9q9F0zVuF8a34exZzddOT5okNG
8JiZCBckG40nHK0PJVhvSYtxhldhaFtJqq6zOpj9d0nlc1xGm1v9GXA2t832Y5wQ
CxOxLa4tFT2trrLEIveH67ma1XX2sos4WZ1wCM5ayF1bqA4vbiydCnsBPruhZIRM
dMygZQZixbo0aJFK3Y8dq0Ogx94ZqzDx+Q7MVNJnD9mVAgMBAAGjZDBiMB0GA1Ud
DgQWBBQnaNaE/YYff36f8bT5hHJfeneDVDAfBgNVHSMEGDAWgBQnaNaE/YYff36f
8bT5hHJfeneDVDAPBgNVHRMBAf8EBTADAQH/MA8GA1UdEQQIMAaHBH8AAAEwDQYJ
KoZIhvcNAQELBQADggEBAJHCsxUG3daAVoTeRd3Fqj4RMrXgbQs39xG/dH4wm+50
fNC4VHkU6c+OYHUPVWaa4UwA4f92rCMriDmwvCS+b/Qt4K+0M+O62E9ajtBnK+i6
Um+yBO1IazQr4cNvMHkfiZBhgIygj3quOKMY9kxd44V+ScgFAln1OVIyb2Uuw1Kl
qYjcVgdeg2+P0Kpwwq2+FpnSJgGP3l7RVIh2a+2dAgTeXn7Ivh3jcs8jeKcIryhQ
plyQhRaD6JdZhvP8ewFnXjU8Kxg2JNKxuyEh31tCuVfbgff5XrGwPXsOC19UPIHm
4pYMxnBo8FxTvwwS8UFCNm+IzwwlBn1Cq44R8P0L5aU=
-----END CERTIFICATE-----
"""

#: Private key for `CERTIFICATE_PEM`; test-only and never used elsewhere.
PRIVATE_KEY_PEM: typ.Final[str] = """\
-----BEGIN PRIVATE KEY-----
MIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSjAgEAAoIBAQCr5zP2EEisSiJ+
SLbJ7hMYY9MsPRWww+eYdI06r1LeHL0w6odFEPJUoV4FEsOKj6Z+++rhVuqwiZPI
zkK4poe5Qpm1q7Qku1AF73gF8eomUtrt8RXL1fteXbGUwhOJekcfc9xdh4iQJyLi
SB9q9F0zVuF8a34exZzddOT5okNG8JiZCBckG40nHK0PJVhvSYtxhldhaFtJqq6z
Opj9d0nlc1xGm1v9GXA2t832Y5wQCxOxLa4tFT2trrLEIveH67ma1XX2sos4WZ1w
CM5ayF1bqA4vbiydCnsBPruhZIRMdMygZQZixbo0aJFK3Y8dq0Ogx94ZqzDx+Q7M
VNJnD9mVAgMBAAECggEAC27XXR50oubUV/dQluoLYZQkE51lb6n74X8g9H38IjMh
nxr+D6kioN/Rn79Be5n1vhzVoJKWDdM8nb6gH+VNghQMopFOrCG8HwTwA980zkM0
9d3SsAmli/9pw+XLhFl6WPxs89iDi+jacFfRJR0nBLbazNpeW0AT4IO5hePVmyZh
swJQiuerXRAagrIdBM8q8FYTQK0j1YDfVF/DJf1pGxzqMA1vneBg5BVrCIicI+NR
34CqhOv5e2XkGqBGAy70VqtsfDw9+SKMLoV3EDeCbHiKpAY5srztUcCcNZgI/PPZ
Mb2O1zXGjyuhb5fnvMuVHL6WY3sdg2HZjzQshfeDaQKBgQDuElxRwhW+7sTH8yyn
vmwodrnYm1lbVSCBVu6inAYSiri8NDLEyOPpoCcuShRAqx2JL53wgXyXfGYeKKx+
5KEYzHU4AtQbkwIR6fxoGF9TkZ41wbUJ/Q0XC38CQOFmi7/QOd37dpLCyZC9f0Mx
bdy6xyGCuk0bBftwjMY/nkzVaQKBgQC42Tf2WlS55lNPPGJs44o4vHQZAURm1NOp
SUMT7ESAIx9zgrZfLyiLetb6cmf0qBr/0eP2QPuLqulB9H5gluaiiA7M5eRTlDhi
L8bL6iGU/HobHYvtdaarLVuldGh561s4L1Vk8Zh7/otDavfVeXXiJfm/yUqOtIpn
Oj4cfSRBTQKBgBypVTf6l8ee004HNq8TO847TPCI818NhpZLsvscsZZ14Pu3hQ0v
6lBsGYt3G60VbL7hXrBfxnIuZdTnrKyyNbrYkpwlqweO0Ub61opptMBI3bU3FuaW
8oZ9udK5oOlJldQpw1HM2qpcvfIyahJ/WX2bD6u/Zh0EwVqxeAScuD9RAoGAIQC/
ah+kovBJjYsVc6WIhyq2e8RrJwT2qXHoJQV5EXuz4NbEoufzkDulaF/Gybq931Dl
J5FgmAYy0VDKw4oNsZUjN1gQVQ/QpU8sajnqNwSA9Jho5EOuNz/I7hiDksj/p19k
Grq749+Fk8rkZtvhvTfUiHZy9lmroFSYxFNfhr0CgYEAyaC9BbMnCLuRs2P06nyi
L1u5w4lRR9cdyp5/hqa5HwCrbw4te59CFA6S6nc1l5/7zTBxnzkna9GdffbRmisQ
Y3xwM4F/00QaYVh+jzWtR2JctogBquGwx0zmqk9iYKGkRjNQQwg+VJ2ywfOiPWwA
d556szJEqB/UzUKPWd0Rcmk=
-----END PRIVATE KEY-----
"""


@dc.dataclass(frozen=True, slots=True)
class Reply:
    """One scripted HTTP response.

    Attributes
    ----------
    status : int
        HTTP status code.
    body : bytes
        Response body sent with an explicit `Content-Length`.
    headers : tuple[tuple[str, str], ...]
        Extra header fields, such as a redirect `Location`.
    """

    status: int = 200
    body: bytes = b""
    headers: tuple[tuple[str, str], ...] = ()


@dc.dataclass(frozen=True, slots=True)
class LoopbackReleasePolicy(ReleaseUrlPolicy):
    """A release policy whose release origin is one loopback server.

    The production policy pins `https://github.com` on the default HTTPS port.
    This subclass keeps the two decisions that make the download boundary
    meaningful — the first request must be exactly the asset URL, and every
    redirect must stay on the allowed host list — and relaxes only the origin
    so an unprivileged loopback port can stand in for it.
    """

    origin: str = ""

    def asset_url(self) -> str:
        """Return the loopback URL standing in for the release URL."""
        return (
            f"{self.origin}/{self.repository}/releases/download/"
            f"{self.tag}/{self.asset_name}"
        )

    def check_initial(self, url: str) -> None:
        """Require exactly the loopback URL standing in for the asset URL."""
        expected = self.asset_url()
        if url != expected:
            msg = f"snapshot release URL must be exactly '{expected}', got '{url}'"
            raise ProverToolError(msg)

    def check_redirect(self, url: str) -> None:
        """Require a redirect to stay on an allowed host and contained path."""
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme != "https" or parsed.hostname not in self.redirect_hosts:
            msg = f"snapshot redirect host is not allowed: '{parsed.hostname}'"
            raise ProverToolError(msg)
        if ".." in urllib.parse.unquote(parsed.path).split("/"):
            msg = f"snapshot redirect URL traverses its host path: '{url}'"
            raise ProverToolError(msg)


class _TLSServer(http.server.ThreadingHTTPServer):
    """A loopback HTTPS server with one scripted route table.

    The server owns the hit counts and the request log so each test observes
    how many times, and in what order, the downloader knocked.
    """

    def __init__(
        self,
        routes: cabc.Mapping[str, cabc.Callable[[int], Reply]],
    ) -> None:
        """Bind an ephemeral loopback port and start routing requests."""
        super().__init__(("127.0.0.1", 0), _Handler)
        self.routes = routes
        self.hits: dict[str, int] = {}
        self.requests: list[str] = []

    @property
    def origin(self) -> str:
        """The HTTPS origin this server listens on."""
        return f"https://127.0.0.1:{self.server_address[1]}"


class _Handler(http.server.BaseHTTPRequestHandler):
    """Serve one scripted `Reply` for each request path."""

    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:
        """Answer one GET from the server's route table."""
        self._reply()

    def _reply(self) -> None:
        """Log the request, look up its route and send the scripted reply."""
        server = typ.cast("_TLSServer", self.server)
        path = urllib.parse.urlsplit(self.path).path
        server.requests.append(self.path)
        server.hits[path] = server.hits.get(path, 0) + 1
        route = server.routes.get(path)
        reply = Reply(status=404) if route is None else route(server.hits[path])
        self.send_response(reply.status)
        for name, value in reply.headers:
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(reply.body)))
        self.end_headers()
        self.wfile.write(reply.body)

    def log_message(self, format: str, *args: object) -> None:  # ruff: ignore[builtin-argument-shadowing]
        """Silence the server's stderr access log."""


@contextlib.contextmanager
def tls_server(
    routes: cabc.Mapping[str, cabc.Callable[[int], Reply]],
    certificate: Path,
    private_key: Path,
) -> cabc.Generator[_TLSServer]:
    """Serve `routes` over TLS on a loopback port for the block's duration."""
    server = _TLSServer(routes)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certificate, private_key)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=10)


def trust_loopback_certificate(
    monkeypatch: pytest.MonkeyPatch,
    directory: Path,
) -> None:
    """Write the loopback certificate and trust it for this test only.

    `http.client.HTTPSConnection` builds a fresh default SSL context for each
    connection, so setting `SSL_CERT_FILE` here makes every subsequent request
    verify the loopback server without patching the connection boundary.
    """
    certificate = directory / "loopback-cert.pem"
    certificate.write_text(CERTIFICATE_PEM, encoding="utf-8")
    monkeypatch.setenv("SSL_CERT_FILE", str(certificate))


def loopback_release_policy(origin: str, asset_name: str) -> LoopbackReleasePolicy:
    """Return a release policy naming `origin` as the release host.

    The tag is a real tag in the snapshot grammar, so the policy's own
    validation runs rather than being bypassed by the fixture.
    """
    return LoopbackReleasePolicy(
        repository=REPOSITORY,
        tag="kani-snapshot-2026-09-aaaaaaaaaaaa-r1",
        asset_name=asset_name,
        redirect_hosts=frozenset({"127.0.0.1"}),
        origin=origin,
    )
