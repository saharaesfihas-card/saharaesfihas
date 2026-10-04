"""Smoke da imagem de produção com volume descartável; não hospeda o PDV público."""
import http.client
import json
import os
from pathlib import Path
import secrets
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
import uuid


def main():
    environment = os.environ.copy()
    for key in ('DOCKER_HOST', 'DOCKER_CONTEXT', 'DOCKER_TLS', 'DOCKER_TLS_VERIFY', 'DOCKER_CERT_PATH'):
        environment.pop(key, None)
    docker = ['docker', '--host=unix:///var/run/docker.sock']

    def command(*arguments, check=True):
        return subprocess.run(docker + list(arguments), env=environment, capture_output=True,
                              text=True, check=check)

    image = os.environ.get('SAHARA_TEST_IMAGE', 'sahara-pdv:host-test')
    suffix = uuid.uuid4().hex[:12]
    name, volume = f'sahara-deploy-test-{suffix}', f'sahara-data-test-{suffix}'
    password = secrets.token_urlsafe(24)
    csrf, cookie = '', ''
    base = ''
    proxy_headers = {'Host': 'pdv.sahara.test', 'Origin': 'https://pdv.sahara.test',
                     'X-Forwarded-Proto': 'https'}

    def request(path, payload=None, status=200, authenticated=False):
        headers = dict(proxy_headers)
        if authenticated:
            headers |= {'Cookie': cookie, 'X-Sahara-CSRF': csrf}
        if payload is not None:
            headers['Content-Type'] = 'application/json'
        body = json.dumps(payload).encode() if payload is not None else None
        query = urllib.request.Request(base + path, data=body, headers=headers)
        try:
            response = urllib.request.urlopen(query, timeout=5)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            assert response.status == status, f'{path}: HTTP {response.status}'
            data = response.read()
            return (json.loads(data) if 'application/json' in response.headers.get('Content-Type', '') else data), response.headers

    def ready():
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            try:
                health, _ = request('/api/health')
                assert health['ready'] and health['admin_configured']
                return
            except (OSError, http.client.HTTPException, AssertionError):
                running = command('inspect', '--format', '{{.State.Running}}', name, check=False)
                if running.stdout.strip() == 'false':
                    raise AssertionError('O processo do servidor encerrou durante a inicialização.')
                time.sleep(0.1)
        raise AssertionError('A imagem não ficou pronta com administração configurada.')

    try:
        blocked = command('run', '--rm', image, check=False)
        assert blocked.returncode != 0 and 'SAHARA_ADMIN_PASSWORD' in blocked.stderr
        print('PASS: produção recusa iniciar sem senha configurada', flush=True)
        command('volume', 'create', volume)
        with tempfile.TemporaryDirectory(prefix='sahara-deploy-') as directory:
            env_file = Path(directory) / 'test.env'
            env_file.touch(mode=0o600)
            env_file.write_text('SAHARA_ADMIN_PASSWORD=' + password + '\nFORWARDED_ALLOW_IPS=*\n')
            command('run', '-d', '--name', name, '--env-file', str(env_file),
                    '-p', '127.0.0.1::10000', '--mount', f'type=volume,src={volume},dst=/var/lib/sahara', image)
        mapping = command('port', name, '10000/tcp').stdout.strip()
        assert mapping.startswith('127.0.0.1:')
        base = 'http://' + mapping
        ready()
        products, _ = request('/api/catalog')
        assert len(products['products']) == 55
        for product in products['products']:
            if product['id'] == 'carne':
                assert product['price_cents'] == 400
        request('/')
        request('/admin.html')
        for path in ('/server/run.py', '/.git/config', '/var/lib/sahara/data/sahara.sqlite3'):
            request(path, status=404)
        print('PASS: imagem serve catálogo, painel e API; bloqueia arquivos privados', flush=True)

        login, headers = request('/api/admin/login', {'password': password})
        csrf = login['csrf_token']
        cookie = headers['Set-Cookie'].split(';')[0]
        assert 'Secure' in headers['Set-Cookie'] and 'HttpOnly' in headers['Set-Cookie']
        assert 'SameSite=strict' in headers['Set-Cookie']
        payload = {'items': [{'id': 'carne', 'quantity': 2}],
                   'customer': {'name': 'Cliente de teste', 'phone': '44999998888'},
                   'delivery': {'street': 'Rua de teste', 'number': '123', 'neighborhood': 'Centro'},
                   'payment_method': 'Pix', 'idempotency_key': str(uuid.uuid4())}
        registered, _ = request('/api/admin/orders', payload, status=201, authenticated=True)
        replayed, _ = request('/api/admin/orders', payload, status=201, authenticated=True)
        assert registered['id'] == replayed['id'] and replayed['replayed']
        assert registered['total_cents'] == 800
        print('PASS: login atrás de proxy HTTPS e PDV com preço real e repetição segura', flush=True)

        assertion = (
            "from pathlib import Path; import re,os; "
            "s=Path('/proc/1/status').read_text(); "
            "assert re.search(r'Uid:\\s+10001\\s+10001',s); "
            "p=Path('/var/lib/sahara/data'); assert p.stat().st_uid==10001; "
            "assert p.stat().st_mode&0o777==0o700; "
            "assert (p/'sahara.sqlite3').stat().st_mode&0o777==0o600"
        )
        command('exec', name, 'python', '-c', assertion)
        print('PASS: aplicação executa sem root e mantém banco privado', flush=True)
        command('restart', '-t', '3', name)
        # O Docker pode selecionar outra porta efêmera do host no reinício.
        mapping = command('port', name, '10000/tcp').stdout.strip()
        assert mapping.startswith('127.0.0.1:')
        base = 'http://' + mapping
        ready()
        orders, _ = request('/api/admin/orders', authenticated=True)
        assert len(orders['orders']) == 1 and orders['orders'][0]['id'] == registered['id']
        assert orders['orders'][0]['source'] == 'pdv'
        print('PASS: pedido e sessão permanecem após reiniciar com disco persistente', flush=True)
    except BaseException:
        logs = command('logs', '--tail', '30', name, check=False)
        print((logs.stdout + logs.stderr).replace(password, '[senha de teste omitida]'), flush=True)
        raise
    finally:
        command('rm', '-f', name, check=False)
        command('volume', 'rm', volume, check=False)


if __name__ == '__main__':
    main()
