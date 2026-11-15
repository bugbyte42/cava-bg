# Noctalia v5 wallpaper detection

cava-bg queries the running Noctalia process with `noctalia msg wallpaper-get`.
This detector runs before the other direct-command detectors.
It uses the existing five-second detection cache and wallpaper watchers.
It adds no polling thread.

The IPC response timeout is 500 ms.
On timeout, cava-bg stops and reaps the request process.
Missing commands, failed requests, and invalid responses use the existing fallback detectors.
These failures do not generate repeated warnings.

The response must contain one absolute path to an existing regular media file.
Spaces, quotes, and shell characters remain unchanged.
Solid-color responses such as `color:#123456` do not produce an image path.

The default query returns the persisted default wallpaper.
It can differ from the effective wallpaper for an output.
For a single output, compare these commands in your desktop session:

```bash
noctalia msg wallpaper-get
noctalia msg wallpaper-get DP-1
```

Replace `DP-1` with your output connector.
Distinct wallpapers per output require a separate connector-aware detector and cache change.
This initial integration does not provide that behavior.

Reference: [Noctalia v5 wallpaper IPC](https://docs.noctalia.dev/noctalia/ipc/media-and-ui/#wallpaper).
