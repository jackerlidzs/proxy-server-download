"""
Authentication - supports both API key and JWT, with role enforcement
"""
from typing import Optional
from datetime import datetime, timedelta
from fastapi import HTTPException, Header, Depends
from jose import JWTError, jwt
from passlib.context import CryptContext
from config import API_KEY, JWT_SECRET, JWT_ALGORITHM, JWT_EXPIRE_HOURS

pwd_ctx = CryptContext(schemes=["bcrypt"], deprecated="auto")


def hash_password(password: str) -> str:
    return pwd_ctx.hash(password)


def verify_password(plain: str, hashed: str) -> bool:
    return pwd_ctx.verify(plain, hashed)


def create_token(username: str, role: str) -> str:
    expire = datetime.utcnow() + timedelta(hours=JWT_EXPIRE_HOURS)
    return jwt.encode({"sub": username, "role": role, "exp": expire}, JWT_SECRET, algorithm=JWT_ALGORITHM)


def decode_token(token: str) -> dict:
    try:
        return jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
    except JWTError:
        return None


async def verify_key(authorization: Optional[str] = Header(None)):
    """Verify API key or JWT token. Returns dict with user info."""
    if API_KEY == "public":
        return {"username": "public", "role": "admin"}

    if not authorization:
        raise HTTPException(401, "Missing Authorization")

    token = authorization.replace("Bearer ", "").strip()

    # Try API key first
    if token == API_KEY:
        return {"username": "api_key", "role": "admin"}

    # Try JWT
    payload = decode_token(token)
    if payload:
        return {"username": payload.get("sub"), "role": payload.get("role", "user")}

    raise HTTPException(403, "Invalid API key or token")


async def require_admin(user=Depends(verify_key)):
    """Require admin role."""
    if user["role"] != "admin":
        raise HTTPException(403, "Admin access required")
    return user


async def get_user_quota(username: str) -> dict:
    """Get user quota info from database."""
    from database import get_db
    db = await get_db()
    rows = await db.execute_fetchall(
        "SELECT quota_bytes, used_bytes FROM users WHERE username=?", (username,)
    )
    if not rows:
        return {"quota_bytes": 0, "used_bytes": 0, "unlimited": True}
    r = rows[0]
    quota = r["quota_bytes"] or 0
    used = r["used_bytes"] or 0
    return {"quota_bytes": quota, "used_bytes": used, "unlimited": quota == 0}


async def check_quota(username: str, additional_bytes: int = 0):
    """Check if user has enough quota. Raises 413 if exceeded."""
    info = await get_user_quota(username)
    if info["unlimited"]:
        return True
    if info["used_bytes"] + additional_bytes > info["quota_bytes"]:
        raise HTTPException(413, f"Quota exceeded. Used {info['used_bytes']}, limit {info['quota_bytes']}")
    return True


async def update_used_bytes(username: str, delta: int):
    """Update user's used_bytes by delta (positive for add, negative for remove)."""
    if username in ("api_key", "public"):
        return
    from database import get_db
    db = await get_db()
    await db.execute(
        "UPDATE users SET used_bytes = MAX(0, COALESCE(used_bytes, 0) + ?) WHERE username=?",
        (delta, username)
    )
    await db.commit()

