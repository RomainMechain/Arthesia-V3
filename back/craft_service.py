"""Service de craft et cache des recettes."""

import hashlib
import json
import re

import anthropic
from fastapi import HTTPException

try:
    from .config import ANTHROPIC_API_KEY, CRAFT_MODEL, CRAFT_PROMPT_FILE, TEMPLATES_DIR
    from .content_store import load_template
    from .database import db
except ImportError:
    from config import ANTHROPIC_API_KEY, CRAFT_MODEL, CRAFT_PROMPT_FILE, TEMPLATES_DIR
    from content_store import load_template
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
        "Réponds uniquement avec le JSON demandé, sans aucun texte avant ou après."
    )
    response = client.messages.create(
        model=CRAFT_MODEL,
        max_tokens=2000,
        system=load_craft_system_prompt(),
        messages=[{"role": "user", "content": user_message}],
    )
    raw_text = "".join(block.text for block in response.content if block.type == "text")
    cleaned = re.sub(r"^```(?:json)?\s*", "", raw_text.strip())
    cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        return json.loads(cleaned)["item"]
    except (json.JSONDecodeError, KeyError) as exc:
        raise HTTPException(502, "Réponse IA invalide pour le craft — réessaie.") from exc


def save_new_craft_item(item: dict) -> None:
    path = TEMPLATES_DIR / "items" / f"{item['id']}.json"
    if path.exists():
        raise HTTPException(500, f"Collision d'id sur l'item généré ('{item['id']}') — réessaie.")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(item, ensure_ascii=False, indent=2), encoding="utf-8")
