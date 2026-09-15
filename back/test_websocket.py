import asyncio
import websockets
import json

async def test():
    async with websockets.connect("ws://127.0.0.1:8000/ws/combat") as ws:
        await ws.send(json.dumps({
            "action": "start_combat",
            "player_id": "Newsimi",
            "zone_id": "sentier_roseaux",
            "entry_id": "entry_rat_roseaux_01",
        }))
        print(await ws.recv())
        await asyncio.sleep(1)
        while True:
            await ws.send(json.dumps({"action": "basic_attack"}))
            print(await ws.recv())

            await ws.send(json.dumps({"action": "use_skill", "skill_id": "charge_du_sanglier"}))
            print(await ws.recv())

            await asyncio.sleep(0.3)

asyncio.run(test())