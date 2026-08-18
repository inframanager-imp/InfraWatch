from datetime import datetime
from typing import Optional

from pydantic import BaseModel, EmailStr


class Token(BaseModel):
    access_token: str
    token_type: str = "bearer"


class UserOut(BaseModel):
    id: str
    email: str
    name: str
    role: str
    created_at: datetime

    class Config:
        from_attributes = True


class UserCreate(BaseModel):
    email: EmailStr
    name: str
    password: str
    role: str = "readonly"
    vm_ids: list[str] = []


class EnvironmentOut(BaseModel):
    id: str
    name: str

    class Config:
        from_attributes = True


class EnvironmentCreate(BaseModel):
    name: str


class VMCreate(BaseModel):
    name: str
    hostname: Optional[str] = None
    ip_address: Optional[str] = None
    environment_id: str


class VMOut(BaseModel):
    id: str
    name: str
    hostname: Optional[str]
    ip_address: Optional[str]
    status: str
    last_heartbeat: Optional[datetime]
    created_at: datetime
    environment: EnvironmentOut

    class Config:
        from_attributes = True


class VMCreated(VMOut):
    agent_token: str


class ContainerOut(BaseModel):
    id: str
    name: str
    image: Optional[str]
    status: str
    last_seen: datetime

    class Config:
        from_attributes = True


class ServiceOut(BaseModel):
    id: str
    name: str
    status: str
    sub_state: Optional[str]
    last_seen: datetime

    class Config:
        from_attributes = True


class ContainerIn(BaseModel):
    name: str
    image: Optional[str] = None
    status: str


class ServiceIn(BaseModel):
    name: str
    status: str
    sub_state: Optional[str] = None


class HeartbeatIn(BaseModel):
    name: str
    token: str
    containers: list[ContainerIn] = []
    services: list[ServiceIn] = []
