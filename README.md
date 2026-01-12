# 🍺 Oktoberfest Tent Reservation Bot

Never miss a table reservation at Oktoberfest! This bot monitors tent reservation pages 24/7 and sends instant Telegram notifications the moment tables become available.

## Why?

Getting a table at popular Oktoberfest tents is extremely competitive. Reservations open unpredictably and fill up within minutes. This bot monitors reservation pages continuously so you don't have to.

## Features

- 🍺 **Multi-tent support** - Monitor multiple tents simultaneously
- 📱 **Instant notifications** - Telegram alerts the moment tables are available
- 🔄 **24/7 monitoring** - Automatic checks at configurable intervals
- 🛡️ **Smart error handling** - Detects issues and notifies you
- 📊 **State tracking** - Remembers availability across restarts
- 🔧 **Extensible** - Easy to add new tents with custom scrapers

## Quick Start

### 1. Installation

```bash
# Clone the repository
git clone https://github.com/YOUR_USERNAME/oktoberfest-bot.git
cd oktoberfest-bot

# Install the package
pip install -e .

# Install Playwright browsers
playwright install chromium
playwright install-deps
```

### 2. Configure Telegram Bot

1. Open Telegram and search for `@BotFather`
2. Send `/newbot` to create a new bot
3. Choose a name and username for your bot
4. Save the bot token you receive
5. Start a chat with your bot and send any message
6. Visit `https://api.telegram.org/bot<YOUR_BOT_TOKEN>/getUpdates` to get your chat ID

### 3. Configure the Bot

```bash
# Copy example config
cp config/config.example.json config/config.json

# Edit config with your details
nano config/config.json
```

Update with your Telegram bot token and chat ID.

### 4. Configure Tents

Edit `config/tents.json` to add or modify tents you want to monitor. Each tent needs:
- `id` - Unique identifier
- `name` - Display name
- `url` - Reservation page URL
- `scraper_type` - Type of scraper to use
- `enabled` - Set to `true` to monitor

### 5. Run the Bot

```bash
# Run directly
oktoberfest-bot

# Or run as module
python3 -m oktoberfest_bot.main

# Or run in background with systemd
sudo cp systemd/oktoberfest-bot.service.example /etc/systemd/system/oktoberfest-bot.service
# Edit the service file with correct paths
sudo systemctl enable oktoberfest-bot.service
sudo systemctl start oktoberfest-bot.service
```

## Configuration

### Main Config (`config/config.json`)

```json
{
  "telegram_bot_token": "your_bot_token",
  "telegram_chat_id": "your_chat_id",
  "state_file": "/opt/oktoberfest-bot/state.json",
  "log_file": "/opt/oktoberfest-bot/logs/monitor.log"
}
```

### Tents Config (`config/tents.json`)

```json
{
  "tents": [
    {
      "id": "schuetzenfestzelt",
      "name": "Schützenfestzelt",
      "url": "https://reservierung.schuetzenfestzelt.com/reservation",
      "scraper_type": "select_dropdown",
      "selector": "select.form-select",
      "check_interval": 180,
      "enabled": true
    }
  ]
}
```

## Adding New Tents

1. Add tent configuration to `config/tents.json`
2. If the tent uses a different page structure, create a new scraper in `oktoberfest_bot/scrapers/`
3. Update the `create_scraper()` factory in `oktoberfest_bot/main.py`

## Monitoring

```bash
# Check service status
systemctl status oktoberfest-bot.service

# View logs
tail -f /opt/oktoberfest-bot/logs/monitor.log

# Or with journalctl
journalctl -u oktoberfest-bot.service -f
```

## Contributing

Contributions are very welcome! Here's how you can help:

- 🐛 **Report bugs** - Open an issue if you find a problem
- 💡 **Suggest features** - Have an idea? Share it!
- 🏕️ **Add tent scrapers** - Help support more Oktoberfest tents
- 📝 **Improve docs** - Better documentation helps everyone
- 🔧 **Submit PRs** - Code contributions are appreciated

Please open an issue first to discuss major changes.

## License

MIT License - See [LICENSE](LICENSE) file for details.

## Disclaimer

This bot is for personal use only. Please:
- Respect the terms of service of reservation websites
- Use reasonable check intervals (180+ seconds recommended)
- Do not overload servers with excessive requests

The authors are not responsible for any misuse of this software.
