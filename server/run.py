"""Inicialização de produção, sem senha padrão e sem registros privados em logs."""
import os
from pathlib import Path
import re


def main():
    encoded = os.environ.get('SAHARA_ADMIN_PASSWORD_HASH', '')
    password = os.environ.pop('SAHARA_ADMIN_PASSWORD', '')
    if not encoded and password:
        from .core import hash_password

        try:
            encoded = hash_password(password)
        except ValueError:
            raise SystemExit('A senha da gestão deve ter pelo menos oito caracteres.')
        os.environ['SAHARA_ADMIN_PASSWORD_HASH'] = encoded
    del password
    if not re.fullmatch(r'scrypt\$[0-9a-f]{32}\$[0-9a-f]{64}', encoded):
        raise SystemExit('Configure uma senha privada em SAHARA_ADMIN_PASSWORD ou use SAHARA_ADMIN_PASSWORD_HASH.')
    try:
        port = int(os.environ.get('PORT', '10000'))
        if not 1 <= port <= 65535:
            raise ValueError
    except ValueError:
        raise SystemExit('PORT deve ser um número entre 1 e 65535.')

    root = Path(__file__).resolve().parents[1]
    data = Path(os.environ.get('SAHARA_DATA_DIR', '/var/lib/sahara/data')).resolve()
    if data == root or root in data.parents:
        raise SystemExit('SAHARA_DATA_DIR deve ficar fora da pasta publicada.')
    data.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.environ['SAHARA_DATA_DIR'] = str(data)

    # Discos persistentes montados pelo provedor podem começar com proprietário root.
    # Apenas este diretório e os três arquivos conhecidos de SQLite recebem o ajuste.
    if os.getuid() == 0:
        os.chown(data, 10001, 10001)
        os.chmod(data, 0o700)
        for name in ('sahara.sqlite3', 'sahara.sqlite3-wal', 'sahara.sqlite3-shm'):
            target = data / name
            if target.is_symlink():
                raise SystemExit('O banco privado não pode ser um link simbólico.')
            if target.exists():
                os.chown(target, 10001, 10001)
                os.chmod(target, 0o600)
        os.setgroups([])
        os.setgid(10001)
        os.setuid(10001)

    import uvicorn

    uvicorn.run('server.main:app', host='0.0.0.0', port=port, workers=1,
                proxy_headers=True, forwarded_allow_ips=os.environ.get('FORWARDED_ALLOW_IPS', '127.0.0.1'),
                access_log=False)


if __name__ == '__main__':
    main()
