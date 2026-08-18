from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .database import Base, SessionLocal, engine
from .routers import auth, environments, users, vms
from .seed import seed

Base.metadata.create_all(bind=engine)

app = FastAPI(title="InfraWatch API")

# Local-dev default — restrict to the real frontend origin before deploying.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router)
app.include_router(vms.router)
app.include_router(users.router)
app.include_router(environments.router)


@app.on_event("startup")
def on_startup():
    db = SessionLocal()
    try:
        seed(db)
    finally:
        db.close()


@app.get("/health")
def health():
    return {"status": "ok"}
