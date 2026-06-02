# Playlist Downloader (Minimal Steps)

Desktop app that takes a public Apple Music playlist link, searches matching audio on YouTube, and downloads files to your computer.

No cookie export. No account login inside the app.

---

## One-time setup

### Mac
1. Download this folder to your computer
2. Double-click `setup/install_mac.sh`
3. Wait for it to finish

### Windows
1. Download this folder to your computer
2. Double-click `setup/install_windows.bat`
3. Wait for it to finish

---

## How to use

1. Run the app
   - **Mac:** `Apple Music Downloader.command`
   - **Windows:** `Apple Music Downloader.bat`
2. Paste a public Apple Music playlist URL
3. Choose output folder and format (`mp3` or `wav`)
4. Click **Download Playlist**
5. Folder opens automatically when complete

---

## Notes

- This flow uses YouTube search/download for each track match.
- `ffmpeg` is required for audio conversion.
- If some songs fail, check `failed_downloads.txt` in the output folder.
- The launcher installs the Python downloader package automatically if it is missing.
