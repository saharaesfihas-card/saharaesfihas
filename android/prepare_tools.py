"""Instala o toolchain Android verificado em diretório local, sem alterar o sistema."""
import hashlib
import json
import os
from pathlib import Path
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parents[1]
DESTINATION = Path(os.environ.get('SAHARA_ANDROID_TOOLS', '/workspace/.sahara-android-tools'))


def main():
    DESTINATION.mkdir(parents=True, exist_ok=True)
    artifacts = json.loads((ROOT / 'android' / 'toolchain.lock.json').read_text())
    for artifact in artifacts:
        target = DESTINATION / artifact['url'].rsplit('/', 1)[1]
        if not target.exists() or hashlib.sha256(target.read_bytes()).hexdigest() != artifact['sha256']:
            temporary = target.with_suffix(target.suffix + '.part')
            with urllib.request.urlopen(artifact['url'], timeout=30) as response, temporary.open('wb') as output:
                while chunk := response.read(1024 * 1024):
                    output.write(chunk)
            if hashlib.sha256(temporary.read_bytes()).hexdigest() != artifact['sha256']:
                temporary.unlink(missing_ok=True)
                raise SystemExit('Checksum não confere: ' + target.name)
            temporary.replace(target)
        if target.suffix == '.zip':
            with zipfile.ZipFile(target) as archive:
                for name in archive.namelist():
                    destination = (DESTINATION / name).resolve()
                    if DESTINATION.resolve() not in destination.parents:
                        raise SystemExit('Arquivo inválido dentro do pacote SDK.')
                archive.extractall(DESTINATION)
        print('Verificado:', target.name)
    for path in DESTINATION.rglob('*'):
        if path.name in ('aapt2', 'aapt', 'd8', 'zipalign', 'apksigner'):
            path.chmod(path.stat().st_mode | 0o111)


if __name__ == '__main__':
    main()
