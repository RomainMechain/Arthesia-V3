import asyncio
import websockets
import json

async def test():
    async with websockets.connect("ws://127.0.0.1:8000/ws/combat") as ws:
        await ws.send(json.dumps({
            "action": "start_combat",
            "player_id": "test",
            "zone_id": "jardins_suspendus",
            "entry_id": "sylphe_petales_1",
        }))
        print(await ws.recv())
        await asyncio.sleep(1)
        while True:
            await ws.send(json.dumps({"action": "basic_attack"}))
            print(await ws.recv())

            await ws.send(json.dumps({"action": "use_skill", "skill_id": "lame_du_vent_ancien"}))
            print(await ws.recv())

            await asyncio.sleep(0.3)

asyncio.run(test())