# LeraBack — prepared PostgreSQL backend

One Timeweb App Platform application serves the admin API (`/api/*`), health (`/healthz`) and VK Callback API (`/vk/callback`). Its one in-process outgoing worker starts only when callback configuration and `VK_COMMUNITY_TOKEN` are both present. The frontend is a separate application; the database is a separate managed PostgreSQL service. The constructor has been verified locally against PostgreSQL 16.15 and Chrome; **Timeweb deployment and real VK delivery have not been verified**.

## Schema initialization and admin constructor

Set `DATABASE_URL` through the hosting environment, ideally with `sslmode=require`; do not commit credentials. From a trusted environment with access to the database:

```sh
python initialize.py
python provision_admin.py salon_admin
python ops.py verify
```

`initialize.py` creates the empty schema or applies migration 3 to an existing database. It preserves catalog, schedules, bookings and administrator accounts and is repeatable. Migration 3 changes the service duration constraint to 1–1440 whole minutes (one day; overnight shifts remain unsupported). Provisioning prompts for a password of at least 12 characters. The application rejects startup unless exactly one active administrator exists.

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
- `PORT`: container HTTP port, default `8080`; Timeweb handles public TLS.
- Optional until VK test-community approval: `VK_GROUP_ID`, `VK_CALLBACK_SECRET`, `VK_CONFIRMATION_CODE`, `VK_COMMUNITY_TOKEN`, `VK_API_VERSION`, `VK_MASTER_PHOTO_IDS_JSON`. Supply callback fields as a complete set. The worker remains off without a token. Photo IDs must be genuine uploaded VK community photos.

Build the included `Dockerfile` as the backend App Platform service. The backend itself serves no frontend files. Connect its URL through `BACKEND_URL` in LeraFront. Register `/vk/callback` only after an authorized test-community run and deployment approval.

## Integrity and checks

All service mutations use a transaction-scoped PostgreSQL advisory lock. A DB trigger protects master, room and client/phone overlap from a direct booking writer. Outgoing VK sends use the same lock through the send and state update, so a later cancellation is ordered after the in-flight send. A failed or ambiguous VK send retains the stable `random_id` for retry. This trades throughput for simple serial behavior suitable for one salon; it is not a production performance measurement.

Run `python -m compileall -q .` and `python -m unittest discover -s tests -v`. With separate fresh disposable databases named `*_test`, set `TEST_DATABASE_URL` for the original booking suite and `CONSTRUCTOR_TEST_DATABASE_URL` for constructor tests (empty schema, creation/eligibility, split hours and break exclusion, 90-minute booking, confirmation/rollback, validation and repeatable migration). Each suite is skipped when its database variable is absent. The old SQLite backup commands are intentionally absent: Timeweb PostgreSQL backup, retention and restore need separate configuration and a restore rehearsal before release.

Local verification (2026-09-30): the original 2 integration tests and 5 constructor tests passed against isolated PostgreSQL 16.15 databases; the 5 constructor tests were repeated after the Cyrillic-name validation fix. An independent verifier exercised availability boundaries, authenticated HTTP/CSRF, rollback after an injected failure, and competing confirmations. Chrome completed 16 checks through the real admin API: empty setup, catalog/eligibility, split hours, 90-minute booking, Moscow time with a Los Angeles browser timezone, confirmation/dismissal, manual-contact reminder and a 390px viewport. This used a local proxy, not the production Nginx/Timeweb deployment. The independent verifier's cloud browser could not access localhost, so that verifier's UI result remains unverified.

For an existing installation, run the new backend's `python initialize.py` with its existing `DATABASE_URL` before serving the new API, then redeploy both applications. Do not reseed or recreate the existing administrator. For a fresh installation, use the initialization/provisioning commands above and configure the salon through the constructor.

Remaining release work: Timeweb configuration and deployment verification, backups/restore, and real VK connection/photo/delivery checks. PostgreSQL credentials and VK tokens must come from the hosting environment.
