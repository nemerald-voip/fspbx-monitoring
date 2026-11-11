#!/usr/bin/env python3
"""Isolated installer tests; no packages, certificates, or services are changed."""

import contextlib
import importlib.util
import io
import json
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import shutil
import socket
import ssl
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import patch


SPEC = importlib.util.spec_from_file_location(
    "homer_api", Path(__file__).resolve().parents[1] / "ops/homer_api.py")
api = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(api)


def arguments(*extra):
    with tempfile.NamedTemporaryFile(mode="w") as inventory:
        inventory.write('ALLOWED_PBX_SOURCES="198.51.100.10/32"\n')
        inventory.flush()
        return api.parse_args([
            "--hostname", "homer.example.com", "--email", "admin@example.com",
            "--listen-address", "192.0.2.20", "--access-config", inventory.name, *extra])


class FakeCommands:
    def __init__(self, root):
        self.root = root
        self.commands = []
        self.fail_certificate = False
        self.fail_reload = False
        self.fail_candidate = False
        self.fail_verify = False
        self.packages_missing = True
        self.host_ip = "127.0.0.1"
        self.hep_bindings = ["192.0.2.20"]

    def __call__(self, command, **_kwargs):
        self.commands.append(command)
        output = ""
        if command[:2] == ["docker", "compose"]:
            ports = [{"target": 8080, "published": "8080", "host_ip": self.host_ip}]
            ports.extend({"target": 9060, "published": "9060", "host_ip": address,
                          "protocol": protocol}
                         for address in self.hep_bindings for protocol in ("udp", "tcp"))
            output = json.dumps({"services": {"homer": {"ports": ports}}})
        elif command[:3] == ["ip", "-j", "-4"]:
            output = json.dumps([{"addr_info": [{"local": "192.0.2.20"}]}])
        elif command[0] == "dpkg-query":
            output = "" if self.packages_missing else "installed"
        elif command[:2] == ["apt-get", "install"]:
            bootstrap = (self.root / "etc/nginx/nginx.conf").read_text()
            # Package postinst may start Nginx: the default site must never be loaded.
            assert "acme-challenge" in bootstrap
            assert "location / { return 404; }" in bootstrap
            assert "include" not in bootstrap
            assert "Dpkg::Options::=--force-confold" in command
            self.packages_missing = False
        elif command[:2] == ["certbot", "certonly"] and self.fail_certificate:
            raise subprocess.CalledProcessError(1, command)
        elif command[:2] == ["openssl", "verify"] and self.fail_verify:
            raise subprocess.CalledProcessError(1, command)
        elif command[:3] == ["nginx", "-t", "-c"] and self.fail_candidate:
            raise subprocess.CalledProcessError(1, command)
        elif (command[0] == "systemctl" and command[1] in ("reload", "reload-or-restart")
              and self.fail_reload):
            self.fail_reload = False
            raise subprocess.CalledProcessError(1, command)
        return subprocess.CompletedProcess(command, 0, stdout=output)


