#!/usr/bin/env python3
"""Install the HOMER API HTTPS proxy on a dedicated Debian monitoring host."""

import argparse
import fcntl
import ipaddress
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile


MARKER = "# Managed by fspbx-monitoring install-homer-api.sh\n"
CERT_NAME = "fspbx-homer-api"
WEBROOT = "/var/www/fspbx-homer-acme"
LINEAGE = f"/etc/letsencrypt/live/{CERT_NAME}"
ENDPOINTS = ("messages", "export/pcap", "qos")
ACCESS_CONFIG = Path("/etc/fs-monitoring/pbx-access.env")
LEGACY_HEP_CONFIG = Path("/etc/fs-monitoring/hep-firewall.env")
SOURCE_KEYS = ("ALLOWED_PBX_SOURCES", "ALLOWED_HEP_SOURCES")
ACL_BEGIN = "        # BEGIN shared PBX allowlist\n"
ACL_END = "        # END shared PBX allowlist\n"


def validate_sources(value):
    sources = set()
    for source in value.split():
        # Match iptables' normalization of an address with a subnet mask.
        network = ipaddress.IPv4Network(source, strict=False)
        if (network.prefixlen == 0 or network.network_address.is_unspecified
                or network.network_address.is_multicast):
            raise ValueError("PBX allowlist contains an unrestricted or invalid source")
        sources.add(str(network.network_address) if network.prefixlen == 32 else str(network))
    if not sources:
        raise ValueError("ALLOWED_PBX_SOURCES must contain at least one approved source")
    return sorted(sources)


def default_access_config():
    current = ACCESS_CONFIG.exists() or ACCESS_CONFIG.is_symlink()
    legacy = LEGACY_HEP_CONFIG.exists() or LEGACY_HEP_CONFIG.is_symlink()
    if current and legacy and ACCESS_CONFIG.resolve() != LEGACY_HEP_CONFIG.resolve():
        raise ValueError("Both PBX and legacy HEP inventories exist; keep one shared source file")
    return ACCESS_CONFIG if current or not legacy else LEGACY_HEP_CONFIG


def read_access_sources(path):
    """Read the shipped single-line EnvironmentFile format without executing it."""
    values = []
    for line in path.read_text().splitlines():
        match = re.fullmatch(r"\s*(?:ALLOWED_PBX_SOURCES|ALLOWED_HEP_SOURCES)\s*=\s*(.*?)\s*", line)
        if match:
            values.append(" ".join(shlex.split(match.group(1), comments=True, posix=True)))
    if len(values) != 1:
        raise ValueError(f"{path} must define ALLOWED_PBX_SOURCES (or its legacy alias) exactly once")
    return validate_sources(values[0])


def validate_listen_address(value):
    address = ipaddress.IPv4Address(value)
    if address.is_unspecified or address.is_multicast or address.is_loopback:
        raise ValueError("listen address must be a specific non-loopback IPv4 host address")
    return str(address)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hostname", help="DNS hostname, without https://")
    parser.add_argument("--email", help="Let's Encrypt account email")
    parser.add_argument("--listen-address",
                        help="MON01's local IPv4 address (default: configured HEP bind address)")
    parser.add_argument("--access-config", "--hep-firewall-config", type=Path,
                        help=f"shared PBX allowlist (default: {ACCESS_CONFIG}, with legacy fallback)")
    parser.add_argument("--install-dir", type=Path, default=Path("/opt/monitoring"))
    parser.add_argument("--homer-port", type=int, default=8080)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--preview", action="store_true",
                      help="print the HTTPS configuration without changing anything")
    mode.add_argument("--show-sources", action="store_true",
                      help="print validated sources from the shared PBX allowlist")
    mode.add_argument("--sync-allowlist", action="store_true",
                      help="refresh an installed API proxy from the shared PBX allowlist only")
    args = parser.parse_args(argv)
    try:
        if (args.sync_allowlist and args.access_config is None
                and any(key in os.environ for key in SOURCE_KEYS)):
            # The access systemd service/direct helper supplies its effective list.
            # Normal installation always reads the file, never ambient environment.
            values = [os.environ[key] for key in SOURCE_KEYS if key in os.environ]
            if len(set(values)) != 1:
                raise ValueError("Conflicting PBX and legacy HEP source environment variables")
            args.allow_source = validate_sources(values[0])
        else:
            args.allow_source = read_access_sources(args.access_config or default_access_config())
    except (OSError, ValueError) as error:
        parser.error(f"Cannot reuse PBX allowlist: {error}")
    if args.sync_allowlist or args.show_sources:
        if args.hostname or args.email or args.listen_address:
            parser.error("hostname/email/listen-address are only used for installation/preview")
        return args
    if not all((args.hostname, args.email)):
        parser.error("installation/preview requires --hostname and --email")
    args.hostname = args.hostname.lower()
    labels = args.hostname.split(".")
    if (len(args.hostname) > 253 or len(labels) < 2
            or any(not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
                   for label in labels) or labels[-1].isdigit()):
        parser.error("--hostname must be a DNS hostname (use ASCII/punycode)")
    if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", args.email) or args.email.startswith("-"):
        parser.error("--email must be a valid account email")
    if args.listen_address is not None:
        try:
            args.listen_address = validate_listen_address(args.listen_address)
        except ValueError as error:
            parser.error(str(error))
    if not 1 <= args.homer_port <= 65535 or args.homer_port in (80, 443):
        parser.error("--homer-port must be a valid loopback upstream port, other than 80/443")
    if not args.install_dir.is_absolute():
        parser.error("--install-dir must be absolute")
    return args


