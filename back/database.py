"""Initialisation et acces a Firestore."""

import firebase_admin
from firebase_admin import credentials, firestore

try:
    from .config import SERVICE_ACCOUNT_PATH
except ImportError:
    from config import SERVICE_ACCOUNT_PATH


if not firebase_admin._apps:
    firebase_admin.initialize_app(credentials.Certificate(SERVICE_ACCOUNT_PATH))

db = firestore.client()
