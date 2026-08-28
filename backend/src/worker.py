from workers import asgi

from app.main import app

Default = asgi.entrypoint(app)
