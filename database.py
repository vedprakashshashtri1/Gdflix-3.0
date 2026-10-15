import json
import os
import logging
from motor.motor_asyncio import AsyncIOMotorClient
from config import MONGODB_URI, DATA_FILE

class Database:
    def __init__(self, uri, db_name="gdflix_bot"):
        self._client = AsyncIOMotorClient(uri)
        self.db = self._client[db_name]
        self.config = self.db.config
        self.history = self.db.history
        self.stats = self.db.stats

    async def init_db(self):
        # Default config document
        if not await self.config.find_one({"_id": "settings"}):
            await self.config.insert_one({
                "_id": "settings",
                "admins": [],
                "user_keys": {},
                "user_backup": {},
                "user_backup_channel": {},
                "user_backup_tag_enabled": {},
                "default_backup_enabled": True
            })
        
        # Default stats document
        if not await self.stats.find_one({"_id": "global"}):
            await self.stats.insert_one({
                "_id": "global",
                "total": 0,
                "users": {}
            })
            
        # Migrate old JSON data if it exists and config was just created
        await self._migrate_from_json()

    async def _migrate_from_json(self):
        if os.path.exists(DATA_FILE):
            try:
                with open(DATA_FILE, "r") as f:
                    old_data = json.load(f)
                    
                settings = await self.config.find_one({"_id": "settings"})
                if settings and len(settings.get("user_keys", {})) == 0 and len(old_data.get("user_keys", {})) > 0:
                    logging.info("Migrating old JSON data to MongoDB...")
                    
                    # Migrate config
                    await self.config.update_one({"_id": "settings"}, {"$set": {
                        "user_keys": old_data.get("user_keys", {}),
                        "user_backup": old_data.get("user_backup", {}),
                        "default_backup_enabled": old_data.get("default_backup_enabled", True)
                    }}, upsert=True)
                    
                    # Migrate stats
                    old_stats = old_data.get("stats", {"total": 0, "users": {}})
                    await self.stats.update_one({"_id": "global"}, {"$set": {
                        "total": old_stats.get("total", 0),
                        "users": old_stats.get("users", {})
                    }}, upsert=True)
                    
                    # Migrate history
                    old_history = old_data.get("history", {})
                    for uid, history_list in old_history.items():
                        await self.history.update_one(
                            {"_id": str(uid)},
                            {"$set": {"items": history_list}},
                            upsert=True
                        )
                    
                    # Backup old file to prevent re-migration
                    os.rename(DATA_FILE, DATA_FILE + ".migrated")
                    logging.info("Migration complete!")
            except Exception as e:
                logging.error(f"Migration failed: {e}")

    async def get_settings(self):
        return await self.config.find_one({"_id": "settings"})

    async def get_admins(self):
        doc = await self.get_settings()
        return doc.get("admins", []) if doc else []

    async def add_admin(self, user_id):
        await self.config.update_one({"_id": "settings"}, {"$addToSet": {"admins": user_id}})

    async def remove_admin(self, user_id):
        await self.config.update_one({"_id": "settings"}, {"$pull": {"admins": user_id}})

    async def get_user_key(self, user_id, default_key):
        doc = await self.get_settings()
        if doc and "user_keys" in doc:
            return doc["user_keys"].get(str(user_id), default_key)
        return default_key

    async def set_user_key(self, user_id, api_key):
        await self.config.update_one({"_id": "settings"}, {"$set": {f"user_keys.{user_id}": api_key}})

    async def remove_user_key(self, user_id):
        await self.config.update_one({"_id": "settings"}, {"$unset": {f"user_keys.{user_id}": ""}})



    async def set_user_backup(self, user_id, backup):
        await self.config.update_one({"_id": "settings"}, {"$set": {f"user_backup.{user_id}": backup}})

    async def remove_user_backup(self, user_id):
        await self.config.update_one({"_id": "settings"}, {"$unset": {f"user_backup.{user_id}": ""}})

    async def get_user_backup(self, user_id):
        doc = await self.get_settings()
        if doc and "user_backup" in doc:
            return doc["user_backup"].get(str(user_id))
        return None

    async def get_user_backup_channel(self, user_id):
        doc = await self.get_settings()
        if doc and "user_backup_channel" in doc:
            return doc["user_backup_channel"].get(str(user_id))
        return None

    async def set_user_backup_channel(self, user_id, channel_id):
        await self.config.update_one({"_id": "settings"}, {"$set": {f"user_backup_channel.{user_id}": str(channel_id)}})

    async def remove_user_backup_channel(self, user_id):
        await self.config.update_one({"_id": "settings"}, {"$unset": {f"user_backup_channel.{user_id}": ""}})

    async def is_backup_tag_enabled(self, user_id):
        doc = await self.get_settings()
        if doc and "user_backup_tag_enabled" in doc:
            val = doc["user_backup_tag_enabled"].get(str(user_id))
            if val is None:
                return True
            return bool(val)
        return True

    async def set_backup_tag_enabled(self, user_id, enabled: bool):
        await self.config.update_one({"_id": "settings"}, {"$set": {f"user_backup_tag_enabled.{user_id}": bool(enabled)}})

    async def is_default_backup_enabled(self):
        doc = await self.get_settings()
        return doc.get("default_backup_enabled", True) if doc else True

    async def add_history(self, user_id, entry):
        uid = str(user_id)
        # Push to front, and slice to max 10
        await self.history.update_one(
            {"_id": uid},
            {
                "$push": {
                    "items": {
                        "$each": [entry],
                        "$position": 0,
                        "$slice": 10
                    }
                }
            },
            upsert=True
        )

    async def get_history(self, user_id):
        doc = await self.history.find_one({"_id": str(user_id)})
        return doc.get("items", []) if doc else []

    async def increment_stats(self, user_id):
        uid = str(user_id)
        await self.stats.update_one(
            {"_id": "global"},
            {
                "$inc": {
                    "total": 1,
                    f"users.{uid}": 1
                }
            }
        )

    async def get_stats(self):
        return await self.stats.find_one({"_id": "global"})

db = Database(MONGODB_URI)
