"""Running one URL check, and the certificate inspection that goes with it.

Deliberately stdlib-only (urllib + ssl): the backend has no HTTP client
dependency and this does not justify adding one.

Nothing here touches the database. A check takes a plain dict of settings and
returns a plain dict of results, so it can run in a worker thread while the
event loop carries on -- see the checker loop in main.py.
"""
import json
import socket
import ssl
import time
import urllib.error
import urllib.request
from datetime import datetime
from urllib.parse import urlparse

DEFAULT_TIMEOUT = 10
MAX_BODY_BYTES = 64 * 1024  # enough to match against; we are not storing pages


def parse_expected(expected: str | None) -> list[tuple[int, int]]:
    """"200-399, 401" -> [(200, 399), (401, 401)]. A blank or unparseable
    value falls back to "any success-ish code", which is what someone who
    never touched the field meant."""
    ranges: list[tuple[int, int]] = []
    for part in (expected or "").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            if "-" in part:
                lo, hi = part.split("-", 1)
                ranges.append((int(lo), int(hi)))
            else:
                ranges.append((int(part), int(part)))
        except ValueError:
            continue
    return ranges or [(200, 399)]


def status_allowed(code: int, expected: str | None) -> bool:
    return any(lo <= code <= hi for lo, hi in parse_expected(expected))


def parse_headers(raw: str | None) -> dict:
    """Accepts JSON, or the "Name: value" per line form people actually type."""
    raw = (raw or "").strip()
    if not raw:
        return {}
    if raw.startswith("{"):
        try:
            return {str(k): str(v) for k, v in json.loads(raw).items()}
        except Exception:
            return {}
    out = {}
    for line in raw.splitlines():
        if ":" in line:
            name, _, value = line.partition(":")
            if name.strip():
                out[name.strip()] = value.strip()
    return out


def inspect_certificate(url: str, timeout: int = DEFAULT_TIMEOUT) -> dict:
    """Expiry and validity for an https URL, checked on its own TLS handshake.

    Separate from the HTTP request on purpose: a site can fail its request for
    unrelated reasons and still have a certificate worth reporting on, and an
    expiring certificate is worth knowing about on a site that is perfectly up.
    """
    parsed = urlparse(url)
    if parsed.scheme != "https":
        return {"cert_checked": False}

    host = parsed.hostname or ""
    port = parsed.port or 443
    context = ssl.create_default_context()
    try:
        with socket.create_connection((host, port), timeout=timeout) as raw:
            with context.wrap_socket(raw, server_hostname=host) as tls:
                cert = tls.getpeercert()
        expires = datetime.strptime(cert["notAfter"], "%b %d %H:%M:%S %Y %Z")
        issuer = dict(x[0] for x in cert.get("issuer", ()) if x).get("organizationName", "")
        return {
            "cert_checked": True,
            "cert_valid": True,
            "cert_expires_at": expires,
            "cert_issuer": issuer or None,
            "cert_error": None,
        }
    except ssl.SSLCertVerificationError as e:
        # The common real-world cases: expired, self-signed, hostname mismatch,
        # and a chain missing its intermediate -- which browsers often paper
        # over and other clients do not.
        return {"cert_checked": True, "cert_valid": False, "cert_expires_at": None,
                "cert_issuer": None, "cert_error": e.verify_message or str(e)}
    except (socket.timeout, TimeoutError, socket.gaierror, ConnectionError, OSError) as e:
        # Never reached the host, so nothing was learned about its certificate.
        # Reporting "invalid" here would blame the certificate for a firewall,
        # a DNS answer we cannot route to, or a host that is simply down -- and
        # would raise a certificate alert for a network fault.
        return {"cert_checked": False, "cert_unreachable": f"{type(e).__name__}: {e}"}
    except Exception as e:
        return {"cert_checked": True, "cert_valid": False, "cert_expires_at": None,
                "cert_issuer": None, "cert_error": f"{type(e).__name__}: {e}"}


def run_check(cfg: dict) -> dict:
    """Perform one check. Never raises -- a failed check is a result, not an error."""
    url = cfg["url"]
    timeout = int(cfg.get("timeout_seconds") or DEFAULT_TIMEOUT)
    method = (cfg.get("method") or "GET").upper()
    verify_tls = cfg.get("verify_tls", True)

    result = {
        "checked_at": datetime.utcnow(),
        "ok": False, "status_code": None, "response_ms": None, "error": None,
        "cert_checked": False, "cert_valid": None, "cert_expires_at": None,
        "cert_issuer": None, "cert_error": None,
    }

    if verify_tls:
        result.update(inspect_certificate(url, timeout))

    context = ssl.create_default_context()
    if not verify_tls:
        # Explicitly asked for by the operator, for internal hosts with
        # self-signed certificates. Certificate checks are skipped entirely
        # rather than reported as failures.
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE

    request = urllib.request.Request(url, method=method)
    for name, value in parse_headers(cfg.get("headers")).items():
        request.add_header(name, value)
    request.add_header("User-Agent", "InfraWatch/1.0 (+url-monitor)")

    started = time.monotonic()
    body = ""
    try:
        with urllib.request.urlopen(request, timeout=timeout, context=context) as resp:
            code = resp.status
            if cfg.get("body_contains"):
                body = resp.read(MAX_BODY_BYTES).decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        # A 4xx/5xx is a real answer: the expected-status setting decides
        # whether it counts as a failure, not urllib.
        code = e.code
        if cfg.get("body_contains"):
            try:
                body = e.read(MAX_BODY_BYTES).decode("utf-8", "replace")
            except Exception:
                body = ""
    except Exception as e:
        result["response_ms"] = int((time.monotonic() - started) * 1000)
        result["error"] = f"{type(e).__name__}: {e}"
        return result

    result["response_ms"] = int((time.monotonic() - started) * 1000)
    result["status_code"] = code

    if not status_allowed(code, cfg.get("expected_status")):
        result["error"] = f"Unexpected status {code}"
        return result

    needle = (cfg.get("body_contains") or "").strip()
    if needle and needle not in body:
        # A 200 that renders an error page is the failure this catches.
        result["error"] = f"Response did not contain {needle!r}"
        return result

    result["ok"] = True
    return result
