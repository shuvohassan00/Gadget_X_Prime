# Gadget_X_Prime

## YouTube "Sign in to confirm you’re not a bot" fix

If yt-dlp returns a YouTube anti-bot/sign-in error, configure one of these env vars in your `.env`:

- `YTDLP_COOKIES_FILE=/absolute/path/to/cookies.txt`
- `YTDLP_COOKIES_FROM_BROWSER=firefox`
- `YTDLP_COOKIES_FROM_BROWSER=chrome:Profile 1`

Then restart the bot. The bot now reads these settings and applies them to yt-dlp calls.
