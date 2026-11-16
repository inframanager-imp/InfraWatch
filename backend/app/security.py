from datetime import datetime, timedelta

import bcrypt
import jwt
from fastapi import Depends, HTTPException, Query, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.orm import Session

from .config import settings
from .database import get_db
from .models import User

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/login")
# A download carries its credential in the query string, so the header is
# optional on those routes rather than required.
oauth2_optional = OAuth2PasswordBearer(tokenUrl="/auth/login", auto_error=False)

# Long enough to survive a slow click, short enough that the URL is worthless
# by the time anyone reads it out of a log.
DOWNLOAD_TICKET_SECONDS = 60


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def verify_password(password: str, password_hash: str) -> bool:
    return bcrypt.checkpw(password.encode(), password_hash.encode())


def create_access_token(subject: str) -> str:
    expire = datetime.utcnow() + timedelta(minutes=settings.jwt_expire_minutes)
    payload = {"sub": subject, "exp": expire}
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def get_user_from_token(token: str, db: Session) -> User | None:
    """Shared by the HTTP auth dependency and WebSocket handlers (which have
    no header-based auth, so take the token as a plain query param instead)."""
    try:
        payload = jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
        email = payload.get("sub")
        if not email:
            return None
    except jwt.PyJWTError:
        return None
    return db.query(User).filter(User.email == email).first()


def create_download_ticket(subject: str) -> str:
    """A credential for one file download, safe to put in a URL.

    Scoped rather than reused: an access token in a query string would be a
    full-privilege credential sitting in browser history and proxy logs for
    as long as those are kept. This one expires in a minute and is accepted
    for nothing but downloads.
    """
    expire = datetime.utcnow() + timedelta(seconds=DOWNLOAD_TICKET_SECONDS)
    payload = {"sub": subject, "exp": expire, "scope": "download"}
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def get_download_user(
    token: str | None = Depends(oauth2_optional),
    ticket: str | None = Query(None),
    db: Session = Depends(get_db),
) -> User:
    """The usual bearer token, or a download ticket in the query string."""
    if token:
        user = get_user_from_token(token, db)
        if user:
            return user
    if ticket:
        try:
            payload = jwt.decode(ticket, settings.jwt_secret,
                                 algorithms=[settings.jwt_algorithm])
        except jwt.PyJWTError:
            payload = None
        # The scope check is the point of the whole arrangement: an ordinary
        # access token must not be usable here, because this is the one place
        # a credential is accepted from somewhere it leaks out of.
        if payload and payload.get("scope") == "download" and payload.get("sub"):
            user = db.query(User).filter(User.email == payload["sub"]).first()
            if user:
                return user
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )


def get_current_user(token: str = Depends(oauth2_scheme), db: Session = Depends(get_db)) -> User:
    user = get_user_from_token(token, db)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Could not validate credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return user


def require_admin(user: User = Depends(get_current_user)) -> User:
    if user.role != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin access required")
    return user
