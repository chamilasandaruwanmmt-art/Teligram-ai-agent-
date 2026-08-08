# Telegram AI Agent Bot - Setup Guide (Sinhala)

## කරන්නේ මොකක්ද
Telegram bot එකකට message එකක් යවනකොට, Gemini AI එකෙන් reply එකක් එනවා.
`/browse <url>` කිව්වොත් Playwright එකෙන් page එක load කරලා AI එකෙන්
Sinhala summary එකක් දෙනවා. `/shot <url>` කිව්වොත් screenshot එකක් එවනවා.

## Step 1: Telegram Bot Token එක ගන්න
1. Telegram එකේ **@BotFather** search කරන්න
2. `/newbot` කියලා type කරන්න
3. Bot එකට නමක් සහ username එකක් දෙන්න (username එක `bot` වලින් ඉවර වෙන්න ඕන)
4. එයාට token එකක් දෙනවා - ඒක save කරගන්න (`123456:ABC-DEF...` වගේ)

## Step 2: Gemini API Key එක ගන්න
1. https://aistudio.google.com/apikey යන්න
2. "Create API key" click කරන්න
3. Key එක copy කරගන්න - මේක Free tier එකෙන් day එකකට request 1,500ක් විතර ලැබෙනවා

## Step 3: Hosting - Oracle Cloud Free Tier (recommended, permanent free)
Colab එකේදී session disconnect වෙන නිසා 24/7 bot එකකට Oracle Cloud VM එක හොඳම free option එක.

1. https://www.oracle.com/cloud/free/ එකේ account එකක් හදන්න (credit card verification අවශ්‍යයි,
   ඒත් charge වෙන්නේ නෑ Always Free tier එකේ ඉන්නකම්)
2. "Create a VM instance" - **Ampere A1 (Arm)** shape එක select කරන්න (4 OCPU, 24GB RAM, permanent free)
3. Ubuntu image එකක් select කරන්න
4. VM එක start උනාම, SSH කරලා connect වෙන්න

### VM එකේ setup කරන commands:
```
sudo apt update && sudo apt install -y python3-pip python3-venv
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
playwright install --with-deps chromium
```

### Bot එක run කරන්න:
```
export TELEGRAM_BOT_TOKEN="ඔයාගේ token එක"
export GEMINI_API_KEY="ඔයාගේ key එක"
python3 bot.py
```

### 24/7 run වෙන්න (VM restart උනත් continue වෙන්න) - systemd service එකක් හදමු:

`/etc/systemd/system/aibot.service` file එක හදන්න:
```
[Unit]
Description=Telegram AI Agent Bot
After=network.target

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/home/ubuntu/telegram_ai_agent
Environment="TELEGRAM_BOT_TOKEN=ඔයාගේ_token"
Environment="GEMINI_API_KEY=ඔයාගේ_key"
ExecStart=/home/ubuntu/telegram_ai_agent/venv/bin/python3 bot.py
Restart=always

[Install]
WantedBy=multi-user.target
```

Enable කරන්න:
```
sudo systemctl daemon-reload
sudo systemctl enable aibot
sudo systemctl start aibot
sudo systemctl status aibot
```

## Alternative: Railway.app (ලේසි, ඒත් resources අඩුයි)
Playwright browser automation එකට heavier නිසා Railway free tier එකේ tight විය හැක,
ඒත් simple testing එකට හොඳයි:
1. github repo එකකට code එක push කරන්න
2. railway.app එකේ "New Project" -> "Deploy from GitHub"
3. Environment variables (TELEGRAM_BOT_TOKEN, GEMINI_API_KEY) settings වල දාන්න
4. Start command: `python bot.py`

## Bot එක test කරන්න
Telegram එකේ ඔයාගේ bot එකට message යවන්න:
- `/start`
- `/ask BOS සහ CHoCH වල වෙනස මොකක්ද`
- `/browse https://example.com`
