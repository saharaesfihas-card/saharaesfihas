"""Compila APK Android com ferramentas oficiais; assinatura privada fora do Git."""
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import zipfile

ROOT = Path(__file__).resolve().parents[1]
TOOLS = Path(os.environ.get('SAHARA_ANDROID_TOOLS', '/workspace/.sahara-android-tools'))
SIGNING = Path(os.environ.get('SAHARA_ANDROID_SIGNING', '/workspace/.sahara-android-signing'))
VERSION = '2026.10.04'
VERSION_CODE = '20261004'


def run(*arguments, environment=None):
    subprocess.run([str(argument) for argument in arguments], check=True, env=environment)


def main():
    tools = TOOLS / 'android-15'
    platform = TOOLS / 'android-35' / 'android.jar'
    compiler = TOOLS / 'ecj-3.42.0.jar'
    if not all(path.is_file() for path in (platform, compiler, tools / 'aapt2', tools / 'apksigner')):
        raise SystemExit('Prepare as ferramentas Android verificadas; veja android/README.md.')
    signing_directory = SIGNING.resolve()
    if signing_directory == ROOT.resolve() or ROOT.resolve() in signing_directory.parents:
        raise SystemExit('Guarde a assinatura fora do repositório publicado.')
    SIGNING.mkdir(parents=True, exist_ok=True, mode=0o700)
    SIGNING.chmod(0o700)
    keystore, password_file = SIGNING / 'sahara-release.p12', SIGNING / 'store-password'
    if keystore.exists() != password_file.exists():
        raise SystemExit('Assinatura incompleta. Restaure o backup; não crie uma assinatura diferente.')
    if not keystore.exists():
        password_file.touch(mode=0o600)
        password_file.write_text(secrets.token_urlsafe(32))
    password_file.chmod(0o600)
    environment = os.environ.copy()
    environment['SAHARA_ANDROID_STORE_PASSWORD'] = password_file.read_text()
    environment['SAHARA_ANDROID_KEY_PASSWORD'] = environment['SAHARA_ANDROID_STORE_PASSWORD']
    if not keystore.exists():
        run('keytool', '-genkeypair', '-keystore', keystore, '-storetype', 'PKCS12',
            '-alias', 'sahara-release', '-keyalg', 'RSA', '-keysize', '2048', '-validity', '10000',
            '-dname', 'CN=Sahara Esfihas, OU=Aplicativo Android, O=Sahara Esfihas, L=Maringa, ST=Parana, C=BR',
            '-storepass:env', 'SAHARA_ANDROID_STORE_PASSWORD', '-keypass:env', 'SAHARA_ANDROID_KEY_PASSWORD',
            environment=environment)
    keystore.chmod(0o600)
    build = ROOT / 'android' / 'build'
    if build.exists():
        shutil.rmtree(build)
    build.mkdir()
    for directory in ('classes', 'dex', 'generated'):
        (build / directory).mkdir()
    run(tools / 'aapt2', 'compile', '--dir', ROOT / 'android' / 'res', '-o', build / 'compiled.zip')
    run(tools / 'aapt2', 'link', '-I', platform, '--manifest', ROOT / 'android' / 'AndroidManifest.xml',
        '--min-sdk-version', '23', '--target-sdk-version', '35', '--version-code', VERSION_CODE,
        '--version-name', VERSION, '--java', build / 'generated', '-o', build / 'resources.apk', build / 'compiled.zip')
    sources = sorted((ROOT / 'android' / 'src').rglob('*.java'))
    run('java', '-jar', compiler, '-source', '1.8', '-target', '1.8', '-encoding', 'UTF-8',
        '-nowarn', '-bootclasspath', platform, '-d', build / 'classes', *sources)
    classes = sorted((build / 'classes').rglob('*.class'))
    if not classes:
        raise SystemExit('Nenhuma classe foi compilada.')
    run(tools / 'd8', '--lib', platform, '--min-api', '23', '--output', build / 'dex', *classes)
    unsigned = build / 'unsigned.apk'
    shutil.copyfile(build / 'resources.apk', unsigned)
    with zipfile.ZipFile(unsigned, 'a', compression=zipfile.ZIP_DEFLATED) as package:
        for dex in sorted((build / 'dex').glob('*.dex')):
            package.write(dex, dex.name)
    run(tools / 'zipalign', '-f', '-P', '16', '4', unsigned, build / 'aligned.apk')
    output = ROOT / 'downloads' / f'sahara-esfihas-{VERSION}.apk'
    output.parent.mkdir(exist_ok=True)
    run(tools / 'apksigner', 'sign', '--ks', keystore, '--ks-key-alias', 'sahara-release',
        '--ks-pass', 'env:SAHARA_ANDROID_STORE_PASSWORD', '--key-pass', 'env:SAHARA_ANDROID_KEY_PASSWORD',
        '--v1-signing-enabled', 'true', '--v2-signing-enabled', 'true', '--v3-signing-enabled', 'true',
        '--v4-signing-enabled', 'false', '--out', output, build / 'aligned.apk', environment=environment)
    run(tools / 'apksigner', 'verify', '--verbose', '--print-certs', output)
    run(tools / 'zipalign', '-c', '-P', '16', '4', output)
    run(tools / 'aapt', 'dump', 'badging', output)
    with zipfile.ZipFile(output) as package:
        assert package.testzip() is None
        assert 'classes.dex' in package.namelist() and 'AndroidManifest.xml' in package.namelist()
    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    (output.parent / (output.name + '.sha256')).write_text(digest + '  ' + output.name + '\n')
    print('APK pronto:', output)
    print('SHA256:', digest)


if __name__ == '__main__':
    main()
