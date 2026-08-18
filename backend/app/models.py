import uuid
from datetime import datetime

from sqlalchemy import Column, DateTime, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship

from .database import Base


def gen_uuid():
    return str(uuid.uuid4())


class User(Base):
    __tablename__ = "users"

    id = Column(UUID(as_uuid=False), primary_key=True, default=gen_uuid)
    email = Column(String, unique=True, nullable=False, index=True)
    name = Column(String, nullable=False)
    password_hash = Column(String, nullable=False)
    role = Column(String, nullable=False, default="readonly")  # "admin" | "readonly"
    created_at = Column(DateTime, default=datetime.utcnow)

    vm_access = relationship("UserVMAccess", back_populates="user", cascade="all, delete-orphan")


class Environment(Base):
    __tablename__ = "environments"

    id = Column(UUID(as_uuid=False), primary_key=True, default=gen_uuid)
    name = Column(String, unique=True, nullable=False)

    vms = relationship("VM", back_populates="environment")


class VM(Base):
    __tablename__ = "vms"

    id = Column(UUID(as_uuid=False), primary_key=True, default=gen_uuid)
    name = Column(String, unique=True, nullable=False)
    hostname = Column(String, nullable=True)
    ip_address = Column(String, nullable=True)
    environment_id = Column(UUID(as_uuid=False), ForeignKey("environments.id"), nullable=False)
    status = Column(String, nullable=False, default="pending")  # "pending" | "online" | "offline"
    agent_token_hash = Column(String, nullable=False)
    last_heartbeat = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    environment = relationship("Environment", back_populates="vms")
    access = relationship("UserVMAccess", back_populates="vm", cascade="all, delete-orphan")


class UserVMAccess(Base):
    __tablename__ = "user_vm_access"
    __table_args__ = (UniqueConstraint("user_id", "vm_id", name="uq_user_vm"),)

    id = Column(UUID(as_uuid=False), primary_key=True, default=gen_uuid)
    user_id = Column(UUID(as_uuid=False), ForeignKey("users.id"), nullable=False)
    vm_id = Column(UUID(as_uuid=False), ForeignKey("vms.id"), nullable=False)

    user = relationship("User", back_populates="vm_access")
    vm = relationship("VM", back_populates="access")


class AuditLog(Base):
    __tablename__ = "audit_log"

    id = Column(UUID(as_uuid=False), primary_key=True, default=gen_uuid)
    actor_email = Column(String, nullable=False)
    action = Column(String, nullable=False)
    target = Column(String, nullable=True)
    detail = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
