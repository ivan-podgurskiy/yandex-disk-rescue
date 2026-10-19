"""Read-only HTTPS transport. Exceptions contain categories, never remote bodies."""
import datetime
import email.utils
import json
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request

from .models import CloudFile

API = 'https://cloud-api.yandex.net/v1/disk'
SUFFIXES = ('.yandex.ru', '.yandex.net', '.yandex.com', '.yandex.by', '.yandex.kz')


class ApiError(Exception):
    def __init__(self, code, retry_after=None):
        super().__init__(code)
        self.code = code
        self.retry_after = retry_after
        self.requires_auth = code == 'auth'


def allowed(url):
    try:
        u = urllib.parse.urlsplit(url)
        return (u.scheme == 'https' and not u.username and not u.password
                and u.port in (None, 443) and (u.hostname or '').lower().endswith(SUFFIXES))
    except ValueError:
        return False


class SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not allowed(newurl):
            raise ApiError('unsafe_url')
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def retry_after(value):
    try:
        return max(0, float(value))
    except (TypeError, ValueError):
        try:
            return max(0, email.utils.parsedate_to_datetime(value).timestamp() - time.time())
        except (ValueError, TypeError, OverflowError):
            return None


class YandexClient:
    def __init__(self, token, timeout=12, opener=None):
        if not token or any(c in token for c in '\r\n'):
            raise ApiError('auth')
        self._token = token.strip()
        self.preview_size = 'XXXL'
        self.timeout = timeout
        self._opener = opener or urllib.request.build_opener(SafeRedirect())

    def open_stream(self, url, authenticated=False):
        if not allowed(url):
            raise ApiError('unsafe_url')
        headers = {'Authorization': 'OAuth ' + self._token} if authenticated else {}
        try:
            return self._opener.open(urllib.request.Request(url, headers=headers), timeout=self.timeout)
        except urllib.error.HTTPError as e:
            delay = retry_after(e.headers.get('Retry-After'))
            code = 'auth' if e.code in (401,403) and authenticated else 'throttled' if e.code == 429 else 'unavailable'
            e.close()
            raise ApiError(code, delay) from None
        except (TimeoutError, socket.timeout):
            raise ApiError('timeout') from None
        except (urllib.error.URLError, OSError, ValueError):
            raise ApiError('unavailable') from None

    def _json(self, path, params=None):
        url = API + path
        if params:
            url += '?' + urllib.parse.urlencode(params)
        try:
            with self.open_stream(url, True) as response:
                # Avoid unbounded error/metadata responses.
                raw = response.read(16 * 1024 * 1024 + 1)
                if len(raw) > 16 * 1024 * 1024:
                    raise ApiError('invalid_response')
                result = json.loads(raw)
                if not isinstance(result, dict):
                    raise ApiError('invalid_response')
                return result
        except (ValueError, UnicodeError, TypeError):
            raise ApiError('invalid_response') from None
        except OSError:
            raise ApiError('unavailable') from None

    def check_access(self):
        info = self._json('/', {'fields':'user.uid'})
        # Binding an account prevents a new token from mixing queues with another disk.
        return str(info.get('user', {}).get('uid', ''))

    def list_page(self, offset, limit=1000):
        data = self._json('/resources/files', {'offset':offset,'limit':limit,
                          'fields':'items.path,items.size,items.md5,items.media_type,items.resource_id', 'sort':'path'})
        items = data.get('items')
        if not isinstance(items, list):
            raise ApiError('invalid_response')
        files = []
        for item in items:
            if not isinstance(item, dict):
                raise ApiError('invalid_response')
            path, size, md5 = item.get('path'), item.get('size'), item.get('md5') or ''
            if (not isinstance(path,str) or not path.startswith('disk:/') or not isinstance(size,int)
                    or isinstance(size,bool) or size < 0 or size > 2**63-1
                    or not isinstance(md5,str) or (md5 and not re.fullmatch('[0-9a-fA-F]{32}', md5))):
                raise ApiError('invalid_response')
            files.append(CloudFile(path[5:], size, md5.lower(), str(item.get('media_type') or ''), str(item.get('resource_id') or '')))
        return files

    def original_url(self, file):
        value = self._json('/resources/download', {'path':'disk:' + file.path}).get('href')
        if not isinstance(value,str) or not allowed(value):
            raise ApiError('unsafe_url')
        return value

    def preview_url(self, file):
        value = self._json('/resources', {'path':'disk:' + file.path,'preview_size':self.preview_size,'preview_crop':'false','fields':'preview'}).get('preview')
        if value is not None and (not isinstance(value,str) or not allowed(value)):
            raise ApiError('unsafe_url')
        return value
