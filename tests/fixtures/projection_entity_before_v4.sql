-- Exact pre-repair v4 function, for startup refresh regression only.
CREATE OR REPLACE FUNCTION __APP_SCHEMA__.projection_entity_before() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog, __APP_SCHEMA__ AS $$
DECLARE typ bigint; key jsonb := '{}'::jsonb; column_name text;
BEGIN
 IF TG_OP='UPDATE' THEN
  IF NEW.salon_id<>OLD.salon_id OR NEW.entity_id IS DISTINCT FROM OLD.entity_id THEN
   RAISE EXCEPTION 'Projection ownership is immutable' USING ERRCODE='23514';
  END IF;
  FOREACH column_name IN ARRAY string_to_array(TG_ARGV[1],',') LOOP
   IF to_jsonb(NEW)->column_name IS DISTINCT FROM to_jsonb(OLD)->column_name THEN
    RAISE EXCEPTION 'Projection identity is immutable' USING ERRCODE='23514';
   END IF;
  END LOOP;
  IF TG_TABLE_NAME='masters' AND (to_jsonb(NEW)->'shared_master_id') IS DISTINCT FROM (to_jsonb(OLD)->'shared_master_id') THEN
   RAISE EXCEPTION 'Shared master identity is immutable; attach a new salon profile' USING ERRCODE='23514';
  END IF;
  RETURN NEW;
 END IF;
 SELECT id INTO STRICT typ FROM __APP_SCHEMA__.entity_types WHERE salon_id=NEW.salon_id AND code=TG_ARGV[0];
 FOREACH column_name IN ARRAY string_to_array(TG_ARGV[1],',') LOOP
  key:=key || jsonb_build_object(column_name,to_jsonb(NEW)->column_name);
 END LOOP;
 IF NEW.entity_id IS NOT NULL THEN
  RAISE EXCEPTION 'Projection entity identity is assigned internally' USING ERRCODE='23514';
 END IF;
 INSERT INTO __APP_SCHEMA__.entities(salon_id,entity_type_id,projection_key) VALUES(NEW.salon_id,typ,key)
 RETURNING id INTO NEW.entity_id;
 IF TG_TABLE_NAME='masters' AND (to_jsonb(NEW)->>'shared_master_id') IS NULL THEN
  INSERT INTO __APP_SCHEMA__.shared_masters DEFAULT VALUES RETURNING id INTO NEW.shared_master_id;
 END IF;
 RETURN NEW;
END $$;
