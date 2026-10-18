"""Initialisation et acces a Firestore."""

import socket

import firebase_admin
from firebase_admin import credentials, firestore

try:
    from .config import SERVICE_ACCOUNT_PATH
except ImportError:
    from config import SERVICE_ACCOUNT_PATH


def prefer_ipv4() -> None:
    """Essaie les adresses IPv4 avant les IPv6.

    Sur un réseau où IPv6 est annoncé mais ne passe pas, chaque connexion HTTP
    vers Google (jeton OAuth de Firestore, certificats des jetons Firebase)
    attend l'expiration TCP de Windows, environ 21 s, avant de se replier sur
    IPv4. Les requêtes des joueurs dépassent alors le délai du client et
    saturent le serveur. Les adresses IPv6 restent utilisées quand il n'y a
    pas d'IPv4.
    """
    original = socket.getaddrinfo
    if getattr(original, "prefers_ipv4", False):
        return

    def getaddrinfo(*args, **kwargs):
        return sorted(original(*args, **kwargs), key=lambda info: info[0] != socket.AF_INET)

    getaddrinfo.prefers_ipv4 = True
    socket.getaddrinfo = getaddrinfo


prefer_ipv4()

if not firebase_admin._apps:
    firebase_admin.initialize_app(credentials.Certificate(SERVICE_ACCOUNT_PATH))

db = firestore.client()
