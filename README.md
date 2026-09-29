# LeraBack — prepared PostgreSQL backend

One Timeweb App Platform application serves the admin API (`/api/*`), health (`/healthz`) and VK Callback API (`/vk/callback`). Its one in-process outgoing worker starts only when callback configuration and `VK_COMMUNITY_TOKEN` are both present. The frontend is a separate application; the database is a separate managed PostgreSQL service. This is a prepared local port, **not deployed or verified against PostgreSQL/VK**.

## Data initialization

Use a **new empty PostgreSQL database**. `DATABASE_URL` must be supplied through Timeweb secrets, ideally with `sslmode=require`; do not commit credentials. From a trusted environment with access to that database:

```sh
python seed_starter.py
python provision_admin.py salon_admin
python ops.py verify
```

The seed initializes the schema and the approved **synthetic** 10 services, 5 masters, 2 rooms and 09:00–18:00 schedule. Provisioning prompts for a password of at least 12 characters. The application will reject startup unless exactly one active administrator exists. No real salon names, photos, VK IDs or tokens are in this repository.

## Configuration

- `DATABASE_URL`: PostgreSQL connection URI. One authoritative database, used by API, callback and worker.
- `SALON_POLICY_JSON`: the `policy` object in `starter_data.json` for this synthetic stage.
- `SALON_CSRF_SECRET`: random secret of at least 32 bytes, stored in Timeweb secrets.
- `PORT`: container HTTP port, default `8080`; Timeweb handles public TLS.
- Optional until VK test-community approval: `VK_GROUP_ID`, `VK_CALLBACK_SECRET`, `VK_CONFIRMATION_CODE`, `VK_COMMUNITY_TOKEN`, `VK_API_VERSION`, `VK_MASTER_PHOTO_IDS_JSON`. Supply callback fields as a complete set. The worker remains off without a token. Photo IDs must be genuine uploaded VK community photos.

Build the included `Dockerfile` as the backend App Platform service. The backend itself serves no frontend files. Connect its URL through `BACKEND_URL` in LeraFront. Register `/vk/callback` only after an authorized test-community run and deployment approval.

## Integrity and checks

All service mutations use a transaction-scoped PostgreSQL advisory lock. A DB trigger protects master, room and client/phone overlap from a direct booking writer. Outgoing VK sends use the same lock through the send and state update, so a later cancellation is ordered after the in-flight send. A failed or ambiguous VK send retains the stable `random_id` for retry. This trades throughput for simple serial behavior suitable for one salon; it is not a production performance measurement.

Run `python -m compileall -q .` and `python -m unittest discover -s tests -v`. With a fresh disposable database named `*_test`, set `TEST_DATABASE_URL` to run the PostgreSQL integration tests. The integration suite is skipped when no test database exists. The old SQLite backup commands are intentionally absent: Timeweb PostgreSQL backup, retention and restore need separate configuration and a restore rehearsal before release.

Current remaining gates: actual PostgreSQL integration test, browser/mobile check, VK test community and carousel photos, privacy/retention review, Timeweb secret/network/backup setup, and explicit authorization for repository writes and deployment.
