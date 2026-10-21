"""Service de craft et cache des recettes."""

import hashlib
import json
import re
import time
import unicodedata

import anthropic
import httpx
from fastapi import HTTPException

try:
    from .config import ANTHROPIC_API_KEY, CRAFT_MODEL, CRAFT_PROMPT_FILE, GEMINI_API_KEY, GEMINI_MODEL, TEMPLATES_DIR
    from .content_store import load_template
    from .craft_procedural import normalize_craft_item, procedural_craft_item
    from .content_targets import craft_inputs_targets
    from .database import db
except ImportError:
    from config import ANTHROPIC_API_KEY, CRAFT_MODEL, CRAFT_PROMPT_FILE, GEMINI_API_KEY, GEMINI_MODEL, TEMPLATES_DIR
    from content_store import load_template
    from craft_procedural import normalize_craft_item, procedural_craft_item
    from content_targets import craft_inputs_targets
    from database import db


def build_recipe_key(workshop_id: str, inputs: list[dict], parameters: dict, item_type: str | None = None) -> str:
    """Clé canonique d'une recette : atelier, ingrédients triés, paramètres et
    type d'objet choisi (mêmes ingrédients, autre type : autre recette)."""
    sorted_inputs = sorted(inputs, key=lambda item: item["item_id"])
    inputs_part = ",".join(f"{item['item_id']}:{item['quantity']}" for item in sorted_inputs)
    choices = {**parameters, **({"type": item_type} if item_type else {})}
    params_part = ",".join(f"{key}={value}" for key, value in sorted(choices.items()))
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


def ai_configured() -> bool:
    return bool(GEMINI_API_KEY or ANTHROPIC_API_KEY)


def load_craft_system_prompt() -> str:
    if not CRAFT_PROMPT_FILE.exists():
        raise HTTPException(500, f"Prompt de craft introuvable : {CRAFT_PROMPT_FILE}")
    return CRAFT_PROMPT_FILE.read_text(encoding="utf-8")


class CraftAIUnavailable(Exception):
    """Une IA de craft n'a pas pu répondre (clé absente ou refusée, quota,
    panne, réponse invalide) : on passe à la suivante."""


# Champs d'un ingrédient utiles à l'IA (le reste de la fiche est du bruit).
INPUT_FIELDS = ("id", "name", "description", "category", "level", "rarity", "base_value", "quantity", "equipment", "consumable")


def craft_user_message(workshop: dict, item_type: dict, parameters: dict, inputs_full: list[dict]) -> str:
    workshop_info = {
        "id": workshop["id"],
        "name": workshop.get("name"),
        "description": workshop.get("description"),
        "parameters_choisis": parameters,
    }
    requested = {key: item_type[key] for key in ("label", "category", "slot", "effect_type", "stats") if key in item_type}
    inputs = [{key: item[key] for key in INPUT_FIELDS if key in item} for item in inputs_full]
    return (
        "Génère le résultat de ce craft inédit :\n\n"
        f"workshop:\n{json.dumps(workshop_info, ensure_ascii=False, indent=2)}\n\n"
        f"type_demande:\n{json.dumps(requested, ensure_ascii=False)}\n\n"
        f"inputs:\n{json.dumps(inputs, ensure_ascii=False, indent=2)}\n\n"
        f"balance_targets:\n{json.dumps(craft_inputs_targets(inputs_full), ensure_ascii=False)}\n\n"
        "Réponds uniquement avec le JSON demandé, sans aucun texte avant ou après."
    )


GEMINI_API = "https://generativelanguage.googleapis.com/v1beta"
# Modèles « flash » utilisables par la clé, du meilleur au moins bon : découverts
# une fois par processus (GEMINI_MODEL, s'il est défini, passe en tête).
_gemini_models: list[str] | None = None
# Variantes de « flash » qu'on écarte : moins bonnes en texte, ou spécialisées.
GEMINI_EXCLUDED_WORDS = ("lite", "image", "tts", "live", "audio", "embedding", "exp", "thinking", "8b")
# Modèles essayés au plus par craft, et pause avant de réessayer un modèle surchargé.
GEMINI_MAX_MODELS = 3
GEMINI_RETRY_PAUSE_SECONDS = 1.5
# Codes passagers : surcharge (503), erreur serveur (500), quota du modèle (429).
GEMINI_TRANSIENT_STATUSES = (429, 500, 503)


