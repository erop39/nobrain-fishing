# Nobrain Fishing: background mode design

## Current behavior
WASAPI audio detection can run while the remote-desktop window is not focused, but SendInput goes to the foreground Windows application. We must **not** send Space to Excel, a browser, or another foreground application.

## Proposed full background architecture
- Run **Nobrain Worker** on the home PC next to WoW. It captures the game's local sound and performs detection and input there.
- Run **Nobrain Controller** on the work PC. It shows status, session counts, logs, and sends Start/Pause/Stop commands; it never sends Space to the work desktop.
- Connect over authenticated Tailscale or WireGuard. Do not open an unauthenticated control port on the public Internet.
- Keep the WoW window active on the home desktop; this does not require the RustDesk window to remain active at work.
- Worker should pause on lost connection or unexpected window focus, according to a configurable policy.

## Scope
**Design only:** the worker/controller network architecture is not implemented in v1.6. Current audio-only background monitoring is separate from remote keyboard control.
