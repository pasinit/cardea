"""Disposable Keycloak issuer and real browser authorization-code/PKCE flow."""
import html
import json
import os
import secrets
import shutil
import socket
import subprocess
import tempfile
from html.parser import HTMLParser
from pathlib import Path

import httpx

from cardea.service.login import OIDCLogin
from cardea.service.models import OIDCConfig
from harness import wait_for


class Form(HTMLParser):
    action = ""
    def handle_starttag(self, tag, attrs):
        if tag == "form":
            self.action = dict(attrs).get("action", "")


class KeycloakLab:
    def __init__(self):
        self.temp = tempfile.TemporaryDirectory(prefix="cko-", dir="/tmp")
        self.root = Path(self.temp.name)
        self.home = self.root / "keycloak"
        shutil.copytree(os.environ.get("KEYCLOAK_HOME", "/tmp/cardea-m1-keycloak/keycloak-26.7.4"), self.home)
        self.password, self.secret = secrets.token_urlsafe(24), secrets.token_urlsafe(24)
        self.subject = "676b44a1-fc9c-4847-bbf9-bfeae58abcdf"
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            self.port = sock.getsockname()[1]
        self.issuer = f"http://127.0.0.1:{self.port}/realms/cardea-fit"
        self.secret_path = self.root / "validator-secret"
        self.secret_path.write_text(self.secret)
        self.secret_path.chmod(0o600)
        realm = {"realm": "cardea-fit", "enabled": True, "sslRequired": "none",
            "users": [{"id": self.subject, "username": "fixture", "enabled": True,
                "email": "fixture@example.invalid", "emailVerified": True,
                "firstName": "Test", "lastName": "Fixture",
                "credentials": [{"type": "password", "value": self.password, "temporary": False}]}],
            "clients": [
                {"clientId": "cardea-cli", "publicClient": True, "standardFlowEnabled": True,
                    "directAccessGrantsEnabled": False, "redirectUris": ["http://127.0.0.1:8765/callback"],
                    "attributes": {"pkce.code.challenge.method": "S256"},
                    "protocolMappers": [{"name": "kernel-audience", "protocol": "openid-connect",
                        "protocolMapper": "oidc-audience-mapper", "config": {"included.custom.audience": "cardea-kernel",
                        "access.token.claim": "true", "id.token.claim": "false"}}]},
                {"clientId": "cardea-kernel", "publicClient": False, "secret": self.secret,
                    "standardFlowEnabled": False, "directAccessGrantsEnabled": False} ]}
        imports = self.home / "data/import"
        imports.mkdir(parents=True, exist_ok=True)
        (imports / "realm.json").write_text(json.dumps(realm))
        (imports / "realm.json").chmod(0o600)
        self.process = None

    def start(self):
        with (self.root / "keycloak.log").open("wb") as log:
            self.process = subprocess.Popen([str(self.home / "bin/kc.sh"), "start-dev", "--import-realm",
                "--http-host=127.0.0.1", f"--http-port={self.port}", "--log-level=ERROR"],
                stdout=log, stderr=subprocess.STDOUT, env={**os.environ, "JAVA_OPTS_APPEND": "-Xms128m -Xmx512m"})
        def ready():
            assert self.process.poll() is None, "Keycloak exited"
            with httpx.Client(timeout=1, trust_env=False) as client:
                assert client.get(self.issuer + "/.well-known/openid-configuration").status_code == 200
        wait_for(ready, 90)

    def config(self):
        return OIDCConfig(issuer=self.issuer, client_id="cardea-kernel",
            client_secret_file=self.secret_path, allow_loopback_http=True)

    def flow(self):
        return OIDCLogin(self.issuer, "cardea-cli", "http://127.0.0.1:8765/callback", loopback_test=True)

    def authorize(self, flow):
        with httpx.Client(timeout=10, trust_env=False, follow_redirects=False) as browser:
            response = browser.get(flow.start())
            assert response.status_code == 200
            # Browsers treat HTTP loopback as a secure context; httpx does not.
            # This fixture uses only a private, explicitly loopback development issuer.
            for cookie in browser.cookies.jar:
                cookie.secure = False
            form = Form()
            form.feed(response.text)
            assert form.action
            response = browser.post(html.unescape(form.action), data={"username": "fixture", "password": self.password})
            if response.status_code != 302:
                # HTML is fixture-only; retain privately for diagnosing required actions.
                (self.root / "login-failure.html").write_text(response.text)
                import re
                titles = re.findall(r'<(?:h1|span|p)[^>]*>([^<]+)</(?:h1|span|p)>', response.text)
                raise AssertionError((response.status_code, titles))
            return response.headers["location"]

    def login(self):
        flow = self.flow()
        try:
            return flow.complete(self.authorize(flow))
        finally:
            flow.close()

    def close(self):
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        self.temp.cleanup()
