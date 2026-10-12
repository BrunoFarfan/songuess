"""Delete expired local challenges: python -m app.rooms_cleanup."""

import asyncio

from app.database import SQLiteDatabase, database_connection, initialize_database
from app.rooms import cleanup_expired_rooms


def main() -> None:
    initialize_database()
    with database_connection() as connection:
        count = asyncio.run(cleanup_expired_rooms(SQLiteDatabase(connection)))
    print(f"Deleted {count} expired challenge rooms")


if __name__ == "__main__":
    main()
