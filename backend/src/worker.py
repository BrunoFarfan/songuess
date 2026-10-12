from workers import asgi

from app.database import D1Database
from app.main import app
from app.rooms import cleanup_expired_rooms


class Default(asgi.entrypoint(app)):
    async def scheduled(self, controller, env, ctx):
        deleted = await cleanup_expired_rooms(D1Database(self.env.DB))
        print(f"Challenge room cleanup: deleted {deleted} expired rooms")
