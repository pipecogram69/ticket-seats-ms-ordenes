import asyncio
import secrets
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import UUID

import httpx
import segno
from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

import db
from auth import requiere_roles
from config import EVENTOS_URL, USUARIOS_URL
from schemas import DevolucionIn, OrdenIn, PagoIn, ValidarIn


# ---------- Utilidades ----------
def a_fecha(texto: str) -> datetime:
    dt = datetime.fromisoformat(texto)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def dec(x) -> Decimal:
    return Decimal(str(x))


def nuevo_codigo() -> str:
    h = secrets.token_hex(8).upper()
    return "-".join(h[i:i + 4] for i in range(0, 16, 4))


async def llamar_eventos(metodo: str, ruta: str, token: str | None = None, cuerpo: dict | None = None):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.request(metodo, EVENTOS_URL + ruta, headers=headers, json=cuerpo)
    except httpx.HTTPError:
        raise HTTPException(502, "No se pudo contactar al servicio de eventos")
    if r.status_code >= 400:
        try:
            detalle = r.json().get("detail")
        except Exception:
            detalle = None
        if not isinstance(detalle, str):
            detalle = "Error en el servicio de eventos"
        raise HTTPException(r.status_code if r.status_code in (401, 403, 404, 409, 422) else 502, detalle)
    return r.json()


COLS = ("id_orden, id_usuario, id_evento, evento_nombre, evento_fecha, venue, ciudad, imagen_url, "
        "subtotal, cargo_servicio, total, expira_en, created_at")
SEL_ORDEN = f"""SELECT {COLS},
    CASE WHEN estado = 'PENDIENTE_PAGO' AND expira_en < now() THEN 'EXPIRADA' ELSE estado END AS estado
    FROM orden"""

SQL_BOLETAS = """SELECT be.id_emitida, be.codigo, be.estado, be.usada_en, o.id_orden, o.evento_nombre,
                        o.evento_fecha, o.venue, o.ciudad, o.imagen_url, d.localidad, d.fila, d.asiento, d.precio
                 FROM boleta_emitida be
                 JOIN orden o ON o.id_orden = be.id_orden
                 JOIN orden_detalle d ON d.id_detalle = be.id_detalle"""


def con_qr(r) -> dict:
    return {**dict(r), "qr": segno.make(r["codigo"], error="m").svg_data_uri(scale=4, border=2)}


async def orden_completa(id_orden: UUID) -> dict | None:
    async with db.pool.acquire() as conn:
        o = await conn.fetchrow(SEL_ORDEN + " WHERE id_orden = $1", id_orden)
        if not o:
            return None
        det = await conn.fetch(
            """SELECT id_boleta, localidad, fila, asiento, precio
               FROM orden_detalle WHERE id_orden = $1 ORDER BY id_detalle""", id_orden)
    return {**dict(o), "detalle": [dict(d) for d in det]}


async def boletas_de_orden(id_orden: UUID) -> list[dict]:
    async with db.pool.acquire() as conn:
        rows = await conn.fetch(
            SQL_BOLETAS + " WHERE be.id_orden = $1 ORDER BY d.localidad, d.fila, d.asiento", id_orden)
    return [con_qr(r) for r in rows]


async def expirar_ordenes():
    while True:
        await asyncio.sleep(60)
        try:
            async with db.pool.acquire() as conn:
                await conn.execute(
                    """UPDATE orden SET estado = 'EXPIRADA', updated_at = now()
                       WHERE estado = 'PENDIENTE_PAGO' AND expira_en < now()""")
        except Exception as e:
            print("[expiracion ordenes]", e)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await db.init_pool()
    tarea = asyncio.create_task(expirar_ordenes())
    yield
    tarea.cancel()
    await db.close_pool()