def render_allowlist(sources):
    return (ACL_BEGIN + "".join(f"        allow {source};\n" for source in sources)
            + "        deny all;\n" + ACL_END)


def render_nginx(args, tls=True):
    """Own the complete config: never include distribution default sites."""
    config = MARKER + f"# Hostname: {args.hostname}\n# Listen: {args.listen_address}\n" + f"""
user www-data;
worker_processes auto;
pid /run/nginx.pid;
error_log /var/log/nginx/error.log warn;
events {{ worker_connections 1024; }}
http {{
    server_tokens off;
    default_type application/octet-stream;
    # No query strings, tokens, request bodies, or SIP payloads in access logs.
    log_format homer_api '$remote_addr [$time_local] $request_method $status $body_bytes_sent';
    access_log /var/log/nginx/access.log homer_api;
    client_max_body_size 64k;
    client_body_timeout 15s;
    client_header_timeout 15s;
    keepalive_timeout 15s;

    server {{
        listen {args.listen_address}:80 default_server;
        server_name {args.hostname};
        if ($host != {args.hostname}) {{ return 404; }}
        # Only existing HTTP-01 token files; no homepage, API, or redirects.
        location ~ "^/\\.well-known/acme-challenge/[A-Za-z0-9_-]+$" {{
            root {WEBROOT};
            default_type text/plain;
            limit_except GET {{ deny all; }}
            try_files $uri =404;
        }}
        location / {{ return 404; }}
    }}
"""
    if tls:
        config += f"""
    server {{
        listen {args.listen_address}:443 ssl default_server;
        server_name {args.hostname};
        if ($host != {args.hostname}) {{ return 404; }}
        ssl_certificate {LINEAGE}/fullchain.pem;
        ssl_certificate_key {LINEAGE}/privkey.pem;
        ssl_protocols TLSv1.2 TLSv1.3;
        ssl_session_tickets off;
        ssl_session_cache shared:HomerTLS:10m;
{render_allowlist(args.allow_source)}\
        satisfy all;

        # Forward only the integration credential, never cookies or Bearer JWTs.
        proxy_pass_request_headers off;
        proxy_set_header Host $host;
        proxy_set_header Content-Type application/json;
        proxy_set_header Auth-Token $http_auth_token;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $remote_addr;
        proxy_set_header X-Forwarded-Proto https;
        proxy_connect_timeout 5s;
        proxy_read_timeout 30s;
        proxy_send_timeout 30s;
        proxy_buffering off;
        proxy_max_temp_file_size 0;
        proxy_cache off;
        add_header Cache-Control "private, no-store" always;
        add_header X-Content-Type-Options nosniff always;
"""
        for endpoint in ENDPOINTS:
            config += f"""
        location = /api/v4/transactions/{endpoint} {{
            if ($request_method != POST) {{ return 405; }}
            if ($args != "") {{ return 400; }}
            if ($http_auth_token = "") {{ return 401; }}
            proxy_pass http://127.0.0.1:{args.homer_port};
        }}
"""
        config += "        location / { return 404; }\n    }\n"
    return config + "}\n"


def render_hook():
    return "#!/bin/sh\n" + MARKER + f"""set -eu
[ "${{RENEWED_LINEAGE:-}}" = "{LINEAGE}" ] || exit 0
/usr/sbin/nginx -t
/usr/bin/systemctl reload nginx.service
"""


def run(command, **kwargs):
    return subprocess.run(command, check=True, text=True, **kwargs)


def write_atomic(path, text, mode=0o644):
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=".homer-api-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w") as output:
            output.write(text)
            os.fchmod(output.fileno(), mode)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