def gemini_version(name: str) -> tuple:
    """``models/gemini-3.6-flash`` → ``(3, 6)`` ; sans version lisible → ``()``."""
    match = re.search(r"gemini-(\d+(?:\.\d+)*)", name)
    return tuple(int(part) for part in match.group(1).split(".")) if match else ()


def discover_gemini_models() -> list[str]:
    """Modèles « flash » que la clé peut utiliser pour générer du texte, du
    meilleur au moins bon : stables avant les aperçus, puis du plus récent."""
    try:
        response = httpx.get(f"{GEMINI_API}/models", params={"pageSize": 1000},
                             headers={"x-goog-api-key": GEMINI_API_KEY}, timeout=30)
    except httpx.HTTPError as exc:
        raise CraftAIUnavailable(f"liste des modèles Gemini injoignable ({exc})") from exc
    if response.status_code != 200:
        raise CraftAIUnavailable(f"liste des modèles Gemini : HTTP {response.status_code} : {response.text[:200]}")
    candidates = []
    for model in response.json().get("models", []):
        name = model.get("name", "")
        if ("flash" in name and "generateContent" in model.get("supportedGenerationMethods", [])
                and not any(word in name for word in GEMINI_EXCLUDED_WORDS)):
            candidates.append(name.removeprefix("models/"))
    candidates.sort(key=lambda name: ("preview" not in name, gemini_version(name), "latest" not in name), reverse=True)
    return candidates


def gemini_models() -> list[str]:
    global _gemini_models
    if _gemini_models is None:
        try:
            discovered = discover_gemini_models()
        except CraftAIUnavailable:
            if not GEMINI_MODEL:
                raise
            discovered = []
        _gemini_models = list(dict.fromkeys(([GEMINI_MODEL] if GEMINI_MODEL else []) + discovered))
        if not _gemini_models:
            raise CraftAIUnavailable("aucun modèle Gemini « flash » disponible pour cette clé")
        print(f"Craft IA : modèles Gemini retenus {_gemini_models[:GEMINI_MAX_MODELS]}")
    return _gemini_models


def ask_gemini(system: str, message: str) -> str:
    """Appel REST à Gemini (Google AI Studio, offre gratuite), en mode JSON.
    Un modèle surchargé ou à court de quota est réessayé une fois, puis on passe
    au suivant ; un modèle inconnu (404, retiré par Google) est oublié."""
    if not GEMINI_API_KEY:
        raise CraftAIUnavailable("GEMINI_API_KEY absente")
    body = {
        "systemInstruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": [{"text": message}]}],
        "generationConfig": {"responseMimeType": "application/json", "temperature": 0.9, "maxOutputTokens": 8192},
    }
    failures = []
    for model in list(gemini_models())[:GEMINI_MAX_MODELS]:
        for attempt in range(2):
            try:
                response = httpx.post(f"{GEMINI_API}/models/{model}:generateContent", json=body,
                                      headers={"x-goog-api-key": GEMINI_API_KEY}, timeout=45)
            except httpx.HTTPError as exc:
                failures.append(f"{model} injoignable ({exc})")
                break
            if response.status_code in GEMINI_TRANSIENT_STATUSES and attempt == 0:
                time.sleep(GEMINI_RETRY_PAUSE_SECONDS)
                continue
            break
        else:
            response = None
        if response is None or response.status_code != 200:
            if response is not None:
                failures.append(f"{model} HTTP {response.status_code} : {response.text[:120]}")
                if response.status_code == 404 and model in _gemini_models:
                    _gemini_models.remove(model)
            continue
        try:
            candidate = response.json()["candidates"][0]
            if candidate.get("finishReason") not in (None, "STOP"):
                failures.append(f"{model} a arrêté sa réponse ({candidate.get('finishReason')})")
                continue
            return "".join(part.get("text", "") for part in candidate["content"]["parts"])
        except (KeyError, IndexError, ValueError):
            failures.append(f"{model} : réponse inattendue")
    raise CraftAIUnavailable("Gemini indisponible : " + " ; ".join(failures))