class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.deployment = self.root / "deployment"
        self.deployment.mkdir()
        (self.deployment / ".env").write_text("private-and-preserved\n")
        api.write_atomic(self.root / "etc/os-release", 'ID=debian\nVERSION_ID="12"\n')
        api.write_atomic(self.root / "proc/1/comm", "systemd\n")
        self.args = arguments("--install-dir", str(self.deployment))
        self.runner = FakeCommands(self.root)
        self.installer = api.Installer(self.args, self.root, self.runner)
        self.uid = patch.object(api.os, "geteuid", return_value=0)
        self.uid.start()
        self.addCleanup(self.uid.stop)

    def apply(self):
        with contextlib.redirect_stdout(io.StringIO()):
            self.installer.apply()

    def test_first_install_and_repeat_preserve_state(self):
        self.apply()
        config = self.installer.config.read_text()
        self.assertIn("192.0.2.20:443 ssl", config)
        self.assertIn("allow 198.51.100.10;", config)
        self.assertIn("deny all;", config)
        self.assertEqual(self.installer.hook.stat().st_mode & 0o777, 0o750)
        self.assertEqual(self.installer.previous.stat().st_mode & 0o777, 0o600)
        previous = self.installer.previous.read_text()
        self.runner.commands.clear()
        self.apply()
        self.assertEqual(config, self.installer.config.read_text())
        self.assertEqual(previous, self.installer.previous.read_text())
        self.assertFalse(any(cmd[0] == "apt-get" for cmd in self.runner.commands))
        self.assertEqual((self.deployment / ".env").read_text(), "private-and-preserved\n")
        self.assertFalse(any(cmd[0] in ("iptables", "ip6tables") for cmd in self.runner.commands))
        docker = [cmd for cmd in self.runner.commands if cmd[0] == "docker"]
        self.assertTrue(all("config" in cmd for cmd in docker))
        certbot = next(cmd for cmd in self.runner.commands if cmd[0] == "certbot")
        self.assertIn("--keep-until-expiring", certbot)
        self.assertNotIn("--nginx", certbot)
        self.assertIn(["systemctl", "enable", "--now", "certbot.timer"], self.runner.commands)

    def test_certificate_failure_keeps_challenge_only(self):
        self.runner.fail_certificate = True
        with self.assertRaises(subprocess.CalledProcessError):
            self.apply()
        config = self.installer.config.read_text()
        self.assertNotIn(":443", config)
        self.assertIn("location / { return 404; }", config)

    def test_untrusted_certificate_never_enables_https(self):
        self.runner.fail_verify = True
        with self.assertRaises(subprocess.CalledProcessError):
            self.apply()
        self.assertNotIn(":443", self.installer.config.read_text())

    def test_failed_rerun_preserves_existing_https(self):
        self.apply()
        old = self.installer.config.read_text()
        self.args.allow_source = ["198.51.100.22"]
        self.runner.fail_certificate = True
        with self.assertRaises(subprocess.CalledProcessError):
            self.apply()
        self.assertEqual(old, self.installer.config.read_text())

    def test_bad_candidate_preserves_config(self):
        self.apply()
        old = self.installer.config.read_text()
        self.args.allow_source = ["198.51.100.22"]
        self.runner.fail_candidate = True
        with self.assertRaises(subprocess.CalledProcessError):
            self.apply()
        self.assertEqual(old, self.installer.config.read_text())

    def test_reload_failure_rolls_back(self):
        self.apply()
        old = self.installer.config.read_text()
        self.args.allow_source = ["198.51.100.22"]
        self.runner.fail_reload = True
        with self.assertRaises(subprocess.CalledProcessError):
            self.apply()
        self.assertEqual(old, self.installer.config.read_text())

    def test_successful_update_replaces_full_allowlist(self):
        self.apply()
        self.args.allow_source = ["198.51.100.22"]
        self.apply()
        config = self.installer.config.read_text()
        self.assertIn("allow 198.51.100.22;", config)
        self.assertNotIn("allow 198.51.100.10;", config)
        self.assertIn("allow 198.51.100.10;", self.installer.previous.read_text())

    def test_unmanaged_nginx_is_untouched(self):
        api.write_atomic(self.installer.config, "operator-owned config\n")
        with self.assertRaisesRegex(RuntimeError, "unmanaged"):
            self.apply()
        self.assertEqual(self.installer.config.read_text(), "operator-owned config\n")
        self.assertTrue(all(command[0] == "docker" and "config" in command
                            for command in self.runner.commands))

    def test_changed_hostname_requires_migration(self):
        self.apply()
        self.args.hostname = "other.example.com"
        with self.assertRaisesRegex(RuntimeError, "migration"):
            self.apply()

    def test_public_homer_bind_rejected_before_install(self):
        self.runner.host_ip = "0.0.0.0"
        with self.assertRaisesRegex(RuntimeError, "127.0.0.1"):
            self.apply()
        self.assertFalse(self.installer.config.exists())

    def test_install_reuses_configured_hep_bind_address(self):
        self.args.listen_address = None
        self.apply()
        config = self.installer.config.read_text()
        self.assertIn("listen 192.0.2.20:80", config)
        self.assertIn("listen 192.0.2.20:443", config)
        self.assertEqual((self.deployment / ".env").read_text(), "private-and-preserved\n")

    def test_ambiguous_or_wildcard_bind_stops_before_host_changes(self):
        for bindings in ([], ["0.0.0.0"], ["::"], ["127.0.0.1"],
                         ["192.0.2.20", "198.51.100.20"]):
            with self.subTest(bindings=bindings):
                self.args.listen_address = None
                self.runner.hep_bindings = bindings
                self.runner.commands.clear()
                with self.assertRaisesRegex(RuntimeError, "pass --listen-address"):
                    self.apply()
                self.assertFalse(self.installer.config.exists())
                self.assertTrue(all(command[0] == "docker" and "config" in command
                                    for command in self.runner.commands))

    def test_explicit_address_overrides_wildcard_hep_binding(self):
        self.runner.hep_bindings = ["0.0.0.0"]
        self.apply()
        self.assertIn("listen 192.0.2.20:443", self.installer.config.read_text())

    def test_inferred_address_must_be_assigned_before_install(self):
        self.args.listen_address = None
        self.runner.hep_bindings = ["192.0.2.21"]
        with self.assertRaisesRegex(RuntimeError, "not assigned"):
            self.apply()
        self.assertFalse(self.installer.config.exists())

    def test_new_directories_readable_under_private_umask(self):
        old_umask = api.os.umask(0o077)
        try:
            self.apply()
        finally:
            api.os.umask(old_umask)
        webroot = self.root / api.WEBROOT.lstrip("/")
        for path in (webroot.parent, webroot, webroot / ".well-known",
                     webroot / ".well-known/acme-challenge"):
            self.assertEqual(path.stat().st_mode & 0o777, 0o755)

    def test_sync_replaces_sources_without_reissuing_or_changing_other_settings(self):
        self.apply()
        old = self.installer.config.read_text() + "# operator note\n"
        self.installer.config.write_text(old)
        self.args.allow_source = ["198.51.100.22", "10.20.0.0/24"]
        self.runner.commands.clear()
        with contextlib.redirect_stdout(io.StringIO()):
            self.installer.sync_allowlist()
        updated = self.installer.config.read_text()
        self.assertIn("allow 198.51.100.22;", updated)
        self.assertIn("allow 10.20.0.0/24;", updated)
        self.assertNotIn("allow 198.51.100.10;", updated)
        self.assertIn("# operator note\n", updated)
        self.assertIn("ssl_certificate " + api.LINEAGE, updated)
        self.assertEqual({command[0] for command in self.runner.commands}, {"nginx", "systemctl"})
        # A boot-time update must work even if Nginx has not started yet.
        self.assertIn(["systemctl", "reload-or-restart", "nginx.service"], self.runner.commands)

    def test_sync_reload_failure_preserves_previous_allowlist(self):
        self.apply()
        old = self.installer.config.read_text()
        self.args.allow_source = ["198.51.100.22"]
        self.runner.fail_reload = True
        with self.assertRaises(subprocess.CalledProcessError):
            self.installer.sync_allowlist()
        self.assertEqual(self.installer.config.read_text(), old)

    def test_sync_without_installed_api_is_noop(self):
        self.installer.sync_allowlist()
        self.assertFalse(self.installer.config.exists())
        self.assertEqual(self.runner.commands, [])

    def test_sync_upgrades_legacy_proxy_markers(self):
        self.apply()
        old = self.installer.config.read_text().replace("shared PBX allowlist", "shared HEP allowlist")
        self.installer.config.write_text(old)
        self.args.allow_source = ["198.51.100.22"]
        with contextlib.redirect_stdout(io.StringIO()):
            self.installer.sync_allowlist()
        updated = self.installer.config.read_text()
        self.assertIn("allow 198.51.100.22;", updated)
        self.assertNotIn("allow 198.51.100.10;", updated)
        self.assertIn(api.ACL_BEGIN, updated)