class Installer:
    def __init__(self, args, root=Path("/"), runner=run):
        # root/runner are injectable for isolated tests, never CLI options.
        self.args = args
        self.root = root
        self.run = runner
        self.config = root / "etc/nginx/nginx.conf"
        self.hook = root / f"etc/letsencrypt/renewal-hooks/deploy/{CERT_NAME}"
        self.previous = root / "etc/fs-monitoring/homer-api-nginx.previous"

    def read_compose_model(self):
        if not (self.args.install_dir / ".env").is_file():
            raise RuntimeError("Configure the monitoring stack before installing its HTTPS proxy")
        # The rendered model includes secrets: capture it in memory, never print it.
        result = self.run(["docker", "compose", "--project-directory",
                           str(self.args.install_dir), "config", "--format", "json"],
                          capture_output=True)
        return json.loads(result.stdout)

    def resolve_listen_address(self, model=None):
        if self.args.listen_address is not None:
            return
        if model is None:
            model = self.read_compose_model()
        addresses = {port.get("host_ip", "0.0.0.0")
                     for port in model["services"]["homer"].get("ports", [])
                     if port["target"] == 9060}
        if len(addresses) == 1:
            try:
                self.args.listen_address = validate_listen_address(addresses.pop())
                return
            except ValueError:
                pass
        raise RuntimeError("Cannot infer a specific IPv4 listener from HOMER's HEP binding; "
                           "pass --listen-address with MON01's local IPv4 address "
                           "(wildcard, IPv6, and multiple bindings are not selected automatically)")

    def check_ownership(self):
        for path in (self.config, self.hook):
            if path.is_symlink():
                raise RuntimeError(f"Refusing to replace symlink: {path}")
            if path.exists() and MARKER not in path.read_text().splitlines(keepends=True)[:2]:
                raise RuntimeError(f"Existing unmanaged file: {path}; use a dedicated Nginx host")
        if self.config.exists():
            lines = self.config.read_text().splitlines()
            for setting in (f"# Hostname: {self.args.hostname}",
                            f"# Listen: {self.args.listen_address}"):
                if setting not in lines[:3]:
                    raise RuntimeError("Hostname/listen address changed; review migration manually")
        elif (self.root / f"etc/letsencrypt/renewal/{CERT_NAME}.conf").exists():
            raise RuntimeError("Existing certificate lineage is not owned by this installer")

    def preflight(self):
        if os.geteuid() != 0:
            raise RuntimeError("Run as root; --preview does not require root")
        release = dict(line.split("=", 1) for line in
                       (self.root / "etc/os-release").read_text().splitlines() if "=" in line)
        if (release.get("ID", "").strip('"') != "debian"
                or release.get("VERSION_ID", "").strip('"') not in ("11", "12", "13")):
            raise RuntimeError("Supported installer hosts are Debian 11, 12, and 13")
        if (self.root / "proc/1/comm").read_text().strip() != "systemd":
            raise RuntimeError("systemd must be PID 1")
        model = self.read_compose_model()
        self.resolve_listen_address(model)
        self.check_ownership()
        ports = [port for port in model["services"]["homer"].get("ports", [])
                 if port["target"] == 8080]
        if (len(ports) != 1 or ports[0].get("host_ip") != "127.0.0.1"
                or str(ports[0].get("published")) != str(self.args.homer_port)):
            raise RuntimeError("HOMER must publish only 127.0.0.1:<--homer-port> for target 8080")
        addresses = json.loads(self.run(["ip", "-j", "-4", "address", "show"],
                                       capture_output=True).stdout)
        if not any(item.get("local") == self.args.listen_address
                   for interface in addresses for item in interface.get("addr_info", [])):
            raise RuntimeError("--listen-address is not assigned to this host")

    def install_packages(self):
        packages = ("nginx", "certbot", "ca-certificates", "openssl")
        missing = []
        for package in packages:
            try:
                result = self.run(["dpkg-query", "-W", "-f=${db:Status-Status}", package],
                                  capture_output=True)
                if result.stdout.strip() != "installed":
                    missing.append(package)
            except subprocess.CalledProcessError:
                missing.append(package)
        if missing:
            self.run(["apt-get", "update"])
            # A restrictive bootstrap config already exists before package scripts
            # can start Nginx. Preserve it instead of installing the default site.
            self.run(["apt-get", "install", "-y", "--no-install-recommends",
                      "-o", "Dpkg::Options::=--force-confold", *missing],
                     env={**os.environ, "DEBIAN_FRONTEND": "noninteractive"})

    def activate(self, content, service_action="reload"):
        old = self.config.read_text()
        if content == old:
            self.run(["nginx", "-t"])
            return
        candidate = self.config.with_name("homer-api.candidate")
        write_atomic(candidate, content)
        try:
            self.run(["nginx", "-t", "-c", str(candidate)])
            write_atomic(self.previous, old, 0o600)
            os.replace(candidate, self.config)
            try:
                self.run(["systemctl", service_action, "nginx.service"])
            except subprocess.CalledProcessError:
                write_atomic(self.config, old)
                self.run(["nginx", "-t"])
                self.run(["systemctl", service_action, "nginx.service"])
                raise
        finally:
            candidate.unlink(missing_ok=True)

    def sync_allowlist(self):
        if not self.config.exists():
            return
        old = self.config.read_text()
        if self.config.is_symlink() or not old.startswith(MARKER):
            raise RuntimeError("Refusing to change an unmanaged Nginx configuration")
        if "ssl_certificate " not in old:
            return  # First-time certificate setup has not enabled HTTPS yet.
        # Upgrade a proxy rendered before the shared list was renamed, too.
        pairs = [(ACL_BEGIN, ACL_END),
                 (ACL_BEGIN.replace("PBX", "HEP"), ACL_END.replace("PBX", "HEP"))]
        present = [(begin, end) for begin, end in pairs if begin in old or end in old]
        if (len(present) != 1 or old.count(present[0][0]) != 1
                or old.count(present[0][1]) != 1):
            raise RuntimeError("Managed API allowlist markers missing; rerun the HTTPS installer")
        begin, end = present[0]
        before, remainder = old.split(begin, 1)
        _old_sources, after = remainder.split(end, 1)
        # The access boot service may run before Nginx starts. A changed allowlist
        # must still take effect; reload an active proxy or start an inactive one.
        self.activate(before + render_allowlist(self.args.allow_source) + after,
                      service_action="reload-or-restart")
        print("HOMER API proxy now uses the shared PBX source allowlist.")

    def apply(self):
        self.preflight()
        # The directory is dedicated to challenge files, never application files.
        webroot = self.root / WEBROOT.lstrip("/")
        for directory in (webroot.parent, webroot, webroot / ".well-known",
                          webroot / ".well-known/acme-challenge"):
            if not directory.exists():
                directory.mkdir(parents=True)
                directory.chmod(0o755)
            if directory.is_symlink():
                raise RuntimeError(f"Refusing a symlink in the challenge path: {directory}")
        if not self.config.exists():
            write_atomic(self.config, render_nginx(self.args, tls=False))
        self.install_packages()
        self.run(["nginx", "-t"])
        self.run(["systemctl", "enable", "--now", "nginx.service"])
        write_atomic(self.hook, render_hook(), 0o750)
        self.run(["certbot", "certonly", "--config", "/dev/null", "--non-interactive",
                  "--agree-tos", "--email", self.args.email, "--authenticator", "webroot",
                  "--webroot-path", WEBROOT, "--preferred-challenges", "http-01",
                  "--cert-name", CERT_NAME, "--domain", self.args.hostname,
                  "--keep-until-expiring", "--server",
                  "https://acme-v02.api.letsencrypt.org/directory"])
        self.run(["openssl", "verify", "-purpose", "sslserver", "-verify_hostname",
                  self.args.hostname, "-CApath", "/etc/ssl/certs", "-untrusted",
                  f"{LINEAGE}/chain.pem", f"{LINEAGE}/cert.pem"])
        self.activate(render_nginx(self.args))
        self.run(["systemctl", "enable", "--now", "certbot.timer"])
        print(f"HTTPS proxy installed: https://{self.args.hostname}")
        print("HTTP serves only ACME challenges. HTTPS permits only the listed PBX IPs.")
        print("Firewall rules, HOMER tokens, .env, containers, and PBXs were not changed.")
        print("Follow docs/HOMER_API.md to configure the firewall/token and test renewal.")


def main(argv=None):
    args = parse_args(argv)
    if args.show_sources:
        print("\n".join(args.allow_source))
        return 0
    if not args.preview and os.geteuid() != 0:
        print("ERROR: run as root; --preview does not require root", file=sys.stderr)
        return 1
    try:
        if args.preview:
            installer = Installer(args)
            installer.resolve_listen_address()
            print(render_nginx(args), end="")
            return 0
        # Serialize installers so concurrent runs cannot overwrite a validated config.
        with open("/run/lock/fspbx-homer-api.lock", "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            installer = Installer(args)
            if args.sync_allowlist:
                installer.sync_allowlist()
            else:
                installer.apply()
    except (OSError, ValueError, KeyError, RuntimeError, subprocess.CalledProcessError) as error:
        # CalledProcessError does not print captured Compose stdout (which has secrets).
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
