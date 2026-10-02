"""Salon-scoped metadata CRUD; core projections remain service API commands.

All public functions receive an already-scoped Database. Mutations own a
transaction and write their audit atomically. Authorization/membership belongs
to the HTTP/auth caller; this module validates scope, ownership and values.
"""
from __future__ import annotations

import json
import re
from contextlib import contextmanager
from datetime import date, datetime, timezone
from decimal import Decimal

CODE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
VALUE_COLUMNS = {
    "string": "value_string", "integer": "value_integer", "number": "value_number",
    "boolean": "value_boolean", "date": "value_date", "reference": "value_reference",
}


class ConstructorError(ValueError):
    pass


def _id(value, label="id"):
    if type(value) is not int or not 1 <= value < 9223372036854775807:
        raise ConstructorError(f"Invalid {label}")
    return value


def _label(value):
    if not isinstance(value, str) or not 1 <= len(value.strip()) <= 200:
        raise ConstructorError("Label must contain 1–200 characters")
    return value.strip()


def _code(value):
    if not isinstance(value, str) or not CODE.fullmatch(value):
        raise ConstructorError("Code must be lowercase Latin letters, digits and underscores")
    return value


def _scope(db):
    sid = db.execute("SELECT public.current_salon_id()").fetchone()[0]
    if sid is None or db.execute("SELECT 1 FROM public.salons WHERE id=? AND active=1", (sid,)).fetchone() is None:
        raise ConstructorError("Active salon context is required")
    return sid


def _row(db, table, sid, object_id):
    # Called with module-owned, static table names only.
    row = db.execute(f"SELECT * FROM public.{table} WHERE salon_id=? AND id=?", (sid, _id(object_id))).fetchone()
    if row is None:
        raise ConstructorError("Object is not available in this salon")
    return row


