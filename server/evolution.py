"""Adaptador Evolution API v2 / Baileys; segredos permanecem no servidor."""
import base64
import binascii
import ipaddress
import json
import re
import socket
from urllib.error import HTTPError
from urllib.parse import quote, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

REQUIRED = ('SAHARA_EVOLUTION_URL', 'SAHARA_EVOLUTION_API_KEY',
            'SAHARA_EVOLUTION_INSTANCE', 'SAHARA_EVOLUTION_WEBHOOK_SECRET')
EVENTS = ['MESSAGES_UPSERT', 'MESSAGES_UPDATE', 'CONNECTION_UPDATE']
DEFAULT_NUMBER = '5544991748318'


def canonical_phone(number):
    """Brazilian mobile JIDs may omit the ninth digit; landlines stay distinct."""
    if not isinstance(number, str) or not re.fullmatch(r'[0-9]{10,15}', number):
        return ''
    if re.fullmatch(r'55[1-9][0-9][6-9][0-9]{7}', number):
        return number[:4] + '9' + number[4:]
    return number


def phone_aliases(number):
    canonical = canonical_phone(number)
    if not canonical:
        return ()
    if re.fullmatch(r'55[1-9][0-9]9[6-9][0-9]{7}', canonical):
        return (canonical, canonical[:4] + canonical[5:])
    return (canonical,)


