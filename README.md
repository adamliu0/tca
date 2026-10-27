# TCA

Twitch Chat Autoreply. A small program that watches your Twitch chat and answers as your existing account.

`!song` and `!playlist` ask Spotify what that account is playing and post the link. Any other command you add in `commands.json` posts the text you wrote.

Replies are sent with an HTTPS request to Twitch. The program does not type into chat, OBS, or a game, and it does not use the keyboard or mouse. Leave its window open in the background while you play.

## What has to be running

The program has to be running to answer. Your PC has to be on and awake. You do not need a second Twitch account, a cloud server, or a program that starts at boot.

A small status window shows whether the program is running and whether Twitch and Spotify are connected. The first launch asks for a Twitch application and a Spotify application in that window. Twitch and Spotify require those applications to be created on their websites. The window opens the right page, copies the redirect URL, and saves what you paste. After the Twitch application is saved, Link Twitch is the sign-in, and the account that signs in is the channel. Spotify is an app on the Spotify account that plays the music, then a sign-in.

When an account is not connected, use its Link button. The browser opens for that sign-in. The window does not take your keyboard or mouse, so leave it in the background while you play.

Spotify can be playing on your PC, phone, or any other device on the same Spotify account. Close the status window when you are done. If you are using the console, press Ctrl+C once and wait a few seconds.

## Setup

1. Install Python 3.13, then in this folder:

```
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m tca
```

2. In the window, open the Twitch console and create an application named TCA. Category: Chat Bot. Copy the redirect URL from the window into the application, then paste the client ID and secret and save. The sign-in page opens. Approve your channel account. Later launches reuse that sign-in.

3. In the same window, open the Spotify dashboard and create an app. Copy the redirect URL from the window into the app, then paste the client ID and secret and save. Approve Spotify in the browser. Development mode is enough, because the app owner is the account that plays the music.

Do the first approval before you are in a game. When Program says Running, leave the window unfocused.

Twitch scopes are read chat and write chat for your own user. Spotify scopes are the current track and private playlist names.

## Commands

Edit `commands.json` while the program is running. It picks up the change on the next chat message. `commands.example.json` shows a plain text command.

```json
{
  "cooldown_seconds": 10,
  "!discord": "https://discord.gg/example",
  "!song": { "handler": "spotify_song" },
  "!playlist": { "handler": "spotify_playlist" }
}
```

A text value is sent as written. `spotify_song` and `spotify_playlist` are the built-in handlers. Each command waits 10 seconds before it can post again, so chat cannot make your account speak in a burst. A reply should be the answer itself, not another command.

Examples:

- `!song` → `Now playing: Artist - Title — https://open.spotify.com/track/...`
- `!playlist` → `Playlist: Name — https://open.spotify.com/playlist/...`

If nothing is playing, Spotify is paused, or the music is an album, radio, or Liked Songs rather than a playlist, the reply says so. A private Spotify session can hide the track. Spotify's AI DJ sometimes has a playlist link and no name; the link is posted on its own.

Messages show up as you. There is no separate bot name.
