"""Gera um hash de senha sem gravar nem exibir a senha original."""
import getpass
from server.core import hash_password

if __name__ == '__main__':
    password = getpass.getpass('Senha de administrador: ')
    repeated = getpass.getpass('Repita a senha: ')
    if password != repeated:
        raise SystemExit('As senhas são diferentes.')
    print(hash_password(password))