app = FastAPI(title="MS Órdenes, Pagos y Boletas", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


@app.get("/health")
async def health():
    return {"ok": True}


# ---------- Órdenes ----------
@app.post("/ordenes", status_code=201)
async def crear_orden(data: OrdenIn, user=Depends(requiere_roles("CLIENTE", "ADMIN"))):
    ev = await llamar_eventos("GET", f"/eventos/{data.id_evento}")
    if ev["estado"] != "PUBLICADO":
        raise HTTPException(409, "El evento no está disponible para la venta")

    res = await llamar_eventos("GET", f"/inventario/mis-reservas/{data.id_evento}", user["token"])
    boletas = res["boletas"]
    if not boletas:
        raise HTTPException(409, "No tienes boletas reservadas para este evento (o la reserva expiró)")

    subtotal = sum((dec(b["precio"]) for b in boletas), Decimal(0))
    cargo = (subtotal * dec(res["cargo_servicio_pct"]) / 100).quantize(Decimal("0.01"))
    expira = min(a_fecha(b["reservada_hasta"]) for b in boletas)
    uid = UUID(user["sub"])

    async with db.pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                """UPDATE orden SET estado = 'CANCELADA', updated_at = now()
                   WHERE id_usuario = $1 AND id_evento = $2 AND estado = 'PENDIENTE_PAGO'""",
                uid, data.id_evento)
            id_orden = await conn.fetchval(
                """INSERT INTO orden (id_usuario, id_evento, evento_nombre, evento_fecha, venue, ciudad,
                          imagen_url, subtotal, cargo_servicio, total, expira_en)
                   VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11) RETURNING id_orden""",
                uid, data.id_evento, ev["nombre"], a_fecha(ev["fecha_inicio"]), ev.get("venue"),
                ev.get("ciudad"), ev.get("imagen_url"), subtotal, cargo, subtotal + cargo, expira)
            await conn.executemany(
                """INSERT INTO orden_detalle (id_orden, id_boleta, localidad, fila, asiento, precio)
                   VALUES ($1,$2,$3,$4,$5,$6)""",
                [(id_orden, b["id_boleta"], b["localidad"], b["fila"], b["asiento"], dec(b["precio"]))
                 for b in boletas])
    return await orden_completa(id_orden)


@app.get("/ordenes/mias")
async def mis_ordenes(user=Depends(requiere_roles("CLIENTE", "ADMIN"))):
    async with db.pool.acquire() as conn:
        rows = await conn.fetch(
            SEL_ORDEN + " WHERE id_usuario = $1 ORDER BY created_at DESC LIMIT 50", UUID(user["sub"]))
    return [dict(r) for r in rows]


@app.get("/ordenes/{id_orden}")
async def ver_orden(id_orden: UUID, user=Depends(requiere_roles("CLIENTE", "ADMIN"))):
    o = await orden_completa(id_orden)
    if not o:
        raise HTTPException(404, "Orden no encontrada")
    if str(o["id_usuario"]) != user["sub"] and user["rol"] != "ADMIN":
        raise HTTPException(403, "Esta orden no es tuya")
    return o


@app.post("/ordenes/{id_orden}/cancelar")
async def cancelar_orden(id_orden: UUID, user=Depends(requiere_roles("CLIENTE", "ADMIN"))):
    async with db.pool.acquire() as conn:
        o = await conn.fetchrow("SELECT id_usuario, id_evento, estado FROM orden WHERE id_orden = $1", id_orden)
        if not o:
            raise HTTPException(404, "Orden no encontrada")
        if str(o["id_usuario"]) != user["sub"] and user["rol"] != "ADMIN":
            raise HTTPException(403, "Esta orden no es tuya")
        if o["estado"] != "PENDIENTE_PAGO":
            raise HTTPException(409, f"La orden está {o['estado']}")
        ids = [r["id_boleta"] for r in await conn.fetch(
            "SELECT id_boleta FROM orden_detalle WHERE id_orden = $1", id_orden)]
        await conn.execute(
            "UPDATE orden SET estado = 'CANCELADA', updated_at = now() WHERE id_orden = $1 AND estado = 'PENDIENTE_PAGO'",
            id_orden)
    try:       # si falla, el MS de eventos libera las boletas solo al vencer la reserva
        await llamar_eventos("POST", "/inventario/liberar", user["token"],
                             {"id_evento": str(o["id_evento"]), "ids": ids})
    except HTTPException:
        pass
    await notificar_reserva(user["token"], o["id_evento"], "CANCELADA")
    return {"estado": "CANCELADA"}


