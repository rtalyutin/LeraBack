# LeraBack — prepared PostgreSQL backend

One Timeweb App Platform application serves the admin API (`/api/*`), health (`/healthz`) and VK Callback API (`/vk/callback`). Its one in-process outgoing worker starts only when callback configuration and `VK_COMMUNITY_TOKEN` are both present. The frontend is a separate application; the database is a separate managed PostgreSQL service. The constructor has been verified locally against PostgreSQL 16.15 and Chrome; **Timeweb deployment and real VK delivery have not been verified**.

## First launch and admin constructor

The Docker command `python app.py` now applies the repeatable schema migration before opening the HTTP server. On a database with no administrator accounts, it creates the first administrator from `SALON_ADMIN_USERNAME` (default `salon_admin`) and `SALON_ADMIN_PASSWORD` (12–256 characters). Set these through the hosting environment; the example password is rejected. A restart retains existing credentials and sessions, even if bootstrap variables remain set. Remove `SALON_ADMIN_PASSWORD` from hosting settings after the first successful login. Disabled accounts require developer recovery; startup never reactivates them.

See [TIMEWEB.md](TIMEWEB.md) for the three-service setup, exact variables and checks. No manual database initialization or interactive password prompt is needed for the normal Docker startup.

### Developer maintenance

Set `DATABASE_URL` through the hosting environment, ideally with `sslmode=require`; do not commit credentials. From a trusted environment with access to the database:

```sh
python initialize.py
python provision_admin.py salon_admin
python ops.py verify
```

`initialize.py` can also be run separately. It creates the empty schema or applies migration 3 to an existing database and preserves catalog, schedules, bookings and administrator accounts. Migration 3 changes service duration to 1–1440 whole minutes (one day; overnight shifts remain unsupported). `provision_admin.py` is the explicit developer recovery/rotation command; it prompts for a password of at least 12 characters and revokes the administrator's sessions. The application rejects startup unless exactly one active administrator exists.

After login, the administrator builds the salon in LeraFront: services and durations → masters and their services → rooms and their services → weekly hours for both resource types. No fixed masters, rooms or 09:00–18:00 hours are required. A new resource has no opening hours until the administrator supplies them. Multiple intervals per weekday represent breaks; an empty interval list means a day off. Booking starts still follow the configured 30-minute grid and existing notice/cancellation/overlap rules.

Existing test data is not deleted by this upgrade. `seed_starter.py` remains an **optional synthetic test fixture** for a fresh database only; do not run it after `initialize.py` or in a live database. No real salon names, photos, VK IDs or tokens are in this repository.

### Constructor API

All writes require the administrator session and `X-CSRF-Token`. `GET /api/snapshot?date=YYYY-MM-DD` returns catalog, weekly openings and `service_ids` on every master and room.

- `POST /api/services`: `{name, duration_minutes}` creates an active service.
- `POST /api/services/{id}`: `{name, duration_minutes, active}` updates a service.
- `POST /api/masters` or `/api/rooms`: `{name, service_ids: [integer, ...], active: boolean}` creates a resource.
- `POST /api/masters/{id}` or `/api/rooms/{id}`: same fields update the resource. Deactivate with `active: false`; physical deletion is not exposed so booking history is preserved.
- `POST /api/weekly-schedule`: `{resource_kind: "master"|"room", resource_id, weekday: 0..6, intervals: [{start_minute, end_minute}, ...]}` replaces that resource/day's weekly openings. Bounds: `0 <= start_minute < end_minute <= 1440`; intervals must not overlap. Adjacent intervals are merged. Legacy `start_minute`/`end_minute` remains supported.

Creates return HTTP 201, updates 200, invalid input 422, unknown objects 404. An availability change affecting active bookings returns 409 with `affected_booking_ids` and leaves all data unchanged. Only a deliberate retry with `acknowledge: true` may cancel future affected bookings; a visit already underway cannot be cancelled by changing resource availability or hours. Rename-only edits preserve bookings and their name/time snapshots. Existing notification/manual-contact behavior is retained.

## Configuration

