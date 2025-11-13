import os

from sqlalchemy.engine import make_url


def is_absolute_db_path(url_string: str) -> bool:
    url = make_url(url_string)

    database = url.database

    if database is None:
        return False

    return os.path.isabs(database)
