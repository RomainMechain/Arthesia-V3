"""Service de craft et cache des recettes."""

import hashlib
import json
import re
import unicodedata

import anthropic
from fastapi import HTTPException

try:
    from .config import ANTHROPIC_API_KEY, CRAFT_MODEL, CRAFT_PROMPT_FILE, TEMPLATES_DIR
    from .content_store import load_template
    from .content_targets import craft_inputs_targets
    from .database import db
except ImportError:
    from config import ANTHROPIC_API_KEY, CRAFT_MODEL, CRAFT_PROMPT_FILE, TEMPLATES_DIR
    from content_store import load_template
    from content_targets import craft_inputs_targets
    from database import db


def build_recipe_key(workshop_id: str, inputs: list[dict], parameters: dict) -> str:
    sorted_inputs = sorted(inputs, key=lambda item: item["item_id"])
    inputs_part = ",".join(f"{item['item_id']}:{item['quantity']}" for item in sorted_inputs)
    params_part = ",".join(f"{key}={value}" for key, value in sorted(parameters.items()))
    return f"{workshop_id}|{inputs_part}|{params_part}"


def recipe_key_doc_id(recipe_key: str) -> str:
    return hashlib.sha256(recipe_key.encode()).hexdigest()[:32]


def get_cached_craft(recipe_key: str) -> str | None:
    doc = db.collection("craft_cache").document(recipe_key_doc_id(recipe_key)).get()
    if not doc.exists:
        return None
    return doc.to_dict()["output_item_id"]


def save_craft_cache(recipe_key: str, output_item_id: str) -> None:
    db.collection("craft_cache").document(recipe_key_doc_id(recipe_key)).set({
        "recipe_key": recipe_key,
        "output_item_id": output_item_id,
    })


def load_craft_system_prompt() -> str:
    if not CRAFT_PROMPT_FILE.exists():
        raise HTTPException(500, f"Prompt de craft introuvable : {CRAFT_PROMPT_FILE}")
    return CRAFT_PROMPT_FILE.read_text(encoding="utf-8")


def generate_craft_item_via_ai(workshop: dict, parameters: dict, inputs_full: list[dict]) -> dict:
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    workshop_info = {
        "id": workshop["id"],
        "name": workshop.get("name"),
        "description": workshop.get("description"),
        "parameters_choisis": parameters,
    }
    user_message = (
        "Génère le résultat de ce craft inédit :\n\n"
        f"workshop:\n{json.dumps(workshop_info, ensure_ascii=False, indent=2)}\n\n"
        f"inputs:\n{json.dumps(inputs_full, ensure_ascii=False, indent=2)}\n\n"
        f"balance_targets:\n{json.dumps(craft_inputs_targets(inputs_full), ensure_ascii=False)}\n\n"
        "Réponds uniquement avec le JSON demandé, sans aucun texte avant ou après."
    )
    try:
        # La réflexion du modèle compte dans max_tokens : une limite trop basse
        # coupe le JSON avant la fin.
        response = client.messages.create(
            model=CRAFT_MODEL,
            max_tokens=16000,
            system=load_craft_system_prompt(),
            messages=[{"role": "user", "content": user_message}],
        )
    except anthropic.AuthenticationError as exc:
        # Erreur de configuration du serveur, pas du joueur : on la signale clairement.
        print(f"❌ Craft IA : clé ANTHROPIC_API_KEY refusée par l'API ({exc})")
        raise HTTPException(503, "La création d'objets inédits est indisponible : la clé API du serveur est invalide.") from exc
    except anthropic.APIError as exc:
        print(f"❌ Craft IA : erreur de l'API ({type(exc).__name__} : {exc})")
        raise HTTPException(503, "La création d'un objet inédit est indisponible pour l'instant — réessaie plus tard.") from exc
    if response.stop_reason in ("max_tokens", "refusal"):
        raise HTTPException(502, "Réponse IA incomplète pour le craft — réessaie.")
    raw_text = "".join(block.text for block in response.content if block.type == "text")
    cleaned = re.sub(r"^```(?:json)?\s*", "", raw_text.strip())
    cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        item = json.loads(cleaned)["item"]
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise HTTPException(502, "Réponse IA invalide pour le craft — réessaie.") from exc
    if not isinstance(item, dict) or not item.get("name") or not item.get("category"):
        raise HTTPException(502, "Réponse IA invalide pour le craft — réessaie.")
    return item


def save_new_craft_item(item: dict) -> str:
    """Enregistre l'item généré comme template et retourne son id définitif.

    L'id proposé par l'IA sert aussi de nom de fichier : il est réduit à
    ``[a-z0-9_]`` et suffixé si un item porte déjà ce nom.
    """
    raw_id = unicodedata.normalize("NFKD", str(item.get("id") or item["name"])).encode("ascii", "ignore").decode()
    base_id = re.sub(r"[^a-z0-9_]+", "_", raw_id.lower()).strip("_") or "objet_artisanal"
    item_id = base_id
    suffix = 2
    while (TEMPLATES_DIR / "items" / f"{item_id}.json").exists():
        item_id = f"{base_id}_{suffix}"
        suffix += 1
    item["id"] = item_id
    path = TEMPLATES_DIR / "items" / f"{item_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(item, ensure_ascii=False, indent=2), encoding="utf-8")
    return item_id
