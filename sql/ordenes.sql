CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE orden (
    id_orden       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    id_usuario     UUID NOT NULL,
    id_evento      UUID NOT NULL,
    evento_nombre  VARCHAR(150) NOT NULL,
    evento_fecha   TIMESTAMPTZ NOT NULL,
    venue          VARCHAR(120),
    ciudad         VARCHAR(80),
    imagen_url     VARCHAR(255),
    subtotal       NUMERIC(12,2) NOT NULL,
    cargo_servicio NUMERIC(12,2) NOT NULL DEFAULT 0,
    total          NUMERIC(12,2) NOT NULL,
    estado         VARCHAR(15) NOT NULL DEFAULT 'PENDIENTE_PAGO'
                   CHECK (estado IN ('PENDIENTE_PAGO','PAGADA','EXPIRADA','CANCELADA','FALLIDA')),
    expira_en      TIMESTAMPTZ NOT NULL,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_orden_usuario ON orden(id_usuario, created_at DESC);
CREATE INDEX idx_orden_pendientes ON orden(expira_en) WHERE estado = 'PENDIENTE_PAGO';

CREATE TABLE orden_detalle (
    id_detalle BIGSERIAL PRIMARY KEY,
    id_orden   UUID NOT NULL REFERENCES orden(id_orden) ON DELETE CASCADE,
    id_boleta  BIGINT NOT NULL,
    localidad  VARCHAR(60) NOT NULL,
    fila       VARCHAR(5) NOT NULL,
    asiento    INT NOT NULL,
    precio     NUMERIC(12,2) NOT NULL,
    UNIQUE (id_orden, id_boleta)
);

CREATE TABLE pago (
    id_pago    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    id_orden   UUID NOT NULL REFERENCES orden(id_orden),
    metodo     VARCHAR(10) NOT NULL CHECK (metodo IN ('TARJETA','PSE','NEQUI')),
    estado     VARCHAR(10) NOT NULL CHECK (estado IN ('APROBADO','RECHAZADO','REVERSADO')),
    monto      NUMERIC(12,2) NOT NULL,
    ultimos4   CHAR(4),
    referencia VARCHAR(40) NOT NULL,
    detalle    VARCHAR(200),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_pago_orden ON pago(id_orden);

CREATE TABLE boleta_emitida (
    id_emitida   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    id_orden     UUID NOT NULL REFERENCES orden(id_orden),
    id_detalle   BIGINT NOT NULL UNIQUE REFERENCES orden_detalle(id_detalle),
    id_usuario   UUID NOT NULL,
    id_evento    UUID NOT NULL,
    codigo       VARCHAR(19) NOT NULL UNIQUE,
    estado       VARCHAR(10) NOT NULL DEFAULT 'VALIDA' CHECK (estado IN ('VALIDA','USADA','ANULADA')),
    emitida_en   TIMESTAMPTZ NOT NULL DEFAULT now(),
    usada_en     TIMESTAMPTZ,
    validada_por UUID
);
CREATE INDEX idx_emitida_usuario ON boleta_emitida(id_usuario);

CREATE TABLE IF NOT EXISTS devolucion (
    id_devolucion UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    id_emitida    UUID NOT NULL UNIQUE REFERENCES boleta_emitida(id_emitida),
    id_orden      UUID NOT NULL REFERENCES orden(id_orden),
    id_usuario    UUID NOT NULL,
    monto         NUMERIC(12,2) NOT NULL,
    incluye_cargo BOOLEAN NOT NULL DEFAULT FALSE,
    motivo        VARCHAR(200),
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