def _audit(db, actor, action, kind, object_id, details=None):
    db.execute("""INSERT INTO public.admin_audit_log(actor,action,object_type,object_id,details_json,created_at)
        VALUES(?,?,?,?,?,?)""", (actor or "system", action, kind, str(object_id),
        json.dumps(details or {}, ensure_ascii=False), datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")))


@contextmanager
def _mutation(db):
    db.execute("BEGIN IMMEDIATE")
    try:
        yield _scope(db)
        db.commit()
    except Exception:
        db.rollback()
        raise


def _json_value(value):
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        if value == value.to_integral_value():
            return int(value)
        as_float = float(value)
        return str(value) if as_float in (float("inf"), -float("inf")) else as_float
    return value


def constructor_snapshot(db):
    sid = _scope(db)
    types = [dict(row) for row in db.execute("SELECT * FROM public.entity_types WHERE salon_id=? ORDER BY id", (sid,))]
    by_type = {t["id"]: t for t in types}
    for t in types:
        t["parameters"] = []
    for p in db.execute("SELECT * FROM public.entity_parameters WHERE salon_id=? ORDER BY id", (sid,)):
        by_type[p["entity_type_id"]]["parameters"].append(dict(p))
    entities = [dict(row) for row in db.execute("SELECT * FROM public.entities WHERE salon_id=? ORDER BY id", (sid,))]
    by_entity = {e["id"]: e for e in entities}
    for e in entities:
        e["values"] = {}
        e["core"] = by_type[e["entity_type_id"]]["core"]
    for v in db.execute("""SELECT v.*,p.code,p.data_type FROM public.entity_parameter_values v
        JOIN public.entity_parameters p ON p.salon_id=v.salon_id AND p.id=v.parameter_id
        WHERE v.salon_id=? ORDER BY v.entity_id,p.id""", (sid,)):
        by_entity[v["entity_id"]]["values"][v["code"]] = _json_value(v[VALUE_COLUMNS[v["data_type"]]])
    return {"types": types, "entities": entities}


def get_policy(db):
    sid = _scope(db)
    rows = db.execute("""SELECT p.code,p.data_type,v.value_string,v.value_integer,v.value_boolean
        FROM public.entity_types t JOIN public.entities e ON e.salon_id=t.salon_id AND e.entity_type_id=t.id
        JOIN public.entity_parameters p ON p.salon_id=t.salon_id AND p.entity_type_id=t.id
        JOIN public.entity_parameter_values v ON v.salon_id=e.salon_id AND v.entity_id=e.id AND v.parameter_id=p.id
        WHERE t.salon_id=? AND t.code='salon_settings' AND p.core AND NOT e.archived""", (sid,)).fetchall()
    result = {r["code"]: r[VALUE_COLUMNS[r["data_type"]]] for r in rows if r["code"] != "name"}
    from booking_core import validate_policy
    validate_policy(result)
    return result


get_salon_policy = get_policy


def create_type(db, code, label, actor=None):
    code, label = _code(code), _label(label)
    with _mutation(db) as sid:
        row = db.execute("""INSERT INTO public.entity_types(salon_id,code,label) VALUES(?,?,?) RETURNING *""",
                         (sid, code, label)).fetchone()
        _audit(db, actor, "constructor_type_created", "entity_type", row["id"])
        return dict(row)


def update_type(db, type_id, label, actor=None):
    label = _label(label)
    with _mutation(db) as sid:
        existing = _row(db, "entity_types", sid, type_id)
        if existing["core"]:
            raise ConstructorError("Core type definitions cannot be edited")
        row = db.execute("UPDATE public.entity_types SET label=? WHERE salon_id=? AND id=? RETURNING *",
                         (label, sid, type_id)).fetchone()
        _audit(db, actor, "constructor_type_updated", "entity_type", type_id)
        return dict(row)


def delete_type(db, type_id, actor=None):
    with _mutation(db) as sid:
        existing = _row(db, "entity_types", sid, type_id)
        if existing["core"]:
            raise ConstructorError("Core types cannot be deleted")
        if db.execute("SELECT 1 FROM public.entities WHERE salon_id=? AND entity_type_id=? LIMIT 1", (sid, type_id)).fetchone():
            raise ConstructorError("A type with entities cannot be deleted")
        db.execute("DELETE FROM public.entity_parameters WHERE salon_id=? AND entity_type_id=?", (sid, type_id))
        db.execute("DELETE FROM public.entity_types WHERE salon_id=? AND id=?", (sid, type_id))
        _audit(db, actor, "constructor_type_deleted", "entity_type", type_id)
    return {"deleted": True, "id": type_id}


def save_parameter(db, payload, actor=None):
    if not isinstance(payload, dict):
        raise ConstructorError("Parameter object required")
    parameter_id = payload.get("id")
    with _mutation(db) as sid:
        old = _row(db, "entity_parameters", sid, parameter_id) if parameter_id is not None else None
        data = {**(dict(old) if old else {}), **payload}
        code, label = _code(data.get("code")), _label(data.get("label"))
        dtype = data.get("data_type")
        if dtype not in VALUE_COLUMNS:
            raise ConstructorError("Unsupported parameter data_type")
        required = data.get("required", False)
        if type(required) is not bool:
            raise ConstructorError("required must be boolean")
        type_id = _id(data.get("entity_type_id"), "entity_type_id")
        ref_id = data.get("reference_type_id")
        if (dtype == "reference") != (ref_id is not None):
            raise ConstructorError("A reference parameter needs reference_type_id")
        typ = _row(db, "entity_types", sid, type_id)
        if typ["core"] and required and not (old and old["core"]):
            raise ConstructorError("Additional core parameters must be optional")
        if ref_id is not None:
            _row(db, "entity_types", sid, _id(ref_id, "reference_type_id"))
        if parameter_id is None:
            row = db.execute("""INSERT INTO public.entity_parameters
                (salon_id,entity_type_id,code,label,data_type,required,reference_type_id)
                VALUES(?,?,?,?,?,?,?) RETURNING *""", (sid, type_id, code, label, dtype, required, ref_id)).fetchone()
            action = "constructor_parameter_created"
        else:
            if old["core"]:
                raise ConstructorError("Core parameters cannot be edited")
            if old["entity_type_id"] != type_id or old["code"] != code:
                raise ConstructorError("Parameter code and ownership cannot change")
            row = db.execute("""UPDATE public.entity_parameters SET label=?,data_type=?,required=?,reference_type_id=?
                WHERE salon_id=? AND id=? RETURNING *""", (label, dtype, required, ref_id, sid, parameter_id)).fetchone()
            action = "constructor_parameter_updated"
        _audit(db, actor, action, "entity_parameter", row["id"])
        return dict(row)


def delete_parameter(db, parameter_id, actor=None):
    with _mutation(db) as sid:
        old = _row(db, "entity_parameters", sid, parameter_id)
        if old["core"]:
            raise ConstructorError("Core parameters cannot be deleted")
        if db.execute("SELECT 1 FROM public.entity_parameter_values WHERE salon_id=? AND parameter_id=? LIMIT 1", (sid, parameter_id)).fetchone():
            raise ConstructorError("A populated parameter cannot be deleted")
        db.execute("DELETE FROM public.entity_parameters WHERE salon_id=? AND id=?", (sid, parameter_id))
        _audit(db, actor, "constructor_parameter_deleted", "entity_parameter", parameter_id)
    return {"deleted": True, "id": parameter_id}


def _typed_value(db, sid, parameter, value):
    dtype = parameter["data_type"]
    if dtype == "string":
        if not isinstance(value, str) or len(value) > 100000:
            raise ConstructorError(f"{parameter['code']} requires a string of at most 100000 characters")
    elif dtype == "integer":
        if type(value) is not int or not -9223372036854775808 <= value <= 9223372036854775807:
            raise ConstructorError(f"{parameter['code']} requires an integer")
    elif dtype == "number":
        if type(value) not in (int, float, Decimal):
            raise ConstructorError(f"{parameter['code']} requires a finite number")
        value = Decimal(str(value))
        if not value.is_finite():
            raise ConstructorError(f"{parameter['code']} requires a finite number")
    elif dtype == "boolean":
        if type(value) is not bool:
            raise ConstructorError(f"{parameter['code']} requires boolean")
    elif dtype == "date":
        if not isinstance(value, str):
            raise ConstructorError(f"{parameter['code']} requires an ISO date")
        original = value
        try:
            value = date.fromisoformat(value)
        except ValueError:
            raise ConstructorError(f"{parameter['code']} requires an ISO date") from None
        if value.isoformat() != original:
            raise ConstructorError(f"{parameter['code']} requires YYYY-MM-DD")
    elif dtype == "reference":
        ref = _row(db, "entities", sid, _id(value, "reference"))
        if ref["archived"] or ref["entity_type_id"] != parameter["reference_type_id"]:
            raise ConstructorError("Reference has an invalid type or is archived")
    return value


def save_entity(db, payload, actor=None):
    if not isinstance(payload, dict) or not isinstance(payload.get("values"), dict):
        raise ConstructorError("Entity object with values is required")
    values = payload["values"]
    with _mutation(db) as sid:
        entity_id = payload.get("id")
        old = _row(db, "entities", sid, entity_id) if entity_id is not None else None
        type_id = _id(payload.get("entity_type_id", old["entity_type_id"] if old else None), "entity_type_id")
        typ = _row(db, "entity_types", sid, type_id)
        if entity_id is None:
            if typ["core"]:
                raise ConstructorError("Create core objects through the service, master or room API")
            entity_id = db.execute("""INSERT INTO public.entities(salon_id,entity_type_id)
                VALUES(?,?) RETURNING id""", (sid, type_id)).fetchone()[0]
        else:
            if old["entity_type_id"] != type_id or old["archived"]:
                raise ConstructorError("Entity has another type or is archived")
        parameters = {p["code"]: p for p in db.execute("SELECT * FROM public.entity_parameters WHERE salon_id=? AND entity_type_id=?", (sid, type_id))}
        for code, value in values.items():
            if code not in parameters:
                raise ConstructorError(f"Unknown parameter: {code}")
            p = parameters[code]
            if p["core"]:
                raise ConstructorError("Core values must be changed through their service API")
            if value is None:
                if p["required"]:
                    raise ConstructorError(f"Required value cannot be cleared: {code}")
                db.execute("DELETE FROM public.entity_parameter_values WHERE salon_id=? AND entity_id=? AND parameter_id=?", (sid, entity_id, p["id"]))
                continue
            value = _typed_value(db, sid, p, value)
            column = VALUE_COLUMNS[p["data_type"]]
            db.execute(f"""INSERT INTO public.entity_parameter_values(salon_id,entity_id,parameter_id,entity_type_id,{column})
                VALUES(?,?,?,?,?) ON CONFLICT(salon_id,entity_id,parameter_id) DO UPDATE SET {column}=excluded.{column}""",
                (sid, entity_id, p["id"], type_id, value))
        missing = db.execute("""SELECT p.code FROM public.entity_parameters p WHERE p.salon_id=?
            AND p.entity_type_id=? AND p.required AND NOT EXISTS(SELECT 1 FROM public.entity_parameter_values v
             WHERE v.salon_id=p.salon_id AND v.entity_id=? AND v.parameter_id=p.id)""", (sid, type_id, entity_id)).fetchall()
        if missing:
            raise ConstructorError("Missing required parameters: " + ", ".join(r["code"] for r in missing))
        _audit(db, actor, "constructor_entity_saved", "entity", entity_id, {"parameters": sorted(values)})
        result = dict(_row(db, "entities", sid, entity_id))
        result["core"] = typ["core"]
        result["values"] = {}
        for v in db.execute("""SELECT v.*,p.code,p.data_type FROM public.entity_parameter_values v
            JOIN public.entity_parameters p ON p.salon_id=v.salon_id AND p.id=v.parameter_id
            WHERE v.salon_id=? AND v.entity_id=?""", (sid, entity_id)):
            result["values"][v["code"]] = _json_value(v[VALUE_COLUMNS[v["data_type"]]])
        return result


def archive_entity(db, entity_id, actor=None):
    with _mutation(db) as sid:
        old = _row(db, "entities", sid, entity_id)
        typ = _row(db, "entity_types", sid, old["entity_type_id"])
        if typ["core"]:
            raise ConstructorError("Core objects cannot be archived through the generic constructor")
        if db.execute("""SELECT 1 FROM public.entity_parameter_values v JOIN public.entities e
            ON e.salon_id=v.salon_id AND e.id=v.entity_id
            WHERE v.salon_id=? AND v.value_reference=? AND NOT e.archived LIMIT 1""", (sid, entity_id)).fetchone():
            raise ConstructorError("Entity is referenced by an active object")
        result = db.execute("UPDATE public.entities SET archived=true WHERE salon_id=? AND id=? RETURNING *", (sid, entity_id)).fetchone()
        _audit(db, actor, "constructor_entity_archived", "entity", entity_id)
        return dict(result)
