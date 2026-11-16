import os

JWT_SECRET = os.getenv("JWT_SECRET", "cambia-esto-en-produccion")
JWT_ALG = "HS256"
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://postgres:postgres@localhost:5437/ticketseats_ordenes")
EVENTOS_URL = os.getenv("EVENTOS_URL", "http://localhost:8002")

USUARIOS_URL = os.getenv("USUARIOS_URL", "http://localhost:8001")
HORAS_LIMITE_DEVOLUCION = int(os.getenv("HORAS_LIMITE_DEVOLUCION", "24"))
