"""Separa somente dados escritos pelo cliente, sem IA, geocodificação ou suposições."""
import re
import unicodedata

NUMBER = r'(?:[0-9]{1,8}(?:\s*[-/]?\s*[A-Za-z])?|s\s*/\s*n|sem\s+n[uú]mero)'
LABEL = re.compile(
    r'(?<!\w)(?:(?P<street>endere[cç]o|logradouro|rua|avenida|travessa|av\.?)\s*:\s*'
    r'|(?P<number>n[uú]mero(?:\s+da\s+casa)?|nro\.?|num\.?|n[º°.o]?)\s*:?\s*(?=' + NUMBER + r'(?:\s|[,;]|$))'
    r'|(?P<neighborhood>bairro)(?:\s*:\s*|\s+)'
    r'|(?P<complement>complemento|refer[eê]ncia)\s*:\s*)', re.IGNORECASE)
SEPARATORS = r'\s*(?:[,;\n|]|\s+[-–—]\s+)\s*'
LIMITS = {'street': 160, 'number': 20, 'neighborhood': 100, 'complement': 160}


def normalized(value):
    return ''.join(c for c in unicodedata.normalize('NFD', value.lower()) if not unicodedata.combining(c))


def house(value):
    value = re.sub(r'^(?:n[uú]mero|nro\.?|num\.?|n[º°.o]?)\s*:?\s*', '', value.strip(), flags=re.IGNORECASE)
    if not re.fullmatch(NUMBER, value, re.IGNORECASE):
        return None
    return 'S/N' if normalized(value).startswith(('s', 'sem')) else re.sub(r'\s+', '', value)


def valid(fields):
    for key, value in fields.items():
        if not value or len(value) > LIMITS[key] or any(ord(c) < 32 for c in value):
            return False
        if key == 'number' and house(value) is None:
            return False
        if key == 'street' and normalized(value).strip(' .:') in ('rua', 'avenida', 'av', 'travessa', 'logradouro', 'endereco'):
            return False
        if key == 'neighborhood' and (not any(c.isalpha() for c in value) or normalized(value) == 'bairro'):
            return False
    return bool(fields)


def street_number(value, allow_bare=False):
    if not allow_bare and not re.match(r'^(rua|avenida|av\.?|travessa|alameda|estrada|rodovia|pra[cç]a)\b', value, re.IGNORECASE):
        return None
    match = re.fullmatch(r'(.+?)\s+(' + NUMBER + r')', value, re.IGNORECASE)
    if match:
        fields = {'street': match[1].strip(), 'number': house(match[2])}
        if valid(fields):
            return fields
    return None


def positional(text):
    parts = [p.strip(' .') for p in re.split(SEPARATORS, text.strip()) if p.strip()]
    # A cidade não é usada como bairro. Aceita o sufixo local explicitamente escrito.
    if len(parts) == 4 and normalized(parts[-1]).replace('/', ' ').replace('-', ' ').strip() in ('maringa', 'maringa pr'):
        parts.pop()
    if len(parts) == 3 and house(parts[1]):
        return {'street': parts[0], 'number': house(parts[1]), 'neighborhood': parts[2]}
    if len(parts) == 2:
        if house(parts[1]):
            return {'street': parts[0], 'number': house(parts[1])}
        first = street_number(parts[0], allow_bare=True)
        if first:
            return first | {'neighborhood': parts[1]}
        match = re.fullmatch(r'(' + NUMBER + r')\s+(.+)', parts[1], re.IGNORECASE)
        if match:
            return {'street': parts[0], 'number': house(match[1]), 'neighborhood': match[2]}
        if not re.search(r'\d', parts[1]):
            return {'street': parts[0], 'neighborhood': parts[1]}
        return None
    if len(parts) != 1:
        return None
    text = parts[0]
    candidates = []
    for match in re.finditer(r'(?<!\S)(' + NUMBER + r')(?=\s|$)', text, re.IGNORECASE):
        fields = {'street': text[:match.start()].strip(), 'number': house(match[1]),
                  'neighborhood': text[match.end():].strip()}
        if valid(fields):
            candidates.append(fields)
    if len(candidates) == 1:
        return candidates[0]
    return street_number(text)


def parse(text):
    """Retorna campos reconhecidos ou None; nunca inventa um campo ausente."""
    if not isinstance(text, str) or not text.strip() or len(text) > 600:
        return None
    text = text.strip()
    if any(ord(c) < 32 and c not in '\n\r\t' for c in text):
        return None
    text = text.replace('\r\n', '\n').replace('\r', '\n').replace('\t', ' ')
    labels = []
    for match in LABEL.finditer(text):
        if match.lastgroup == 'neighborhood' and ':' not in match[0]:
            prefix = text[:match.start()].strip(' ,;\n|-–—')
            # "Bairro" pode fazer parte do nome da rua ou do próprio bairro.
            if labels and labels[-1].lastgroup == 'neighborhood':
                continue
            parsed_prefix = positional(prefix) if prefix else None
            separated = re.search(r'[,;\n|]\s*$', text[:match.start()])
            if prefix and not separated and not any(label.lastgroup == 'number' for label in labels) and not (parsed_prefix and parsed_prefix.get('number')):
                continue
        labels.append(match)
    fields = {}
    if labels:
        prefix = text[:labels[0].start()].strip(' ,;\n|-–—')
        if prefix:
            fields = positional(prefix) or {'street': prefix}
        for index, label in enumerate(labels):
            key = label.lastgroup
            end = labels[index + 1].start() if index + 1 < len(labels) else len(text)
            value = text[label.end():end].strip(' ,;\n|-–—')
            if key in fields or not value:
                return None
            if key == 'street':
                combined = positional(value)
                if combined:
                    if fields.keys() & combined.keys():
                        return None
                    fields.update(combined)
                    continue
                if normalized(label[key]).strip('.') in ('rua', 'avenida', 'travessa', 'av') and not re.match(r'^(rua|avenida|travessa|av\b)', value, re.IGNORECASE):
                    value = label[key] + ' ' + value
            fields[key] = house(value) if key == 'number' else value
    else:
        fields = positional(text)
    return fields if fields and valid(fields) else None


def combined(text):
    """Evita salvar como rua/número uma mensagem de endereço não separada com segurança."""
    return bool(LABEL.search(text) or
                (re.search(r'[,;\n|]|\s[-–—]\s', text) and re.search(r'\d|s\s*/\s*n', text, re.IGNORECASE)) or
                (re.match(r'^(rua|avenida|av\.?|travessa)\b', text, re.IGNORECASE) and
                 len(re.findall(r'\b\d+[A-Za-z]?\b', text)) > 1))
