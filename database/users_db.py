import datetime
from datetime import timedelta  # Added missing import
import pytz
import motor.motor_asyncio
import logging
from info import DB_URL, DB_NAME, TIMEZONE, VERIFY_EXPIRE

logger = logging.getLogger(__name__)

client = motor.motor_asyncio.AsyncIOMotorClient(DB_URL)
mydb = client[DB_NAME]

class Database:
    def __init__(self):
        self.users = mydb.users
        self.blocked_users = mydb.blocked_users
        self.blocked_channels = mydb.blocked_channels
        self.files = mydb.files
        self.refer_collection = mydb.refers 
        self.misc = mydb.misc
        self.verify_id = mydb.verify_id
        self.protected_links = mydb.protected_links

    def new_user(self, id, name):
        return {
            "id": int(id),
            "name": name,
            }

    async def add_user(self, id, name):
        if not await self.is_user_exist(id):
            user = self.new_user(id, name)
            await self.users.insert_one(user)

    async def is_user_exist(self, id):
        return bool(await self.users.find_one({'id': int(id)}))

    async def total_users_count(self):
        return await self.users.count_documents({})

    async def get_all_users(self):
        return self.users.find({})

    async def delete_user(self, user_id):
        await self.users.delete_many({'id': int(user_id)})
    
    async def get_link_by_url(self, url):
        return await self.protected_links.find_one({"url": url})

    async def update_protected_link(self, token, password, title, channel_link):
        await self.protected_links.update_one(
            {"token": token},
            {"$set": {"password": password, "title": title, "channel_link": channel_link}}
        )

    async def add_protected_link(self, token, original_url, password, title, channel_link):
        await self.protected_links.insert_one({
            "token": token,
            "url": original_url,
            "password": password,
            "title": title,                 # Feature 5
            "channel_link": channel_link    # Feature 1
        })

    async def get_protected_link(self, token):
        return await self.protected_links.find_one({"token": token})

    async def delete_protected_link(self, token):
        # Token match karke delete karega
        result = await self.protected_links.delete_one({"token": token})
        return result.deleted_count > 0
        
    async def get_all_protected_links(self):
        # Saare documents return karega cursor ke roop mein
        return self.protected_links.find({})
        
    async def get_notcopy_user(self, user_id):
        user_id = int(user_id)
        user = await self.misc.find_one({"user_id": user_id})
        ist_timezone = pytz.timezone(TIMEZONE)

        if not user:
            res = {
                "user_id": user_id,
                "last_verified": datetime.datetime(2020, 5, 17, 0, 0, 0, tzinfo=ist_timezone),
                "second_time_verified": datetime.datetime(2019, 5, 17, 0, 0, 0, tzinfo=ist_timezone),
            }
            user = await self.misc.insert_one(res)
            user = await self.misc.find_one({"user_id": user_id})
        return user

    async def update_notcopy_user(self, user_id, value: dict):
        user_id = int(user_id)
        myquery = {"user_id": user_id}
        newvalues = {"$set": value}
        return await self.misc.update_one(myquery, newvalues)

    async def is_user_verified(self, user_id):
        user = await self.get_notcopy_user(user_id)
        try:
            pastDate = user["last_verified"]
        except Exception:
            user = await self.get_notcopy_user(user_id)
            pastDate = user["last_verified"]
        ist_timezone = pytz.timezone(TIMEZONE)
        if pastDate.tzinfo is None:
             pastDate = pytz.utc.localize(pastDate)
        pastDate = pastDate.astimezone(ist_timezone)
        current_time = datetime.datetime.now(tz=ist_timezone)
        midnight = datetime.datetime(current_time.year, current_time.month, current_time.day, 0, 0, 0, tzinfo=ist_timezone)
        seconds_since_midnight = (current_time - midnight).total_seconds()
        time_diff = current_time - pastDate
        total_seconds = time_diff.total_seconds()
        return total_seconds <= seconds_since_midnight

    async def use_second_shortener(self, user_id):
        user = await self.get_notcopy_user(user_id)
        if not user.get("second_time_verified"):
            ist_timezone = pytz.timezone(TIMEZONE)
            await self.update_notcopy_user(user_id, {"second_time_verified": datetime.datetime(2019, 5, 17, 0, 0, 0, tzinfo=ist_timezone)})
            user = await self.get_notcopy_user(user_id)
        if await self.is_user_verified(user_id):
            try:
                pastDate = user["last_verified"]
            except Exception:
                user = await self.get_notcopy_user(user_id)
                pastDate = user["last_verified"]
            ist_timezone = pytz.timezone(TIMEZONE)
            if pastDate.tzinfo is None: pastDate = pytz.utc.localize(pastDate)
            pastDate = pastDate.astimezone(ist_timezone)
            if user["second_time_verified"].tzinfo is None:
                 user["second_time_verified"] = pytz.utc.localize(user["second_time_verified"])
            second_time = user["second_time_verified"].astimezone(ist_timezone)
            current_time = datetime.datetime.now(tz=ist_timezone)
            time_difference = current_time - pastDate
            if time_difference > datetime.timedelta(seconds=VERIFY_EXPIRE):
                return second_time < pastDate
        return False

    async def create_verify_id(self, user_id: int, hash, file_id=None):
        # Humne 'file_id' parameter add kiya hai
        res = {"user_id": user_id, "hash": hash, "verified": False, "file_id": file_id}
        return await self.verify_id.insert_one(res)

    async def get_verify_id_info(self, user_id: int, hash):
        return await self.verify_id.find_one({"user_id": user_id, "hash": hash})

    async def update_verify_id_info(self, user_id, hash, value: dict):
        myquery = {"user_id": user_id, "hash": hash}
        newvalues = {"$set": value}
        return await self.verify_id.update_one(myquery, newvalues)

    async def get_verification_stats(self):
        ist_timezone = pytz.timezone(TIMEZONE)
        current_time = datetime.datetime.now(tz=ist_timezone)
        
        # Aaj raat 12 baje (Midnight) ka time nikalo
        midnight = current_time.replace(hour=0, minute=0, second=0, microsecond=0)
        
        # MongoDB query ke liye UTC mein convert karein
        midnight_utc = midnight.astimezone(pytz.utc)

        # Level 1: Wo log jinhone aaj 1st Link verify kiya
        level1_count = await self.misc.count_documents({
            "last_verified": {"$gte": midnight_utc}
        })

        # Level 2: Wo log jinhone aaj 2nd Link verify kiya
        level2_count = await self.misc.count_documents({
            "second_time_verified": {"$gte": midnight_utc}
        })

        return level1_count, level2_count
        
    async def is_user_in_list(self, user_id):
        user = await self.refer_collection.find_one({"user_id": int(user_id)})
        return bool(user)

    async def get_refer_points(self, user_id: int):
        user = await self.refer_collection.find_one({"user_id": int(user_id)})
        return user.get("points", 0) if user else 0

    async def add_refer_points(self, user_id: int, points: int):
        await self.refer_collection.update_one(
            {"user_id": int(user_id)}, 
            {"$set": {"points": points}}, 
            upsert=True
        )

    async def change_points(self, user_id: int, amount: int):
        current_points = await self.get_refer_points(user_id)
        new_points = current_points + amount
        if new_points < 0:
            new_points = 0 # पॉइंट्स माइनस में नहीं जाएंगे
        await self.refer_collection.update_one(
            {"user_id": int(user_id)}, 
            {"$set": {"points": new_points}}, 
            upsert=True
        )
        return new_points

    async def is_user_blocked(self, user_id: int) -> bool:
        return await self.blocked_users.find_one({"user_id": int(user_id)}) is not None

    async def block_user(self, user_id: int, reason: str = "No reason provided."):
        await self.blocked_users.update_one(
            {"user_id": int(user_id)},
            {
                "$set": {
                    "user_id": int(user_id),
                    "reason": reason,
                    "blocked_at": datetime.datetime.utcnow()
                }
            },
            upsert=True
        )

    async def unblock_user(self, user_id: int):
        await self.blocked_users.delete_one({"user_id": int(user_id)})
        
    async def get_all_blocked_users(self):
        return self.blocked_users.find({})

    async def total_blocked_count(self):
        return await self.blocked_users.count_documents({})
        
    async def is_channel_blocked(self, channel_id: int) -> bool:
        return await self.blocked_channels.find_one({"channel_id": int(channel_id)}) is not None

    async def block_channel(self, channel_id: int, reason: str = "No reason provided."):
        await self.blocked_channels.update_one(
            {"channel_id": int(channel_id)},
            {
                "$set": {
                    "channel_id": int(channel_id),
                    "reason": reason,
                    "blocked_at": datetime.datetime.utcnow()
                }
            },
            upsert=True
        )

    async def unblock_channel(self, channel_id: int):
        await self.blocked_channels.delete_one({"channel_id": int(channel_id)})

    async def get_all_blocked_channels(self):
        return self.blocked_channels.find({})

    async def get_channel_block_data(self, channel_id: int):
        return await self.blocked_channels.find_one({"channel_id": channel_id})

    async def total_blocked_channels_count(self):
        return await self.blocked_channels.count_documents({})
        
    async def get_expired(self, current_time):
        expired_users = []
        cursor = self.users.find({"expiry_time": {"$lt": current_time}})
        async for user in cursor:
            expired_users.append(user)
        return expired_users