class ValidationTests(unittest.TestCase):
    def test_hostname_and_email_only_preview_reuses_compose_bind(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".env").write_text("private-and-preserved\n")
            runner = FakeCommands(root)
            with patch.object(api.Installer, "apply") as apply, \
                    patch.object(api, "read_access_sources", return_value=["198.51.100.10"]), \
                    patch.object(api.subprocess, "run", side_effect=runner), \
                    contextlib.redirect_stdout(io.StringIO()) as output:
                result = api.main(["--hostname", "homer.example.com", "--email", "admin@example.com",
                                   "--install-dir", str(root), "--preview"])
                apply.assert_not_called()
            self.assertEqual(result, 0)
            self.assertIn("listen 192.0.2.20:443", output.getvalue())
            self.assertNotIn("private-and-preserved", output.getvalue())
            self.assertEqual(len(runner.commands), 1)
            self.assertIn("config", runner.commands[0])

    def test_rejects_invalid_installation_arguments(self):
        for option, value in (("--hostname", "homer.example.com;include /tmp/evil"),
                              ("--hostname", "https://homer.example.com"),
                              ("--listen-address", "0.0.0.0"),
                              ("--listen-address", "::"),
                              ("--homer-port", "443")):
            with self.subTest(option=option, value=value):
                with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                    arguments(option, value)

    def test_preview_has_no_host_changes(self):
        argv = ["--hostname", "homer.example.com", "--email", "admin@example.com",
                "--listen-address", "192.0.2.20",
                "--preview"]
        with patch.object(api.Installer, "apply") as apply, \
                patch.object(api, "read_access_sources", return_value=["198.51.100.10"]):
            with contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(api.main(argv), 0)
            apply.assert_not_called()
        for endpoint in api.ENDPOINTS:
            self.assertIn(f"location = /api/v4/transactions/{endpoint}", output.getvalue())


