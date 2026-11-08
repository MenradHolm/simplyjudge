"""Server-only downloads from administrator-controlled HTTPS origins."""
import ipaddress
import socket
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from django.conf import settings


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def validate_download_url(url, allowed_hosts):
    parsed = urlsplit(url)
    if (parsed.scheme != 'https' or not parsed.hostname
            or parsed.hostname not in allowed_hosts
            or parsed.username is not None or parsed.password is not None
            or parsed.port not in (None, 443) or parsed.fragment):
        raise ValueError('Use an HTTPS URL on an administrator-approved download host.')
    # Approved hosts must be controlled by the deployment administrator, never
    # user-controlled DNS. Also deny private/reserved destinations at validation.
    addresses = socket.getaddrinfo(parsed.hostname, 443, type=socket.SOCK_STREAM)
    if not addresses or any(not ipaddress.ip_address(row[4][0]).is_global for row in addresses):
        raise ValueError('Download destination is not public.')
    return parsed


def open_remote_file(url, allowed_hosts, timeout=60):
    validate_download_url(url, allowed_hosts)
    opener = build_opener(ProxyHandler({}), NoRedirects())
    return opener.open(Request(url, headers={'User-Agent': 'SimplyJudge File Importer'}), timeout=timeout)


def validate_zip_url(url):
    return validate_download_url(url, settings.ZIP_IMPORT_ALLOWED_HOSTS)
