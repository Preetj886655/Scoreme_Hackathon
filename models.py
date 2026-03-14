from sqlalchemy import create_engine, Column, String, Float, DateTime, Text, Integer
from sqlalchemy.orm import declarative_base, sessionmaker
from datetime import datetime
import uuid

DATABASE_URL = "sqlite:///./workflow_engine.db"

engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


class WorkflowRequest(Base):
    __tablename__ = "workflow_requests"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    idempotency_key = Column(String, unique=True, nullable=False)
    workflow_name = Column(String, nullable=False)
    input_data = Column(Text, nullable=False)       # JSON string
    status = Column(String, default="pending")      # pending, approved, rejected, manual_review, failed
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    retry_count = Column(Integer, default=0)


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    request_id = Column(String, nullable=False)
    workflow_name = Column(String, nullable=False)
    step = Column(String, nullable=False)
    rule_name = Column(String, nullable=True)
    field = Column(String, nullable=True)
    expected = Column(String, nullable=True)
    actual_value = Column(String, nullable=True)
    result = Column(String, nullable=False)         # passed, failed, skipped
    reason = Column(Text, nullable=True)
    timestamp = Column(DateTime, default=datetime.utcnow)


class StateHistory(Base):
    __tablename__ = "state_history"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    request_id = Column(String, nullable=False)
    old_status = Column(String, nullable=True)
    new_status = Column(String, nullable=False)
    changed_by = Column(String, default="system")
    reason = Column(Text, nullable=True)
    timestamp = Column(DateTime, default=datetime.utcnow)


def init_db():
    Base.metadata.create_all(bind=engine)
    print("✅ Database initialized successfully")
