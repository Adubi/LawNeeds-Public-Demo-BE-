import os
from urllib.parse import quote_plus
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from contextlib import contextmanager
import logging
from sqlalchemy.exc import SQLAlchemyError
from fastapi import HTTPException
from .models import Base, AppSetting, Log, User, KnowledgeBase



logger = logging.getLogger(__name__)

class DB:
    def __init__(self, app_name):
        self.app_name = app_name
        # Individual components are kept available for callers that introspect
        # them (e.g. legacy scripts), but the engine is built via _build_db_url
        # which prefers DATABASE_URL when set.
        self.db_user = os.getenv('DB_USER')
        self.db_password = os.getenv('DB_PASSWORD')
        self.db_host = os.getenv('DB_HOST')
        self.db_port = os.getenv('DB_PORT')
        self.db_name = os.getenv('DB_NAME', app_name.lower())
        self.db_uri = os.getenv('DATABASE_URL')

        self.engine = self.create_db_engine()
        self.Session = sessionmaker(bind=self.engine)

        self.create_tables()

    def _build_db_url(self) -> str:
        """Resolve the database connection URL.

        Order of precedence (matches migrations/env.py and scripts/*):
          1. ``DATABASE_URL``  — used as-is. Recommended in prod / DO / Docker.
          2. Individual ``DB_USER`` / ``DB_PASSWORD`` / ``DB_HOST`` /
             ``DB_PORT`` / ``DB_NAME`` env vars assembled into a
             ``postgresql://`` URL. The password is URL-encoded so special
             characters (e.g. ``@``, ``/``, ``:``) cannot break the parser.

        Raises ``RuntimeError`` with a clear message naming the missing
        variables when neither path can produce a usable URL. The full URL
        and password are never logged.
        """
        url = os.getenv('DATABASE_URL')
        if url:
            return url

        missing = [
            name for name, value in (
                ('DB_USER', self.db_user),
                ('DB_PASSWORD', self.db_password),
                ('DB_HOST', self.db_host),
                ('DB_PORT', self.db_port),
            )
            if not value
        ]
        if missing:
            raise RuntimeError(
                "Database configuration missing. Set DATABASE_URL, or all of: "
                + ", ".join(missing)
            )

        return (
            f"postgresql://{self.db_user}:{quote_plus(self.db_password)}"
            f"@{self.db_host}:{self.db_port}/{self.db_name}"
        )

    def create_db_engine(self):
        try:
            url = self._build_db_url()
            logger.info(
                "Creating database engine (host=%s, db=%s)",
                self.db_host or '<from DATABASE_URL>',
                self.db_name,
            )
            return create_engine(url)
        except Exception as e:
            logger.error("Error creating database engine: %s", e)
            raise

    def create_tables(self):
        # In production, tables are managed by Alembic migrations. Set
        # SKIP_AUTO_CREATE_TABLES=1 to disable the auto-create on startup,
        # which avoids permission issues on managed databases (PostgreSQL 15+).
        if os.getenv('SKIP_AUTO_CREATE_TABLES'):
            logger.info("Skipping auto-create tables (SKIP_AUTO_CREATE_TABLES is set)")
            return
        try:
            Base.metadata.create_all(self.engine)
            logger.info("All tables created successfully.")
        except Exception as e:
            logger.error("Error creating tables: %s", e)
            raise

    @contextmanager
    def get_session(self):
        """Provide a transactional scope around a series of operations."""
        session = self.Session()
        try:
            yield session
            session.commit()
        except HTTPException:
            # Let FastAPI HTTP exceptions propagate without rollback noise or re-wrapping
            session.rollback()
            raise
        except Exception as e:
            session.rollback()
            # SQLAlchemy exception strings can include bound credential/profile data.
            logger.error("Session error (%s)", type(e).__name__)
            raise
        finally:
            session.close()

    def query(self, model, filters=None):
        """Query a model with optional filters"""
        with self.get_session() as session:
            try:
                query = session.query(model)
                if filters:
                    query = query.filter_by(**filters)
                return query.all()
            except Exception as e:
                logger.error(f"Database query error: {str(e)}")
                raise

    def query_all_knowledgebase(self):
        return self.query(KnowledgeBase)

    def execute(self, statement, params=None):
        """Execute a SQL statement directly"""
        with self.get_session() as session:
            try:
                result = session.execute(statement, params or {})
                session.commit()
                return result
            except SQLAlchemyError as e:
                session.rollback()
                logger.error(f"Database execution error: {str(e)}")
                raise
