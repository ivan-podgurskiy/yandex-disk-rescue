import io
import json
import time
import unittest
import urllib.error
import urllib.request
from email.utils import formatdate

try:
    from rescue.api import YandexClient, ApiError, SafeRedirect
except ImportError:
    YandexClient = None


class Transport:
    def __init__(self, body=None, error=None):
        self.body, self.error, self.requests = body, error, []

    def open(self, req, timeout=None):
        self.requests.append(req)
        if self.error:
            raise self.error
        return io.BytesIO(json.dumps(self.body).encode())


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(YandexClient, 'safe client must exist')

    def client(self, body=None, error=None):
        transport = Transport(body, error)
        client = YandexClient('TOPSECRET', opener=transport)
        return client, transport

    def test_reject_http_and_foreign_redirect(self):
        handler = SafeRedirect()
        req = urllib.request.Request('https://cloud-api.yandex.net/a')
        for url in ['http://downloader.disk.yandex.ru/a','https://yandex.ru.evil.test/a','https://evil.test/a']:
            with self.assertRaises(ApiError):
                handler.redirect_request(req, None, 302, '', {}, url)

    def test_auth_only_on_allowed_https_hosts(self):
        client, transport = self.client({})
        for url in ['http://cloud-api.yandex.net/a','https://evil.test/a']:
            with self.assertRaises(ApiError):
                client.open_stream(url, True)
        self.assertEqual(len(transport.requests), 0)
        client.open_stream('https://cloud-api.yandex.net/a', True).close()
        self.assertEqual(transport.requests[0].get_header('Authorization'), 'OAuth TOPSECRET')

    def test_signed_download_has_no_auth(self):
        client, transport = self.client({})
        client.open_stream('https://downloader.disk.yandex.ru/a?secret=x', False).close()
        self.assertIsNone(transport.requests[0].get_header('Authorization'))

    def test_401_pauses_auth(self):
        client, _ = self.client(error=urllib.error.HTTPError('secret',401,'secret',{},None))
        with self.assertRaises(ApiError) as caught:
            client.check_access()
        self.assertTrue(caught.exception.requires_auth)

    def test_429_parses_retry_after(self):
        for value in ['30', formatdate(time.time()+40, usegmt=True)]:
            client, _ = self.client(error=urllib.error.HTTPError('secret',429,'secret',{'Retry-After':value},None))
            with self.assertRaises(ApiError) as caught:
                client.check_access()
            self.assertEqual(caught.exception.code, 'throttled')
            self.assertGreaterEqual(caught.exception.retry_after, 29)

    def test_invalid_page_does_not_mean_end(self):
        for body in [{}, {'items':None}, {'items':[{'path':'disk:/a','size':-1}]}, {'items':[{'path':'disk:/a','size':3,'md5':'bad'}]}]:
            client, _ = self.client(body)
            with self.assertRaises(ApiError):
                client.list_page(0)
        client, _ = self.client({'items':[]})
        self.assertEqual(client.list_page(0), [])

    def test_error_contains_no_token_or_signed_url(self):
        client, _ = self.client(error=urllib.error.URLError('TOPSECRET https://signed-secret'))
        with self.assertRaises(ApiError) as caught:
            client.check_access()
        self.assertEqual(str(caught.exception), 'unavailable')

    def test_page_keeps_resource_identity(self):
        client, _ = self.client({'items':[{'path':'disk:/a','size':0,'md5':'','resource_id':'123','media_type':'image'}]})
        self.assertEqual(client.list_page(0)[0].path, '/a')
        self.assertEqual(client.list_page(0)[0].resource_id, '123')
