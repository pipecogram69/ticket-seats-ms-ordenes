import jwt
from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from config import JWT_ALG, JWT_SECRET

bearer = HTTPBearer()


async def contexto(cred: HTTPAuthorizationCredentials = Depends(bearer)) -> dict:
    try:
        payload = jwt.decode(cred.credentials, JWT_SECRET, algorithms=[JWT_ALG])
    except jwt.ExpiredSignatureError:
        raise HTTPException(401, "Token expirado")
    except jwt.InvalidTokenError:
        raise HTTPException(401, "Token inválido")
    payload["token"] = cred.credentials      # se reenvía al MS de eventos
    return payload


def requiere_roles(*roles: str):
    async def checker(user: dict = Depends(contexto)):
        if user["rol"] not in roles:
            raise HTTPException(403, "No tienes permisos para esta acción")
        return user
    return checker
