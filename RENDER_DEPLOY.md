# Render cloud deployment

This is the first deployment layer for the Render version of ianAutoKTV. It
provides a cloud-safe WSGI entry point, HTTPS-aware sessions, and a shared
access-code gate. The Windows desktop launcher remains the normal local entry
point.

## Deploy

1. Push this project to a GitHub repository. Keep it private and do not commit
   `google_drive_deploy.json`, `google_drive_account.bin`, `.env` files, or song
   media.
2. In Render, create a Blueprint from the repository and use `render.yaml`.
3. Set `CLOUD_ACCESS_CODE` to a private value of at least 12 characters when
   Render prompts for it. Render generates `SECRET_KEY` for the session cookie.
4. After deployment, open the Render service URL. The login page protects the
   player, remote, API, and Socket.IO connection. The service health endpoint
   is `/healthz`.

## Current cloud-mode behavior

- Render serves the existing Flask pages and Socket.IO endpoint over HTTPS and
  WSS. `PORT` is provided by Render.
- All authenticated clients currently share the existing single in-memory
  playback queue and controls. Room codes and separate room state are not yet
  implemented.
- Desktop conversion, upload, rename, and yt-dlp update actions are disabled in
  cloud mode. Song files must not be stored on Render's ephemeral filesystem.
- The current Google Drive prototype is intentionally not registered in cloud
  mode. It uses a single Windows DPAPI-protected account and server-side media
  downloads; it is not a per-user cloud connector.
- A Render restart clears in-memory playback state. Persisted room recovery and
  per-user Drive/NAS access are later upgrade steps.

## Render free service notes

The free web service can sleep after inactivity and may take about a minute to
wake. It has a monthly instance-hour allowance and ephemeral local storage.
Use it first for a private prototype; keep source songs in their own cloud
storage.