# Inside your DB manager class
    async def get_expiring_soon(self, label, delta):
        reminder_key = f"reminder_{label}_sent"
        now = datetime.datetime.utcnow()
        target_time = now + delta
        window = timedelta(seconds=30)

        start_range = target_time - window
        end_range = target_time + window

        reminder_users = []
        cursor = self.users.find({
            "expiry_time": {"$gte": start_range, "$lte": end_range},
            reminder_key: {"$ne": True}
        })

        async for user in cursor:
            reminder_users.append(user)
            await self.users.update_one(
                {"id": user["id"]}, {"$set": {reminder_key: True}}
            )

        return reminder_users

    async def get_user(self, user_id):
        user_data = await self.users.find_one({"id": int(user_id)})
        return user_data
        
    async def update_user(self, user_data):
        await self.users.update_one(
            {"id": user_data["id"]}, 
            {"$set": user_data}, 
            upsert=True
        )

    async def has_premium_access(self, user_id):
        user_data = await self.get_user(user_id)
        if user_data:
            expiry_time = user_data.get("expiry_time")
            if expiry_time is None:
                return False
            # Check if expiry_time is naive (no timezone) or aware
            if isinstance(expiry_time, datetime.datetime):
                if datetime.datetime.now() <= expiry_time:
                    return True
                else:
                    await self.users.update_one({"id": int(user_id)}, {"$set": {"expiry_time": None}})
                    return False
        return False
            
    async def all_premium_users_count(self):
        count = await self.users.count_documents({
            "expiry_time": {"$gt": datetime.datetime.now()}
        })
        return count

    async def remove_premium_access(self, user_id):
        await self.users.update_one(
            {"id": int(user_id)}, 
            {"$set": {"expiry_time": None}}
        )
        return True

db = Database()
            
