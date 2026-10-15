import asyncio
from motor.motor_asyncio import AsyncIOMotorClient
from config import MONGODB_URI

async def add_user():
    client = AsyncIOMotorClient(MONGODB_URI)
    db = client['gdflix_bot']
    settings = await db.config.find_one({'_id': 'settings'})
    if not settings:
        await db.config.insert_one({
            '_id': 'settings',
            'allowed_groups': [],
            'allowed_users': [7383334104],
            'user_keys': {},
            'user_backup': {},
            'default_backup_enabled': True
        })
        print('Settings created with user 7383334104!')
    else:
        users = settings.get('allowed_users', [])
        if 7383334104 not in users:
            users.append(7383334104)
            await db.config.update_one({'_id': 'settings'}, {'$set': {'allowed_users': users}})
            print('User 7383334104 added!')
        else:
            print('User 7383334104 already allowed!')
    settings = await db.config.find_one({'_id': 'settings'})
    print('Allowed Users:', settings.get('allowed_users', []))
    client.close()

asyncio.run(add_user())