# ---------- Pago (simulado) ----------
@app.post("/ordenes/{id_orden}/pagar")
async def pagar(id_orden: UUID, data: PagoIn, user=Depends(requiere_roles("CLIENTE", "ADMIN"))):
    if data.metodo == "TARJETA" and not data.ultimos4:
        raise HTTPException(422, "Indica los últimos 4 dígitos de la tarjeta")
    error = None
    referencia = "PAY-" + secrets.token_hex(5).upper()

    async with db.pool.acquire() as conn:
        async with conn.transaction():
            o = await conn.fetchrow("SELECT * FROM orden WHERE id_orden = $1 FOR UPDATE", id_orden)
            if not o:
                raise HTTPException(404, "Orden no encontrada")
            if str(o["id_usuario"]) != user["sub"]:
                raise HTTPException(403, "Esta orden no es tuya")

            if o["estado"] == "PAGADA":
                pass                                   # idempotente: devuelve las mismas boletas
            elif o["estado"] != "PENDIENTE_PAGO":
                error = (409, f"La orden está {o['estado']}")
            elif o["expira_en"] <= datetime.now(timezone.utc):
                await conn.execute(
                    "UPDATE orden SET estado = 'EXPIRADA', updated_at = now() WHERE id_orden = $1", id_orden)
                error = (409, "La reserva expiró; vuelve a elegir tus asientos")
            elif data.ultimos4 == "0000":              # tarjeta de prueba que siempre se rechaza
                await conn.execute(
                    """INSERT INTO pago (id_orden, metodo, estado, monto, ultimos4, referencia, detalle)
                       VALUES ($1, $2, 'RECHAZADO', $3, $4, $5, 'Rechazado por la entidad (simulación)')""",
                    id_orden, data.metodo, o["total"], data.ultimos4, referencia)
                error = (402, "Pago rechazado por la entidad. Prueba con otro método o tarjeta.")
            else:
                id_pago = await conn.fetchval(
                    """INSERT INTO pago (id_orden, metodo, estado, monto, ultimos4, referencia, detalle)
                       VALUES ($1, $2, 'APROBADO', $3, $4, $5, 'Aprobado (simulación)') RETURNING id_pago""",
                    id_orden, data.metodo, o["total"], data.ultimos4, referencia)
                ids = [r["id_boleta"] for r in await conn.fetch(
                    "SELECT id_boleta FROM orden_detalle WHERE id_orden = $1", id_orden)]
                try:
                    await llamar_eventos("POST", "/inventario/confirmar", user["token"],
                                         {"id_evento": str(o["id_evento"]), "ids": ids})
                except HTTPException as ex:           # compensación: se revierte el pago
                    await conn.execute(
                        "UPDATE pago SET estado = 'REVERSADO', detalle = $2 WHERE id_pago = $1",
                        id_pago, f"Reversado: {ex.detail}"[:200])
                    await conn.execute(
                        "UPDATE orden SET estado = 'FALLIDA', updated_at = now() WHERE id_orden = $1", id_orden)
                    error = (409 if ex.status_code == 409 else 502,
                             f"No se pudo confirmar el inventario; el pago fue reversado. {ex.detail}")
                else:
                    await conn.execute(
                        "UPDATE orden SET estado = 'PAGADA', updated_at = now() WHERE id_orden = $1", id_orden)
                    for d in await conn.fetch(
                            "SELECT id_detalle FROM orden_detalle WHERE id_orden = $1 ORDER BY id_detalle", id_orden):
                        await conn.execute(
                            """INSERT INTO boleta_emitida (id_orden, id_detalle, id_usuario, id_evento, codigo)
                               VALUES ($1, $2, $3, $4, $5)""",
                            id_orden, d["id_detalle"], o["id_usuario"], o["id_evento"], nuevo_codigo())

    if error:
        raise HTTPException(*error)
    await notificar_reserva(user["token"], o["id_evento"], "CONVERTIDA")
    return {"estado": "PAGADA", "referencia": referencia, "boletas": await boletas_de_orden(id_orden)}


# ---------- Boletas e ingreso ----------
@app.get("/boletas/mias")
async def mis_boletas(user=Depends(requiere_roles("CLIENTE", "ADMIN"))):
    async with db.pool.acquire() as conn:
        rows = await conn.fetch(
            SQL_BOLETAS + """ WHERE be.id_usuario = $1
                              ORDER BY o.evento_fecha, o.id_orden, d.localidad, d.fila, d.asiento""",
            UUID(user["sub"]))
    return [con_qr(r) for r in rows]


