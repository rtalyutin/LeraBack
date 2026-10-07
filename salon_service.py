"""Server-controlled salon access and shared master attachment."""

from contextlib import closing
from datetime import datetime, timezone
import json
import re
from pathlib import Path

from admin_auth import AuthorizationError
from admin_service import _admin_audit, _catalog_name, _unique_name
from pg_store import SalonScope, connect


def accessible_salons(url, user_id):
    with closing(connect(url)) as db:
        return [dict(row) for row in db.execute(
            "SELECT s.id,s.name,m.role FROM salons s JOIN salon_memberships m ON m.salon_id=s.id "
            "JOIN accounts a ON a.id=m.user_id "
            "WHERE m.user_id=? AND m.active=1 AND s.active=1 AND a.active=1 ORDER BY s.id", (user_id,))]


def require_salon(url, user_id, salon_id):
    if type(salon_id) is not int or salon_id <= 0:
        raise ValueError("X-Salon-Id must be a positive integer")
    if not any(row["id"] == salon_id for row in accessible_salons(url, user_id)):
        raise AuthorizationError("Salon access denied")
    return SalonScope(url, salon_id)


def _salon_name(value):
    if not isinstance(value, str) or not 1 <= len(value.strip()) <= 200:
        raise ValueError("Название салона должно содержать от 1 до 200 символов")
    return value.strip()


def create_salon(url, user_id, name, action_key):
    """Authenticated onboarding: never accept account/membership IDs from the browser."""
    from shared_schema import provision_salon
    name = _salon_name(name)
    if not isinstance(action_key, str) or not re.fullmatch(r"[A-Za-z0-9_-]{16,100}", action_key):
        raise ValueError("Требуется ключ операции создания салона")
    with connect(url) as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            account = db.execute("SELECT id FROM accounts WHERE id=? AND active=1 AND role='admin' FOR UPDATE", (user_id,)).fetchone()
            if not account:
                raise AuthorizationError("Active administrator required")
            previous = db.execute("SELECT salon_id,requested_name FROM salon_creation_requests WHERE user_id=? AND action_key=?",
                                  (user_id, action_key)).fetchone()
            if previous:
                if previous["requested_name"] != name:
                    raise ValueError("Этот ключ уже использован для другого названия салона")
                salon_id = previous["salon_id"]
                if not db.execute("SELECT 1 FROM salon_memberships WHERE user_id=? AND salon_id=? AND active=1",
                                  (user_id, salon_id)).fetchone():
                    raise AuthorizationError("Salon access denied")
            else:
                policy = json.loads(Path(__file__).with_name("starter_data.json").read_text())["policy"]
                salon_id = provision_salon(db, name, policy)
                grant_membership(db, user_id, salon_id)
                db.execute("INSERT INTO salon_creation_requests(user_id,action_key,salon_id,requested_name) VALUES (?,?,?,?)",
                           (user_id, action_key, salon_id, name))
                db.execute("INSERT INTO account_audit_log(actor,action,object_type,object_id,details_json,created_at) VALUES (?,?,?,?,?,?)",
                           (f"admin:{user_id}", "salon_created", "salon", str(salon_id), "{}", datetime.now(timezone.utc).isoformat()))
            row = db.execute("SELECT id,name FROM salons WHERE id=? AND active=1", (salon_id,)).fetchone()
            if row is None:
                raise AuthorizationError("Salon access denied")
            db.commit()
        except Exception:
            db.rollback()
            raise
    return {**dict(row), "role": "admin", "salons": accessible_salons(url, user_id)}


def salon_profile(scope):
    with connect(scope) as db:
        row = db.execute("SELECT id,name FROM salons WHERE id=? AND active=1", (scope.salon_id,)).fetchone()
        if row is None:
            raise LookupError("Салон не найден")
        return dict(row)


def update_salon(scope, user_id, name):
    name = _salon_name(name)
    with connect(scope) as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            if not db.execute("SELECT 1 FROM salon_memberships m JOIN accounts a ON a.id=m.user_id "
                              "JOIN salons s ON s.id=m.salon_id WHERE m.salon_id=? AND m.user_id=? "
                              "AND m.active=1 AND a.active=1 AND s.active=1 FOR UPDATE OF m,a,s",
                              (scope.salon_id, user_id)).fetchone():
                raise AuthorizationError("Salon access denied")
            # Name is canonical constructor data; its existing trigger maintains the registry.
            db.execute("SELECT set_config('app.salon_profile_write','on',true)")
            db.execute("UPDATE __APP_SCHEMA__.entity_parameter_values v SET value_string=? "
                       "FROM __APP_SCHEMA__.entity_parameters p JOIN __APP_SCHEMA__.entity_types t "
                       "ON t.salon_id=p.salon_id AND t.id=p.entity_type_id "
                       "WHERE v.salon_id=? AND p.salon_id=v.salon_id AND p.id=v.parameter_id "
                       "AND p.code='name' AND t.code='salon_settings'", (name, scope.salon_id))
            _admin_audit(db, f"admin:{user_id}", "salon_updated", "salon", scope.salon_id, {}, datetime.now(timezone.utc))
            row = db.execute("SELECT id,name FROM salons WHERE id=?", (scope.salon_id,)).fetchone()
            db.commit()
            return dict(row)
        except Exception:
            db.rollback()
            raise