def ask_claude(system: str, message: str) -> str:
    if not ANTHROPIC_API_KEY:
        raise CraftAIUnavailable("ANTHROPIC_API_KEY absente")
    try:
        # La réflexion du modèle compte dans max_tokens : une limite trop basse
        # coupe le JSON avant la fin.
        response = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY).messages.create(
            model=CRAFT_MODEL,
            max_tokens=16000,
            system=system,
            messages=[{"role": "user", "content": message}],
        )
    except anthropic.APIError as exc:
        raise CraftAIUnavailable(f"Claude : {type(exc).__name__} ({exc})") from exc
    if response.stop_reason in ("max_tokens", "refusal"):
        raise CraftAIUnavailable(f"réponse Claude incomplète ({response.stop_reason})")
    return "".join(block.text for block in response.content if block.type == "text")


def parse_craft_item(raw_text: str) -> dict:
    cleaned = re.sub(r"^```(?:json)?\s*", "", raw_text.strip())
    cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        item = json.loads(cleaned)["item"]
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise CraftAIUnavailable("JSON de craft invalide") from exc
    if not isinstance(item, dict) or not item.get("name"):
        raise CraftAIUnavailable("objet de craft incomplet")
    return item


CRAFT_AI_PROVIDERS = (("gemini", ask_gemini), ("claude", ask_claude))


def invent_with_ai(workshop: dict, item_type: dict, parameters: dict, inputs_full: list[dict],
                   generation: int) -> dict | None:
    """Objet de base inventé par la première IA qui répond, ou None."""
    system = load_craft_system_prompt()
    message = craft_user_message(workshop, item_type, parameters, inputs_full)
    for name, ask in CRAFT_AI_PROVIDERS:
        try:
            item = parse_craft_item(ask(system, message))
        except CraftAIUnavailable as exc:
            print(f"⚠ Craft IA ({name}) indisponible : {exc}")
            continue
        item["origin"] = name
        return normalize_craft_item(item, inputs_full, item_type, generation)
    return None


def generate_craft_item(workshop: dict, item_type: dict, parameters: dict, inputs_full: list[dict], recipe_key: str,
                        generation: int) -> dict:
    """Objet de base d'un craft inédit, du type choisi : la première IA
    disponible l'invente, sinon le générateur sans IA le compose. Le craft ne
    tombe donc jamais en panne. Le résultat est toujours ramené dans les bornes
    d'équilibrage (normalize_craft_item)."""
    item = invent_with_ai(workshop, item_type, parameters, inputs_full, generation)
    if item is None:
        item = normalize_craft_item(procedural_craft_item(workshop, item_type, parameters, inputs_full, recipe_key),
                                    inputs_full, item_type, generation)
    return item


# Les objets inventés par un craft sont écrits comme templates locaux, et aussi
# dans Firestore : un hébergement gratuit efface ses fichiers à chaque
# redémarrage, et restore_crafted_items() les réécrit au démarrage.
CRAFTED_ITEMS_COLLECTION = "crafted_items"


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
    db.collection(CRAFTED_ITEMS_COLLECTION).document(item_id).set(item)
    write_item_template(item)
    return item_id


def write_item_template(item: dict) -> None:
    path = TEMPLATES_DIR / "items" / f"{item['id']}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(item, ensure_ascii=False, indent=2), encoding="utf-8")


def restore_crafted_items() -> dict:
    """Aligne les templates locaux et Firestore pour les objets craftés.

    Réécrit en local chaque objet de ``crafted_items`` qui manque sur le disque
    (redémarrage d'un hébergement), et envoie dans ``crafted_items`` chaque
    résultat du ``craft_cache`` qui n'existe qu'en local (objets craftés avant
    cette sauvegarde).
    """
    stored = {doc.id: doc.to_dict() for doc in db.collection(CRAFTED_ITEMS_COLLECTION).stream()}
    restored = uploaded = 0
    for item_id, item in stored.items():
        if not (TEMPLATES_DIR / "items" / f"{item_id}.json").exists():
            write_item_template({**item, "id": item_id})
            restored += 1
    for doc in db.collection("craft_cache").stream():
        item_id = (doc.to_dict() or {}).get("output_item_id")
        path = TEMPLATES_DIR / "items" / f"{item_id}.json"
        if item_id and item_id not in stored and path.exists():
            db.collection(CRAFTED_ITEMS_COLLECTION).document(item_id).set(json.loads(path.read_text(encoding="utf-8")))
            stored[item_id] = True
            uploaded += 1
    return {"restored": restored, "uploaded": uploaded}