- `DATABASE_URL`: PostgreSQL connection URI. One authoritative database, used by API, callback and worker.
- `SALON_POLICY_JSON`: the approved booking policy from `.env.example`, independent of catalog and working hours.
- `SALON_CSRF_SECRET`: random secret of at least 32 bytes, stored in Timeweb secrets.
- `SALON_ADMIN_USERNAME`: initial login, default `salon_admin`.
- `SALON_ADMIN_PASSWORD`: required only when no administrator accounts exist. Never rotates an existing password; remove after the first successful login. Values from `.env.example` must be replaced.
- `PORT`: container HTTP port, default `8080`; Timeweb handles public TLS.
- Optional until VK test-community approval: `VK_GROUP_ID`, `VK_CALLBACK_SECRET`, `VK_CONFIRMATION_CODE`, `VK_COMMUNITY_TOKEN`, `VK_API_VERSION`, `VK_MASTER_PHOTO_IDS_JSON`. Supply callback fields as a complete set. The worker remains off without a token. Photo IDs must be genuine uploaded VK community photos.

Build the included `Dockerfile` as the backend App Platform service. The backend itself serves no frontend files. Connect its URL through `BACKEND_URL` in LeraFront. Register `/vk/callback` only after an authorized test-community run and deployment approval.

Configure Timeweb's process health check as `/livez`: unauthenticated HTTP 200 while the HTTP server is running, with no database call. `/healthz` remains the separate database readiness check (200 or 503). A successful `/livez` alone does not prove database connectivity, administrator login or booking behavior.

## Integrity and checks

All service mutations use a transaction-scoped PostgreSQL advisory lock. A DB trigger protects master, room and client/phone overlap from a direct booking writer. Outgoing VK sends use the same lock through the send and state update, so a later cancellation is ordered after the in-flight send. A failed or ambiguous VK send retains the stable `random_id` for retry. This trades throughput for simple serial behavior suitable for one salon; it is not a production performance measurement.

Run `python -m compileall -q .` and `python -m unittest discover -s tests -v`. With separate fresh disposable databases named `*_test`, set `TEST_DATABASE_URL` for the original booking suite and `CONSTRUCTOR_TEST_DATABASE_URL` for constructor tests (empty schema, creation/eligibility, split hours and break exclusion, 90-minute booking, confirmation/rollback, validation and repeatable migration). Each suite is skipped when its database variable is absent. The old SQLite backup commands are intentionally absent: Timeweb PostgreSQL backup, retention and restore need separate configuration and a restore rehearsal before release.

Local verification (2026-09-30): the original 2 integration tests and 5 constructor tests passed against isolated PostgreSQL 16.15 databases; the 5 constructor tests were repeated after the Cyrillic-name validation fix. An independent verifier exercised availability boundaries, authenticated HTTP/CSRF, rollback after an injected failure, and competing confirmations. Chrome completed 16 checks through the real admin API: empty setup, catalog/eligibility, split hours, 90-minute booking, Moscow time with a Los Angeles browser timezone, confirmation/dismissal, manual-contact reminder and a 390px viewport. This used a local proxy, not the production Nginx/Timeweb deployment. The independent verifier's cloud browser could not access localhost, so that verifier's UI result remains unverified.

For an existing installation, retain `DATABASE_URL` and `SALON_CSRF_SECRET` and redeploy the backend; the application applies the non-destructive migration automatically. Do not reseed or recreate the administrator. For a fresh installation, supply the first-launch variables above and configure the salon through the constructor.

Startup verification (2026-10-01): `tests/test_startup.py` exercises initial credential creation, repeated starts/session retention, competing initial provisioners, disabled-account recovery and invalid configuration through 5 SQLite-backed auth units and 2 configuration/real-HTTP tests. SQLite does not verify the PostgreSQL adapter. The HTTP test uses an actually refused psycopg connection and confirms `/livez` 200 with `/healthz` 503. Set `STARTUP_TEST_DATABASE_URL` to a separate fresh `*_test` PostgreSQL database to run the additional acceptance case covering missing/invalid passwords, competing first starts, catalog/session retention and refusal to reactivate a disabled account. This gate was not executed in the current environment, which has no runnable PostgreSQL service. Timeweb startup and container builds remain unverified.

Remaining release work: Timeweb configuration and deployment verification, backups/restore, and real VK connection/photo/delivery checks. PostgreSQL credentials and VK tokens must come from the hosting environment.
