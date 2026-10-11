"""The delivery origin is private; customer delivery addresses remain usable."""
import re

from . import delivery_estimates


def store_question(text):
    from .whatsapp import normalize
    words = re.sub(r'[^\w\s]', ' ', normalize(text))
    words = ' '.join(words.split())
    business = re.search(r'\b(loja|sahara|voce|voces|seu|sua|ai|restaurante|estabelecimento)\b', words)
    asking = re.search(r'\b(qual|onde|aonde|fica|ficam|manda|mande|envia|envie|passa|passe|mostra|mostre|informe)\b|como chegar', words)
    location = re.search(r'\b(endereco|localizacao|pino|mapa|rua|avenida|onde|aonde|fica|ficam)\b', words)
    generic = re.fullmatch(r'(?:qual(?: e)?(?: o| a)? (?:endereco|localizacao)|'
                           r'(?:onde|aonde)(?: voces)? (?:fica|ficam)|'
                           r'(?:me )?(?:manda|mande|envia|envie|passa|passe)(?: o| a)? (?:endereco|localizacao))', words)
    pickup = re.search(r'\b(?:posso|pode|tem|fazem|aceitam)\b.*\b(?:retirar|retirada|buscar na loja)\b', words)
    return bool(business and asking and location or generic or pickup)


def response():
    return ('Atendemos somente por delivery em Maringá. Peça pelo site ou por aqui, pelo WhatsApp. '
            'Para montar seu pedido aqui, envie PEDIR.')


def contains_origin(conn, text):
    from .whatsapp import normalize
    cfg = delivery_estimates.config(conn)
    address = cfg['address']
    if address != delivery_estimates.ADDRESS:
        street = re.split(r'[,—–•]', normalize(address))[0]
        street = re.sub(r'\b\d+\b.*', '', street).strip()
        name = re.sub(r'^(?:rua|avenida|av\.?|travessa|alameda|estrada|rodovia|praca)\s+', '', street)
        if name and re.search(r'(?<!\w)' + re.escape(name) + r'(?!\w)', normalize(text)):
            return True
    origin = cfg['origin']
    if origin:
        for number in re.findall(r'(?<![\w\d])-?\d{1,3}[.,]\d{2,}', text):
            value = float(number.replace(',', '.'))
            tolerance = 10 ** -len(re.split('[.,]', number)[1])
            if any(abs(value - origin[key]) <= tolerance for key in ('latitude', 'longitude')):
                return True
    return False


def safe_reply(conn, text):
    return response() if contains_origin(conn, text) else text