def _public_host(hostname):
    """Reject local names and literal non-public IPs before any request."""
    if not isinstance(hostname, str) or not hostname or hostname.endswith('.'):
        return False
    try:
        return ipaddress.ip_address(hostname).is_global
    except ValueError:
        if re.fullmatch(r'[0-9.]+', hostname):
            return False
        labels = hostname.lower().split('.')
        return bool(len(labels) > 1 and len(hostname) <= 253
                    and labels[-1] not in ('localhost', 'localdomain', 'local', 'internal', 'lan', 'home', 'invalid')
                    and all(re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', label) for label in labels))


def valid_url(value):
    try:
        url = urlsplit(value)
        return bool(url.scheme == 'https' and _public_host(url.hostname)
                    and url.username is None and url.password is None
                    and not url.query and not url.fragment
                    and (url.port is None or 1 <= url.port <= 65535)
                    and not any(c.isspace() or ord(c) < 32 for c in value))
    except (ValueError, TypeError, AttributeError):
        return False


def valid_config(cfg):
    try:
        return bool(valid_url(cfg['SAHARA_EVOLUTION_URL'])
                    and re.fullmatch(r'[A-Za-z0-9_-]{1,80}', cfg['SAHARA_EVOLUTION_INSTANCE'])
                    and re.fullmatch(r'[0-9]{10,15}', cfg.get('SAHARA_EVOLUTION_EXPECTED_NUMBER', DEFAULT_NUMBER)))
    except (ValueError, TypeError, KeyError):
        return False


class NoRedirect(HTTPRedirectHandler):
    """Never forward apikey credentials to a redirect destination."""
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        return None


def open_url(request, timeout=15):
    # Preserve the environment proxy and standard TLS/CA verification.
    return build_opener(NoRedirect()).open(request, timeout=timeout)


def _verify_public_destination(url):
    host = urlsplit(url).hostname
    addresses = socket.getaddrinfo(host, urlsplit(url).port or 443, type=socket.SOCK_STREAM)
    if not addresses or any(not ipaddress.ip_address(item[4][0]).is_global for item in addresses):
        raise ValueError('O servidor precisa ter um endereço público HTTPS.')


def api(cfg, path, data=None):
    if (not valid_config(cfg) or not valid_url(cfg.get('menu', '')) or not isinstance(path, str)
            or not path.startswith('/') or path.startswith('//')):
        raise ValueError('Configuração inválida.')
    url = cfg['SAHARA_EVOLUTION_URL'].rstrip('/') + path
    _verify_public_destination(url)
    public = urlsplit(cfg['menu'])
    request = Request(url,
                      data=json.dumps(data).encode() if data is not None else None,
                      headers={'apikey': cfg['SAHARA_EVOLUTION_API_KEY'], 'Content-Type': 'application/json',
                               'Origin': public.scheme + '://' + public.netloc},
                      method='POST' if data is not None else 'GET')
    with open_url(request, timeout=15) as response:
        raw = response.read(262145)
    if len(raw) > 262144:
        raise ValueError('Resposta muito grande.')
    result = json.loads(raw)
    if not isinstance(result, (dict, list)):
        raise ValueError('Resposta inválida.')
    return result


def instance_path(cfg):
    return quote(cfg['SAHARA_EVOLUTION_INSTANCE'], safe='')


def connection(cfg):
    result = api(cfg, '/instance/connectionState/' + instance_path(cfg))
    if not isinstance(result, dict) or not isinstance(result.get('instance'), dict):
        raise ValueError('Resposta de conexão inválida.')
    state = result['instance'].get('state')
    if state not in ('open', 'close', 'connecting'):
        raise ValueError('Estado de conexão inválido.')
    if state == 'open':
        # A QR scan can attach another number. Never dispatch from that account.
        instances = api(cfg, '/instance/fetchInstances?instanceName=' + instance_path(cfg))
        if not isinstance(instances, list):
            raise ValueError('Identidade da conexão indisponível.')
        instance = next((item for item in instances if isinstance(item, dict)
                         and item.get('name') == cfg['SAHARA_EVOLUTION_INSTANCE']), None)
        owner = instance.get('ownerJid') if instance else None
        expected = cfg.get('SAHARA_EVOLUTION_EXPECTED_NUMBER', DEFAULT_NUMBER)
        if not isinstance(owner, str) or owner not in tuple(number + '@s.whatsapp.net' for number in phone_aliases(expected)):
            return 'wrong_number'
    return state


def connect(cfg):
    try:
        state = connection(cfg)
    except HTTPError as error:
        if error.code != 404:
            raise
        state = 'not_created'
    if state == 'not_created':
        api(cfg, '/instance/create', {'instanceName': cfg['SAHARA_EVOLUTION_INSTANCE'],
                                     'integration': 'WHATSAPP-BAILEYS', 'qrcode': False})
    if state == 'wrong_number':
        return {'state': state, 'connected': False, 'qrcode': None}
    # Reaplica mesmo em instâncias existentes e depois de reinícios.
    api(cfg, '/webhook/set/' + instance_path(cfg), {'webhook': {
        'enabled': True, 'url': cfg['menu'].removesuffix('/index.html') + '/api/whatsapp/evolution/webhook',
        'headers': {'X-Sahara-Webhook-Secret': cfg['SAHARA_EVOLUTION_WEBHOOK_SECRET']},
        'byEvents': False, 'base64': False, 'events': EVENTS}})
    if state == 'open':
        return {'state': state, 'connected': True, 'qrcode': None}
    result = api(cfg, '/instance/connect/' + instance_path(cfg))
    if not isinstance(result, dict):
        raise ValueError('Resposta de conexão inválida.')
    instance = result.get('instance') or {}
    qrcode = result.get('qrcode') or {}
    if not isinstance(instance, dict) or not isinstance(qrcode, dict):
        raise ValueError('Resposta de conexão inválida.')
    state = instance.get('state', 'connecting')
    image = result.get('base64') or qrcode.get('base64')
    if image and (not isinstance(image, str) or len(image) > 100000 or
                  not re.fullmatch(r'data:image/png;base64,[A-Za-z0-9+/=\r\n]+', image)):
        raise ValueError('QR Code inválido.')
    if image:
        try:
            decoded = base64.b64decode(image.split(',', 1)[1], validate=True)
        except (ValueError, binascii.Error):
            raise ValueError('QR Code inválido.') from None
        if not decoded.startswith(b'\x89PNG\r\n\x1a\n'):
            raise ValueError('QR Code inválido.')
    if state == 'open':
        state = connection(cfg)
    return {'state': state if state in ('open', 'close', 'connecting', 'wrong_number') else 'connecting',
            'connected': state == 'open', 'qrcode': image or None}


def send(cfg, data):
    operation = 'sendMedia' if data.get('mediatype') == 'image' else 'sendText'
    result = api(cfg, '/message/' + operation + '/' + instance_path(cfg), data)
    key = result.get('key') if isinstance(result, dict) else None
    identifier = key.get('id') if isinstance(key, dict) else None
    if not isinstance(identifier, str) or not identifier or len(identifier) > 512:
        raise ValueError('Envio sem identificador.')
    return 'evolution:' + identifier


def jid_phone(key):
    # LIDs não são números. Usa apenas o endereço telefônico fornecido pela sessão.
    if not isinstance(key, dict):
        return ''
    jid = key.get('remoteJid', '')
    if not isinstance(jid, str) or jid.endswith(('@g.us', '@broadcast', '@newsletter')):
        return ''
    if jid.endswith('@lid'):
        jid = key.get('remoteJidAlt', '')
    if not isinstance(jid, str):
        return ''
    match = re.fullmatch(r'([0-9]{10,15})@s\.whatsapp\.net', jid)
    return canonical_phone(match.group(1)) if match else ''


def text_message(message):
    if not isinstance(message, dict):
        raise ValueError('Mensagem inválida.')
    # Não executa ações a partir de edições, reações, mensagens apagadas ou contexto citado.
    if 'conversation' in message:
        return message['conversation']
    if 'extendedTextMessage' in message:
        content = message['extendedTextMessage']
        if not isinstance(content, dict):
            raise ValueError('Mensagem inválida.')
        return content.get('text', '')
    for kind in ('ephemeralMessage', 'viewOnceMessage', 'viewOnceMessageV2'):
        if kind in message:
            content = message[kind]
            if not isinstance(content, dict):
                raise ValueError('Mensagem inválida.')
            return text_message(content.get('message', {}))
    return ''
