# applem
public apple music playlist downloader.

paste a playlist link, pick a folder, choose `mp3` or `wav`, and the app downloads matched audio from youtube.

no apple login. no cookies. no browser extension. just the playlist url.

## what it does

- reads songs from a public apple music playlist
- searches youtube for each track with `yt-dlp`
- saves numbered audio files like `001 - song - artist.mp3`
- skips songs you already downloaded, even if the playlist reordered them since
- cleans up an unfinished download, so a cancelled or failed track leaves no stray partial files, even after the playlist was reordered
- replaces a cut off copy instead of keeping two files of the same song
- lets you pause, resume, cancel, verify, and retry missing tracks
- writes failed tracks to `failed_downloads.txt`, and clears a stale report when a run ends with none
- writes verification results to `verification_report.txt`

## setup

windows:

1. run `setup/install_windows.bat`
2. open `Apple Music Downloader.bat`

macos:

1. run `setup/install_mac.sh`
2. open `Apple Music Downloader.command`

the mac launcher creates its own `.venv`, installs `yt-dlp` there, and writes launch issues to `applem-launch.log`.

## how to use

1. paste a public apple music playlist link
2. choose where the songs should save
3. choose `mp3` or `wav`
4. click `download playlist`

the folder opens when the run finishes.

any shape of apple music playlist link works: the modern page, an old `itunes.apple.com` link, a `geo.` or `embed.` share link, one with a trailing slash or tracking parameters, or one pasted without the `https://` in front. they all point at the same playlist, so the app reads them all as one.

## controls

- `pause`: pause a long run
- `resume`: continue after pausing
- `cancel`: stop the current run safely
- `skip songs already downloaded`: keep completed files and only fill gaps
- `verify downloads`: check if every expected song exists
- `retry missing`: try missing or incomplete tracks again
- `clear log`: wipe the visible log only

## heads up

- this does not download from apple music directly
- apple music is used for the public playlist track list
- youtube is used for matched audio downloads
- `yt-dlp` fetches the audio and `ffmpeg` converts it; the launchers install both, and the app says what to do if one is missing
- matching depends on youtube search, so weird covers/live versions can happen sometimes

## open source

this is open source. fork it, break it, fix it, ship it.

if applem helped you, star the repo. costs nothing, helps a lot.
<!-- last touched 2026-09-23 13:19:54 -->
