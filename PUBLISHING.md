# Publishing Nobrain Fishing v1.5

The complete project includes Python source, WAV samples, assets, and documentation.

## Windows one-click upload

1. Download the prepared [GitHub-ready project archive](https://github.com/erop39/nobrain-fishing).
2. Extract the supplied ZIP package from the ChatGPT conversation.
3. Run `publish-to-github.bat` (requires Git for Windows).
4. Authenticate to GitHub if prompted.

The publishing script clones this repository, copies project files, commits, and pushes to `main`. It excludes `.venv/`, `logs/`, and `settings.json`.

## Launch after publication

Run `start.bat`; for optional computer-vision and tray dependencies, use `install-components.bat`.
