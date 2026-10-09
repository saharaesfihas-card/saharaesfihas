"""Adaptador Evolution API v2 / Baileys; segredos permanecem no servidor."""
import json
import re
from urllib.error import HTTPError
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen

REQUIRED = ('SAHARA_EVOLUTION_URL', 'SAHARA_EVOLUTION_API_KEY',
            'SAHARA_EVOLUTION_INSTANCE', 'SAHARA_EVOLUTION_WEBHOOK_SECRET')
EVENTS = ['MESSAGES_UPSERT', 'MESSAGES_UPDATE', 'CONNECTION_UPDATE']


def valid_config(cfg):
    try:
        url = urlsplit(cfg['SAHARA_EVOLUTION_URL'])
        return bool(url.scheme == 'https' and url.hostname and not url.username and not url.password
                    and not url.query and not url.fragment
                    and re.fullmatch(r'[A-Za-z0-9_-]{1,80}', cfg['SAHARA_EVOLUTION_INSTANCE']))
    except ValueError:
        return False


def api(cfg, path, data=None):
    request = Request(cfg['SAHARA_EVOLUTION_URL'].rstrip('/') + path,
                      data=json.dumps(data).encode() if data is not None else None,
                      headers={'apikey': cfg['SAHARA_EVOLUTION_API_KEY'], 'Content-Type': 'application/json'},
                      method='POST' if data is not None else 'GET')
    with urlopen(request, timeout=15) as response:
        raw = response.read(262145)
    if len(raw) > 262144:
        raise ValueError('Resposta muito grande.')
    return json.loads(raw)


def instance_path(cfg):
    return quote(cfg['SAHARA_EVOLUTION_INSTANCE'], safe='')


def connection(cfg):
    result = api(cfg, '/instance/connectionState/' + instance_path(cfg))
    state = result.get('instance', {}).get('state')
    return state if state in ('open', 'close', 'connecting') else 'not_created'


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
    # Reaplica mesmo em instâncias existentes e depois de reinícios.
    api(cfg, '/webhook/set/' + instance_path(cfg), {'webhook': {
        'enabled': True, 'url': cfg['menu'].removesuffix('/index.html') + '/api/whatsapp/evolution/webhook',
        'headers': {'X-Sahara-Webhook-Secret': cfg['SAHARA_EVOLUTION_WEBHOOK_SECRET']},
        'byEvents': False, 'base64': False, 'events': EVENTS}})
    if state == 'open':
        return {'state': state, 'connected': True, 'qrcode': None}
    result = api(cfg, '/instance/connect/' + instance_path(cfg))
    state = result.get('instance', {}).get('state', 'connecting')
    image = result.get('base64') or result.get('qrcode', {}).get('base64')
    if image and (not isinstance(image, str) or len(image) > 100000 or
                  not re.fullmatch(r'data:image/png;base64,[A-Za-z0-9+/=\r\n]+', image)):
        raise ValueError('QR Code inválido.')
    return {'state': state if state in ('open', 'close', 'connecting') else 'connecting',
            'connected': state == 'open', 'qrcode': image or None}


def send(cfg, data):
    result = api(cfg, '/message/sendText/' + instance_path(cfg), data)
    identifier = result.get('key', {}).get('id')
    if not isinstance(identifier, str) or not identifier:
        raise ValueError('Envio sem identificador.')
    return 'evolution:' + identifier


def jid_phone(key):
    # LIDs não são números. Usa apenas o endereço telefônico fornecido pela sessão.
    jid = key.get('remoteJid', '')
    if not isinstance(jid, str) or jid.endswith(('@g.us', '@broadcast', '@newsletter')):
        return ''
    if jid.endswith('@lid'):
        jid = key.get('remoteJidAlt', '')
    if not isinstance(jid, str):
        return ''
    match = re.fullmatch(r'([0-9]{10,15})@s\.whatsapp\.net', jid)
    return match.group(1) if match else ''


def text_message(message):
    if not isinstance(message, dict):
        raise ValueError('Mensagem inválida.')
    # Não executa ações a partir de edições, reações, mensagens apagadas ou contexto citado.
    if 'conversation' in message:
        return message['conversation']
    if 'extendedTextMessage' in message:
        return message['extendedTextMessage'].get('text', '')
    for kind in ('ephemeralMessage', 'viewOnceMessage', 'viewOnceMessageV2'):
        if kind in message:
            return text_message(message[kind].get('message', {}))
    return ''
