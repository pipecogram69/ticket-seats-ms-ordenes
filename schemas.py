from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field


class OrdenIn(BaseModel):
    id_evento: UUID


class PagoIn(BaseModel):
    metodo: Literal["TARJETA", "PSE", "NEQUI"]
    ultimos4: str | None = Field(default=None, pattern=r"^\d{4}$")


class ValidarIn(BaseModel):
    codigo: str = Field(min_length=8, max_length=40)


class DevolucionIn(BaseModel):
    motivo: str | None = Field(default=None, max_length=200)
