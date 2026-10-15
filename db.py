import asyncio
from motor.motor_asyncio import AsyncIOMotorClient
from config import MONGODB_URI

async def fix():
    client = AsyncIOMotorClient(MONGODB_URI)
    db = client['gdflix_bot']
    settings = await db.config.find_one({'_id': 'settings'})
    users = settings.get('allowed_users', [])
    if 537297 not in users:
        users.append(537297)
    await db.config.update_one({'_id': 'settings'}, {'$set': {
        'public_mode': True,
        'allowed_users': users
    }})
    settings = await db.config.find_one({'_id': 'settings'})
    print('Public Mode:', settings.get('public_mode'))
    print('Allowed Users:', settings.get('allowed_users'))
    client.close()

asyncio.run(fix())