@app.post("/boletas/validar")
async def validar(data: ValidarIn, user=Depends(requiere_roles("VALIDADOR", "ADMIN"))):
    codigo = data.codigo.strip().upper()
    async with db.pool.acquire() as conn:
        async with conn.transaction():
            aceptada = await conn.fetchval(
                """UPDATE boleta_emitida SET estado = 'USADA', usada_en = now(), validada_por = $2
                   WHERE codigo = $1 AND estado = 'VALIDA' RETURNING 1""", codigo, UUID(user["sub"]))
            info = await conn.fetchrow(
                """SELECT be.estado, be.usada_en, o.evento_nombre, o.evento_fecha, o.venue, o.ciudad,
                          d.localidad, d.fila, d.asiento
                   FROM boleta_emitida be
                   JOIN orden o ON o.id_orden = be.id_orden
                   JOIN orden_detalle d ON d.id_detalle = be.id_detalle
                   WHERE be.codigo = $1""", codigo)
    if not info:
        raise HTTPException(404, "El código no existe")
    resultado = "ACEPTADA" if aceptada else ("YA_USADA" if info["estado"] == "USADA" else info["estado"])
    return {"resultado": resultado, "boleta": dict(info)}


# ---------- Reservas (aviso al MS-A) y devoluciones ----------
from config import HORAS_LIMITE_DEVOLUCION


async def notificar_reserva(token: str, id_evento, estado: str):
    """Avisa al MS de usuarios para actualizar el historial de reservas. Si falla, no afecta la compra."""
    try:
        async with httpx.AsyncClient(timeout=5) as c:
            await c.post(f"{USUARIOS_URL}/reservas/notificar",
                         headers={"Authorization": f"Bearer {token}"},
                         json={"id_evento": str(id_evento), "estado": estado})
    except Exception as e:
        print("[reservas] no se pudo notificar:", e)


@app.post("/boletas/{id_emitida}/devolver")
async def devolver_boleta(id_emitida: UUID, data: DevolucionIn,
                          user=Depends(requiere_roles("CLIENTE", "ADMIN"))):
    async with db.pool.acquire() as conn:
        async with conn.transaction():
            b = await conn.fetchrow(
                """SELECT be.id_emitida, be.id_orden, be.id_usuario, be.id_evento, be.estado,
                          d.id_boleta, d.precio, o.cargo_servicio,
                          (SELECT count(*) FROM orden_detalle x WHERE x.id_orden = o.id_orden) AS n
                   FROM boleta_emitida be
                   JOIN orden o ON o.id_orden = be.id_orden
                   JOIN orden_detalle d ON d.id_detalle = be.id_detalle
                   WHERE be.id_emitida = $1
                   FOR UPDATE OF be""", id_emitida)
            if not b:
                raise HTTPException(404, "Boleta no encontrada")
            if str(b["id_usuario"]) != user["sub"] and user["rol"] != "ADMIN":
                raise HTTPException(403, "Esta boleta no es tuya")
            if b["estado"] != "VALIDA":
                raise HTTPException(409, "La boleta ya fue usada" if b["estado"] == "USADA"
                                    else "La boleta ya fue devuelta")

            ev = await llamar_eventos("GET", f"/eventos/{b['id_evento']}")
            cancelado = ev["estado"] == "CANCELADO"
            if not cancelado:
                cond = ev.get("condiciones") or {}
                if not cond.get("permite_devolucion"):
                    raise HTTPException(409, "Este evento no permite devoluciones")
                limite = a_fecha(ev["fecha_inicio"]) - timedelta(hours=HORAS_LIMITE_DEVOLUCION)
                if datetime.now(timezone.utc) >= limite:
                    raise HTTPException(
                        409, f"Las devoluciones se cierran {HORAS_LIMITE_DEVOLUCION} horas antes del evento")

            monto = dec(b["precio"])
            if cancelado:       # evento cancelado: se devuelve también la parte proporcional del cargo
                monto += (dec(b["cargo_servicio"]) / b["n"]).quantize(Decimal("0.01"))

            await conn.execute("UPDATE boleta_emitida SET estado = 'ANULADA' WHERE id_emitida = $1", id_emitida)
            await conn.execute(
                """INSERT INTO devolucion (id_emitida, id_orden, id_usuario, monto, incluye_cargo, motivo)
                   VALUES ($1, $2, $3, $4, $5, $6)""",
                id_emitida, b["id_orden"], b["id_usuario"], monto, cancelado, data.motivo)
            # si el inventario falla, se lanza la excepción y la transacción se revierte
            await llamar_eventos("POST", "/inventario/devolver", user["token"],
                                 {"id_evento": str(b["id_evento"]), "ids": [b["id_boleta"]]})
    return {"estado": "DEVUELTA", "monto": monto, "incluye_cargo": cancelado}
