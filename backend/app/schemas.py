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
    cpu_percent: Optional[float]
    mem_percent: Optional[float]
    disk_percent: Optional[float]
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
    logs: Optional[str]
    cpu_percent: Optional[float]
    mem_usage: Optional[str]
    restart_count: Optional[int]
    ports: Optional[str]
    last_seen: datetime
    monitor_enabled: bool = True
    logs_enabled: bool = True

    class Config:
        from_attributes = True


class ServiceOut(BaseModel):
    id: str
    name: str
    status: str
    sub_state: Optional[str]
    is_custom: Optional[bool] = None
    last_seen: datetime
    monitor_enabled: bool = True
    logs_enabled: bool = True

    class Config:
        from_attributes = True


class ContainerIn(BaseModel):
    name: str
    image: Optional[str] = None
    status: str
    logs: Optional[str] = None
    cpu_percent: Optional[float] = None
    mem_usage: Optional[str] = None
    restart_count: Optional[int] = None
    ports: Optional[str] = None


class ServiceIn(BaseModel):
    name: str
    status: str
    sub_state: Optional[str] = None
    custom: Optional[bool] = None  # True if the agent found a hand-written unit file at
    # /etc/systemd/system/<name> rather than one shipped by an OS package. Used only to pick
    # a sensible default the FIRST time this service is ever seen for a VM — never overrides
    # a monitor toggle the user already set. None means an older agent that doesn't report this
    # yet, in which case the existing "monitor everything" default is left alone.


class LogSourceOut(BaseModel):
    id: str
    name: str
    path: str
    created_at: datetime

    class Config:
        from_attributes = True


class LogSourceCreate(BaseModel):
    name: str
    path: str


class ResourceSettingOut(BaseModel):
    resource_type: str
    name: str
    monitor_enabled: bool
    logs_enabled: bool

    class Config:
        from_attributes = True


class ResourceSettingUpdate(BaseModel):
    resource_type: str
    name: str
    monitor_enabled: Optional[bool] = None
    logs_enabled: Optional[bool] = None


class ResourceSettingBulkUpdate(BaseModel):
    resource_type: str
    field: str
    value: bool


class HeartbeatIn(BaseModel):
    name: str
    token: str
    cpu_percent: Optional[float] = None
    mem_percent: Optional[float] = None
    disk_percent: Optional[float] = None
    containers: list[ContainerIn] = []
    services: list[ServiceIn] = []