def shared_masters(scope, user_id):
    allowed = accessible_salons(scope.url, user_id)
    if not any(s["id"] == scope.salon_id for s in allowed):
        raise AuthorizationError("Salon access denied")
    with closing(connect(scope)) as db:
        attached = {row[0] for row in db.execute(
            "SELECT shared_master_id FROM __APP_SCHEMA__.masters WHERE salon_id=?", (scope.salon_id,))}
    result = []
    for salon in allowed:
        if salon["id"] == scope.salon_id:
            continue
        with closing(connect(SalonScope(scope.url, salon["id"]))) as db:
            for row in db.execute("SELECT id,name,shared_master_id FROM __APP_SCHEMA__.masters WHERE salon_id=? AND active=1 ORDER BY name", (salon["id"],)):
                if row["shared_master_id"] not in attached:
                    result.append({"source_salon_id": salon["id"], "source_salon_name": salon["name"],
                                   "master_id": row["id"], "name": row["name"],
                                   "shared_master_id": row["shared_master_id"]})
    return result


def attach_master(scope, user_id, source_salon_id, master_id, service_ids, active=True, name=None):
    from solo_setup import guard_resource
    if type(source_salon_id) is not int or type(master_id) is not int or source_salon_id <= 0 or master_id <= 0:
        raise ValueError("Invalid source master")
    if source_salon_id == scope.salon_id or type(active) is not bool:
        raise ValueError("Choose a master from another salon")
    if not isinstance(service_ids, list) or any(type(x) is not int or x <= 0 for x in service_ids):
        raise ValueError("Service IDs must be a list of positive integers")
    # Read the source through its own scope; arbitrary global identity IDs are never accepted.
    require_salon(scope.url, user_id, source_salon_id)
    with closing(connect(SalonScope(scope.url, source_salon_id))) as source:
        row = source.execute("SELECT id,name,shared_master_id FROM __APP_SCHEMA__.masters WHERE salon_id=? AND id=? AND active=1",
                             (source_salon_id, master_id)).fetchone()
    if row is None:
        raise LookupError("Source master not found")
    name = _catalog_name(row["name"] if name is None else name)
    with closing(connect(scope)) as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            memberships = list(db.execute(
                "SELECT m.salon_id FROM salon_memberships m JOIN salons s ON s.id=m.salon_id "
                "JOIN accounts a ON a.id=m.user_id WHERE m.user_id=? AND m.salon_id IN (?,?) "
                "AND m.active=1 AND s.active=1 AND a.active=1 FOR UPDATE OF m,s,a",
                (user_id, scope.salon_id, source_salon_id)))
            if {r[0] for r in memberships} != {scope.salon_id, source_salon_id}:
                raise AuthorizationError("Salon access denied")
            guard_resource(db, "master", None, active, service_ids)
            _unique_name(db, "masters", name)
            for service_id in set(service_ids):
                if not db.execute("SELECT 1 FROM services WHERE id=?", (service_id,)).fetchone():
                    raise ValueError("Unknown salon service")
            profile_id = db.execute(
                "INSERT INTO __APP_SCHEMA__.masters(salon_id,name,active,shared_master_id) VALUES (?,?,?,?)",
                (scope.salon_id, name, int(active), row["shared_master_id"])).lastrowid
            db.executemany("INSERT INTO master_services(master_id,service_id) VALUES (?,?)",
                           [(profile_id, sid) for sid in sorted(set(service_ids))])
            _admin_audit(db, f"admin:{user_id}", "master_attached", "master", profile_id,
                         {"source_salon_id": source_salon_id, "source_master_id": master_id}, datetime.now(timezone.utc))
            db.commit()
            return {"id": profile_id}
        except Exception:
            db.rollback()
            raise


def grant_membership(db, user_id, salon_id, active=True):
    if type(user_id) is not int or type(salon_id) is not int or user_id <= 0 or salon_id <= 0:
        raise ValueError("Positive account and salon IDs required")
    db.execute("INSERT INTO salon_memberships(salon_id,user_id,role,active) VALUES (?,?,'admin',?) "
               "ON CONFLICT(salon_id,user_id) DO UPDATE SET active=excluded.active",
               (salon_id, user_id, int(active)))