class AccessSourceTests(unittest.TestCase):
    def test_legacy_inventory_is_reused_without_copying_or_reentering_sources(self):
        with tempfile.TemporaryDirectory() as directory:
            current = Path(directory) / "pbx-access.env"
            legacy = Path(directory) / "hep-firewall.env"
            contents = 'ALLOWED_HEP_SOURCES="198.51.100.22/32"\n'
            legacy.write_text(contents)
            with patch.object(api, "ACCESS_CONFIG", current), \
                    patch.object(api, "LEGACY_HEP_CONFIG", legacy):
                with contextlib.redirect_stdout(io.StringIO()) as output:
                    self.assertEqual(api.main(["--show-sources"]), 0)
                self.assertEqual(output.getvalue().strip(), "198.51.100.22")
                self.assertFalse(current.exists())
                self.assertEqual(legacy.read_text(), contents)
                # Renaming the file alone also works; the old variable is an alias.
                legacy.rename(current)
                self.assertEqual(api.default_access_config(), current)
                self.assertEqual(api.read_access_sources(current), ["198.51.100.22"])
                legacy.write_text('ALLOWED_HEP_SOURCES="198.51.100.33"\n')
                with self.assertRaisesRegex(ValueError, "Both"):
                    api.default_access_config()

    def test_legacy_argument_and_environment_remain_supported(self):
        with tempfile.NamedTemporaryFile(mode="w") as inventory:
            inventory.write('ALLOWED_HEP_SOURCES="198.51.100.22/32"\n')
            inventory.flush()
            args = api.parse_args(["--show-sources", "--hep-firewall-config", inventory.name])
            self.assertEqual(args.allow_source, ["198.51.100.22"])
        with patch.dict(os.environ, {"ALLOWED_HEP_SOURCES": "198.51.100.22"}, clear=True):
            self.assertEqual(api.parse_args(["--sync-allowlist"]).allow_source, ["198.51.100.22"])
        with patch.dict(os.environ, {"ALLOWED_HEP_SOURCES": "198.51.100.22",
                                    "ALLOWED_PBX_SOURCES": "198.51.100.33"}, clear=True):
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                api.parse_args(["--sync-allowlist"])

    def test_quotes_comments_cidrs_and_duplicate_addresses(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "pbx-access.env"
            for quote in ('"', "'", ""):
                path.write_text("# old list\nEXTERNAL_INTERFACE=eth0\n"
                                f"ALLOWED_PBX_SOURCES={quote}198.51.100.10/32 "
                                f"10.20.0.5/24 198.51.100.10{quote} # comment\nHEP_PORT=9060\n")
                self.assertEqual(api.read_access_sources(path), ["10.20.0.0/24", "198.51.100.10"])

    def test_invalid_missing_and_empty_lists_fail_without_executing_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "pbx-access.env"
            with self.assertRaises(FileNotFoundError):
                api.read_access_sources(path)
            marker = Path(directory) / "must-not-exist"
            for content in ("", 'ALLOWED_PBX_SOURCES=""',
                            "ALLOWED_PBX_SOURCES=198.51.100.1\nALLOWED_PBX_SOURCES=198.51.100.2",
                            "ALLOWED_PBX_SOURCES=198.51.100.1\nALLOWED_HEP_SOURCES=198.51.100.2",
                            'ALLOWED_PBX_SOURCES="0.0.0.0/0"',
                            'ALLOWED_PBX_SOURCES="::1"',
                            'ALLOWED_PBX_SOURCES="198.51.100.2; allow all"',
                            f'ALLOWED_PBX_SOURCES="$(touch {marker})"',
                            'ALLOWED_PBX_SOURCES="198.51.100.1'):
                path.write_text(content)
                with self.subTest(content=content), self.assertRaises(ValueError):
                    api.read_access_sources(path)
            self.assertFalse(marker.exists())

    def test_installer_reads_default_file_and_ignores_ambient_sources(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "pbx-access.env"
            path.write_text('ALLOWED_PBX_SOURCES="198.51.100.22/32"\n')
            with patch.object(api, "ACCESS_CONFIG", path), \
                    patch.object(api, "LEGACY_HEP_CONFIG", Path(directory) / "old.env"), \
                    patch.dict(os.environ, {"ALLOWED_PBX_SOURCES": "198.51.100.99"}):
                args = api.parse_args(["--hostname", "homer.example.com", "--email",
                                       "admin@example.com", "--listen-address", "192.0.2.20"])
            self.assertEqual(args.allow_source, ["198.51.100.22"])

    def test_sync_uses_effective_access_service_environment(self):
        with patch.dict(os.environ, {"ALLOWED_PBX_SOURCES": "198.51.100.22/32"}), \
                patch.object(api, "read_access_sources") as read:
            args = api.parse_args(["--sync-allowlist"])
        read.assert_not_called()
        self.assertEqual(args.allow_source, ["198.51.100.22"])

    def test_invalid_file_cannot_trigger_installer(self):
        with tempfile.NamedTemporaryFile(mode="w") as inventory:
            inventory.write('ALLOWED_PBX_SOURCES=""\n')
            inventory.flush()
            with patch.object(api.Installer, "apply") as apply, \
                    contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                api.main(["--hostname", "homer.example.com", "--email", "admin@example.com",
                          "--listen-address", "192.0.2.20", "--access-config", inventory.name])
            apply.assert_not_called()


class AccessHelperTests(unittest.TestCase):
    def test_legacy_and_new_helpers_share_rules_and_proxy_inventory(self):
        # All firewall commands and proxy changes are replaced by local recorders.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            scripts = Path(__file__).resolve().parents[1] / "ops"
            config = root / "nginx.conf"
            config.write_text(api.MARKER)
            for name in ("apply-pbx-access.sh", "apply-hep-firewall.sh"):
                path = root / name
                path.write_text((scripts / name).read_text().replace("/etc/nginx/nginx.conf", str(config)))
                path.chmod(0o755)
            fakes = {
                "id": "#!/bin/sh\necho 0\n",
                "iptables": '#!/bin/sh\nprintf "%s\\n" "$*" >> "$TEST_RULES"\n'
                            '[ "$2" != "-C" ]\n',
                "install-homer-api.sh": '#!/bin/sh\nprintf "%s\\n" "$ALLOWED_PBX_SOURCES" "$*" '
                                        '> "$TEST_SYNC"\n',
            }
            for name, content in fakes.items():
                path = root / name
                path.write_text(content)
                path.chmod(0o755)
            rules, sync = root / "rules.log", root / "sync.log"
            environment = {key: value for key, value in os.environ.items() if key not in api.SOURCE_KEYS}
            environment.update(PATH=str(root) + os.pathsep + os.environ.get("PATH", ""),
                               EXTERNAL_INTERFACE="eth0", TEST_RULES=str(rules), TEST_SYNC=str(sync))
            for entry, key in (("apply-pbx-access.sh", "ALLOWED_PBX_SOURCES"),
                               ("apply-hep-firewall.sh", "ALLOWED_HEP_SOURCES")):
                with self.subTest(entry=entry):
                    result = subprocess.run([str(root / entry)],
                                            env={**environment, key: "198.51.100.22/32"},
                                            text=True, capture_output=True)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertIn("-A FSPBX-HEP -p tcp -s 198.51.100.22/32", rules.read_text())
                    self.assertIn("-A FSPBX-HEP -p tcp -m conntrack --ctorigdstport 9060 -j DROP",
                                  rules.read_text())
                    self.assertEqual(sync.read_text(), "198.51.100.22/32\n--sync-allowlist\n")
                    rules.unlink()
                    sync.unlink()
            result = subprocess.run([str(root / "apply-pbx-access.sh")],
                                    env={**environment, "ALLOWED_PBX_SOURCES": "198.51.100.22",
                                         "ALLOWED_HEP_SOURCES": "198.51.100.33"},
                                    text=True, capture_output=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("conflicting", result.stderr)
            self.assertFalse(rules.exists())
            self.assertFalse(sync.exists())


class ProxyTests(unittest.TestCase):
    """Exercise generated Nginx rules on temporary loopback ports, if available."""

    def test_http_and_https_access_controls(self):
        nginx = shutil.which("nginx")
        if not nginx:
            self.skipTest("nginx unavailable; CI installs it for real proxy checks")
        received = []

        class Upstream(BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                received.append((self.path, dict(self.headers), body))
                self.send_response(200 if self.headers.get("Auth-Token") == "test-token" else 401)
                self.end_headers()
                self.wfile.write(b'{"data":{"items":[]}}')

            def log_message(self, *_args):
                pass

        with tempfile.TemporaryDirectory(prefix="homer-proxy-test-") as directory:
            root = Path(directory)
            root.chmod(0o755)
            upstream = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
            thread = threading.Thread(target=upstream.serve_forever, daemon=True)
            thread.start()
            self.addCleanup(upstream.server_close)
            self.addCleanup(upstream.shutdown)

            def free_port():
                with socket.socket() as listener:
                    listener.bind(("127.0.0.1", 0))
                    return listener.getsockname()[1]

            http_port, https_port = free_port(), free_port()
            while https_port == http_port:
                https_port = free_port()
            cert, key = root / "cert.pem", root / "key.pem"
            subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                            "-days", "1", "-subj", "/CN=homer.example.com", "-addext",
                            "subjectAltName=DNS:homer.example.com,IP:127.0.0.1",
                            "-keyout", str(key), "-out", str(cert)],
                           check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            challenge = root / "webroot/.well-known/acme-challenge"
            challenge.mkdir(parents=True)
            (challenge / "test-challenge").write_text("test-validation")
            args = arguments()
            args.listen_address = "127.0.0.1"
            args.allow_source = ["127.0.0.1"]
            args.homer_port = upstream.server_port
            config = api.render_nginx(args)
            replacements = {
                "user www-data;": "",
                "/run/nginx.pid": str(root / "nginx.pid"),
                "/var/log/nginx/error.log": str(root / "error.log"),
                "/var/log/nginx/access.log": str(root / "access.log"),
                "127.0.0.1:80 default_server": f"127.0.0.1:{http_port} default_server",
                "127.0.0.1:443 ssl": f"127.0.0.1:{https_port} ssl",
                f"{api.LINEAGE}/fullchain.pem": str(cert),
                f"{api.LINEAGE}/privkey.pem": str(key),
                api.WEBROOT: str(root / "webroot"),
                "http {": (f"http {{\nclient_body_temp_path {root}/body;\n"
                           f"proxy_temp_path {root}/proxy;\n"
                           f"fastcgi_temp_path {root}/fastcgi;\n"
                           f"uwsgi_temp_path {root}/uwsgi;\nscgi_temp_path {root}/scgi;"),
            }
            for before, after in replacements.items():
                config = config.replace(before, after)
            config_path = root / "nginx.conf"
            config_path.write_text(config)
            checked = subprocess.run([nginx, "-t", "-p", str(root), "-c", str(config_path)],
                                     capture_output=True, text=True)
            self.assertEqual(checked.returncode, 0, checked.stderr)
            process = subprocess.Popen([nginx, "-p", str(root), "-c", str(config_path),
                                        "-g", "daemon off;"], stdout=subprocess.DEVNULL,
                                       stderr=subprocess.DEVNULL)
            try:
                for _attempt in range(50):
                    try:
                        with socket.create_connection(("127.0.0.1", http_port), timeout=0.2):
                            break
                    except OSError:
                        if process.poll() is not None:
                            self.fail((root / "error.log").read_text())
                        time.sleep(0.05)
                context = ssl.create_default_context(cafile=str(cert))

                def request(path, method="POST", tls=True, source="127.0.0.1", headers=None):
                    options = {"timeout": 3, "source_address": (source, 0)}
                    if tls:
                        connection = http.client.HTTPSConnection("127.0.0.1", https_port,
                                                                 context=context, **options)
                    else:
                        connection = http.client.HTTPConnection("127.0.0.1", http_port, **options)
                    try:
                        connection.request(method, path, body="{}" if method == "POST" else None,
                                           headers={"Host": "homer.example.com",
                                                    "Auth-Token": "test-token", **(headers or {})})
                        response = connection.getresponse()
                        return response.status, response.read()
                    finally:
                        connection.close()

                self.assertEqual(request("/.well-known/acme-challenge/test-challenge",
                                         method="GET", tls=False), (200, b"test-validation"))
                for path in ("/", "/api/v4/transactions/messages",
                             "/.well-known/acme-challenge/missing",
                             "/.well-known/acme-challenge/../nginx.conf"):
                    self.assertEqual(request(path, method="GET", tls=False)[0], 404)
                self.assertEqual(request("/.well-known/acme-challenge/test-challenge",
                                         tls=False)[0], 403)
                self.assertEqual(received, [])
                for endpoint in api.ENDPOINTS:
                    route = f"/api/v4/transactions/{endpoint}"
                    self.assertEqual(request(route)[0], 200)
                    count = len(received)
                    self.assertEqual(request(route, source="127.0.0.2",
                                             headers={"X-Forwarded-For": "127.0.0.1"})[0], 403)
                    self.assertEqual(request(route, method="GET")[0], 405)
                    self.assertEqual(request(route + "?token=foo")[0], 400)
                    self.assertEqual(request(route, headers={"Auth-Token": ""})[0], 401)
                    self.assertEqual(len(received), count)
                route = "/api/v4/transactions/messages"
                self.assertEqual(request(route, headers={"Auth-Token": "invalid"})[0], 401)
                self.assertEqual(request(route, headers={"Cookie": "session=bad",
                                                       "Authorization": "Bearer bad",
                                                       "X-Forwarded-For": "198.51.100.10"})[0], 200)
                _, headers, body = received[-1]
                self.assertNotIn("Cookie", headers)
                self.assertNotIn("Authorization", headers)
                self.assertEqual(headers["X-Forwarded-For"], "127.0.0.1")
                self.assertEqual(body, b"{}")
                count = len(received)
                for path in ("/", "/api/v4/auth-tokens", "/api/v4/query", route + "/"):
                    self.assertEqual(request(path)[0], 404)
                self.assertEqual(len(received), count)
            finally:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)


class ComposeTests(unittest.TestCase):
    def test_existing_env_needs_no_api_opt_in(self):
        if not shutil.which("docker"):
            self.skipTest("Docker Compose unavailable")
        project = Path(__file__).resolve().parents[1]
        # Model an older installation: its environment has no new API settings.
        template = (project / ".env.example").read_text()
        legacy_env = "\n".join(line for line in template.splitlines()
                               if "API_TOKEN_ACCESS" not in line)
        with tempfile.NamedTemporaryFile(mode="w") as environment:
            environment.write(legacy_env)
            environment.flush()
            result = subprocess.run(
                ["docker", "compose", "-f", str(project / "compose.yaml"),
                 "--env-file", environment.name, "config", "--format", "json"],
                check=True, capture_output=True, text=True,
                env={key: value for key, value in os.environ.items()
                     if key not in ("COMPOSE_FILE", "HOMER_API_TOKEN_ACCESS")})
        homer = json.loads(result.stdout)["services"]["homer"]
        self.assertEqual(homer["environment"]["HOMER_COORDINATOR_API_SETTINGS_ENABLE_TOKEN_ACCESS"],
                         "true")
        self.assertEqual(homer["environment"]["HOMER_COORDINATOR_API_SETTINGS_AUTH_TOKEN_HEADER"],
                         "Auth-Token")


if __name__ == "__main__":
    unittest.main()
