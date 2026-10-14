from fastapi import APIRouter, HTTPException, Depends
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from jose import JWTError, jwt
from datetime import datetime, timedelta
from typing import Optional
import logging

logger = logging.getLogger(__name__)

# Configuration
SECRET_KEY = "2NuxW14GmMkWKDtcK1cZT3BlbkFJlXIb1G9iYrzeSn1myt8M"  # Change this to a secure secret key
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 30

# Hardcoded credentials
CREDENTIALS = {
    "steve@lawneeds.co.uk": {
        "password": "admin123",
        "user_id": 1,
        "role": "admin"
    }
}

class AuthAPI:
    def __init__(self):
        self.oauth2_scheme = OAuth2PasswordBearer(tokenUrl="token")

    def create_access_token(self, data: dict, expires_delta: Optional[timedelta] = None):
        to_encode = data.copy()
        if expires_delta:
            expire = datetime.utcnow() + expires_delta
        else:
            expire = datetime.utcnow() + timedelta(minutes=15)
        to_encode.update({"exp": expire})
        encoded_jwt = jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)
        return encoded_jwt

    def get_router(self):
        router = APIRouter()

        @router.post("/token")
        async def login(form_data: OAuth2PasswordRequestForm = Depends()):
            # Check if email exists and password matches
            user_data = CREDENTIALS.get(form_data.username)
            if not user_data or form_data.password != user_data["password"]:
                raise HTTPException(
                    status_code=401,
                    detail="Incorrect email or password",
                    headers={"WWW-Authenticate": "Bearer"},
                )

            # Create access token
            access_token_expires = timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
            access_token = self.create_access_token(
                data={
                    "sub": form_data.username,
                    "user_id": user_data["user_id"],
                    "role": user_data["role"]
                },
                expires_delta=access_token_expires
            )

            return {
                "access_token": access_token,
                "token_type": "bearer",
                "expires_in": ACCESS_TOKEN_EXPIRE_MINUTES * 60,
                "user_id": user_data["user_id"],
                "role": user_data["role"]
            }

        @router.get("/verify-token")
        async def verify_token(token: str = Depends(self.oauth2_scheme)):
            try:
                payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
                email: str = payload.get("sub")
                if email is None or email not in CREDENTIALS:
                    raise HTTPException(
                        status_code=401,
                        detail="Invalid authentication credentials",
                        headers={"WWW-Authenticate": "Bearer"},
                    )
                return {"valid": True, "user": payload}
            except JWTError:
                raise HTTPException(
                    status_code=401,
                    detail="Invalid authentication credentials",
                    headers={"WWW-Authenticate": "Bearer"},
                )

        return router
