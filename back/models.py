"""Modeles de requetes HTTP du serveur Arthesia."""

from pydantic import BaseModel, Field


class MonsterKillRequest(BaseModel):
    player_id: str
    zone_id: str
    entry_id: str


class ResourceGatherRequest(BaseModel):
    player_id: str
    zone_id: str
    entry_id: str


class CreatePlayerRequest(BaseModel):
    player_id: str = Field(min_length=1)
    current_zone_id: str | None = None


class StartQuestRequest(BaseModel):
    player_id: str
    quest_id: str


class TurnInQuestRequest(BaseModel):
    player_id: str
    quest_id: str


class CraftInput(BaseModel):
    item_id: str
    quantity: int


class CraftRequest(BaseModel):
    player_id: str
    workshop_id: str
    inputs: list[CraftInput]
    parameters: dict[str, str]


class AllocateStatPointsRequest(BaseModel):
    player_id: str
    allocations: dict[str, int]


class EquipItemRequest(BaseModel):
    player_id: str
    item_id: str


class UnequipItemRequest(BaseModel):
    player_id: str
    slot: str


class EquipSkillRequest(BaseModel):
    player_id: str
    skill_id: str


class UnequipSkillRequest(BaseModel):
    player_id: str
    skill_id: str


class EquipTitleRequest(BaseModel):
    player_id: str
    title_id: str


class UnequipTitleRequest(BaseModel):
    player_id: str


class BuyFromShopRequest(BaseModel):
    player_id: str
    zone_id: str
    entry_id: str
    item_id: str
    quantity: int = 1


class SellToServerRequest(BaseModel):
    player_id: str
    item_id: str
    quantity: int = 1


class ShopBuyRequest(BaseModel):
    player_id: str
    zone_id: str
    entry_id: str
    item_id: str
    quantity: int = 1


class ShopSellRequest(BaseModel):
    player_id: str
    zone_id: str
    entry_id: str
    item_id: str
    quantity: int = 1


class ListItemForSaleRequest(BaseModel):
    player_id: str
    zone_id: str
    entry_id: str
    item_id: str
    quantity: int = 1
    price: int


class BuyListingRequest(BaseModel):
    player_id: str
    listing_id: str


class CancelListingRequest(BaseModel):
    player_id: str
    listing_id: str
