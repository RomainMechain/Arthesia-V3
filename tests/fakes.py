"""Firestore factice en mémoire pour les tests.

``install()`` remplace le client Firestore de chaque module du serveur par une
base en mémoire (``STORE``, chemin « collection/document » → dict), et chaque
fonction ``@firestore.transactional`` par un exécuteur qui joue son corps sur
une transaction factice puis valide ses écritures. Une exception levée dans la
transaction annule toutes ses écritures, comme avec Firestore.

Aucun test ne touche à la vraie base. Le serveur exige quand même
``firebase_key.json`` à la racine du dépôt pour s'importer (``config.py``).
"""

import copy
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

STORE: dict[str, dict] = {}


class Snapshot:
    def __init__(self, ref, data):
        self.reference = ref
        self.id = ref.path.split("/")[-1]
        self._data = data

    @property
    def exists(self) -> bool:
        return self._data is not None

    def to_dict(self):
        return copy.deepcopy(self._data)


class DocumentRef:
    def __init__(self, path: str):
        self.path = path

    def get(self, transaction=None):
        if transaction is not None:
            # Firestore impose toutes les lectures avant la première écriture.
            assert not transaction.writes, f"lecture après écriture dans une transaction : {self.path}"
        return Snapshot(self, copy.deepcopy(STORE.get(self.path)))

    def set(self, data, merge=False):
        STORE[self.path] = copy.deepcopy(data)

    def delete(self):
        STORE.pop(self.path, None)


class Collection:
    def __init__(self, name: str):
        self.name = name

    def document(self, doc_id: str) -> DocumentRef:
        return DocumentRef(f"{self.name}/{doc_id}")

    def stream(self):
        prefix = self.name + "/"
        return [Snapshot(DocumentRef(path), copy.deepcopy(data))
                for path, data in list(STORE.items()) if path.startswith(prefix) and "/" not in path[len(prefix):]]

    def limit(self, _count):
        return self


class Transaction:
    def __init__(self):
        self.writes = []

    def set(self, ref, data, merge=False):
        self.writes.append(("set", ref.path, copy.deepcopy(data)))

    def delete(self, ref):
        self.writes.append(("delete", ref.path, None))

    def commit(self):
        for operation, path, data in self.writes:
            if operation == "set":
                STORE[path] = data
            else:
                STORE.pop(path, None)


class FakeDB:
    def collection(self, name: str) -> Collection:
        return Collection(name)

    def transaction(self) -> Transaction:
        return Transaction()


class TransactionalRunner:
    """Remplace un ``@firestore.transactional`` : joue le corps de la
    fonction sur la transaction reçue, puis valide ses écritures."""

    def __init__(self, transactional):
        self.function = transactional.to_wrap

    def __call__(self, transaction, *args, **kwargs):
        transaction = transaction if isinstance(transaction, Transaction) else Transaction()
        result = self.function(transaction, *args, **kwargs)
        transaction.commit()
        return result


MODULES = ("action_routes", "combat_routes", "craft_service", "main", "player_routes", "player_store", "progression")
_installed = False


def install() -> None:
    """Branche la base factice dans tous les modules du serveur (une fois)."""
    global _installed
    if _installed:
        return
    import importlib
    fake = FakeDB()
    for name in MODULES:
        module = importlib.import_module(f"back.{name}")
        module.db = fake
        for attribute, value in list(vars(module).items()):
            if hasattr(value, "to_wrap") and callable(value):
                setattr(module, attribute, TransactionalRunner(value))
    _installed = True


def reset() -> None:
    STORE.clear()


def new_player(player_id: str = "joueur", **changes) -> dict:
    """Joueur neuf enregistré dans la base factice, avec des champs modifiés."""
    from back import player_store
    player = player_store.default_player_state(player_id)
    player.update(changes)
    STORE[f"players/{player_id}"] = copy.deepcopy(player)
    return player


def player(player_id: str = "joueur") -> dict:
    return STORE[f"players/{player_id}"]
