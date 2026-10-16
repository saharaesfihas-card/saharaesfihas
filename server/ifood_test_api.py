"""Transporte dos cenários privados de teste; nenhum destino vem do navegador."""
import json
import hashlib
import re
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlsplit
from urllib.request import Request as URLRequest

from . import ifood, ifood_merchant, ifood_homologation_profile as test_profile

MAX_RESPONSE = 2 * 1024 * 1024
configuration = test_profile.selected_config
current_config = test_profile.current_config
metadata = test_profile.metadata


def failure(state, status=None, uncertain=False):
    error = ifood.ProviderError(state, 'test_api', status)
    error.uncertain = uncertain
    return error


def scope(cfg):
    """Remote resource ownership survives secret renewal for the same app/shop."""
    return hashlib.sha256(json.dumps(['test', cfg[ifood.REQUIRED[0]].lower(),
                                    cfg[ifood.REQUIRED[2]].lower()]).encode()).hexdigest()


class Session:
    """Configuração capturada, verificação atual e token efêmero por operação."""

    def __init__(self, request, order_ids=(), legacy=False):
        self.request = request
        self.cfg = configuration(legacy=legacy)
        self.digest = ifood.fingerprint(self.cfg)
        state = ifood_merchant.readiness(request, self.cfg)
        if state != 'available':
            raise failure(state)
        self.merchant_id = self.cfg[ifood.REQUIRED[2]].lower()
        self.client_id = self.cfg[ifood.REQUIRED[0]]
        self.scope = scope(self.cfg)
        self.profile_mode = self.cfg['profile_mode']
        if not isinstance(order_ids, (tuple, list)) or any(not ifood.identifier(x) for x in order_ids):
            raise failure('request')
        # Callers bind IDs from the private imported-order registry, never a free target.
        self.order_ids = {x.lower() for x in order_ids}
        self._token = None
        self.unchanged()

    def unchanged(self):
        try:
            ifood_merchant.unchanged(self.request, self.cfg, self.digest)
        except ifood.ProviderError:
            raise failure('configuration_changed') from None

    def sanitized(self, value):
        return ifood_merchant.sanitize(value, (self.cfg[ifood.REQUIRED[1]], self._token),
                                      max_response=MAX_RESPONSE, max_nodes=50000)

    def _allowed(self, method, path):
        if method not in ('GET', 'POST', 'PUT', 'PATCH', 'DELETE') or not isinstance(path, str):
            return False
        if (len(path) > 2048 or not path.startswith('/') or path.startswith('//')
                or not re.fullmatch(r'[A-Za-z0-9_./?=&%:~+,\-]+', path)
                or '%' in path or '..' in path or '//' in path):
            return False
        parsed = urlsplit(path)
        if parsed.scheme or parsed.netloc or parsed.fragment:
            return False
        merchant = '/merchant/v1.0/merchants/' + self.merchant_id
        catalog = '/catalog/v2.0/merchants/' + self.merchant_id
        # Resource IDs are supplied by the server registry; paths remain segment-bound.
        identifier = r'[A-Za-z0-9_-]{1,80}'
        if parsed.path == '/merchant/v1.0/merchants' and method == 'GET':
            if not parsed.query:
                return True
            pairs = parse_qsl(parsed.query, keep_blank_values=True)
            fields = dict(pairs)
            return (len(fields) == len(pairs) and set(fields) <= {'page', 'size'}
                    and all(re.fullmatch(r'[1-9][0-9]{0,3}', value) for value in fields.values())
                    and int(fields.get('size', '100')) <= 100)
        if parsed.query:
            return bool(method == 'GET' and parsed.query == 'includeItems=true'
                        and re.fullmatch(re.escape(catalog) + r'/catalogs/' + identifier
                            + r'/categories(?:/' + identifier + r')?', parsed.path))
        fixed = {
            merchant: {'GET'}, merchant + '/status': {'GET'},
            merchant + '/interruptions': {'GET', 'POST'},
            merchant + '/opening-hours': {'GET'},
            catalog + '/catalogs': {'GET'}, catalog + '/items': {'PUT'},
            **{catalog + '/' + entity + '/' + field: {'PATCH'}
               for entity in ('items', 'options') for field in ('price', 'status')},
        }
        if method in fixed.get(parsed.path, set()):
            return True
        if method == 'DELETE' and re.fullmatch(re.escape(merchant) + r'/interruptions/[A-Za-z0-9_-]{1,128}', parsed.path):
            return True
        if method in ('GET', 'POST') and re.fullmatch(re.escape(catalog) + '/catalogs/' + identifier + '/categories', parsed.path):
            return True
        if method == 'GET' and (re.fullmatch(re.escape(catalog) + '/catalogs/' + identifier + '/categories/' + identifier, parsed.path)
                               or re.fullmatch(re.escape(catalog) + '/items/' + identifier + '/flat', parsed.path)):
            return True
        for order_id in self.order_ids:
            order = '/order/v1.0/orders/' + order_id
            if method == 'GET' and parsed.path in (order, order + '/cancellationReasons'):
                return True
            if method == 'POST' and parsed.path in tuple(order + '/' + action for action in
                    ('confirm', 'startPreparation', 'readyToPickup', 'dispatch', 'requestCancellation')):
                return True
        return False

    def call(self, method, path, payload=None, statuses=(200,), expected=dict):
        method = method.upper() if isinstance(method, str) else method
        if not self._allowed(method, path):
            raise failure('request')
        if (not isinstance(statuses, (tuple, list)) or not statuses
                or any(not isinstance(x, int) or not 200 <= x < 300 for x in statuses)):
            raise failure('request')
        writing = method != 'GET'
        if not writing and payload is not None:
            raise failure('request')
        try:
            body = None if payload is None else json.dumps(payload, ensure_ascii=False, allow_nan=False).encode()
            if body is not None and len(body) > MAX_RESPONSE:
                raise ValueError()
        except (ValueError, TypeError, RecursionError, OverflowError):
            raise failure('request') from None
        self.unchanged()
        if self._token is None:
            try:
                self._token = ifood.access_token(self.cfg)
            except ifood.ProviderError as error:
                # OAuth failure precedes any business request.
                error.uncertain = False
                raise
        self.unchanged()
        headers = {'Authorization': 'Bearer ' + self._token, 'Accept': 'application/json'}
        if body is not None:
            headers['Content-Type'] = 'application/json'
        status = None
        try:
            with ifood.evolution.open_url(URLRequest(ifood.HOST + path, data=body,
                    headers=headers, method=method), timeout=8) as response:
                status = getattr(response, 'status', 200)
                raw = response.read(MAX_RESPONSE + 1)
            if status not in statuses or len(raw) > MAX_RESPONSE:
                raise ValueError()
            if expected is None:
                # Status-only commands (202/204) do not consume a response payload.
                data = None
            elif not raw:
                if isinstance(expected, tuple) and type(None) in expected:
                    data = None
                elif status == 204 and (expected is list or
                        isinstance(expected, tuple) and list in expected):
                    data = []
                else:
                    raise ValueError()
            else:
                data = json.loads(raw)
                if not isinstance(data, expected):
                    raise ValueError()
            data = self.sanitized(data)
            self.unchanged()
            return status, data
        except HTTPError as error:
            code = error.code
            state = {400: 'request', 401: 'authentication', 403: 'permissions',
                     404: 'provider', 409: 'request', 422: 'request', 429: 'quota'}.get(code, 'provider')
            error.close()
            raise failure(state, code, writing and (code >= 500 or code in (301, 302, 303, 307, 308, 408))) from None
        except (URLError, TimeoutError, OSError):
            raise failure('network', status, writing) from None
        except (ValueError, TypeError, UnicodeError, RecursionError, OverflowError):
            raise failure('response', status, writing) from None
        except ifood.ProviderError as error:
            # A changed configuration after a write leaves its remote result uncertain.
            error.uncertain = writing
            raise
